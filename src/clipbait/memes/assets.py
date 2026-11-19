"""Normalise a generated meme into a clip-ready insert.

Whatever a provider hands back (arbitrary size, frame rate, pixel format, no
audio), the renderer needs one exact shape: ``out_w x out_h``, the clip's frame
rate, ``yuv420p``, silent, and no longer than the window it fills. Doing that
here keeps the render filter graph simple and the insert deterministic.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import media
from ..config import get_settings

log = logging.getLogger("clipbait.memes")


def prepare_insert(
    source: Path,
    out_path: Path,
    *,
    width: int,
    height: int,
    duration: float,
    fps: int = 30,
) -> Path:
    """Return ``out_path``: ``source`` cropped to fill ``width x height``, silent."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Loop then trim so a 1s asset still fills a 2s window; cover-crop so it
    # fills the frame without letterboxing, matching the reframe layouts.
    chain = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={fps},setsar=1"
    )
    cmd = [
        get_settings().ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-stream_loop",
        "-1",
        "-i",
        str(source),
        "-t",
        f"{duration:.3f}",
        "-an",
        "-vf",
        chain,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    media.run(cmd)
    return out_path


__all__ = ["prepare_insert"]
