"""Publish clips to the operator's own accounts, at the right moment.

The last mile of the money loop: the queue decides what is worth posting, and
this package decides when and how it leaves the machine.

    plan  -> what will be posted, where, at which window (no network)
    send  -> one multipart request per clip, idempotent, marked once posted

Two backends can carry the last mile, chosen by ``CLIPBAIT_PUBLISH_PROVIDER``:
Upload-Post (metered; one request reaches TikTok, Instagram, YouTube, LinkedIn,
X and more, and reports measured best hours) or Postiz (self-hosted, unmetered).
Everything is dry by default; a run only sends when ``live`` is set.
"""

from .compact import MAX_UPLOAD_BYTES, compact_for_upload
from .postiz import PostizProvider
from .provider import (
    PLATFORM_MAP,
    ProviderError,
    PublishResult,
    UploadPostProvider,
    upload_platform,
)
from .service import (
    ClipPlan,
    Provider,
    PublishOptions,
    plan_clip,
    platform_fields,
    postiz_provider,
    publish_clip,
    publish_provider,
    publish_queue,
    resolve_schedule,
    upload_post_provider,
)
from .windows import (
    REGION_TIMEZONES,
    next_posting_time,
    parse_window,
    parse_windows,
    region_timezone,
    spread_minutes,
    windows_from_best_times,
)

__all__ = [
    "MAX_UPLOAD_BYTES",
    "PLATFORM_MAP",
    "REGION_TIMEZONES",
    "ClipPlan",
    "PostizProvider",
    "Provider",
    "ProviderError",
    "PublishOptions",
    "PublishResult",
    "UploadPostProvider",
    "compact_for_upload",
    "next_posting_time",
    "parse_window",
    "parse_windows",
    "plan_clip",
    "platform_fields",
    "postiz_provider",
    "publish_clip",
    "publish_provider",
    "publish_queue",
    "region_timezone",
    "resolve_schedule",
    "spread_minutes",
    "upload_platform",
    "upload_post_provider",
    "windows_from_best_times",
]
