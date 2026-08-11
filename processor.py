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
    """Process a single job: download -> transcode -> catalog."""
    job = get_job(jid)
    if not job:
        return

    update_job(jid, status="downloading")
    logger.info(f"[{jid}] Downloading: {job['title']}")

    output_dir = settings.output_path
    safe_title = _sanitize(job["title"])
    outtmpl = str(output_dir / f"{safe_title}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "writethumbnail": True,
        "verbose": False,
    }

    downloaded_file = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(job["url"], download=True)
            # yt-dlp gives us the output filename
            downloaded_file = info.get("filepath")
            if not downloaded_file:
                # Fallback: find what we just downloaded
                for f in output_dir.iterdir():
                    if f.stat().st_mtime > datetime.now(timezone.utc).timestamp() - 60:
                        downloaded_file = str(f)
                        break

        if not downloaded_file or not Path(downloaded_file).exists():
            raise RuntimeError("Download completed but output file not found")

        update_job(jid, status="transcoding")
        logger.info(f"[{jid}] Transcoding: {Path(downloaded_file).name}")

        # Transcode to MP3
        mp3_name = _sanitize_ext(job["title"]).rsplit(".", 1)[0] + ".mp3"
        mp3_path = output_dir / mp3_name

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
            "-b:a", settings.preferred_bitrate,
            "-id3v2_version", "3",
        ]
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

        # Get final metadata from the MP3
        meta = await _get_audio_metadata(mp3_path)

        # Clean up the downloaded file
        try:
            Path(downloaded_file).unlink(missing_ok=True)
        except OSError:
            pass
        # Clean up thumbnail if any
        thumb = str(downloaded_file).rsplit(".", 1)[0] + ".jpg"
        Path(thumb).unlink(missing_ok=True)

        update_job(jid, status="completed", output_file=str(mp3_path))
        logger.info(f"[{jid}] Completed: {mp3_path.name}")

        # Add to catalog
        track_entry = {
            "video_id": job.get("video_id", ""),
            "title": meta.get("title") or job.get("title", ""),
            "artist": meta.get("artist") or ffprobe_meta.get("artist", ""),
            "album": meta.get("album", ""),
            "genre": meta.get("genre", ""),
            "year": meta.get("date", ffprobe_meta.get("date", ""))[:4],
            "duration": meta.get("duration", 0),
            "filesize": meta.get("filesize", 0),
            "codec": meta.get("codec", "mp3"),
            "bitrate": meta.get("bitrate", 0),
            "sample_rate": meta.get("sample_rate", 0),
            "channels": meta.get("channels", 0),
            "bits_per_sample": meta.get("bits_per_sample", 0),
            "file_encoding": "ID3v2.3",
            "filename": mp3_path.name,
            "filepath": str(mp3_path),
            "source_url": job.get("url", ""),
            "date_added": datetime.now(timezone.utc).isoformat(),
        }
        add_track(track_entry)
        logger.info(f"[{jid}] Cataloged: {track_entry['title']}")

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
