"""Unattended batch runner: idempotent resume behaviour.

``process_project`` rebuilds the whole clip list, so a crashed or disk-full run
leaves a mix of ready/failed/draft rows. ``resume_vod`` must finish only the
unfinished ones and never throw away renders that already succeeded, and it must
stop before the disk fills.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from clipbait import db

ROOT = Path(__file__).resolve().parents[1]


def load_speed_batch():
    spec = importlib.util.spec_from_file_location(
        "_speed_batch", ROOT / "scripts" / "speed_batch.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _project(url: str, clips: int = 3) -> dict:
    sb = load_speed_batch()
    return db.create_project(
        {
            "title": "Resume",
            "source_kind": "youtube",
            "source_url": url,
            "source_path": None,
            "options": {**sb.SPEED_OPTIONS, "clips": clips},
        }
    )


def test_resume_renders_only_the_unfinished_clips(monkeypatch):
    sb = load_speed_batch()
    url = "https://example.com/watch?v=resume"
    project = _project(url)
    done = db.create_clip(
        {
            "project_id": project["id"],
            "title": "done",
            "hook": "d",
            "start": 0.0,
            "end": 5.0,
            "status": "ready",
            "render_path": "already.mp4",
        }
    )
    draft = db.create_clip(
        {"project_id": project["id"], "title": "draft", "hook": "d", "start": 5.0, "end": 10.0}
    )
    failed = db.create_clip(
        {
            "project_id": project["id"],
            "title": "failed",
            "hook": "f",
            "start": 10.0,
            "end": 15.0,
            "status": "failed",
        }
    )

    rendered: list[str] = []

    def fake_render(clip_id, **_kwargs):
        rendered.append(clip_id)
        db.update("clips", clip_id, {"status": "ready"})
        return {}

    monkeypatch.setattr("clipbait.pipeline.render_clip_record", fake_render)

    result = sb.resume_vod(url, 3)

    # The finished clip is left alone; both unfinished ones are rendered.
    assert done["id"] not in rendered
    assert set(rendered) == {draft["id"], failed["id"]}
    assert result["clips"] == 2
    assert db.get_project(project["id"])["status"] == "completed"


def test_resume_stops_before_the_disk_fills(monkeypatch):
    sb = load_speed_batch()
    url = "https://example.com/watch?v=lowdisk"
    project = _project(url, clips=1)
    db.create_clip(
        {"project_id": project["id"], "title": "draft", "hook": "d", "start": 0.0, "end": 5.0}
    )

    rendered: list[str] = []
    monkeypatch.setattr(sb, "_free_bytes", lambda path: 0)
    monkeypatch.setattr(
        "clipbait.pipeline.render_clip_record",
        lambda clip_id, **k: rendered.append(clip_id) or {},
    )

    result = sb.resume_vod(url, 1)

    assert rendered == []
    assert result["clips"] == 0
