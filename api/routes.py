"""FastAPI routes for GrabbyTube."""
import asyncio
import json
import logging
import re as _re
from pathlib import Path

import aiohttp
import yt_dlp
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

from config import settings
from db import (
    init_db, enqueue_job, get_job, list_jobs,
    list_tracks, load_catalog, add_track, save_catalog,
    find_duplicates, batch_delete_tracks, get_cleaned_titles,
    get_existing_video_ids, set_config,
)
from processor import worker

logger = logging.getLogger("grabbytube")
router = APIRouter()

# --- Playlist scraping (adapted from youtube_playlist_ids.py) ---

# Prefer an explicit list= param over v=/embed/youtu.be: a watch URL like
# https://www.youtube.com/watch?v=X&list=RD... carries the playlist in list=,
# and grabbing the v= ID instead 404s when treated as a playlist.
_YT_LIST_PARAM_RE = _re.compile(r'[?&]list=([A-Za-z0-9_-]{11,})')
_YT_PLAYLIST_RE = _re.compile(
    r'(?:youtube\.com/(?:watch\?.*v=|playlist\?.*list=|embed/)|youtu\.be/)([A-Za-z0-9_-]{11,})'
)
_YT_PL_ID_RE = _re.compile(r'^[A-Za-z0-9_-]{11,}$')

BROWSE_API = "https://www.youtube.com/youtubei/v1/browse"

YT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _find_json(html: str, marker: str) -> dict:
    idx = html.find(marker)
    if idx == -1:
        return {}
    start = html.find("{", idx)
    if start == -1:
        return {}
    depth = 0
    for i in range(start, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[start : i + 1])
    return {}


def _extract_playlist_ids(data: dict) -> tuple[list[str], list[str]]:
    ids = []
    tokens = []

    def _process_items(items: list):
        for item in items:
            vm = item.get("lockupViewModel", {})
            cid = vm.get("contentId")
            ctype = vm.get("contentType")
            if cid and ctype == "LOCKUP_CONTENT_TYPE_VIDEO":
                ids.append(cid)
            civm = item.get("continuationItemViewModel", {})
            cmd = civm.get("continuationCommand", {})
            inner = cmd.get("innertubeCommand", {})
            inner_cmd = inner.get("continuationCommand", {})
            token = cmd.get("token") or inner_cmd.get("token")
            if token:
                tokens.append(token)

    for action in data.get("onResponseReceivedActions", []):
        append = action.get("appendContinuationItemsAction", {})
        continuation_items = append.get("continuationItems", [])
        if continuation_items:
            _process_items(continuation_items)

    def walk(obj):
        if isinstance(obj, dict):
            sec = obj.get("itemSectionRenderer")
            if sec:
                _process_items(sec.get("contents", []))
                for ct in sec.get("continuations", []):
                    token = ct.get("continuationCommand", {}).get("token")
                    if token:
                        tokens.append(token)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return ids, tokens


def _alert_text(s) -> str:
    if isinstance(s, dict):
        if "simpleText" in s:
            return s["simpleText"]
        if "runs" in s:
            return "".join(r.get("text", "") for r in s["runs"])
        return " ".join(_alert_text(v) for v in s.values())
    if isinstance(s, str):
        return s
    if isinstance(s, list):
        return " ".join(_alert_text(v) for v in s)
    return ""


def _extract_alerts(data: dict) -> str:
    """Collect alertRenderer texts from a ytInitialData / browse response."""
    texts = []

    def walk(o):
        if isinstance(o, dict):
            ar = o.get("alertRenderer")
            if ar:
                t = _alert_text(ar.get("text", "")).strip()
                if t:
                    texts.append(t)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return " ".join(dict.fromkeys(texts)).strip()


async def _fetch_playlist_ids(playlist_id: str) -> tuple[list[str], str]:
    """Return (video_ids, error). error is '' on success, else a user-facing message."""
    async with aiohttp.ClientSession(headers=YT_HEADERS) as session:
        # Visit homepage first for cookies
        try:
            async with session.get("https://www.youtube.com/", timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
        except Exception:
            pass  # non-critical; cookies help but aren't required

        url = f"https://www.youtube.com/playlist?list={playlist_id}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"HTTP error fetching playlist {url}: {e}")
            return [], f"Could not reach YouTube: {type(e).__name__}: {e}"

        initial_data = _find_json(html, "ytInitialData")
        if not initial_data:
            logger.error("Could not find ytInitialData on playlist page")
            return [], "YouTube returned a page with no playlist data (blocked or malformed response)"

        visitor_data = ""
        vd_match = _re.search(r'"VISITOR_DATA"\s*:\s*"([^"]+)"', html)
        if vd_match:
            visitor_data = vd_match.group(1)

        api_key = ""
        ak_match = _re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', html)
        if ak_match:
            api_key = ak_match.group(1)

        ids, continuations = _extract_playlist_ids(initial_data)

        max_passes = 30
        passes = 0
        while continuations and passes < max_passes:
            passes += 1
            token = continuations.pop(0)
            try:
                payload = {
                    "continuation": token,
                    "context": {
                        "client": {
                            "clientName": "WEB",
                            "clientVersion": "2.20240125.08.00",
                        },
                        "thirdParty": {},
                    },
                }
                if visitor_data:
                    payload["context"]["client"]["visitorData"] = visitor_data

                params = {"key": api_key} if api_key else {}
                async with session.post(
                    BROWSE_API,
                    json=payload,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    resp.raise_for_status()
                    more_data = await resp.json()

                more_ids, more_tokens = _extract_playlist_ids(more_data)
                ids.extend(more_ids)
                continuations.extend(more_tokens)
            except Exception as e:
                logger.error(f"Continuation error: {e}")
                break

    if not ids:
        alert = _extract_alerts(initial_data)
        if alert:
            # Translate the common YouTube alert into an actionable message.
            if "unviewable" in alert.lower():
                msg = (
                    f"YouTube: '{alert}' — this is a saved/library playlist (RD prefix). "
                    "It can only be read while signed in as the owning account in the "
                    "ytcookies browser session; re-login there and retry, or share the "
                    "playlist and use its PL/OL URL instead."
                )
            else:
                msg = f"YouTube: {alert}"
            logger.warning(f"Playlist {playlist_id} blocked: {alert}")
            return [], msg
        logger.warning(f"Playlist {playlist_id} returned no video IDs (0 continuations followed)")
        return [], "Playlist loaded but contained no videos (empty, or YouTube blocked the scrape)"
    return ids, ""


def _extract_playlist_id(text: str) -> str | None:
    """Extract a playlist ID from a URL or bare ID."""
    text = text.strip()
    m = _YT_LIST_PARAM_RE.search(text)
    if m:
        return m.group(1)
    m = _YT_PLAYLIST_RE.search(text)
    if m:
        return m.group(1)
    if _YT_PL_ID_RE.match(text) and len(text) >= 12:
        return text
    return None

# Background task handle
_worker_task: asyncio.Task | None = None


def start_worker():
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(worker())
        logger.info("Processor worker started")


class SearchRequest(BaseModel):
    query: str
    limit: int = 10


class DownloadRequest(BaseModel):
    video_id: str
    title: str
    url: str
    search_query: str | None = None
    artist: str | None = None


class BatchQueueRequest(BaseModel):
    text: str


class TrackListRequest(BaseModel):
    sort_by: str = "createdAt"
    reverse: bool = True


class BatchDeleteRequest(BaseModel):
    video_ids: list[str]


class PlaylistQueueRequest(BaseModel):
    url: str


# --- Startup ---

@router.on_event("startup")
async def startup():
    init_db()
    start_worker()


# --- Search ---

@router.post("/search")
async def search(req: SearchRequest):
    """Search YouTube for a song/track."""
    query = f"ytsearch{req.limit}:{req.query}"
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
    except Exception as e:
        raise HTTPException(400, f"Search failed: {e}")

    entries = info.get("entries", [])
    results = []
    for e in entries:
        vid = e.get("id", "")
        thumb_url = e.get("thumbnail", "") or ""
        if not thumb_url:
            thumbs = e.get("thumbnails", [])
            if thumbs:
                thumb_url = thumbs[-1].get("url", "")
        results.append({
            "id": vid,
            "title": e.get("title", ""),
            "uploader": e.get("uploader", "") or e.get("channel", ""),
            "duration": e.get("duration", 0),
            "thumbnail": thumb_url,
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return results


# --- Queue ---

@router.post("/queue")
async def queue_download(req: DownloadRequest):
    """Add a video to the download queue."""
    query = req.search_query or req.title
    jid = enqueue_job(
        query=query,
        video_id=req.video_id,
        title=req.title,
        url=req.url,
        artist=req.artist,
    )
    if jid == 0:
        return {"job_id": 0, "status": "skipped", "message": "Already queued or downloaded"}
    return {"job_id": jid, "status": "queued"}


# Batch regex patterns
_YT_ID_RE = _re.compile(r'(?:v=|youtu\.be/|embed/|watch\?.*v=)([A-Za-z0-9_-]{11})')
_YT_BARE_ID_RE = _re.compile(r'(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])')
_YT_URL_RE = _re.compile(r"(https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s<>]+)")


@router.post("/queue/batch")
async def batch_queue(req: BatchQueueRequest):
    """Extract YouTube URLs/IDs from raw text and enqueue them all."""
    ids_seen = set()
    queued = 0
    skipped = 0
    for line in req.text.splitlines():
        line = line.strip()
        if not line:
            continue
        # First try to find full YouTube URLs on the line
        urls_found = False
        for url in _YT_URL_RE.findall(line):
            urls_found = True
            m = _YT_ID_RE.search(url)
            if m:
                vid = m.group(1)
                if vid in ids_seen:
                    skipped += 1
                    continue
                ids_seen.add(vid)
                jid = enqueue_job(query=url, video_id=vid, title="", url=url)
                if jid:
                    queued += 1
                else:
                    skipped += 1
        # If no URL found, try bare video IDs
        if not urls_found:
            m = _YT_ID_RE.search(line)
            if not m:
                m = _YT_BARE_ID_RE.search(line)
            if m:
                vid = m.group(1)
                url = f"https://www.youtube.com/watch?v={vid}"
                if vid in ids_seen:
                    skipped += 1
                    continue
                ids_seen.add(vid)
                jid = enqueue_job(query=url, video_id=vid, title="", url=url)
                if jid:
                    queued += 1
                else:
                    skipped += 1
    return {"queued": queued, "skipped": skipped}


@router.post("/queue/playlist")
async def queue_playlist(req: PlaylistQueueRequest):
    """Fetch video IDs from a YouTube playlist URL and enqueue them all."""
    playlist_id = _extract_playlist_id(req.url)
    if not playlist_id:
        raise HTTPException(400, "Could not extract playlist ID from URL")

    logger.info(f"Fetching playlist {playlist_id}")
    video_ids, fetch_error = await _fetch_playlist_ids(playlist_id)
    if not video_ids:
        raise HTTPException(400, fetch_error or "No videos found in playlist (or fetch failed)")

    ids_seen = set()
    queued = 0
    skipped = 0
    existing_ids = get_existing_video_ids()
    for vid in video_ids:
        if vid in ids_seen:
            skipped += 1
            continue
        ids_seen.add(vid)
        if vid in existing_ids:
            skipped += 1
            continue
        url = f"https://www.youtube.com/watch?v={vid}"
        jid = enqueue_job(query=f"Playlist: {playlist_id}", video_id=vid, title="", url=url)
        if jid:
            queued += 1
        else:
            skipped += 1

    return {"queued": queued, "skipped": skipped, "total": len(video_ids)}


@router.get("/jobs")
async def get_jobs(status: str | None = None, limit: int = 0):
    """List jobs, optionally filtered by status."""
    return list_jobs(status=status, limit=limit)


@router.get("/jobs/{jid}")
async def get_job_detail(jid: int):
    """Get a single job's status."""
    job = get_job(jid)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.delete("/jobs")
async def clear_jobs(status: str | None = None):
    """Clear jobs by status. If status provided, clear only that status. Otherwise clear completed/failed."""
    from db import get_conn
    conn = get_conn()
    if status:
        cur = conn.execute("DELETE FROM jobs WHERE status = ?", (status,))
    else:
        cur = conn.execute(
            "DELETE FROM jobs WHERE status IN ('completed', 'failed', 'transcoding', 'downloading', 'running')"
        )
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return {"cleared": deleted}


@router.delete("/jobs/all")
async def clear_all_jobs():
    """Clear ALL jobs regardless of status."""
    from db import get_conn
    conn = get_conn()
    cur = conn.execute("DELETE FROM jobs")
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return {"cleared": deleted}


@router.post("/jobs/{jid}/retry")
async def retry_job(jid: int, query: str | None = None):
    """Re-search using the original query (or provided query) and return results."""
    job = get_job(jid)
    if not job:
        raise HTTPException(404, "Job not found")
    search_query = query or job.get("query", "")
    if not search_query:
        raise HTTPException(400, "No search query stored for this job")
    # Re-run the search
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch15:{search_query}", download=False)
    except Exception as e:
        raise HTTPException(400, f"Search failed: {e}")

    entries = info.get("entries", [])
    results = []
    for e in entries:
        vid = e.get("id", "")
        thumb_url = e.get("thumbnail", "") or ""
        if not thumb_url:
            thumbs = e.get("thumbnails", [])
            if thumbs:
                thumb_url = thumbs[-1].get("url", "")
        results.append({
            "id": vid,
            "title": e.get("title", ""),
            "uploader": e.get("uploader", "") or e.get("channel", ""),
            "duration": e.get("duration", 0),
            "thumbnail": thumb_url,
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return results


# --- Tracks catalog ---

@router.get("/tracks")
async def get_tracks(sort_by: str = "createdAt", reverse: bool = True):
    """List all cataloged tracks."""
    return list_tracks(sort_by=sort_by, reverse=reverse)


@router.get("/tracks/count")
async def get_track_count():
    """Get total track count."""
    return {"count": len(load_catalog())}


@router.get("/tracks/file")
async def get_tracks_json():
    """Download the raw grabby-tracks.json."""
    catalog_path = Path("grabby-tracks.json")
    if not catalog_path.exists():
        return {"tracks": []}
    return json.loads(catalog_path.read_text())


@router.get("/tracks/rebuild")
async def rebuild_tracks():
    """Re-probe all MP3 files in the output directory and update the catalog."""
    import asyncio
    from processor import _get_audio_metadata
    catalog = load_catalog()
    updated = 0
    for track in catalog:
        # DrivePod-compatible: audioPath is relative (e.g. "fJ9rUzIMcZQ/fJ9rUzIMcZQ.mp3")
        audio_rel = track.get("audioPath") or track.get("audio_path", "")
        fp = settings.output_path / audio_rel if "/" in audio_rel else Path(audio_rel)
        if not fp.exists():
            # Fallback: try as absolute path (legacy)
            fp = Path(audio_rel)
        if fp.exists():
            meta = await _get_audio_metadata(fp)
            if meta.get("duration"):
                track["duration"] = meta["duration"]
            updated += 1
    save_catalog(catalog)
    return {"rebuilt": updated, "total": len(catalog)}


# --- Serve audio files ---

@router.get("/audio/{video_id}")
async def serve_audio(video_id: str):
    """Serve a completed MP3 file (DrivePod-style per-item directory)."""
    fp = settings.output_path / video_id / f"{video_id}.mp3"
    if not fp.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(
        str(fp),
        media_type="audio/mpeg",
        filename=f"{video_id}.mp3",
    )


# --- Serve thumbnails ---

@router.get("/thumb/{video_id}")
async def serve_thumb(video_id: str):
    """Serve a thumbnail for a video."""
    fp = settings.output_path / video_id / f"{video_id}-small.webp"
    if not fp.exists():
        raise HTTPException(404, "Thumbnail not found")
    return FileResponse(
        str(fp),
        media_type="image/webp",
    )


# --- Serve per-item metadata JSON ---

@router.get("/meta/{video_id}")
async def serve_meta(video_id: str):
    """Serve the per-item metadata JSON."""
    fp = settings.output_path / video_id / f"{video_id}.json"
    if not fp.exists():
        raise HTTPException(404, "Metadata not found")
    return json.loads(fp.read_text())


# --- Delete track ---

@router.delete("/tracks/{video_id}")
async def delete_track(video_id: str):
    """Delete a track: remove from catalog, delete files, rebuild index."""
    import shutil
    from processor import update_index_file

    # Remove from catalog
    catalog = load_catalog()
    new_catalog = [t for t in catalog if t.get("videoId") != video_id]
    if len(new_catalog) == len(catalog):
        raise HTTPException(404, "Track not found")
    save_catalog(new_catalog)

    # Delete the video directory on disk
    video_dir = settings.output_path / video_id
    if video_dir.exists():
        shutil.rmtree(str(video_dir))

    # Rebuild index
    update_index_file()

    return {"deleted": video_id}


# --- Duplicates ---

@router.get("/tracks/duplicates")
async def get_duplicates():
    """Find potential duplicate tracks using LLM analysis."""
    groups = await find_duplicates()
    return {"groups": groups, "total_groups": len(groups)}


@router.get("/tracks/cleaned-titles")
async def get_cleaned_titles_endpoint():
    """Get numbered list of cleaned titles for manual LLM testing."""
    return get_cleaned_titles()


@router.post("/tracks/batch-delete")
async def batch_delete(req: BatchDeleteRequest):
    """Delete multiple tracks at once."""
    deleted = batch_delete_tracks(req.video_ids)
    return {"deleted": deleted, "count": len(deleted)}


# --- Cookies (browser cookies for YouTube auth) ---

from processor import _COOKIES_FILE, effective_cookies_file

@router.post("/cookies")
async def upload_cookies(file: UploadFile = File(...)):
    """Upload Netscape-format cookies file (e.g. from EditThisCookie/Get cookies.txt extension).

    Note: the shared ytcookies export (/shared-cookies) takes precedence when present."""
    content = await file.read()
    _COOKIES_FILE.write_bytes(content)
    return {"status": "ok", "message": f"Cookies saved ({len(content)} bytes)", "file": str(_COOKIES_FILE)}

@router.delete("/cookies")
async def clear_cookies():
    """Remove stored cookies (shared ytcookies export, if mounted, still takes precedence)."""
    if _COOKIES_FILE.exists():
        _COOKIES_FILE.unlink()
    return {"status": "ok", "message": "Cookies cleared"}

@router.get("/cookies/status")
async def cookies_status():
    """Check if cookies are configured."""
    effective = effective_cookies_file()
    return {
        "has_cookies": effective is not None,
        "path": str(effective) if effective else None,
        "source": "shared-ytcookies" if effective and str(effective).startswith("/shared-cookies") else "local",
    }


# --- Tester ---

import shlex
from processor import _COOKIES_FILE

class TestCommandRequest(BaseModel):
    command: str

@router.post("/test/run")
async def run_test_command(req: TestCommandRequest):
    """Run a test command with {COOKIESFILE} replaced by the actual cookies path."""
    cmd = req.command.strip()
    if not cmd:
        raise HTTPException(400, "Command is empty")

    # Replace {COOKIESFILE} token
    effective = effective_cookies_file()
    if effective:
        cmd = cmd.replace("{COOKIESFILE}", str(effective))
    else:
        cmd = cmd.replace("{COOKIESFILE}", "")

    try:
        args = shlex.split(cmd)
    except ValueError as e:
        raise HTTPException(400, f"Invalid command syntax: {e}")

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(timeout=60)

    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    output = stdout_text + stderr_text

    logger.info(f"[TEST] Command: {cmd}")
    logger.info(f"[TEST] Output:\n{output}")

    return {
        "exit_code": proc.returncode,
        "output": output,
        "command": cmd,
    }


# --- Settings ---

class UserAgentRequest(BaseModel):
    user_agent: str

@router.post("/settings/user-agent")
async def set_user_agent(req: UserAgentRequest):
    """Set the user agent for yt-dlp (persisted in the database)."""
    settings.user_agent = req.user_agent
    set_config("user_agent", req.user_agent)
    return {"status": "ok", "user_agent": settings.user_agent}

@router.get("/settings/user-agent")
async def get_user_agent():
    """Get the current user agent."""
    return {"user_agent": settings.user_agent}


# --- Health ---

@router.get("/health")
async def health():
    return {"status": "ok", "tracks": len(load_catalog())}
