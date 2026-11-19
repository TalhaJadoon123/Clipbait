"""Artifact storage.

Every project owns a directory. Nothing is written outside it except the
SQLite database, which makes cleanup and backup trivial.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .config import get_settings


def project_dir(project_id: str) -> Path:
    path = get_settings().projects_dir / project_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def live_dir(monitor_id: str) -> Path:
    path = get_settings().live_dir / monitor_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return cleaned[:120] or "file"


def save_upload(project_id: str, filename: str, stream) -> Path:
    """Persist an uploaded file without trusting its name."""
    directory = project_dir(project_id) / "source"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / safe_filename(filename)
    with target.open("wb") as handle:
        shutil.copyfileobj(stream, handle, length=1024 * 1024)
    return target


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    return path


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def transcript_path(project_id: str) -> Path:
    return project_dir(project_id) / "transcript.json"


def source_path(project_id: str) -> Path:
    return project_dir(project_id) / "source" / "video.mp4"


def audio_path(project_id: str) -> Path:
    return project_dir(project_id) / "source" / "audio.wav"


def clip_dir(project_id: str, clip_id: str) -> Path:
    path = project_dir(project_id) / "clips" / clip_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def relative(path: Path) -> str:
    """Store paths relative to the data dir so a backup can move."""
    try:
        return str(path.resolve().relative_to(get_settings().data_dir.resolve()))
    except ValueError:
        return str(path)


def resolve(stored: str) -> Path:
    candidate = Path(stored)
    if candidate.is_absolute():
        return candidate
    return get_settings().data_dir / candidate


def remove_project(project_id: str) -> None:
    shutil.rmtree(get_settings().projects_dir / project_id, ignore_errors=True)
    shutil.rmtree(get_settings().live_dir / project_id, ignore_errors=True)