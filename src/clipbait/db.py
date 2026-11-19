"""SQLite persistence.

Deliberately dependency-free (stdlib sqlite3) and thread-safe: every call opens
its own connection, WAL mode lets the API, worker and CLI share one file.

Postgres is a drop-in later behind the same function surface; nothing outside
this module writes SQL.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import get_settings
from .schema import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_url TEXT,
  source_path TEXT,
  duration REAL,
  status TEXT NOT NULL DEFAULT 'queued',
  language TEXT,
  options TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clips (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  title TEXT NOT NULL,
  hook TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  hashtags TEXT NOT NULL DEFAULT '[]',
  start REAL NOT NULL,
  end REAL NOT NULL,
  score INTEGER NOT NULL DEFAULT 0,
  scores TEXT NOT NULL DEFAULT '{}',
  reason TEXT NOT NULL DEFAULT '',
  quote TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  render_path TEXT,
  thumb_path TEXT,
  caption_preset TEXT NOT NULL DEFAULT 'default',
  reframe_mode TEXT NOT NULL DEFAULT 'auto',
  captions INTEGER NOT NULL DEFAULT 1,
  hook_titles INTEGER NOT NULL DEFAULT 1,
  meta TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_clips_project ON clips(project_id);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  monitor_id TEXT,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  progress REAL NOT NULL DEFAULT 0,
  step TEXT NOT NULL DEFAULT 'queued',
  message TEXT NOT NULL DEFAULT '',
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id);

CREATE TABLE IF NOT EXISTS monitors (
  id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'idle',
  chat_provider TEXT NOT NULL DEFAULT 'auto',
  options TEXT NOT NULL DEFAULT '{}',
  started_at TEXT,
  stopped_at TEXT,
  error TEXT,
  stats TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS moments (
  id TEXT PRIMARY KEY,
  monitor_id TEXT NOT NULL,
  start REAL NOT NULL,
  end REAL NOT NULL,
  score REAL NOT NULL,
  signal TEXT NOT NULL DEFAULT '{}',
  messages TEXT NOT NULL DEFAULT '[]',
  clip_id TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_moments_monitor ON moments(monitor_id, start);

CREATE TABLE IF NOT EXISTS chat_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  monitor_id TEXT NOT NULL,
  t REAL NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'text'
);
CREATE INDEX IF NOT EXISTS idx_chat_monitor ON chat_events(monitor_id, t);
"""

_JSON_FIELDS = {
    "options",
    "scores",
    "hashtags",
    "meta",
    "stats",
    "signal",
    "messages",
}

_BOOL_FIELDS = {"captions", "hook_titles"}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key, value in list(data.items()):
        if key in _JSON_FIELDS and isinstance(value, str):
            try:
                data[key] = json.loads(value)
            except json.JSONDecodeError:
                data[key] = {} if key not in {"hashtags", "messages"} else []
        if key in _BOOL_FIELDS and value is not None:
            data[key] = bool(value)
    return data


def _encode(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in _JSON_FIELDS and not isinstance(value, str):
            out[key] = json.dumps(value)
        elif key in _BOOL_FIELDS:
            out[key] = 1 if value else 0
        else:
            out[key] = value
    return out


def insert(table: str, data: dict[str, Any]) -> dict[str, Any]:
    payload = _encode(data)
    columns = ", ".join(payload)
    placeholders = ", ".join(f":{name}" for name in payload)
    with connect() as conn:
        conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", payload)
        row = conn.execute(f"SELECT * FROM {table} WHERE rowid = last_insert_rowid()").fetchone()
    return _decode(row)


def update(table: str, row_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
    payload = _encode(data)
    with connect() as conn:
        # Not every table tracks updated_at (moments and chat_events do not).
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "updated_at" in columns:
            payload["updated_at"] = utcnow()
        if not payload:
            row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
            return _decode(row) if row else None
        assignments = ", ".join(f"{name} = :{name}" for name in payload)
        payload["_id"] = row_id
        conn.execute(f"UPDATE {table} SET {assignments} WHERE id = :_id", payload)
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    return _decode(row) if row else None


def get(table: str, row_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    return _decode(row) if row else None


def list_rows(table: str, where: str = "", params: Iterable[Any] = (), limit: int = 200) -> list[dict]:
    clause = f"WHERE {where}" if where else ""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM {table} {clause} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [_decode(row) for row in rows]


def delete(table: str, row_id: str) -> None:
    with connect() as conn:
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))


# ------------------------------------------------------------------ projects


def create_project(data: dict[str, Any]) -> dict[str, Any]:
    now = utcnow()
    payload = {
        "id": new_id("prj"),
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "options": {},
        **data,
    }
    return insert("projects", payload)


def get_project(project_id: str) -> dict[str, Any] | None:
    return get("projects", project_id)


def list_projects(limit: int = 100) -> list[dict]:
    return list_rows("projects", limit=limit)


def delete_project(project_id: str) -> None:
    delete("projects", project_id)


# ---------------------------------------------------------------------- clips


def create_clip(data: dict[str, Any]) -> dict[str, Any]:
    now = utcnow()
    payload = {
        "id": new_id("clip"),
        "status": "draft",
        "created_at": now,
        "updated_at": now,
        **data,
    }
    return insert("clips", payload)


def get_clip(clip_id: str) -> dict[str, Any] | None:
    return get("clips", clip_id)


def list_clips(project_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM clips WHERE project_id = ? ORDER BY score DESC, start ASC",
            (project_id,),
        ).fetchall()
    return [_decode(row) for row in rows]


def delete_clips(project_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM clips WHERE project_id = ?", (project_id,))


# ----------------------------------------------------------------------- jobs


def create_job(data: dict[str, Any]) -> dict[str, Any]:
    now = utcnow()
    payload = {"id": new_id("job"), "created_at": now, "updated_at": now, **data}
    return insert("jobs", payload)


def update_job(job_id: str, **fields: Any) -> dict[str, Any] | None:
    return update("jobs", job_id, fields)


def get_job(job_id: str) -> dict[str, Any] | None:
    return get("jobs", job_id)


def list_jobs(project_id: str | None = None, monitor_id: str | None = None) -> list[dict]:
    if project_id:
        return list_rows("jobs", "project_id = ?", (project_id,), limit=50)
    if monitor_id:
        return list_rows("jobs", "monitor_id = ?", (monitor_id,), limit=50)
    return list_rows("jobs", limit=100)


# ------------------------------------------------------------------- monitors


def create_monitor(data: dict[str, Any]) -> dict[str, Any]:
    now = utcnow()
    payload = {"id": new_id("mon"), "created_at": now, "updated_at": now, **data}
    return insert("monitors", payload)


def get_monitor(monitor_id: str) -> dict[str, Any] | None:
    return get("monitors", monitor_id)


def list_monitors(limit: int = 50) -> list[dict]:
    return list_rows("monitors", limit=limit)


def update_monitor(monitor_id: str, **fields: Any) -> dict[str, Any] | None:
    return update("monitors", monitor_id, fields)


def reconcile_monitors() -> int:
    """Close monitors whose runner died with the process.

    Runners live in memory, so a restart leaves rows claiming to be ``live``
    with nothing behind them. Mark them stopped on startup so the UI tells the
    truth; the moments, chat history and clips stay.
    """
    stale = list_rows(
        "monitors",
        where="status IN ('starting', 'live')",
        limit=1000,
    )
    for monitor in stale:
        update_monitor(monitor["id"], status="stopped", stopped_at=utcnow())
    return len(stale)


# -------------------------------------------------------------------- moments


def create_moment(data: dict[str, Any]) -> dict[str, Any]:
    payload = {"id": new_id("mom"), "created_at": utcnow(), **data}
    return insert("moments", payload)


def list_moments(monitor_id: str, limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM moments WHERE monitor_id = ? ORDER BY created_at DESC LIMIT ?",
            (monitor_id, limit),
        ).fetchall()
    return [_decode(row) for row in rows]


def attach_clip_to_moment(moment_id: str, clip_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE moments SET clip_id = ? WHERE id = ?", (clip_id, moment_id))


# ---------------------------------------------------------------- chat events


def add_chat_events(monitor_id: str, messages: list[dict[str, Any]]) -> None:
    if not messages:
        return
    with connect() as conn:
        conn.executemany(
            "INSERT INTO chat_events (monitor_id, t, author, text, kind) VALUES (?, ?, ?, ?, ?)",
            [
                (monitor_id, m["t"], m.get("author", ""), m["text"], m.get("kind", "text"))
                for m in messages
            ],
        )


def list_chat_events(monitor_id: str, since: float = 0.0, limit: int = 500) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT t, author, text, kind FROM chat_events "
            "WHERE monitor_id = ? AND t >= ? ORDER BY t ASC LIMIT ?",
            (monitor_id, since, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def prune_chat_events(monitor_id: str, keep_seconds: float = 1800) -> None:
    with connect() as conn:
        latest = conn.execute(
            "SELECT MAX(t) AS t FROM chat_events WHERE monitor_id = ?", (monitor_id,)
        ).fetchone()
        if latest and latest["t"]:
            conn.execute(
                "DELETE FROM chat_events WHERE monitor_id = ? AND t < ?",
                (monitor_id, latest["t"] - keep_seconds),
            )