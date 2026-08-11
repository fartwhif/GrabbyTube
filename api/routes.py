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
        # extract_flat=True leaves thumbnail=None; use the thumbnails list instead
        thumb_url = e.get("thumbnail", "") or ""
        if not thumb_url:
            thumbs = e.get("thumbnails", [])
            if thumbs:
                thumb_url = thumbs[0].get("url", "")
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
    return {"job_id": jid, "status": "queued"}


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


@router.post("/tracks/rebuild")
async def rebuild_tracks():
    """Re-probe all MP3 files in the output directory and update the catalog."""
    import asyncio
    from processor import _get_audio_metadata
    catalog = load_catalog()
    updated = 0
    for track in catalog:
        fp = Path(track.get("filepath", ""))
        if fp.exists():
            meta = await _get_audio_metadata(fp)
            for key in ("duration", "filesize", "codec", "bitrate", "sample_rate", "channels", "bits_per_sample"):
                if meta.get(key):
                    track[key] = meta[key]
            updated += 1
    save_catalog(catalog)
    return {"rebuilt": updated, "total": len(catalog)}


# --- Serve MP3 files ---

@router.get("/audio/{filename}")
async def serve_audio(filename: str):
    """Serve a completed MP3 file."""
    fp = settings.output_path / filename
    if not fp.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(
        str(fp),
        media_type="audio/mpeg",
        filename=fp.name,
    )


# --- Health ---

@router.get("/health")
async def health():
    return {"status": "ok", "tracks": len(load_catalog())}
