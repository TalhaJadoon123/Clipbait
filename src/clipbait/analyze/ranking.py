"""Merging, de-duplication and selection.

Both the LLM and heuristic paths end up here, so ranking behaviour is identical
and easy to test: overlap suppression, timeline diversity and score calibration.
"""

from __future__ import annotations

from ..schema import Highlight

MIN_DURATION = 15.0
MAX_DURATION = 185.0


def overlap_ratio(a: Highlight, b: Highlight) -> float:
    intersection = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    shortest = max(0.001, min(a.end - a.start, b.end - b.start))
    return intersection / shortest


def normalise(highlights: list[Highlight]) -> None:
    """Scale scores so the best clip lands at ~92, not automatically 100."""
    if not highlights:
        return
    best = max(h.scores.overall for h in highlights)
    if best <= 0:
        return
    factor = min(1.0, 92.0 / best)
    for highlight in highlights:
        scores = highlight.scores
        highlight.scores = type(scores)(
            hook=round(scores.hook * factor),
            emotion=round(scores.emotion * factor),
            value=round(scores.value * factor),
            share=round(scores.share * factor),
            overall=round(scores.overall * factor),
        )


def select(
    candidates: list[Highlight],
    *,
    max_clips: int = 8,
    max_overlap: float = 0.28,
    min_gap_seconds: float = 2.0,
) -> list[Highlight]:
    """Greedy score-ordered selection with overlap suppression."""
    pool = [h for h in candidates if MIN_DURATION <= h.duration <= MAX_DURATION]
    pool.sort(key=lambda h: h.scores.overall, reverse=True)

    selected: list[Highlight] = []
    for candidate in pool:
        if len(selected) >= max_clips:
            break
        clash = False
        for chosen in selected:
            if overlap_ratio(candidate, chosen) > max_overlap:
                clash = True
                break
            if abs(candidate.start - chosen.start) < min_gap_seconds:
                clash = True
                break
        if not clash:
            selected.append(candidate)

    # A weak tail hurts the product: drop anything far below the best clip.
    # The absolute floor only applies when the best clip clears it. Casual
    # dialogue often scores in the low 20s across the board, and an absolute
    # floor would then discard every candidate and return an empty project;
    # keeping the relative best is always better than returning nothing.
    if selected:
        ceiling = selected[0].scores.overall
        floor = ceiling * 0.55
        if ceiling >= 25:
            floor = max(25.0, floor)
        selected = [h for h in selected if h.scores.overall >= floor]

    selected.sort(key=lambda h: h.start)
    normalise(selected)
    return selected


def snap_to_words(
    highlight: Highlight, words, duration: float | None = None
) -> Highlight:
    """Push boundaries onto the nearest word edge so nothing is cut mid-word."""
    if not words:
        return highlight
    start = highlight.start
    end = highlight.end
    for word in words:
        if word.end > start:
            start = max(0.0, word.start - 0.05)
            break
    for word in reversed(words):
        if word.start < end:
            end = word.end + 0.12
            break
    if duration:
        end = min(end, duration - 0.05)
    return highlight.model_copy(update={"start": round(start, 3), "end": round(end, 3)})