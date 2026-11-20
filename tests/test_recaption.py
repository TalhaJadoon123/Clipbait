"""Recaptioning: the detector's own note must never reach a caption."""

from __future__ import annotations

import pytest

from clipbait import db
from clipbait.recaption import (
    looks_like_detector_note,
    plan_recaption,
    public_body,
    recaption,
)

# Clips made here live in the session-wide test database, where every money test
# reads the same queue. Reject them after each test so a finished integration
# test never crowds out a ranking assertion elsewhere.
_CREATED: list[str] = []


@pytest.fixture(autouse=True)
def _no_queue_leak():
    yield
    from clipbait.money import mark_rejected

    for clip_id in _CREATED:
        mark_rejected(clip_id, reason="recaption test cleanup")
    _CREATED.clear()


def _make_clip(
    *,
    quote: str = "",
    reason: str = "",
    description: str = "",
    status: str = "ready",
    with_render: bool = True,
) -> dict:
    project = db.create_project(
        {
            "title": "Recaption project",
            "source_kind": "local",
            "duration": 120.0,
            "options": {"region": "US", "platform": "tiktok", "language": "en"},
        }
    )
    clip = db.create_clip(
        {
            "project_id": project["id"],
            "title": "A moment",
            "hook": "A moment",
            "description": description,
            "reason": reason,
            "quote": quote,
            "hashtags": ["#shorts"],
            "start": 0.0,
            "end": 20.0,
            "score": 70,
            "status": status,
            "meta": {},
        }
    )
    _CREATED.append(clip["id"])
    if with_render:
        clip = db.update_clip(clip["id"], render_path=f"projects/{project['id']}/clips/{clip['id']}.mp4") or clip
    return clip


# --------------------------------------------------------------------- helpers


def test_public_body_prefers_the_spoken_line() -> None:
    assert public_body({"quote": "  the actual line ", "reason": "qa"}) == "the actual line"


def test_public_body_is_empty_without_a_quote_or_when_it_is_the_reason() -> None:
    assert public_body({"quote": "", "reason": "qa"}) == ""
    assert public_body({"quote": "same", "reason": "same"}) == ""


def test_looks_like_detector_note() -> None:
    assert looks_like_detector_note("2.5/s chat velocity (5.4σ over baseline)")
    assert looks_like_detector_note("loudness spike (+13 dB over the window)")
    assert looks_like_detector_note("anything at all", reason="anything at all")
    assert not looks_like_detector_note("the part nobody expected", reason="qa")
    assert not looks_like_detector_note("", reason="qa")


def test_plan_recaption_only_touches_detector_notes() -> None:
    legit = {"id": "c1", "description": "the part nobody expected", "reason": "qa", "quote": "q"}
    assert plan_recaption(legit) is None

    change = plan_recaption(
        {
            "id": "c2",
            "title": "T",
            "description": "5.0/s chat velocity (5.06σ over baseline)",
            "reason": "5.0/s chat velocity (5.06σ over baseline)",
            "quote": "the actual line",
        }
    )
    assert change is not None
    assert change["before"].startswith("5.0/s chat velocity")
    assert change["after"] == "the actual line"


# ----------------------------------------------------------------- integration


def test_recaption_dry_run_then_apply_is_idempotent() -> None:
    note = "2.5/s chat velocity (5.4σ over baseline)"
    clip = _make_clip(quote="the real spoken line", reason=note, description=note)

    dry = recaption(apply=False)
    assert any(change["clip_id"] == clip["id"] for change in dry)
    # a dry run never writes
    assert db.get_clip(clip["id"])["description"] == note

    applied = recaption(apply=True)
    assert any(change["clip_id"] == clip["id"] for change in applied)
    assert db.get_clip(clip["id"])["description"] == "the real spoken line"

    # second pass finds nothing left to do
    assert not any(change["clip_id"] == clip["id"] for change in recaption(apply=False))


def test_recaption_leaves_real_copy_alone() -> None:
    clip = _make_clip(quote="spoken", reason="qa", description="A caption a model wrote.")
    changes = recaption(apply=True)
    assert not any(change["clip_id"] == clip["id"] for change in changes)
    assert db.get_clip(clip["id"])["description"] == "A caption a model wrote."


def test_recaption_ignores_clips_that_are_not_ready() -> None:
    note = "2.5/s chat velocity (5.4σ over baseline)"
    clip = _make_clip(quote="spoken", reason=note, description=note, status="rendering")
    assert not any(change["clip_id"] == clip["id"] for change in recaption(apply=False))
