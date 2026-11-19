"""Highlight ranking: overlap suppression, duration bounds, calibration."""

from __future__ import annotations

from clipbait.analyze.ranking import normalise, overlap_ratio, select, snap_to_words
from clipbait.schema import ClipScores, Highlight, Word


def highlight(start: float, end: float, overall: int, title: str = "clip") -> Highlight:
    return Highlight(
        start=start,
        end=end,
        title=title,
        scores=ClipScores(hook=overall, emotion=overall, value=overall, share=overall, overall=overall),
    )


def test_overlap_ratio_uses_the_shorter_clip() -> None:
    a = highlight(0, 60, 50)
    b = highlight(30, 120, 50)
    assert overlap_ratio(a, b) == 0.5
    assert overlap_ratio(a, highlight(100, 160, 50)) == 0.0


def test_select_drops_overlapping_and_too_short_candidates() -> None:
    candidates = [
        highlight(0, 60, 90),
        highlight(20, 80, 88),   # overlaps the winner
        highlight(0, 10, 99),    # too short
        highlight(120, 180, 85),
        highlight(300, 520, 91), # too long
    ]
    chosen = select(candidates, max_clips=5)
    assert [(h.start, h.end) for h in chosen] == [(0, 60), (120, 180)]


def test_select_orders_by_time_not_score() -> None:
    chosen = select([highlight(200, 260, 99), highlight(0, 60, 70)], max_clips=5)
    assert [h.start for h in chosen] == [0, 200]


def test_select_respects_max_clips() -> None:
    candidates = [highlight(index * 100, index * 100 + 60, 90 - index) for index in range(6)]
    assert len(select(candidates, max_clips=3)) == 3


def test_select_drops_a_weak_tail() -> None:
    chosen = select(
        [highlight(0, 60, 90), highlight(100, 160, 80), highlight(200, 260, 30)],
        max_clips=5,
    )
    assert all(h.scores.overall >= 40 for h in chosen)
    assert len(chosen) == 2


def test_normalise_caps_the_best_clip_at_92() -> None:
    highlights = [highlight(0, 60, 100), highlight(100, 160, 50)]
    normalise(highlights)
    assert max(h.scores.overall for h in highlights) == 92
    assert highlights[1].scores.overall == 46


def test_normalise_is_idempotent_for_low_scores() -> None:
    highlights = [highlight(0, 60, 20)]
    normalise(highlights)
    assert highlights[0].scores.overall == 20


def test_snap_to_words_avoids_cutting_speech() -> None:
    words = [Word(text="hello", start=9.0, end=9.3), Word(text="world", start=10.0, end=10.4)]
    snapped = snap_to_words(highlight(9.2, 10.2, 80), words, duration=120.0)
    assert snapped.start < 9.2
    assert snapped.end > 10.2


def test_snap_to_words_without_words_is_a_noop() -> None:
    original = highlight(5, 40, 60)
    assert snap_to_words(original, []) is original