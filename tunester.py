"""Run cataloged tracks through the tunester pipeline and apply its results.

tunester (separate container on the shared docker network) takes a dirty
"Artist - Title" name and delivers a verified uncensored 160 kbps CBR MP3.
Its job API carries fixup data (artist/title/year) whenever discovery
succeeds, even when no verified file is delivered:

- job done                    -> replace the track's MP3, apply fixup
- job error with fixup data   -> apply the artist/title/year fixup only
- job error without fixup     -> track untouched, reported as an error

When the year is known the item title becomes "TITLE (YEAR)"; otherwise
just "TITLE".
"""
import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path

import aiohttp

from config import settings
from db import load_catalog, save_catalog, update_track
from processor import _get_audio_metadata, update_index_file

logger = logging.getLogger("grabbytube.tunester")

POLL_INTERVAL = 5  # seconds between tunester job polls
UPSTREAM_CLEAR_TIMEOUT = 15  # seconds to wait on tunester's own queue clear

# Progress of the current/latest batch, keyed by video_id.
_progress: dict[str, dict] = {}
_run_task: asyncio.Task | None = None


def _run_active() -> bool:
    return bool(_run_task is not None and not _run_task.done())


def get_status() -> dict:
    """Progress snapshot for the UI (current or last batch)."""
    return {"active": _run_active(), "tracks": list(_progress.values())}


def start(video_ids: list[str]) -> list[str]:
    """Start a background batch run. Returns the accepted video_ids."""
    global _run_task
    if _run_active():
        raise RuntimeError("a tunester run is already in progress")
    catalog = {t["videoId"]: t for t in load_catalog() if t.get("videoId")}
    ids = [v for v in dict.fromkeys(video_ids) if v in catalog]
    if not ids:
        raise ValueError("no known tracks selected")
    # Enqueue in a random order so the batch doesn't track library order.
    random.shuffle(ids)
    _progress.clear()
    for vid in ids:
        t = catalog[vid]
        _progress[vid] = {
            "video_id": vid,
            "name": " - ".join(x for x in (t.get("author", ""), t.get("title", "")) if x),
            "status": "queued",
            "message": "",
            "replaced": False,
            "artist": None,
            "title": None,
            "year": None,
        }
    _run_task = asyncio.create_task(_run_batch(ids, catalog))
    logger.info(f"tunester run started for {len(ids)} track(s): {ids}")
    return ids


def _set(vid: str, status: str, message: str = "", **fields):
    entry = _progress.setdefault(
        vid, {"video_id": vid, "name": "", "status": "queued",
              "message": "", "replaced": False,
              "artist": None, "title": None, "year": None})
    entry["status"] = status
    if message:
        entry["message"] = message
    entry.update(fields)


async def _run_batch(ids: list[str], catalog: dict[str, dict]):
    base = settings.tunester_url.rstrip("/")
    try:
        async with aiohttp.ClientSession() as session:
            for vid in ids:
                try:
                    await _run_one(session, base, vid, catalog[vid])
                except Exception as e:
                    logger.exception(f"tunester run failed for {vid}")
                    _set(vid, "error", str(e)[:300])
    finally:
        update_index_file()


async def _run_one(session: aiohttp.ClientSession, base: str, vid: str,
                   track: dict):
    query = " - ".join(
        x for x in (track.get("author", ""), track.get("title", "")) if x
    ).strip()
    if not query:
        _set(vid, "error", "track has no title to send to tunester")
        return

    _set(vid, "running", f"submitted to tunester: {query}")
    job = await _submit(session, base, query)
    jid = job["id"]
    deadline = time.monotonic() + settings.tunester_timeout
    while True:
        job = await _job(session, base, jid)
        if job.get("status") in ("done", "error"):
            break
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"tunester job {jid} timed out after {settings.tunester_timeout}s")
        log_tail = (job.get("log") or [""])[-1]
        _set(vid, "running", f"tunester: {log_tail}" if log_tail
             else "waiting on tunester...")
        await asyncio.sleep(POLL_INTERVAL)

    artist, title, year = job.get("artist"), job.get("title"), job.get("year")

    if job.get("status") == "done" and job.get("file"):
        await _replace_file(session, base, jid, vid)
        duration = None
        if artist and title:
            duration = (await _get_audio_metadata(
                settings.output_path / vid / f"{vid}.mp3")).get("duration")
            apply_fixup(vid, track, artist, title, year, duration)
        # Audio data file was actually replaced via tunester.
        mark_tuned(vid)
        _set(vid, "done",
             f"replaced with verified track"
             + (f": {artist} - {title}" + (f" ({year})" if year else "")
                if artist and title else ""),
             replaced=True, artist=artist, title=title, year=year)
    elif artist and title:
        # tunester identified the song but delivered no verified file:
        # fixup only, audio untouched.
        apply_fixup(vid, track, artist, title, year)
        _set(vid, "fixup",
             "no usable file from tunester; fixup applied: "
             f"{artist} - {title}" + (f" ({year})" if year else ""),
             artist=artist, title=title, year=year)
    else:
        _set(vid, "error", (job.get("error") or "tunester failed")[:300])


async def _submit(session: aiohttp.ClientSession, base: str, name: str) -> dict:
    async with session.post(f"{base}/api/tune", json={"name": name},
                            timeout=aiohttp.ClientTimeout(total=60)) as r:
        if r.status != 202:
            text = (await r.text())[:200]
            raise RuntimeError(f"tunester submit failed: HTTP {r.status} {text}")
        return await r.json()


async def _job(session: aiohttp.ClientSession, base: str, jid: str) -> dict:
    async with session.get(f"{base}/api/job/{jid}",
                           timeout=aiohttp.ClientTimeout(total=60)) as r:
        if r.status != 200:
            raise RuntimeError(f"tunester job lookup failed: HTTP {r.status}")
        return await r.json()


async def _replace_file(session: aiohttp.ClientSession, base: str, jid: str,
                        vid: str) -> None:
    """Download tunester's delivery and atomically swap it in as the track MP3."""
    mp3_path = settings.output_path / vid / f"{vid}.mp3"
    if not mp3_path.parent.exists():
        raise RuntimeError(f"track directory missing: {mp3_path.parent}")
    async with session.get(f"{base}/api/dl/{jid}",
                           timeout=aiohttp.ClientTimeout(total=900)) as r:
        if r.status != 200:
            raise RuntimeError(f"tunester file download failed: HTTP {r.status}")
        tmp = mp3_path.with_suffix(".tunester.mp3")
        try:
            with open(tmp, "wb") as f:
                async for chunk in r.content.iter_chunked(1 << 20):
                    f.write(chunk)
            os.replace(tmp, mp3_path)
        finally:
            # A failed/cancelled download leaves the partial delivery behind;
            # without this it would sit in the track directory forever
            # (os.replace already moved it out of the way on success).
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError as e:
                    logger.warning(f"could not remove {tmp}: {e}")
    _invalidate_transcode_sidecar(vid)
    logger.info(f"replaced {mp3_path} with tunester delivery")


def _invalidate_transcode_sidecar(vid: str) -> None:
    """Remove a stale FrogRock transcode sidecar for this track's MP3.

    FrogRock caches a transcode of each non-target original as
    ``<vid>.mp3.tr64k.mp3`` alongside the source. When tunester replaces the
    source, that derivative was built from the *old* audio and is no longer
    a valid cache of the new one — delete it so the next play rebuilds a
    fresh transcode from the current source. Best-effort: a missing sidecar
    (nothing ever transcoded) is not an error.
    """
    sidecar = settings.output_path / vid / f"{vid}.mp3.tr64k.mp3"
    if not sidecar.exists():
        return
    try:
        sidecar.unlink()
    except OSError as e:
        logger.warning(f"could not remove transcode sidecar {sidecar}: {e}")
        return
    logger.info(f"invalidated transcode sidecar {sidecar}")


def apply_fixup(vid: str, track: dict, artist: str, title: str,
                year: str | None, duration: float | None = None) -> None:
    """Write the fixup into the per-item JSON (DrivePod index) and the catalog.

    Item title becomes "TITLE (YEAR)" when the year is known, else "TITLE".
    """
    display_title = f"{title} ({year})" if year else title
    json_path = settings.output_path / vid / f"{vid}.json"
    if json_path.exists():
        data = json.loads(json_path.read_text())
    else:
        data = dict(track)
    data["title"] = display_title
    data["author"] = artist
    if duration is not None:
        data["duration"] = duration
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    update_track(vid, title=display_title, author=artist,
                 **({"duration": duration} if duration is not None else {}))
    logger.info(f"fixup applied to {vid}: {artist} - {display_title}")


def mark_tuned(vid: str) -> None:
    """Record that the track's audio file was replaced via tunester.

    Sets ``tuned: true`` in both the per-item JSON (DrivePod index data)
    and the catalog.
    """
    json_path = settings.output_path / vid / f"{vid}.json"
    if json_path.exists():
        data = json.loads(json_path.read_text())
        data["tuned"] = True
        json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    update_track(vid, tuned=True)
    logger.info(f"marked {vid} as tuned (audio replaced via tunester)")


def clear_tuned_all() -> dict:
    """Strip the tuned flag from every track: catalog, per-item JSONs, index.

    Only the bookkeeping goes - the MP3s keep whatever tunester delivered, so
    every track becomes a tunester candidate again (its badge disappears and
    "Select UnTuned" starts matching it).
    """
    if _run_active():
        raise RuntimeError("a tunester run is already in progress")

    catalog = load_catalog()
    cleared = sum(1 for t in catalog if t.get("tuned"))
    for t in catalog:
        if "tuned" in t:
            t["tuned"] = False
    save_catalog(catalog)

    files = 0
    for subdir in sorted(settings.output_path.iterdir()):
        if not subdir.is_dir():
            continue
        meta_file = subdir / f"{subdir.name}.json"
        if not meta_file.exists():
            continue
        try:
            data = json.loads(meta_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not data.get("tuned"):
            continue
        try:
            data["tuned"] = False
            meta_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            files += 1
        except OSError:
            logger.warning(f"could not rewrite {meta_file}")

    update_index_file()
    logger.info(
        f"cleared tuned flag: {cleared} catalog track(s), {files} item JSON(s)")
    return {"cleared": cleared, "files": files}


async def clear_run() -> dict:
    """Empty the tunester queue: stop the batch, drop its progress rows.

    Cancels the running batch (its not-yet-submitted tracks are never sent),
    asks tunester to cancel its own jobs so the in-flight track stops burning
    LLM/download work, then clears the progress panel. Work already applied
    (replaced MP3s, fixups) is kept; tunester leaves its downloaded files in
    place, so the clear only drops bookkeeping and queued work.
    """
    global _run_task
    cancelled_run = _run_active()
    if cancelled_run:
        _run_task.cancel()
        try:
            await _run_task
        except asyncio.CancelledError:
            pass

    upstream: int | None = None
    base = settings.tunester_url.rstrip("/")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                    f"{base}/api/clear",
                    timeout=aiohttp.ClientTimeout(total=UPSTREAM_CLEAR_TIMEOUT)
            ) as r:
                if r.status == 200:
                    upstream = (await r.json()).get("cleared")
                else:
                    logger.warning(
                        f"tunester queue clear failed: HTTP {r.status}")
    except (aiohttp.ClientError, OSError) as e:
        logger.warning(f"tunester queue clear failed: {e}")

    rows = len(_progress)
    _progress.clear()
    _run_task = None
    logger.info(
        f"tunester queue cleared: run "
        f"{'cancelled' if cancelled_run else 'idle'}, {rows} progress row(s), "
        f"{upstream} upstream job(s)")
    return {
        "cancelled_run": cancelled_run,
        "rows": rows,
        "upstream_jobs": upstream,
        "tracks": list(_progress.values()),
    }
