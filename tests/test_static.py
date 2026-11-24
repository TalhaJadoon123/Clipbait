"""Ad-break / frozen-screen detection and the clips it removes."""

from __future__ import annotations

import subprocess

from helpers import scripted_transcript

from clipbait.analyze import analyze, static
from clipbait.analyze.static import (
    drop_static,
    motion_map,
    static_overlap,
    static_ranges,
    static_share,
)
from clipbait.media import MediaError
from clipbait.schema import Highlight, ProjectOptions


def _highlight(start: float, end: float) -> Highlight:
    return Highlight(start=start, end=end, title="clip")


def _metadata(*frames: tuple[float, float]) -> str:
    """The ``metadata=print`` stream ffmpeg emits: a pts_time line and a YAVG line per frame."""
    lines: list[str] = []
    for index, (moment, value) in enumerate(frames):
        lines.append(f"frame:{index}    pts:{index}       pts_time:{moment}")
        lines.append(f"lavfi.signalstats.YAVG={value}")
    return "\n".join(lines) + "\n"


def test_motion_map_parses_and_buckets(monkeypatch) -> None:
    stdout = _metadata(
        (0.0, 0.5),  # first frame is against black and is dropped
        (0.5, 2.0),
        (1.0, 2.0),
        (1.5, 2.0),
        (2.0, 0.1),
        (2.5, 0.1),
    )
    monkeypatch.setattr(
        static, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout, "")
    )
    assert motion_map("video.mp4", 2.0) == [(0.0, 2.0), (2.0, 0.1)]


def test_motion_map_is_empty_when_ffmpeg_cannot_read(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise MediaError("missing binary")

    monkeypatch.setattr(static, "run", boom)
    assert motion_map("video.mp4") == []


def test_static_ranges_groups_low_motion_runs(monkeypatch) -> None:
    readings = [(0.0, 2.0), (2.0, 0.1), (4.0, 0.1), (6.0, 0.1), (8.0, 2.0), (10.0, 0.1)]
    monkeypatch.setattr(static, "motion_map", lambda path, bucket=2.0, **k: list(readings))
    ranges = static_ranges("video.mp4", threshold=0.4, min_seconds=2.0)
    # 2-8 is a long frozen stretch; the trailing 10s low frame closes at the last
    # bucket.
    assert ranges == [(2.0, 8.0), (10.0, 12.0)]


def test_static_ranges_ignores_short_flickers(monkeypatch) -> None:
    monkeypatch.setattr(
        static, "motion_map", lambda path, bucket=2.0, **k: [(0.0, 2.0), (2.0, 0.1), (4.0, 2.0)]
    )
    assert static_ranges("video.mp4", threshold=0.4, min_seconds=6.0) == []


def test_static_overlap_and_share() -> None:
    ranges = [(10.0, 20.0)]
    assert static_overlap(5.0, 15.0, ranges) == 5.0
    assert static_overlap(0.0, 5.0, ranges) == 0.0
    assert static_share(15.0, 25.0, ranges) == 0.5
    assert static_share(20.0, 20.0, ranges) == 0.0


def test_drop_static_keeps_mostly_live_clips() -> None:
    ranges = [(10.0, 20.0)]
    highlights = [_highlight(0.0, 8.0), _highlight(12.0, 18.0), _highlight(18.0, 30.0)]
    kept, dropped = drop_static(highlights, ranges)
    assert kept == [highlights[0], highlights[2]]
    assert dropped == 1
    # No ranges means nothing is ever dropped.
    assert drop_static(highlights, []) == (highlights, 0)


def test_analyze_excludes_clips_on_static_windows() -> None:
    options = ProjectOptions(clips=3, target_seconds=30)
    live, _ = analyze(scripted_transcript(90), options)
    assert live, "sanity: the transcript yields clips on its own"

    dead_air, meta = analyze(scripted_transcript(90), options, static_ranges=[(0.0, 1000.0)])
    assert dead_air == []
    assert meta["static_dropped"] > 0
