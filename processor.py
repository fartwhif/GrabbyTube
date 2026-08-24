"""Background processor: download, transcode, catalog tracks."""
import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yt_dlp

from config import settings
from db import get_job, update_job, get_pending_jobs, add_track

logger = logging.getLogger("grabbytube.processor")

# Shared cookie export from the ytcookies container (read-only mount,
# continuously refreshed by its CDP cookie exporter). Preferred when present;
# falls back to the manually uploaded file in ./data/.
_SHARED_COOKIES_FILE = Path("/shared-cookies/cookies.txt")
_LOCAL_COOKIES_FILE = Path("/app/data/cookies.txt")


def effective_cookies_file() -> Path | None:
    """Return the cookies file yt-dlp should use: shared export first."""
    for candidate in (_SHARED_COOKIES_FILE, _LOCAL_COOKIES_FILE):
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


# Kept for backwards compatibility with code importing the old constant.
_COOKIES_FILE = _LOCAL_COOKIES_FILE


def _get_cookies_flag() -> list[str]:
    """Return ['--cookies', path] if a cookies file is available."""
    path = effective_cookies_file()
    if path:
        return ["--cookies", str(path)]
    return []


def _get_yt_dlp_base_flags() -> list[str]:
    """Return common yt-dlp flags (cookies, user agent, PO Token)."""
    flags = _get_cookies_flag()
    if settings.user_agent.strip():
        flags.extend(["--user-agent", settings.user_agent])
    # PO Token via bgutil sidecar + EJS solver + mweb client
    flags.extend([
        "--remote-components", "ejs:github",
        "--extractor-args", "youtube:player_client=mweb",
        "--extractor-args", "youtubepot-bgutilhttp:base_url=http://grabbytube-bgutil:4416",
        "--min-sleep-interval", "5",
        "--max-sleep-interval", "15",
    ])
    return flags


def update_index_file():
    """Rebuild output/grabby-index.json from all per-item metadata JSONs (DrivePod-style)."""
    output_dir = settings.output_path
    index_path = output_dir / "grabby-index.json"
    items = []
    for subdir in sorted(output_dir.iterdir()):
        if not subdir.is_dir():
            continue
        meta_file = subdir / f"{subdir.name}.json"
        if meta_file.exists():
            try:
                data = json.loads(meta_file.read_text())
                items.append(data)
            except (json.JSONDecodeError, OSError):
                pass
    items.sort(key=lambda t: t.get("createdAt", ""), reverse=True)
    index_path.write_text(json.dumps(items, indent=2, ensure_ascii=False))
    logger.info(f"Index rebuilt: {len(items)} items -> {index_path}")


# Safe filename pattern
_SAFE_RE = re.compile(r'[^\w\-\.\(\) ]')


def _sanitize(name: str) -> str:
    return _SAFE_RE.sub("", name).strip()


def _sanitize_ext(name: str) -> str:
    """Sanitize but keep the file extension."""
    base, ext = os.path.splitext(name)
    return _sanitize(base) + ext


async def _run(cmd: list[str]) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout, stderr


async def _get_audio_metadata(filepath: Path) -> dict:
    """Probe an audio file with ffprobe and extract metadata."""
    try:
        rc, stdout, stderr = await _run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(filepath),
        ])
        data = json.loads(stdout.decode("utf-8", errors="replace"))
    except Exception as e:
        logger.error(f"ffprobe failed for {filepath}: {e}")
        return {}

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})

    meta = fmt.get("tags", {})

    duration = float(fmt.get("duration", 0))
    filesize = int(fmt.get("size", 0))

    def _safe_int(v, default=0):
        if v is None:
            return default
        try:
            return int(v)
        except (ValueError, TypeError):
            return default

    return {
        "title": meta.get("title", ""),
        "artist": meta.get("artist", "") or meta.get("artist_sort", ""),
        "album": meta.get("album", ""),
        "album_artist": meta.get("album_artist", ""),
        "genre": meta.get("genre", ""),
        "date": meta.get("date", "") or meta.get("creation_time", ""),
        "track_number": meta.get("track", ""),
        "disc_number": meta.get("disc", ""),
        "duration": round(duration, 2),
        "filesize": filesize,
        "codec": audio_stream.get("codec_name", ""),
        "bitrate": _safe_int(audio_stream.get("bit_rate") or fmt.get("bit_rate")),
        "sample_rate": _safe_int(audio_stream.get("sample_rate")),
        "channels": _safe_int(audio_stream.get("channels")),
        "bits_per_sample": _safe_int(audio_stream.get("bits_per_sample")),
    }


async def process_job(jid: int):
    """Process a single job: download -> transcode -> catalog.

    Stores each item in its own subdirectory named by video_id (DrivePod-style):
        output/
          {video_id}/
            {video_id}.mp3
            {video_id}-small.webp
            {video_id}.json
    """
    job = get_job(jid)
    if not job:
        return

    video_id = job.get("video_id", "")
    output_dir = settings.output_path
    video_dir = output_dir / video_id

    update_job(jid, status="downloading")
    logger.info(f"[{jid}] Downloading: {job['title']}")

    # Create per-video directory
    video_dir.mkdir(parents=True, exist_ok=True)

    # Check if already completed
    mp3_path = video_dir / f"{video_id}.mp3"
    if mp3_path.exists():
        logger.info(f"[{jid}] Already exists: {mp3_path}")
        update_job(jid, status="completed", output_file=str(mp3_path))
        return

    outtmpl = str(video_dir / "%(id)s.%(ext)s")

    # Build yt-dlp download command
    cmd = ["yt-dlp"]
    cmd.extend(_get_yt_dlp_base_flags())
    cmd.extend([
        "-f", "b[acodec!=none][height<=360]/b[acodec!=none]/ba/b",
        "--write-thumbnail",
        "--no-overwrites",
        "--continue",
        "--retries", "3",
        "--fragment-retries", "3",
        "--extractor-retries", "3",
        "-o", outtmpl,
        job["url"],
    ])

    downloaded_file = None
    info = None
    try:
        # Extract info first
        info_cmd = ["yt-dlp"]
        info_cmd.extend(_get_yt_dlp_base_flags())
        info_cmd.extend(["--dump-json", job["url"]])

        logger.info(f"[{jid}] CMD: {' '.join(cmd)}")
        logger.info(f"[{jid}] INFO: {' '.join(info_cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *info_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        info_stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            info = json.loads(info_stdout.decode("utf-8", errors="replace"))

        # Download
        rc, _, stderr = await _run(cmd)
        if rc != 0:
            stderr_text = stderr.decode(errors="replace") if stderr else ""
            raise RuntimeError(f"yt-dlp failed: {stderr_text[:300]}")

        # Find the downloaded file
        for f in video_dir.iterdir():
            if f.suffix.lower() not in (".jpg", ".jpeg", ".webp", ".png", ".json"):
                downloaded_file = str(f)
                break

        if not downloaded_file or not Path(downloaded_file).exists():
            raise RuntimeError("Download completed but output file not found")

        update_job(jid, status="transcoding")
        logger.info(f"[{jid}] Transcoding: {Path(downloaded_file).name}")

        # Transcode to MP3 (inside video_dir)
        # Embed metadata from yt-dlp info into the MP3
        ffprobe_meta = {}
        if isinstance(info, dict):
            cats = info.get("categories", [])
            ffprobe_meta = {
                "title": info.get("title", ""),
                "artist": info.get("uploader", "") or info.get("channel", ""),
                "album": info.get("playlist_title", ""),
                "genre": cats[0] if isinstance(cats, list) and cats else "",
                "date": info.get("upload_date", "")[:4] if info.get("upload_date") else "",
            }

        ff_cmd = [
            "ffmpeg", "-y", "-i", downloaded_file,
            "-codec:a", "libmp3lame",
        ]
        # Apply bitrate setting (CBR or VBR)
        if settings.audio_bitrate_mode.lower() == "vbr":
            ff_cmd.extend(["-q:a", "2"])  # VBR quality 2 (~160-250k avg)
        else:
            ff_cmd.extend(["-b:a", settings.audio_bitrate])
        ff_cmd.extend([
            "-ar", str(settings.audio_sample_rate),
            "-ac", str(settings.audio_channels),
            "-id3v2_version", "3",
        ])
        # Add metadata tags
        for tag_key, tag_val in ffprobe_meta.items():
            if tag_val:
                ff_cmd.extend(["-metadata", f"{tag_key}={tag_val}"])
        ff_cmd.extend(["-map_metadata", "0", str(mp3_path)])

        rc, ffmpeg_stdout, ffmpeg_stderr = await _run(ff_cmd)
        if rc != 0:
            stderr = ffmpeg_stderr.decode(errors="replace") if ffmpeg_stderr else ""
            raise RuntimeError(f"ffmpeg failed: {stderr[:200]}")

        if not mp3_path.exists():
            raise RuntimeError("Transcode finished but MP3 not found")

        # Process thumbnail -> resize to small webp
        thumb_path = None
        thumb_files = list(video_dir.glob("*.*"))
        for tf in thumb_files:
            if tf.suffix.lower() in (".jpg", ".jpeg", ".webp", ".png"):
                original_thumb = tf
                break
        else:
            original_thumb = None

        if original_thumb:
            small_thumb = video_dir / f"{video_id}-small.webp"
            try:
                await _run([
                    "ffmpeg", "-y", "-i", str(original_thumb),
                    "-vf", "scale=400:225:force_original_aspect_ratio=decrease",
                    "-y", str(small_thumb),
                ])
                if small_thumb.exists():
                    thumb_path = str(small_thumb)
                    logger.info(f"[{jid}] Thumbnail resized: {small_thumb.name}")
            except Exception as e:
                logger.warning(f"[{jid}] Thumbnail resize failed: {e}")
            # Clean up original thumbnail
            try:
                original_thumb.unlink(missing_ok=True)
            except OSError:
                pass

        # Get final metadata from the MP3
        meta = await _get_audio_metadata(mp3_path)

        # Clean up the downloaded raw audio file
        try:
            Path(downloaded_file).unlink(missing_ok=True)
        except OSError:
            pass

        # Write per-item metadata JSON (DrivePod-compatible schema)
        track_entry = {
            "videoId": video_id,
            "channelId": info.get("channel_id", "") if isinstance(info, dict) else "",
            "title": meta.get("title") or (info.get("title", "") if isinstance(info, dict) else "") or job.get("title", ""),
            "author": meta.get("artist") or (info.get("uploader", "") if isinstance(info, dict) else "") or ffprobe_meta.get("artist", ""),
            "publishedAt": info.get("upload_date", "") if isinstance(info, dict) else "",
            "thumbnailPath": f"{video_id}/{video_id}-small.webp" if thumb_path else None,
            "audioPath": f"{video_id}/{video_id}.mp3",
            "duration": meta.get("duration", 0),
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        json_path = video_dir / f"{video_id}.json"
        json_path.write_text(json.dumps(track_entry, indent=2, ensure_ascii=False))
        logger.info(f"[{jid}] Metadata written: {json_path.name}")

        update_job(jid, status="completed", output_file=str(mp3_path), title=track_entry["title"], artist=track_entry["author"])
        logger.info(f"[{jid}] Completed: {video_dir.name}/")

        # Add to catalog
        add_track(track_entry)
        logger.info(f"[{jid}] Cataloged: {track_entry['title']}")

        # Rebuild the index.json (DrivePod-style)
        update_index_file()

    except Exception as e:
        err_msg = str(e)[:500]
        logger.error(f"[{jid}] Failed: {err_msg}")
        update_job(jid, status="failed", error=err_msg)


async def worker():
    """Background worker loop."""
    while True:
        pending = get_pending_jobs()
        running = len([j for j in pending if j["status"] == "running"])

        for job in pending:
            if running >= settings.max_concurrent:
                break
            update_job(job["id"], status="running")
            running += 1
            try:
                await process_job(job["id"])
            except Exception as e:
                logger.error(f"Worker crash on job {job['id']}: {e}")
                update_job(job["id"], status="failed", error=str(e)[:500])
            finally:
                running -= 1

        await asyncio.sleep(2)
