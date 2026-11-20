"""The money queue: what to post next, where, and with which copy.

A render is not an asset until it is packaged. This layer takes every ready
clip, prices it with :mod:`clipbait.virality.payout`, ranks the list by expected
revenue, and produces an upload-ready folder with the video, the caption block
and the metadata. Uploading happens on the operator's channel; the queue just
removes every reason to delay it.

State lives in ``clips.meta["uploaded"]`` rather than a new table: a clip is
either posted or not, and that flag must survive restarts and follow the clip
through exports.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .. import db, storage
from ..virality.payout import PayoutReport, estimate
from .check import CheckReport, check_clip, load_campaign

DEFAULT_MIN_SCORE = 0.0


@dataclass(frozen=True)
class QueueItem:
    clip_id: str
    project_id: str
    project_title: str
    title: str
    hook: str
    description: str
    hashtags: tuple[str, ...]
    quote: str
    region: str
    source_platform: str
    platform: str
    duration: float
    score: int
    expected_usd: float
    usd_low: float
    usd_high: float
    expected_views: int
    views_low: int
    views_high: int
    rpm: float
    confidence: str
    posted: bool
    posted_url: str = ""
    best_time: str = ""
    reasons: tuple[str, ...] = ()
    payout: PayoutReport | None = None
    created_at: str = ""
    # Set when the estimate was priced from a campaign brief rather than from
    # ad-market RPM medians, so the copy never mixes the two.
    campaign_rate: float | None = None

    @property
    def slug(self) -> str:
        return _slug(self.title) or self.clip_id

    def as_dict(self) -> dict:
        return {
            "clip_id": self.clip_id,
            "project_id": self.project_id,
            "project_title": self.project_title,
            "title": self.title,
            "hook": self.hook,
            "description": self.description,
            "hashtags": list(self.hashtags),
            "quote": self.quote,
            "region": self.region,
            "source_platform": self.source_platform,
            "platform": self.platform,
            "duration": round(self.duration, 1),
            "score": self.score,
            "expected_usd": round(self.expected_usd, 2),
            "usd_low": round(self.usd_low, 2),
            "usd_high": round(self.usd_high, 2),
            "expected_views": self.expected_views,
            "views_low": self.views_low,
            "views_high": self.views_high,
            "rpm": round(self.rpm, 3),
            "confidence": self.confidence,
            "posted": self.posted,
            "posted_url": self.posted_url,
            "best_time": self.best_time,
            "reasons": list(self.reasons),
            "payout": self.payout.as_dict() if self.payout else None,
            "campaign_rate": self.campaign_rate,
            "copy": {
                "title": self.title,
                "caption": self.caption,
                "hashtags": " ".join(self.hashtags),
            },
            "created_at": self.created_at,
        }

    @property
    def caption(self) -> str:
        body = (self.hook or self.title).strip()
        if self.description and self.description not in body:
            body = f"{body}\n\n{self.description}".strip()
        tags = " ".join(self.hashtags)
        return f"{body}\n\n{tags}".strip()


@dataclass(frozen=True)
class Queue:
    items: tuple[QueueItem, ...] = ()
    total_expected_usd: float = 0.0
    posted_count: int = 0
    rate_per_1k: float | None = None
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def count(self) -> int:
        return len(self.items)

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "count": self.count,
            "total_expected_usd": round(self.total_expected_usd, 2),
            "pricing": (
                "campaign rate"
                if self.rate_per_1k
                else "platform RPM medians (no campaign rate set)"
            ),
            "rate_per_1k": self.rate_per_1k,
            "posted_count": self.posted_count,
            "items": [item.as_dict() for item in self.items],
        }


def _slug(text: str) -> str:
    cleaned = storage.safe_filename((text or "").lower().replace(" ", "-"))
    return cleaned.strip("-")[:60]


def _clip_duration(clip: dict) -> float:
    try:
        return max(0.0, float(clip.get("end", 0)) - float(clip.get("start", 0)))
    except (TypeError, ValueError):
        return 0.0


def _clip_score(clip: dict) -> float:
    meta = clip.get("meta") or {}
    forecast = meta.get("forecast") or {}
    for value in (forecast.get("overall"), clip.get("score")):
        try:
            if value is not None:
                return max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError):
            continue
    return 0.0


def _clip_region(clip: dict, project: dict) -> str:
    meta = clip.get("meta") or {}
    options = project.get("options") or {}
    return str(meta.get("region") or options.get("region") or "US").upper()


def _clip_platform(clip: dict, project: dict) -> str:
    meta = clip.get("meta") or {}
    options = project.get("options") or {}
    return str(meta.get("platform") or options.get("platform") or "tiktok")


def _uploaded(clip: dict) -> dict:
    return dict((clip.get("meta") or {}).get("uploaded") or {})


def _rejected(clip: dict) -> dict:
    return dict((clip.get("meta") or {}).get("rejected") or {})


def _render_ok(clip: dict) -> bool:
    stored = clip.get("render_path")
    if not stored:
        return False
    try:
        return storage.resolve(stored).exists()
    except OSError:
        return False


def _to_item(
    clip: dict, project: dict, *, rate_per_1k: float | None = None
) -> QueueItem | None:
    if clip.get("status") != "ready" or not _render_ok(clip):
        return None
    region = _clip_region(clip, project)
    source_platform = _clip_platform(clip, project)
    duration = _clip_duration(clip)
    score = _clip_score(clip)
    report = estimate(score, region, duration=duration, rate_per_1k=rate_per_1k)
    best = report.best
    if best is None:
        return None

    meta = clip.get("meta") or {}
    forecast = meta.get("forecast") or {}
    up = _uploaded(clip)

    return QueueItem(
        clip_id=clip["id"],
        project_id=clip.get("project_id", ""),
        project_title=project.get("title", ""),
        title=clip.get("title", "Untitled clip"),
        hook=clip.get("hook", ""),
        description=clip.get("description", ""),
        hashtags=tuple(clip.get("hashtags") or ()),
        quote=clip.get("quote", ""),
        region=region,
        source_platform=source_platform,
        platform=best.platform,
        duration=duration,
        score=int(max(0, min(100, round(score)))),
        expected_usd=best.expected_usd,
        usd_low=best.usd_low,
        usd_high=best.usd_high,
        expected_views=best.expected_views,
        views_low=best.views_low,
        views_high=best.views_high,
        rpm=best.rpm,
        confidence=best.confidence,
        posted=bool(up),
        posted_url=str(up.get("url") or ""),
        best_time=str(forecast.get("best_time") or ""),
        reasons=tuple(list(forecast.get("reasons") or [])[:3] + list(report.rationale)[:1]),
        payout=report,
        created_at=str(clip.get("created_at") or ""),
        campaign_rate=float(rate_per_1k) if rate_per_1k else None,
    )


def build_queue(
    *,
    limit: int = 20,
    min_score: float = DEFAULT_MIN_SCORE,
    include_posted: bool = False,
    rate_per_1k: float | None = None,
    project_match: str | None = None,
) -> Queue:
    """Rank every ready clip by expected revenue.

    Duplicate renders of the same window (a re-render, a rerun of the pipeline)
    are collapsed to the best-scoring copy so the list is postable, not a log.
    """
    clips = db.list_rows("clips", where="status = ?", params=("ready",), limit=1000)
    projects: dict[str, dict] = {}
    monitors: dict[str, dict] = {}
    items: list[QueueItem] = []
    seen: set[tuple[str, int, int]] = set()
    needle = (project_match or "").strip().lower()

    def belongs_to_campaign(clip: dict, project: dict) -> bool:
        """Live projects are titled by the moment, so the monitor is the source."""
        if not needle:
            return True
        parts = [str(project.get("title") or ""), str(project.get("source_url") or "")]
        monitor_id = str((clip.get("meta") or {}).get("monitor_id") or "")
        if monitor_id:
            if monitor_id not in monitors:
                monitors[monitor_id] = db.get_monitor(monitor_id) or {}
            monitor = monitors[monitor_id]
            parts += [str(monitor.get("title") or ""), str(monitor.get("url") or "")]
        return needle in " ".join(parts).lower()

    for clip in clips:
        project_id = clip.get("project_id", "")
        project = projects.get(project_id)
        if project is None:
            project = db.get_project(project_id) or {"title": "", "options": {}}
            projects[project_id] = project

        item = _to_item(clip, project, rate_per_1k=rate_per_1k)
        if item is None:
            continue
        if _rejected(clip):
            # Curated out by the operator: a reaction with nothing in it, a
            # duplicate, footage the campaign does not accept.
            continue
        if not belongs_to_campaign(clip, project):
            continue
        if item.score < min_score:
            continue
        if item.posted and not include_posted:
            continue

        key = (project_id, int(round(float(clip.get("start", 0)))), int(round(float(clip.get("end", 0)))))
        if key in seen:
            continue
        seen.add(key)
        items.append(item)

    items.sort(key=lambda item: (item.expected_usd, item.score), reverse=True)
    trimmed = tuple(items[: max(0, int(limit))])
    return Queue(
        items=trimmed,
        total_expected_usd=round(sum(item.expected_usd for item in trimmed), 2),
        posted_count=sum(1 for item in items if item.posted),
        rate_per_1k=rate_per_1k,
    )


def queue_item(
    clip_id: str, *, include_posted: bool = True, rate_per_1k: float | None = None
) -> QueueItem | None:
    clip = db.get_clip(clip_id)
    if not clip:
        return None
    project = db.get_project(clip.get("project_id", "")) or {"title": "", "options": {}}
    if clip.get("status") != "ready" or not _render_ok(clip):
        return None
    if _uploaded(clip) and not include_posted:
        return None
    return _to_item(clip, project, rate_per_1k=rate_per_1k)


def mark_rejected(clip_id: str, *, reason: str = "") -> dict | None:
    """Take a clip out of the queue without deleting it. Idempotent."""
    clip = db.get_clip(clip_id)
    if not clip:
        return None
    meta = dict(clip.get("meta") or {})
    meta["rejected"] = {"at": datetime.now(UTC).isoformat(), "reason": reason.strip()}
    return db.update("clips", clip_id, {"meta": meta})


def apply_campaign_copy(
    caption: str,
    rules,
) -> tuple[str, dict]:
    """Add the copy the campaign requires, and say what was added.

    A brief that demands ``#gta6`` and ``@kaicenat`` is not asking for
    inspiration. Mechanically appending them turns a bundle that would be
    rejected for a missing tag into one that is ready, while the check still
    reports the final caption it will actually submit.
    """
    text = (caption or "").strip()
    added: dict[str, list[str] | str] = {"hashtags": [], "mention": ""}
    missing_tags = [
        tag for tag in getattr(rules, "required_hashtags", ()) if tag.lower() not in text.lower()
    ]
    mention = getattr(rules, "required_mention", "") or ""
    if mention and mention.lower() not in text.lower():
        text = f"{mention} {text}".strip()
        added["mention"] = mention
    if missing_tags:
        text = f"{text}\n\n{' '.join(missing_tags)}".strip()
        added["hashtags"] = missing_tags
    return text + ("\n" if text else ""), added


def mark_uploaded(clip_id: str, *, url: str | None = None, platform: str | None = None) -> dict | None:
    """Record that the operator posted this clip. Idempotent."""
    clip = db.get_clip(clip_id)
    if not clip:
        return None
    meta = dict(clip.get("meta") or {})
    meta["uploaded"] = {
        "at": datetime.now(UTC).isoformat(),
        "url": (url or "").strip(),
        "platform": (platform or "").strip(),
    }
    return db.update("clips", clip_id, {"meta": meta})


def export_bundle(
    clip_id: str,
    *,
    directory: Path | None = None,
    campaign: Path | str | None = None,
) -> dict:
    """Copy the render plus its copy into an upload-ready folder.

    The folder ships with the pre-post check result so a rejected aspect ratio
    or a missing hashtag is visible before the upload, not after.

    Returns a relative-to-data-dir path so the API never leaks absolute paths.
    """
    clip = db.get_clip(clip_id)
    if not clip:
        raise FileNotFoundError(f"clip {clip_id} not found")
    rules = load_campaign(campaign)
    item = queue_item(clip_id, rate_per_1k=rules.rate_per_1k)
    if item is None:
        raise FileNotFoundError(f"clip {clip_id} is not rendered")
    source = storage.resolve(clip["render_path"])
    if not source.exists():
        raise FileNotFoundError(f"render for {clip_id} is missing")

    target = directory or storage.exports_dir() / item.slug
    target.mkdir(parents=True, exist_ok=True)

    video = target / "clip.mp4"
    if video.resolve() != source.resolve():
        shutil.copy2(source, video)

    caption, added = apply_campaign_copy(item.caption, rules)
    (target / "caption.txt").write_text(caption, encoding="utf-8")
    (target / "metadata.json").write_text(
        json.dumps(
            {
                "clip_id": item.clip_id,
                "project_id": item.project_id,
                "title": item.title,
                "hook": item.hook,
                "description": item.description,
                "hashtags": list(
                    dict.fromkeys([*item.hashtags, *(added.get("hashtags") or [])])
                ),
                "caption": caption,
                "recommended_platform": item.platform,
                "source_platform": item.source_platform,
                "region": item.region,
                "duration": round(item.duration, 1),
                "score": item.score,
                "expected_usd": item.expected_usd,
                "usd_range": [item.usd_low, item.usd_high],
                "campaign_copy_added": added,
                "expected_views": item.expected_views,
                "views_range": [item.views_low, item.views_high],
                "rpm": item.rpm,
                "pricing": "campaign" if item.campaign_rate else "platform-rpm",
                "best_time": item.best_time,
                "uploaded": item.posted,
                "posted_url": item.posted_url,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    report: CheckReport = check_clip(
        video,
        caption=caption,
        hook=item.hook,
        rules=rules,
    )
    (target / "check.json").write_text(
        json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8"
    )

    readme = (
        f"# {item.title}\n\n"
        f"Post this to **{item.platform}** ({item.region}).\n"
        f"Expected: ${item.usd_low:.2f}-${item.usd_high:.2f} "
        f"(~{item.expected_views:,} views).\n"
        + (f"Best window: {item.best_time}\n" if item.best_time else "")
        + "\n## Caption\n\n```\n"
        + caption
        + "\n```\n\n## Files\n\n- `clip.mp4` - the vertical render, upload as-is\n"
        "- `caption.txt` - paste into the platform caption box\n"
        "- `metadata.json` - machine-readable copy and estimate\n"
        "- `check.json` - pre-post check result (aspect, fps, audio, duration, words)\n"
        "\n## Pre-post check\n\n```\n"
        + "\n".join(report.lines())
        + "\n```\n"
    )
    (target / "README.md").write_text(readme, encoding="utf-8")

    return {
        "clip_id": item.clip_id,
        "dir": storage.relative(target),
        "slug": item.slug,
        "platform": item.platform,
        "files": ["clip.mp4", "caption.txt", "metadata.json", "check.json", "README.md"],
        "expected_usd": item.expected_usd,
        "usd_range": [item.usd_low, item.usd_high],
        "check": {
            "passed": report.passed,
            "failures": [row.name for row in report.failures],
        },
    }