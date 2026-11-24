"""Ad breaks and frozen screens.

Ported from PeakCut's ``studio/quality.py`` (MIT, (c) 2026 PeakCut
contributors) into Clipbait's idiom. Twitch and YouTube stitch commercials into
a stream, and a "be right back" slate can sit right where the good moment is. A
clip taken from there is worthless, so those seconds are found and left out.

Metric: mean absolute luma difference between consecutive frames
(``tblend=difference`` + ``signalstats``). Measured on real captures a slate
sits near zero and gameplay far above it, so a flat threshold separates them
with a wide margin — no OCR, no reference image, no model. The frames are read
once for the whole video, and clips are then checked against that map.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..config import get_settings
from ..media import MediaError, run
from ..schema import Highlight

log = logging.getLogger("clipbait.analyze.static")

# A clip that spends more than this share of its length on a slate or a frozen
# screen is not worth shipping: most of it is dead air.
MAX_STATIC_SHARE = 0.5

_PTS = re.compile(r"pts_time:([0-9.]+)")
_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")


def motion_map(
    path: str | Path,
    bucket: float = 2.0,
    *,
    timeout: float = 3600.0,
) -> list[tuple[float, float]]:
    """(time, motion) for every bucket of the video, or ``[]`` if it cannot be read.

    Motion is the mean luma difference between consecutive frames: near zero for
    a frozen screen, high for anything actually moving.
    """
    settings = get_settings()
    try:
        proc = run(
            [
                settings.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-vf",
                "tblend=all_mode=difference,signalstats,metadata=print:file=-",
                "-an",
                "-f",
                "null",
                "-",
            ],
            timeout=int(timeout),
            check=False,
        )
    except MediaError as exc:  # a missing ffmpeg must not stop a job
        log.warning("motion map skipped for %s: %s", path, exc)
        return []

    times: list[float] = []
    values: list[float] = []
    at = 0.0
    for line in (proc.stdout or "").splitlines():
        match = _PTS.search(line)
        if match:
            at = float(match.group(1))
            continue
        match = _YAVG.search(line)
        if match:
            times.append(at)
            values.append(float(match.group(1)))
    if len(values) > 1:
        # The first blended frame is measured against black, so it reads as a
        # hard cut; drop it rather than let it seed a false "static" run.
        times, values = times[1:], values[1:]
    if not values:
        return []

    buckets: dict[int, list[float]] = {}
    for moment, value in zip(times, values, strict=True):
        buckets.setdefault(int(moment / bucket), []).append(value)
    return [(index * bucket, sum(vals) / len(vals)) for index, vals in sorted(buckets.items())]


def static_ranges(
    path: str | Path,
    *,
    bucket: float = 2.0,
    threshold: float | None = None,
    min_seconds: float | None = None,
    timeout: float = 3600.0,
) -> list[tuple[float, float]]:
    """The stretches that look like an ad break, a slate or a frozen screen."""
    settings = get_settings()
    threshold = settings.static_threshold if threshold is None else threshold
    min_seconds = settings.static_min_seconds if min_seconds is None else min_seconds
    pairs = motion_map(path, bucket, timeout=timeout)
    if not pairs:
        return []
    runs: list[tuple[float, float]] = []
    start: float | None = None
    for moment, value in pairs:
        if value < threshold:
            if start is None:
                start = moment
        elif start is not None:
            runs.append((start, moment))
            start = None
    if start is not None:
        runs.append((start, pairs[-1][0] + bucket))
    return [(a, b) for a, b in runs if b - a >= min_seconds]


def static_overlap(start: float, end: float, ranges: list[tuple[float, float]]) -> float:
    """How much of a clip (in seconds) sits inside a static stretch."""
    total = 0.0
    for a, b in ranges:
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            total += hi - lo
    return total


def static_share(start: float, end: float, ranges: list[tuple[float, float]]) -> float:
    """The fraction of a clip that sits inside a static stretch (0..1)."""
    duration = end - start
    if duration <= 0:
        return 0.0
    return static_overlap(start, end, ranges) / duration


def drop_static(
    highlights: list[Highlight],
    ranges: list[tuple[float, float]],
    *,
    max_share: float = MAX_STATIC_SHARE,
) -> tuple[list[Highlight], int]:
    """Split highlights into (kept, dropped) on how much static each one sits on."""
    if not ranges:
        return list(highlights), 0
    kept: list[Highlight] = []
    dropped = 0
    for highlight in highlights:
        if static_share(highlight.start, highlight.end, ranges) > max_share:
            dropped += 1
            continue
        kept.append(highlight)
    return kept, dropped
