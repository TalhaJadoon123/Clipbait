"""Shrink a clip for the last mile, so a small upload body is not the wait.

A vertical render is tens of megabytes. On a home uplink the body, not the
posting API, is the slow part of a publish: a 15 MB clip can take ten minutes
to hand over. A clip that is already small is sent untouched; a big one is
transcoded once to a lighter 720p copy beside it and reused, so a retry never
transcodes twice. The original render is left exactly as it was.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import media

log = logging.getLogger("clipbait.publish.compact")

# Above this, the upload body dominates the post; below it, send as-is.
MAX_UPLOAD_BYTES = 6 * 1024 * 1024
TARGET_WIDTH = 720
TARGET_HEIGHT = 1280
SUFFIX = ".upload.mp4"


def compact_for_upload(video: Path, *, max_bytes: int = MAX_UPLOAD_BYTES) -> Path:
    """Return an upload-sized copy of ``video``, or ``video`` when already small.

    The compact copy lives next to the render as ``<name>.upload.mp4`` and is
    reused while it is at least as new as the render, so repeated plans and a
    retried send cost one transcode, not one per attempt.
    """
    video = Path(video)
    try:
        if video.stat().st_size <= max_bytes:
            return video
    except OSError:  # pragma: no cover - a missing render is handled upstream
        return video

    target = video.with_name(video.stem + SUFFIX)
    try:
        if target.exists() and target.stat().st_mtime >= video.stat().st_mtime:
            return target
    except OSError:  # pragma: no cover
        pass

    log.info("compacting %s for upload", video.name)
    media.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video),
            "-vf",
            (
                f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2"
            ),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(target),
        ],
        timeout=1800,
    )
    if not target.exists() or target.stat().st_size < 10_000:
        log.warning("compaction produced no usable file; sending the render as-is")
        return video
    return target
