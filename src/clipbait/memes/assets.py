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
    """Return ``out_path``: ``source`` fitted whole on a blurred fill, silent.

    A cutaway covers the picture, but most web memes are small and landscape
    (a 480x360 GIF is typical). Cover-cropping one into a 1080x1920 frame would
    both blow it up ~5x *and* slice off most of its width, so the joke would be
    unreadable. Instead the whole meme is scaled to *fit* (never cropped) and
    centred over a blurred, slightly darkened copy of itself that fills the
    frame. The meme stays whole and legible, the fill hides the low source
    resolution, and a phone-shaped meme needs no fill at all.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Loop then trim so a 1s asset still fills a 2s window. ``boxblur`` is cheap
    # and its radius auto-clamps to the frame, unlike a large gaussian sigma.
    fill = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        f"boxblur=luma_radius=24:luma_power=2:chroma_radius=12:chroma_power=2,"
        f"eq=brightness=-0.08:saturation=0.9[bg]"
    )
    fit = f"scale={width}:{height}:force_original_aspect_ratio=decrease[fg]"
    chain = (
        f"[0:v]{fill};[0:v]{fit};"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={fps},setsar=1[v]"
    )
    # A still image (jpg/png/webp) is one frame, so it needs ``-loop 1`` to fill
    # the window; an animated source loops with ``-stream_loop -1``. Both are
    # then trimmed to the window, so a static meme is as usable as a GIF.
    if _is_still(source):
        pre = ["-loop", "1", "-framerate", str(fps), "-i", str(source)]
    else:
        pre = ["-stream_loop", "-1", "-i", str(source)]
    cmd = [
        get_settings().ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *pre,
        "-t",
        f"{duration:.3f}",
        "-an",
        "-filter_complex",
        chain,
        "-map",
        "[v]",
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


def _is_still(source: Path) -> bool:
    """True for a single-frame image (as opposed to an animated GIF or video).

    A real clip has a duration; a still image reports ~0 (or nothing), which is
    what decides between ``-loop 1`` and ``-stream_loop -1``.
    """
    try:
        info = media.probe(source)
    except media.MediaError:
        return False
    return info.duration <= 0.1


__all__ = ["prepare_insert"]
