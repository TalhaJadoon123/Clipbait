"""Schedule clips and hand them to a publisher.

The money queue already knows *what* is worth posting and with which copy. This
layer adds the two things it lacked: *when* to post, and the act of handing the
clip over.

It is dry by default. A plan is built for every clip -- copy, destination,
window -- and printed without a single byte leaving the machine. Only ``live``
turns a plan into an upload, and only a successful upload marks the clip as
posted. Scheduling is idempotent: the same clip always produces the same
``Idempotency-Key`` and the same window, so re-running the command after a
network error returns the existing job instead of publishing twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import db, media, storage
from ..config import Settings, get_settings
from ..money import QueueItem, build_queue, load_campaign, mark_uploaded, queue_item
from ..money.queue import apply_campaign_copy
from ..virality.regions import get_region, platform_limits
from .compact import compact_for_upload
from .provider import (
    ProviderError,
    PublishResult,
    UploadPostProvider,
    upload_platform,
)
from .windows import (
    next_posting_time,
    parse_windows,
    region_timezone,
    spread_minutes,
    windows_from_best_times,
)

log = logging.getLogger("clipbait.publish")


@dataclass(frozen=True)
class PublishOptions:
    """Everything the operator can decide about a publishing run."""

    live: bool = False
    at: str | None = None  # explicit ISO-8601 start of the post
    post_now: bool = False
    timezone: str | None = None  # IANA override for the region's local time
    platforms: tuple[str, ...] = ()  # Clipbait names; overrides the clip's pick
    campaign: Path | str | None = None
    user: str | None = None
    use_best_times: bool = True
    offset_minutes: int | None = None
    republish: bool = False
    language: str = ""


@dataclass(frozen=True)
class ClipPlan:
    """A post that is ready to send: the file, the copy, the moment."""

    clip_id: str
    title: str
    platform: str  # Upload-Post network
    platforms: tuple[str, ...]
    clipbait_platform: str
    region: str
    scheduled_date: str
    timezone: str
    source: str
    caption: str
    video: Path
    duration: float
    expected_usd: float
    already_posted: bool
    posted_url: str
    fields: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "clip_id": self.clip_id,
            "title": self.title,
            "platform": self.platform,
            "platforms": list(self.platforms),
            "region": self.region,
            "scheduled_date": self.scheduled_date,
            "timezone": self.timezone,
            "source": self.source,
            "caption": self.caption,
            "video": storage.relative(self.video),
            "duration": round(self.duration, 1),
            "expected_usd": self.expected_usd,
            "already_posted": self.already_posted,
            "posted_url": self.posted_url,
            "fields": self.fields,
        }


def upload_post_provider(
    settings: Settings | None = None, *, user: str | None = None
) -> UploadPostProvider:
    """Build the provider from the environment. No key means a dry-only run."""
    settings = settings or get_settings()
    return UploadPostProvider(
        settings.upload_post_api_key,
        base_url=settings.upload_post_base_url,
        user=user or settings.upload_post_user or "",
        timeout=settings.upload_post_timeout,
    )


# ----------------------------------------------------------------------- copy


def _trim(text: str, limit: int) -> str:
    """Cut to ``limit`` characters on a word boundary, never mid-word."""
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-")


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def platform_fields(
    item: QueueItem,
    caption: str,
    *,
    clipbait_platform: str,
    upload_platform_name: str,
    language: str = "",
) -> dict:
    """Shape one caption into the fields the destination network actually reads.

    Upload-Post reads ``title`` as the caption on the short-form networks and
    ignores ``description`` there, while YouTube/LinkedIn/Facebook read
    ``description`` and cap ``title`` hard. Sending both, plus the platform
    override fields, means one caption block is correct everywhere instead of
    truncated on whichever network is unlucky.
    """
    limits = platform_limits(clipbait_platform)
    text = _trim(caption, limits.get("caption", 2200))
    short = _trim(_first_line(item.hook or item.title) or text, limits.get("title", 100))
    fields: dict[str, object] = {"title": text, "description": text}

    if upload_platform_name == "youtube":
        fields["title"] = short
        fields["youtube_title"] = short
        fields["youtube_description"] = text
        tags = [tag.lstrip("#").strip() for tag in item.hashtags if tag and tag.strip()]
        if tags:
            fields["tags"] = tags[:15]
        if language and language.lower() not in {"", "auto"}:
            fields["defaultLanguage"] = language
            fields["defaultAudioLanguage"] = language
    elif upload_platform_name == "linkedin":
        fields["title"] = short
        fields["linkedin_title"] = short
        fields["linkedin_description"] = text
    elif upload_platform_name == "x":
        # X bills URLs far higher and threads a caption that is too long; keep
        # the tweet a single post inside the limit.
        tweet = _trim(caption, 280)
        fields["title"] = tweet
        fields["x_title"] = tweet
    return fields


# -------------------------------------------------------------------- schedule


def _parse_at(value: str, timezone: str) -> datetime:
    text = value.strip().replace(" ", "T")
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(timezone))
    return moment


def resolve_schedule(
    item: QueueItem,
    clip_id: str,
    *,
    options: PublishOptions,
    provider: UploadPostProvider | None,
) -> tuple[str, str, str]:
    """Pick the post time. Returns ``(scheduled_date, timezone, source)``.

    ``source`` names *why* that moment was chosen -- ``manual``, ``now``,
    ``account-best-times`` (measured) or ``region-window`` (curated) -- so the
    operator can tell a data-backed slot from a playbook guess.
    """
    timezone = options.timezone or region_timezone(item.region)

    if options.at:
        return _parse_at(options.at, timezone).isoformat(), timezone, "manual"
    if options.post_now:
        return "", timezone, "now"

    windows = ()
    source = "region-window"
    if options.use_best_times and provider is not None and provider.ready:
        try:
            payload = provider.best_times(
                item.platform,
                profile_username=options.user,
                timezone=timezone,
                top=3,
            )
        except ProviderError as exc:
            log.info("best-times unavailable (%s); using the region playbook", exc)
        else:
            windows = windows_from_best_times(payload.get("best_times") or ())
            if windows:
                source = "account-best-times"

    if not windows:
        windows = parse_windows(get_region(item.region).posting_windows)

    offset = options.offset_minutes
    if offset is None:
        offset = spread_minutes(clip_id)
    now = datetime.now(ZoneInfo(timezone))
    moment = next_posting_time(windows, now=now, offset_minutes=offset)
    return moment.isoformat(), timezone, source


# ----------------------------------------------------------------------- plan


def plan_clip(
    clip_id: str,
    *,
    options: PublishOptions | None = None,
    provider: UploadPostProvider | None = None,
) -> ClipPlan:
    """Build the post for one clip without sending anything."""
    options = options or PublishOptions()
    clip = db.get_clip(clip_id)
    if not clip:
        raise FileNotFoundError(f"clip {clip_id} not found")
    if not clip.get("render_path"):
        raise FileNotFoundError(f"clip {clip_id} has no render yet")

    rules = load_campaign(options.campaign)
    item = queue_item(clip_id, include_posted=True, rate_per_1k=rules.rate_per_1k)
    if item is None:
        raise FileNotFoundError(f"clip {clip_id} is not rendered")

    video = storage.resolve(clip["render_path"])
    if not video.exists():
        raise FileNotFoundError(f"render for {clip_id} is missing")

    caption, _added = apply_campaign_copy(item.caption, rules)

    clipbait_platform = (item.platform or "tiktok").lower()
    requested = options.platforms or (clipbait_platform,)
    platforms = tuple(dict.fromkeys(upload_platform(name) for name in requested))

    language = options.language
    if not language:
        project = db.get_project(item.project_id) or {}
        language = str((project.get("options") or {}).get("language") or "")

    scheduled_date, timezone, source = resolve_schedule(
        item, clip_id, options=options, provider=provider
    )
    fields = platform_fields(
        item,
        caption,
        clipbait_platform=clipbait_platform,
        upload_platform_name=platforms[0],
        language=language,
    )

    return ClipPlan(
        clip_id=clip_id,
        title=item.title,
        platform=platforms[0],
        platforms=platforms,
        clipbait_platform=clipbait_platform,
        region=item.region,
        scheduled_date=scheduled_date,
        timezone=timezone,
        source=source,
        caption=caption,
        video=video,
        duration=item.duration,
        expected_usd=item.expected_usd,
        already_posted=item.posted,
        posted_url=item.posted_url,
        fields=fields,
    )


# -------------------------------------------------------------------- publish


def publish_clip(
    clip_id: str,
    *,
    options: PublishOptions | None = None,
    provider: UploadPostProvider | None = None,
) -> PublishResult:
    """Plan one clip, then either report the plan or send it."""
    options = options or PublishOptions()
    provider = provider or upload_post_provider(user=options.user)

    try:
        plan = plan_clip(clip_id, options=options, provider=provider)
    except (FileNotFoundError, ValueError) as exc:
        return PublishResult(
            ok=False, dry_run=not options.live, clip_id=clip_id, error=str(exc)
        )

    if plan.already_posted and not options.republish:
        return PublishResult(
            ok=True,
            dry_run=not options.live,
            clip_id=clip_id,
            platform=plan.platform,
            title=plan.title,
            scheduled_date=plan.scheduled_date,
            timezone=plan.timezone,
            source="already-posted",
            url=plan.posted_url,
            metadata={"skipped": True},
        )

    if not options.live:
        return PublishResult(
            ok=True,
            dry_run=True,
            clip_id=clip_id,
            platform=plan.platform,
            title=plan.title,
            scheduled_date=plan.scheduled_date,
            timezone=plan.timezone,
            source=plan.source,
            metadata={"plan": plan.as_dict()},
        )

    if not provider.ready:
        return PublishResult(
            ok=False,
            dry_run=False,
            clip_id=clip_id,
            platform=plan.platform,
            title=plan.title,
            scheduled_date=plan.scheduled_date,
            timezone=plan.timezone,
            source=plan.source,
            error=(
                "no Upload-Post API key: set UPLOAD_POST_API_KEY "
                "(and UPLOAD_POST_USER) to publish for real"
            ),
        )
    if not (options.user or provider.user):
        return PublishResult(
            ok=False,
            dry_run=False,
            clip_id=clip_id,
            platform=plan.platform,
            title=plan.title,
            scheduled_date=plan.scheduled_date,
            timezone=plan.timezone,
            source=plan.source,
            error="no Upload-Post profile: pass --user or set UPLOAD_POST_USER",
        )

    try:
        upload_video = compact_for_upload(plan.video)
    except media.MediaError as exc:  # a failed shrink must not block the post
        log.warning("compaction failed (%s); sending the render as-is", exc)
        upload_video = plan.video

    try:
        payload = provider.upload(
            video=upload_video,
            fields=plan.fields,
            platforms=list(plan.platforms),
            scheduled_date=plan.scheduled_date or None,
            timezone=plan.timezone,
            idempotency_key=f"clipbait-{clip_id}",
        )
    except ProviderError as exc:
        return PublishResult(
            ok=False,
            dry_run=False,
            clip_id=clip_id,
            platform=plan.platform,
            title=plan.title,
            scheduled_date=plan.scheduled_date,
            timezone=plan.timezone,
            source=plan.source,
            error=str(exc),
            metadata={
                "status": exc.status,
                "code": exc.code,
                "reconnect_required": exc.reconnect_required,
            },
        )

    job_id = str(payload.get("job_id") or "")
    post_id = str(payload.get("post_id") or payload.get("id") or "")
    url = str(payload.get("url") or payload.get("post_url") or "")

    # A single send may fan out to several networks, so Upload-Post returns the
    # outcome per platform under ``results``. Lift the first usable link so a
    # posted clip records where it actually landed instead of an empty URL.
    results = payload.get("results")
    if isinstance(results, dict):
        for key in (plan.platform, *plan.platforms, *results.keys()):
            entry = results.get(key)
            if not isinstance(entry, dict):
                continue
            if not post_id:
                post_id = str(entry.get("post_id") or entry.get("id") or "")
            if not url:
                url = str(entry.get("url") or entry.get("post_url") or "")
            if url:
                break

    mark_uploaded(
        clip_id,
        url=url,
        platform=plan.platform,
        scheduled_for=plan.scheduled_date,
        job_id=job_id,
        dry_run=False,
    )
    return PublishResult(
        ok=bool(payload.get("success", True)),
        dry_run=False,
        clip_id=clip_id,
        platform=plan.platform,
        title=plan.title,
        scheduled_date=plan.scheduled_date,
        timezone=plan.timezone,
        source=plan.source,
        job_id=job_id,
        post_id=post_id,
        url=url,
        error="" if payload.get("success", True) else str(payload.get("message") or "refused"),
        metadata={"response": payload},
    )


def publish_queue(
    *,
    options: PublishOptions | None = None,
    provider: UploadPostProvider | None = None,
    limit: int = 10,
    min_score: float = 0.0,
    project_match: str | None = None,
    monitor_id: str | None = None,
) -> list[PublishResult]:
    """Plan or send the top of the money queue, best-expected first."""
    options = options or PublishOptions()
    rules = load_campaign(options.campaign)
    queue = build_queue(
        limit=limit,
        min_score=min_score,
        include_posted=options.republish,
        rate_per_1k=rules.rate_per_1k,
        project_match=project_match,
        monitor_id=monitor_id,
    )
    return [
        publish_clip(item.clip_id, options=options, provider=provider)
        for item in queue.items
    ]
