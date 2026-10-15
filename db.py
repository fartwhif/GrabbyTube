"""SQLite job queue and JSON track catalog."""
import json
import logging
import sqlite3
from pathlib import Path

from config import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "grabbytube.db"
CATALOG_PATH = PROJECT_ROOT / "grabby-tracks.json"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            video_id TEXT,
            title TEXT,
            artist TEXT,
            url TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            output_file TEXT,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    # Migration: add artist column if it doesn't exist
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN artist TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    conn.close()
    # Ensure catalog file exists
    if not CATALOG_PATH.exists():
        CATALOG_PATH.write_text("[]")


def get_config(key: str, default: str | None = None) -> str | None:
    conn = get_conn()
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_config(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def is_video_exists(video_id: str) -> bool:
    """Check if a video_id is already downloaded (in catalog)."""
    catalog = load_catalog()
    return any(t.get("videoId") == video_id for t in catalog)


def get_existing_video_ids() -> set[str]:
    """Return a set of all video IDs already in the catalog (single load)."""
    catalog = load_catalog()
    return {t["videoId"] for t in catalog if t.get("videoId")}


def is_video_queued(video_id: str) -> bool:
    """Check if a video_id is already in the pending queue."""
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE video_id = ? AND status = 'queued' LIMIT 1",
        (video_id,),
    ).fetchone()
    conn.close()
    return row is not None


def enqueue_job(query: str, video_id: str, title: str, url: str, artist: str | None = None) -> int:
    # Skip if already downloaded or already queued
    if is_video_exists(video_id) or is_video_queued(video_id):
        return 0
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO jobs (query, video_id, title, artist, url, status) VALUES (?, ?, ?, ?, ?, 'queued')",
        (query, video_id, title, artist, url),
    )
    conn.commit()
    jid = cur.lastrowid
    conn.close()
    return jid


def get_job(jid: int) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_job(jid: int, **kwargs):
    allowed = {"status", "output_file", "error", "updated_at", "title", "artist"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [jid]
    conn = get_conn()
    conn.execute(f"UPDATE jobs SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", values)
    conn.commit()
    conn.close()


def list_jobs(status: str | None = None, limit: int = 0) -> list[dict]:
    conn = get_conn()
    if status:
        if limit > 0:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY id DESC",
                (status,),
            ).fetchall()
    else:
        if limit > 0:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC"
            ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_pending_jobs() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- JSON Track Catalog ---

def load_catalog() -> list[dict]:
    if not CATALOG_PATH.exists():
        return []
    try:
        return json.loads(CATALOG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def save_catalog(tracks: list[dict]):
    CATALOG_PATH.write_text(json.dumps(tracks, indent=2, ensure_ascii=False))


def add_track(track: dict):
    catalog = load_catalog()
    # Avoid duplicates by videoId
    catalog = [t for t in catalog if t.get("videoId") != track.get("videoId")]
    catalog.append(track)
    save_catalog(catalog)


def update_track(video_id: str, **fields) -> dict | None:
    """Update fields on a catalog track by videoId. Returns the updated track."""
    catalog = load_catalog()
    for t in catalog:
        if t.get("videoId") == video_id:
            t.update(fields)
            save_catalog(catalog)
            return t
    return None


def list_tracks(sort_by: str = "createdAt", reverse: bool = True) -> list[dict]:
    catalog = load_catalog()
    key_map = {
        "title": lambda t: t.get("title", "").lower(),
        "author": lambda t: t.get("author", "").lower(),
        "createdAt": lambda t: t.get("createdAt", ""),
        "publishedAt": lambda t: t.get("publishedAt", ""),
        "duration": lambda t: t.get("duration", 0),
    }
    key_fn = key_map.get(sort_by, key_map["createdAt"])
    catalog.sort(key=key_fn, reverse=reverse)
    return catalog


def _clean_title(title: str) -> str:
    """Strip parenthesized/bracketed suffixes from YouTube titles.

    Removes things like:
      "Song (Official Video)" -> "Song"
      "Song [HD UPGRADE]" -> "Song"
      "Song (Lyrics) [4K]" -> "Song"
      "Song (Official Video) [HD Remaster]" -> "Song"
    Keeps meaningful prefixes like "Artist - Song".
    """
    import re as _re
    # Remove trailing bracketed/parenthesized blocks (can be nested)
    # Handle () and [] including nested ones like ((...))
    cleaned = _re.sub(r'\s*[([].*?[])]\s*$', '', title)
    # Remove any remaining trailing parenthesized blocks
    cleaned = _re.sub(r'\s*\(.*?\)\s*$', '', cleaned)
    cleaned = _re.sub(r'\s*\[.*?\]\s*$', '', cleaned)
    # Strip trailing punctuation and whitespace
    cleaned = cleaned.rstrip(" .-")
    return cleaned.strip()


async def find_duplicates() -> list[dict]:
    """Find potential duplicate tracks using an LLM to analyze titles.

    Sends a simple prompt with the cleaned title list to the LLM and parses
    the JSON response back into duplicate groups.
    """
    import asyncio
    import aiohttp

    from config import settings
    catalog = load_catalog()
    if len(catalog) < 2:
        return []

    if not settings.llm_api_key or not settings.llm_base_url:
        logger = logging.getLogger("grabbytube.db")
        logger.warning("LLM API key or base URL not configured — skipping duplicate detection")
        return []

    # Build the list: numbered cleaned titles with artist and duration
    lines = []
    for i, t in enumerate(catalog, 1):
        title = _clean_title(t.get("title", ""))
        author = t.get("author", "")
        dur = t.get("duration", 0) or 0
        dur_min = f"{dur / 60:.1f}m" if dur else "?m"
        lines.append(f"{i}. \"{title}\" | {author} | {dur_min}")

    items_block = "\n".join(lines)
    fmt_example = json.dumps({
        "duplicates": [{
            "track": "Artist - Title",
            "occurrences": [{"index": 1, "entry": "1. Title | Artist | 3.0m"}]
        }]
    }, indent=2)
    user_prompt = (
        "find the duplicates, give JSON output:\n"
        f"{items_block}\n\n"
        "Output format:\n"
        f"{fmt_example}\n\n"
        "Return ONLY the JSON object. No explanation, no reasoning, no text before or after the JSON."
    )

    # --- LLM call (mirrors llm_client.py pattern) ---
    api_key = settings.llm_api_key
    base_url = settings.llm_base_url.rstrip("/")

    url = base_url + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "qwen3.6-27b",
        "messages": [
            {"role": "system", "content": "You are a music library assistant. Output only a JSON object with a 'duplicates' key. No other text."},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 32768,
        "temperature": 0.0,
        "thinking": {"type": "enabled", "budget_tokens": 100},
    }

    logger = logging.getLogger("grabbytube.db")
    try:
        async with asyncio.timeout(300):
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=body,
                                       timeout=aiohttp.ClientTimeout(total=240)) as resp:
                    data = await resp.json()
                    msg = data.get("choices", [{}])[0].get("message", {})
                    content = msg.get("content", "")
                    if not content:
                        reasoning = msg.get("reasoning_content", "")
                        content = reasoning.strip() if reasoning else ""
    except asyncio.TimeoutError:
        logger.error("LLM duplicate detection timed out")
        return []
    except Exception as e:
        logger.error(f"LLM duplicate detection failed: {e}")
        return []

    # Parse JSON from LLM response
    import re as _re
    # Find the JSON object in the content
    content = _re.sub(r'^[^\{]*', '', content)  # strip everything before first {
    # Try to parse incrementally to find the first complete JSON
    result = None
    depth = 0
    json_start = 0
    for i, ch in enumerate(content):
        if ch == '{':
            if depth == 0:
                json_start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                candidate = content[json_start:i+1]
                try:
                    result = json.loads(candidate)
                    break
                except json.JSONDecodeError:
                    pass  # keep going
    if result is None:
        logger.warning(f"LLM returned no parseable JSON: {content[:200]}")
        return []

    # Parse new format: { "duplicates": [ { "track": "...", "occurrences": [ { "index": N, ... } ] } ] }
    duplicates = result.get("duplicates", [])
    result_groups = []
    seen_ids = set()
    for dup in duplicates:
        occurrences = dup.get("occurrences", [])
        if len(occurrences) < 2:
            continue
        tracks = []
        for occ in occurrences:
            idx = occ.get("index", 0) - 1  # 1-indexed
            if 0 <= idx < len(catalog):
                t = catalog[idx]
                vid = t.get("videoId")
                if vid and vid not in seen_ids:
                    seen_ids.add(vid)
                    tracks.append(t)
        if len(tracks) >= 2:
            result_groups.append({
                "key": dup.get("track", tracks[0].get("title", "")),
                "tracks": tracks,
                "count": len(tracks),
            })

    result_groups.sort(key=lambda g: -g["count"])
    return result_groups


def get_cleaned_titles() -> list[str]:
    """Return numbered list of cleaned titles for manual LLM testing."""
    catalog = load_catalog()
    return [f"{i}. {_clean_title(t.get('title', ''))} | {t.get('author', '')} | {t.get('duration', 0) / 60:.1f}m"
            for i, t in enumerate(catalog, 1)]


def batch_delete_tracks(video_ids: list[str]) -> list[str]:
    """Remove multiple tracks from catalog by videoId. Returns list of actually deleted IDs."""
    import shutil
    from config import settings
    from processor import update_index_file
    catalog = load_catalog()
    deleted = []
    for vid in video_ids:
        new_catalog = [t for t in catalog if t.get("videoId") != vid]
        if len(new_catalog) < len(catalog):
            catalog = new_catalog
            deleted.append(vid)
            # Delete the video directory on disk
            video_dir = settings.output_path / vid
            if video_dir.exists():
                shutil.rmtree(str(video_dir))
    if deleted:
        save_catalog(catalog)
        update_index_file()
    return deleted
