"""Re-derive a clip's public caption from what was actually said.

The detector's ``reason`` explains a pick to the operator and is stored on the
clip; it must never be a caption. Clips made before that rule went in carry the
note directly (``description = reason`` -- "5.0/s chat velocity (5.4σ over
baseline)"); this rewrites those from the clip's spoken quote and leaves the
reason untouched, so explainability survives the cleanup.
"""

from __future__ import annotations

import logging
import re

from . import db

log = logging.getLogger("clipbait.recaption")

# Wording the detector writes for itself, never for a viewer.
_DETECTOR_NOTE = re.compile(
    r"(chat velocity|σ over baseline|loudness spike|motion burst|both spiked|"
    r"on-stream signal|offline ranker|signal:)",
    re.IGNORECASE,
)
_BODY_LIMIT = 280


def public_body(clip: dict) -> str:
    """The caption body a clip should carry: its spoken line, or nothing.

    Empty when the clip has no quote, or when the only text available is the
    detector's own note.
    """
    quote = str(clip.get("quote") or "").strip()
    reason = str(clip.get("reason") or "").strip()
    if not quote or quote == reason:
        return ""
    return quote[:_BODY_LIMIT]


def looks_like_detector_note(text: str, reason: str = "") -> bool:
    """True for text the detector wrote about a clip, not text for a viewer."""
    value = (text or "").strip()
    if not value:
        return False
    if value == (reason or "").strip():
        return True
    return bool(_DETECTOR_NOTE.search(value))


def plan_recaption(clip: dict) -> dict | None:
    """The caption a clip would get, or ``None`` when it is already fine.

    Only clips whose body is a detector note are touched, so a caption an LLM
    wrote for a real post is left exactly as it is.
    """
    current = str(clip.get("description") or "").strip()
    if not looks_like_detector_note(current, str(clip.get("reason") or "")):
        return None
    after = public_body(clip)
    if after == current:
        return None
    return {
        "clip_id": clip["id"],
        "title": clip.get("title") or "",
        "before": current,
        "after": after,
    }


def ready_clips() -> list[dict]:
    """Rendered clips on disk, newest first -- the ones a post would pick up."""
    clips = [
        clip
        for clip in db.list_rows("clips", limit=5000)
        if clip.get("status") == "ready" and clip.get("render_path")
    ]
    clips.sort(key=lambda clip: str(clip.get("created_at") or ""), reverse=True)
    return clips


def recaption(*, apply: bool = False, limit: int = 0) -> list[dict]:
    """Plan (and, with ``apply``, write) caption fixes for ready clips."""
    changes: list[dict] = []
    for clip in ready_clips():
        change = plan_recaption(clip)
        if not change:
            continue
        changes.append(change)
        if limit and len(changes) >= limit:
            break
    if apply and changes:
        for change in changes:
            db.update_clip(change["clip_id"], description=change["after"])
        log.info("recaptioned %d clip(s)", len(changes))
    return changes
