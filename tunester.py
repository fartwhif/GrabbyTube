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
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from config import settings
from db import get_config, load_catalog, save_catalog, set_config, update_track
from processor import _get_audio_metadata, update_index_file

logger = logging.getLogger("grabbytube.tunester")

POLL_INTERVAL = 5  # seconds between tunester job polls
UPSTREAM_CLEAR_TIMEOUT = 15  # seconds to wait on tunester's own queue clear

# Progress of the current/latest batch, keyed by video_id. Persisted to the
# config table on every status change (see `_persist`) so the queue list, and
# the run itself, survive a process restart.
_progress: dict[str, dict] = {}
_run_task: asyncio.Task | None = None

# config-table row holding the batch: one JSON blob, keyed so the hub's
# per-partition DB (db.DB_PATH) gives every tenant its own queue for free.
RUN_STATE_KEY = "tunester_run"
# A row in one of these was interrupted mid-batch, so it is still owed work.
PENDING_STATUSES = ("queued", "running")
# Everything else is terminal: that track needs nothing further.
TERMINAL_STATUSES = ("done", "fixup", "error")


def _blank_row(vid: str, name: str = "") -> dict:
    """One progress row. The persisted shape, the in-memory shape and the
    `/tracks/tunester` response are all built from this, so a restored batch
    can never be missing a key the UI reads."""
    return {
        "video_id": vid,
        "name": name,
        "status": "queued",
        "message": "",
        "replaced": False,
        "artist": None,
        "title": None,
        "year": None,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _persist() -> None:
    """Write the batch out, so the queue list survives this process.

    Called on every status CHANGE (never on the 5 s log-tail ticks), so a kill
    costs at most the message of the track that was in flight - and that row is
    re-queued on the next start, so its work is redone rather than lost. A save
    failure is logged, never fatal: losing the panel is no reason to stop tuning.
    """
    try:
        set_config(RUN_STATE_KEY, json.dumps(
            {"updated_at": _now(), "tracks": list(_progress.values())},
            ensure_ascii=False))
    except (sqlite3.Error, OSError) as e:
        logger.warning(f"tunester run state NOT saved: {e}")


def _clear_saved() -> None:
    """Empty the persisted batch. A cleared queue MUST stay cleared across a
    restart, or the next startup would resurrect it and run it again."""
    try:
        set_config(RUN_STATE_KEY, json.dumps(
            {"updated_at": _now(), "tracks": []}, ensure_ascii=False))
    except (sqlite3.Error, OSError) as e:
        logger.warning(f"tunester run state NOT cleared: {e}")


def _saved_rows() -> list[dict]:
    """The persisted rows, repaired to the full row shape ([] when nothing
    usable was ever saved, or the blob is corrupt)."""
    try:
        raw = get_config(RUN_STATE_KEY)
    except (sqlite3.Error, OSError) as e:
        logger.warning(f"tunester run state unreadable: {e}")
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("tunester run state is not JSON; starting clean")
        return []
    items = data.get("tracks") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    rows: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        vid = str(item.get("video_id", "")).strip()
        if not vid or vid in seen:
            continue
        seen.add(vid)
        row = _blank_row(vid, str(item.get("name", "")))
        status = str(item.get("status", "queued"))
        row["status"] = status if status in (
            PENDING_STATUSES + TERMINAL_STATUSES) else "queued"
        row["message"] = str(item.get("message", ""))[:300]
        row["replaced"] = bool(item.get("replaced"))
        for field in ("artist", "title", "year"):
            value = item.get(field)
            row[field] = str(value) if value is not None else None
        rows.append(row)
    return rows


def _catalog_map() -> dict[str, dict]:
    return {t["videoId"]: t for t in load_catalog() if t.get("videoId")}


def restore() -> int:
    """Rebuild the batch list from a run this process did not finish.

    Called once at startup (main.py lifespan), because leaving a batch running
    overnight should mean it is STILL RUNNING - not that it evaporated with the
    container:

    - interrupted batch (rows still `queued`/`running`): the run continues with
      exactly those tracks. A row that was mid-flight when the process died goes
      back to `queued`; its tuner job died with the tuner too, and re-submitting
      the same name is safe (tunester single-flights a song it is already
      fetching, and a finished one is a cache hit).
    - finished batch: nothing to run, but the list comes back, so last night's
      results are there to read instead of an empty panel.

    Returns the number of tracks handed back to the run.
    """
    global _run_task
    if _run_active() or _progress:
        return 0                      # a batch of this process is already here
    rows = _saved_rows()
    if not rows:
        return 0
    pending: list[str] = []
    for row in rows:
        if row["status"] == "running":
            row["status"] = "queued"
            row["message"] = "re-queued after a GrabbyTube restart"
        if row["status"] in PENDING_STATUSES:
            pending.append(row["video_id"])
        _progress[row["video_id"]] = row
    logger.info(f"tunester run state restored: {len(rows)} row(s), "
                f"{len(pending)} still owed work")
    if pending:
        _persist()                    # records the re-queued rows
        _run_task = asyncio.create_task(_run_batch(pending, _catalog_map()))
    else:
        # Every row is terminal, so the batch reached its end and its `finally`
        # rebuilt the DrivePod index - UNLESS the process died between the last
        # status write and that rebuild, which is exactly the divergence a
        # restart can leave. One idempotent sweep heals it.
        try:
            update_index_file()
        except OSError as e:
            logger.warning(f"index rebuild after restore failed: {e}")
    return len(pending)


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
    catalog = _catalog_map()
    ids = [v for v in dict.fromkeys(video_ids) if v in catalog]
    if not ids:
        raise ValueError("no known tracks selected")
    # Enqueue in a random order so the batch doesn't track library order.
    random.shuffle(ids)
    _progress.clear()
    for vid in ids:
        t = catalog[vid]
        _progress[vid] = _blank_row(
            vid, " - ".join(x for x in (t.get("author", ""),
                                        t.get("title", "")) if x))
    # The queue is on disk BEFORE the first submit, so a crash in the next
    # second still leaves the list to restore (and the work to redo).
    _persist()
    _run_task = asyncio.create_task(_run_batch(ids, catalog))
    logger.info(f"tunester run started for {len(ids)} track(s): {ids}")
    return ids


def _set(vid: str, status: str, message: str = "", **fields):
    """Move one row to a new status (and update its fields).

    A status CHANGE is persisted; a message-only update (the tuner's log tail,
    rewritten every poll) is not - it is live display, and re-deriving it after
    a restart is what the resumed run does anyway.
    """
    entry = _progress.setdefault(vid, _blank_row(vid))
    changed = entry["status"] != status
    entry["status"] = status
    if message:
        entry["message"] = message
    entry.update(fields)
    if changed:
        _persist()


async def _run_batch(ids: list[str], catalog: dict[str, dict]):
    """Work through `ids` one at a time, then rebuild the DrivePod index.

    `catalog` is the metadata each name is built from; a resumed run passes a
    FRESH catalog read, so a track whose name was edited while the batch was
    down is tuned under its current name, and a track that was deleted in the
    meantime is reported rather than tuned from a stale row.
    """
    base = settings.tunester_url.rstrip("/")
    try:
        async with aiohttp.ClientSession() as session:
            for vid in ids:
                try:
                    if vid not in catalog:
                        _set(vid, "error",
                             "track is no longer in the library")
                        continue
                    await _run_one(session, base, vid, catalog[vid])
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception(f"tunester run failed for {vid}")
                    _set(vid, "error", str(e)[:300])
    finally:
        try:
            update_index_file()
        except OSError as e:
            logger.warning(f"index rebuild after the tunester run failed: {e}")


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
    LLM/download work, then clears the progress panel AND its saved copy, so a
    queue the operator cleared is not resurrected by the next restart. Work
    already applied (replaced MP3s, fixups) is kept; tunester leaves its
    downloaded files in place, so the clear only drops bookkeeping and queued
    work.
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
    _clear_saved()
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
