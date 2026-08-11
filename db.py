"""SQLite job queue and JSON track catalog."""
import json
import sqlite3
from pathlib import Path

from config import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "grabbytube.db"
CATALOG_PATH = PROJECT_ROOT / "tracks.json"


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
            url TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            output_file TEXT,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
    """)
    conn.commit()
    conn.close()
    # Ensure catalog file exists
    if not CATALOG_PATH.exists():
        CATALOG_PATH.write_text("[]")


def enqueue_job(query: str, video_id: str, title: str, url: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO jobs (query, video_id, title, url, status) VALUES (?, ?, ?, ?, 'queued')",
        (query, video_id, title, url),
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
    allowed = {"status", "output_file", "error", "updated_at"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [jid]
    conn = get_conn()
    conn.execute(f"UPDATE jobs SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", values)
    conn.commit()
    conn.close()


def list_jobs(status: str = None, limit: int = 50) -> list[dict]:
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?",
            (limit,),
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
    # Avoid duplicates by video_id
    catalog = [t for t in catalog if t.get("video_id") != track.get("video_id")]
    catalog.append(track)
    save_catalog(catalog)


def list_tracks(sort_by: str = "date_added", reverse: bool = True) -> list[dict]:
    catalog = load_catalog()
    key_map = {
        "title": lambda t: t.get("title", "").lower(),
        "artist": lambda t: t.get("artist", "").lower(),
        "date_added": lambda t: t.get("date_added", ""),
        "duration": lambda t: t.get("duration", 0),
        "filesize": lambda t: t.get("filesize", 0),
    }
    key_fn = key_map.get(sort_by, key_map["date_added"])
    catalog.sort(key=key_fn, reverse=reverse)
    return catalog
