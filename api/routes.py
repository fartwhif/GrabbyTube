"""FastAPI routes for GrabbyTube."""
import asyncio
import json
import logging
from pathlib import Path

import yt_dlp
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

from config import settings
from db import (
    init_db, enqueue_job, get_job, list_jobs,
    list_tracks, load_catalog, add_track, save_catalog,
)
from processor import worker

logger = logging.getLogger("grabbytube")
router = APIRouter()

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


class BatchQueueRequest(BaseModel):
    text: str


class TrackListRequest(BaseModel):
    sort_by: str = "date_added"
    reverse: bool = True


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
    jid = enqueue_job(
        query=req.title,
        video_id=req.video_id,
        title=req.title,
        url=req.url,
    )
    if jid == 0:
        return {"job_id": 0, "status": "skipped", "message": "Already queued or downloaded"}
    return {"job_id": jid, "status": "queued"}


import re as _re

_YT_ID_RE = _re.compile(r'(?:v=|youtu\.be/|embed/|watch\?.*v=)([A-Za-z0-9_-]{11})')
_YT_URL_RE = _re.compile(r'(https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s\'">]+)')


@router.post("/queue/batch")
async def batch_queue(req: BatchQueueRequest):
    """Extract YouTube URLs/IDs from raw text and enqueue them all."""
    urls_seen = set()
    queued = 0
    skipped = 0
    for line in req.text.splitlines():
        line = line.strip()
        if not line:
            continue
        # First try to find full URLs
        for url in _YT_URL_RE.findall(line):
            if url in urls_seen:
                continue
            urls_seen.add(url)
            m = _YT_ID_RE.search(url)
            if m:
                jid = enqueue_job(query=url, video_id=m.group(1), title="", url=url)
                if jid:
                    queued += 1
                else:
                    skipped += 1
        # If no URL found, try bare video IDs
        if not _YT_URL_RE.search(line):
            m = _YT_ID_RE.search(line)
            if m:
                vid = m.group(1)
                url = f"https://www.youtube.com/watch?v={vid}"
                if vid not in urls_seen:
                    urls_seen.add(vid)
                    jid = enqueue_job(query=url, video_id=vid, title="", url=url)
                    if jid:
                        queued += 1
                    else:
                        skipped += 1
    return {"queued": queued, "skipped": skipped}


@router.get("/jobs")
async def get_jobs(status: str = None, limit: int = 50):
    """List jobs, optionally filtered by status."""
    return list_jobs(status=status, limit=limit)


@router.get("/jobs/{jid}")
async def get_job_detail(jid: int):
    """Get a single job's status."""
    job = get_job(jid)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


# --- Tracks catalog ---

@router.get("/tracks")
async def get_tracks(sort_by: str = "date_added", reverse: bool = True):
    """List all cataloged tracks."""
    return list_tracks(sort_by=sort_by, reverse=reverse)


@router.get("/tracks/count")
async def get_track_count():
    """Get total track count."""
    return {"count": len(load_catalog())}


@router.get("/tracks/file")
async def get_tracks_json():
    """Download the raw tracks.json."""
    catalog_path = Path("tracks.json")
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
        # New structure: audio_path is relative (e.g. "fJ9rUzIMcZQ/fJ9rUzIMcZQ.mp3")
        audio_rel = track.get("audio_path") or track.get("filepath", "")
        fp = settings.output_path / audio_rel if "/" in audio_rel else Path(audio_rel)
        if not fp.exists():
            # Fallback: try as absolute path (legacy)
            fp = Path(audio_rel)
        if fp.exists():
            meta = await _get_audio_metadata(fp)
            for key in ("duration", "filesize", "codec", "bitrate", "sample_rate", "channels", "bits_per_sample"):
                if meta.get(key):
                    track[key] = meta[key]
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
    new_catalog = [t for t in catalog if t.get("video_id") != video_id]
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


# --- Health ---

@router.get("/health")
async def health():
    return {"status": "ok", "tracks": len(load_catalog())}
