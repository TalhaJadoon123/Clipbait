"""Retention keeps a 24/7 roster from filling the disk without eating live work.

The two mistakes that matter: deleting a monitor that is still recording, and
deleting a source whose clips have not been rendered yet (a long transcription
leaves the file untouched for hours). Both are guarded here.
"""

from __future__ import annotations

import os
import time

from clipbait import db, retention
from clipbait.config import get_settings

HOUR = 3600
OLD = 48 * HOUR


def _age(path, seconds: float) -> None:
    when = time.time() - seconds
    os.utime(path, (when, when))


def _make_monitor(monitor_id: str, *, age_seconds: float):
    directory = get_settings().live_dir / monitor_id
    directory.mkdir(parents=True, exist_ok=True)
    segment = directory / "seg_000001.ts"
    segment.write_bytes(b"x" * 2048)
    _age(segment, age_seconds)
    _age(directory, age_seconds)
    return directory


def _make_project(status: str, *, age_seconds: float):
    project = db.create_project(
        {"title": "t", "source_kind": "url", "options": {}, "status": status}
    )
    base = get_settings().projects_dir / project["id"]
    source = base / "source"
    source.mkdir(parents=True, exist_ok=True)
    video = source / "video.mp4"
    video.write_bytes(b"y" * 4096)
    _age(video, age_seconds)
    _age(source, age_seconds)
    _age(base, age_seconds)
    return project["id"], source


def test_plan_flags_stale_monitors_and_terminal_sources():
    stale = _make_monitor("mon_stale00001", age_seconds=OLD)
    fresh = _make_monitor("mon_fresh00001", age_seconds=60)
    _done_id, done_source = _make_project("completed", age_seconds=OLD)
    _busy_id, busy_source = _make_project("processing", age_seconds=OLD)

    flagged = {(target.kind, target.path) for target in retention.plan()}

    assert ("monitor", stale) in flagged
    assert ("monitor", fresh) not in flagged  # still being appended to
    assert ("source", done_source) in flagged
    assert ("source", busy_source) not in flagged  # may still render a clip


def test_plan_respects_the_settle_window():
    recent = _make_monitor("mon_recent0001", age_seconds=2 * HOUR)

    assert all(t.path != recent for t in retention.plan(settle_seconds=6 * HOUR))
    assert any(t.path == recent for t in retention.plan(settle_seconds=1 * HOUR))


def test_apply_deletes_targets_and_reports_reclaimed_bytes():
    stale = _make_monitor("mon_apply00001", age_seconds=OLD)
    targets = [t for t in retention.plan() if t.path == stale]
    assert targets, "stale monitor should be a prune target"

    freed = retention.apply(targets)

    assert not stale.exists()
    assert freed >= 2048


def test_apply_is_quiet_when_a_target_already_vanished(tmp_path):
    missing = retention.PruneTarget(tmp_path / "gone", "monitor", 123, "idle 99h")

    assert retention.apply([missing]) == 123
