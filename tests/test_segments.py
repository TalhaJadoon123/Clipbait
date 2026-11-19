"""Recorder segment selection — the arithmetic behind pre-roll."""

from __future__ import annotations

from pathlib import Path

from clipbait.live.recorder import Segment, select_segments


def make_segments(count: int = 6, duration: float = 4.0) -> list[Segment]:
    return [
        Segment(path=Path(f"seg{index}.ts"), start=index * duration, duration=duration)
        for index in range(count)
    ]


def test_selects_overlapping_segments_and_offset() -> None:
    chosen, offset = select_segments(make_segments(), 5.0, 15.0)
    # A segment starting exactly at the window end contributes nothing.
    assert [s.start for s in chosen] == [4.0, 8.0, 12.0]
    assert offset == 1.0


def test_offset_is_zero_when_start_aligns() -> None:
    chosen, offset = select_segments(make_segments(), 8.0, 12.0)
    assert [s.start for s in chosen] == [8.0]
    assert offset == 0.0


def test_no_overlap_returns_empty() -> None:
    chosen, offset = select_segments(make_segments(), 100.0, 120.0)
    assert chosen == []
    assert offset == 0.0


def test_empty_buffer_is_safe() -> None:
    assert select_segments([], 0.0, 10.0) == ([], 0.0)


def test_segments_are_sorted_before_selection() -> None:
    shuffled = list(reversed(make_segments(4)))
    chosen, _ = select_segments(shuffled, 2.0, 10.0)
    assert [s.start for s in chosen] == sorted(s.start for s in chosen)