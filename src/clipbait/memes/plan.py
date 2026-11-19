"""Where a meme *video* insert should go inside a clip.

This is deliberately separate from :mod:`clipbait.virality.memes`, which is
about *copy* formats (POV, Nobody:, ...). Here the subject is footage: a short
b-roll cutaway that interrupts the picture for a moment.

The rule that keeps captions honest: an insert replaces the **video** only. The
spoken audio keeps playing underneath, so word-level captions never drift and
never need to be shifted. Nothing here needs to know the audio exists.

A moment is worth an insert when there is a natural seam (a beat of dead air
between sentences) *or* a strong emotional signal in the words. Otherwise the
clip is left alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..schema import Word
from ..virality.memes import detect_signals

# Signals worth interrupting for, and how much they matter. Weak, purely
# informational signals (``number``, ``hack``) are absent on purpose: they read
# as nuance, not as a moment.
SIGNAL_WEIGHTS: dict[str, float] = {
    "surprise": 3.0,
    "revelation": 3.0,
    "failure": 3.0,
    "conflict": 2.0,
    "awe": 2.0,
    "absurd": 2.0,
    "story_peak": 2.0,
    "contrast": 1.5,
    "confession": 1.5,
    "envy": 1.2,
    "hustle": 1.0,
    "wholesome": 1.0,
}

MIN_SIGNAL_SCORE = 3.0


@dataclass(frozen=True)
class InsertConfig:
    """Knobs for :func:`plan_inserts`. Defaults suit a 30-60s vertical clip."""

    max_inserts: int = 2
    insert_seconds: float = 2.0
    # A gap this short is a breath, not a beat; ignore it.
    min_gap: float = 0.65
    # Two inserts closer than this feel like a montage, not a clip.
    min_spacing: float = 8.0
    # Never open or close a clip on b-roll.
    head_skip: float = 1.5
    tail_skip: float = 1.0
    # Transcript window used to judge whether a moment carries a signal.
    signal_window: float = 6.0
    min_insert_seconds: float = 0.8


@dataclass(frozen=True)
class Insert:
    """A planned cutaway, with times relative to the start of the clip."""

    start: float
    duration: float
    prompt: str
    reason: str
    signals: tuple[str, ...] = ()
    source: str = "gap"

    @property
    def end(self) -> float:
        return self.start + self.duration

    def as_dict(self) -> dict:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "prompt": self.prompt,
            "reason": self.reason,
            "signals": list(self.signals),
            "source": self.source,
        }


@dataclass
class _Candidate:
    at: float
    score: float
    reason: str
    signals: tuple[str, ...] = ()
    source: str = "gap"


def _local_words(words: list[Word], start: float, end: float) -> list[tuple[str, float, float]]:
    """Words overlapping ``[start, end]``, clamped and rebased to clip-local time."""
    local: list[tuple[str, float, float]] = []
    for word in words:
        if word.end <= start or word.start >= end:
            continue
        local.append(
            (word.text, max(0.0, word.start - start), max(0.0, min(end, word.end) - start))
        )
    local.sort(key=lambda item: item[1])
    return local


def _gap_candidates(local: list[tuple[str, float, float]], config: InsertConfig) -> list[_Candidate]:
    out: list[_Candidate] = []
    for before, after in zip(local, local[1:], strict=False):
        gap = after[1] - before[2]
        if gap >= config.min_gap:
            out.append(
                _Candidate(
                    at=before[2],
                    # Longer gaps are better seams, but a 5s silence should not
                    # outrank a signal; cap the contribution.
                    score=min(2.5, 1.0 + gap),
                    reason=f"{gap:.1f}s of dead air",
                    source="gap",
                )
            )
    return out


def _signal_candidates(
    local: list[tuple[str, float, float]], config: InsertConfig
) -> list[_Candidate]:
    out: list[_Candidate] = []
    if not local:
        return out
    for index, (_, at, _) in enumerate(local):
        end = at + config.signal_window
        window = [item for item in local[index:] if item[1] < end]
        if not window:
            continue
        # Anchor the cutaway on the word that first completes a signal inside
        # the window, not on the window's own start: otherwise a signal six
        # seconds away pulls the insert onto unrelated build-up.
        onset = window[0][1]
        for k in range(1, len(window) + 1):
            if detect_signals(" ".join(text for text, _, _ in window[:k])):
                onset = window[k - 1][1]
                break
        signals = tuple(detect_signals(" ".join(text for text, _, _ in window)))
        score = sum(SIGNAL_WEIGHTS.get(signal, 0.0) for signal in signals)
        if score >= MIN_SIGNAL_SCORE:
            out.append(
                _Candidate(
                    at=onset,
                    score=score,
                    reason="signal: " + ", ".join(signals),
                    signals=signals,
                    source="signal",
                )
            )
    return out


def _merge(candidates: list[_Candidate], config: InsertConfig) -> list[_Candidate]:
    """Keep the best candidate per neighbourhood, then sort by time."""
    ordered = sorted(candidates, key=lambda candidate: (-candidate.score, candidate.at))
    chosen: list[_Candidate] = []
    for candidate in ordered:
        if any(abs(candidate.at - kept.at) < config.min_spacing for kept in chosen):
            continue
        chosen.append(candidate)
        if len(chosen) >= max(1, config.max_inserts):
            break
    return sorted(chosen, key=lambda candidate: candidate.at)


def _prompt_for(candidate: _Candidate, local: list[tuple[str, float, float]]) -> str:
    """A short, deterministic generation prompt for the cutaway."""
    end = candidate.at + 5.0
    snippet = " ".join(
        text for text, at, _ in local if candidate.at - 1.0 <= at <= end
    ).strip()
    snippet = " ".join(snippet.split())[:120]
    if candidate.signals:
        lead = "reaction b-roll for " + ", ".join(candidate.signals[:3])
    else:
        lead = "reaction b-roll"
    return f"{lead}: {snippet}" if snippet else lead


def plan_inserts(
    words: list[Word],
    start: float,
    end: float,
    config: InsertConfig | None = None,
) -> list[Insert]:
    """Return the cutaways to place in ``[start, end]`` (clip-local times).

    At most ``config.max_inserts`` are returned, each separated by at least
    ``config.min_spacing`` and never within ``head_skip`` / ``tail_skip`` of the
    clip edges. An empty list is the common, correct answer.
    """
    config = config or InsertConfig()
    local = _local_words(words, start, end)
    if len(local) < 3:
        return []

    clip_length = max(0.0, end - start)
    latest = clip_length - config.tail_skip - config.min_insert_seconds
    candidates = _gap_candidates(local, config) + _signal_candidates(local, config)
    # Drop edge candidates *before* choosing, so the strongest candidate can
    # never be spent on an edge and leave the clip with nothing.
    candidates = [c for c in candidates if config.head_skip <= c.at <= latest]

    inserts: list[Insert] = []
    for candidate in _merge(candidates, config):
        duration = min(config.insert_seconds, max(0.0, clip_length - config.tail_skip - candidate.at))
        if duration < config.min_insert_seconds:
            continue
        inserts.append(
            Insert(
                start=round(candidate.at, 3),
                duration=round(duration, 3),
                prompt=_prompt_for(candidate, local),
                reason=candidate.reason,
                signals=candidate.signals,
                source=candidate.source,
            )
        )
    return inserts


__all__ = ["Insert", "InsertConfig", "SIGNAL_WEIGHTS", "plan_inserts"]
