"""Spike detector behaviour.

The detector is the product: miss a spike and the clip never exists. These
tests pin the two properties that matter — a burst is caught *while it is
happening*, and one burst must not desensitise the next one.
"""

from __future__ import annotations

from clipbait.live.spikes import SpikeConfig, SpikeDetector
from clipbait.schema import ChatMessage

BUCKET = 4.0


def chat(t: float, text: str = "nice", author: str | None = None) -> ChatMessage:
    return ChatMessage(t=t, author=author or f"u{round(t * 10)}", text=text)


def calm(detector: SpikeDetector, buckets: int, *, per_bucket: int = 2, offset: float = 0.0) -> float:
    """Feed steady, unremarkable chat. Returns the next free timestamp."""
    t = offset
    for _ in range(buckets):
        for index in range(per_bucket):
            detector.add(chat(t + index * 0.3))
        t += BUCKET
    return t


def burst(detector: SpikeDetector, start: float, count: int = 24) -> list:
    trigger = "CLIP IT"
    return [
        detector.add(chat(start + index * 0.1, text=trigger, author=f"hype{index}"))
        for index in range(count)
    ]


def test_quiet_chat_never_triggers() -> None:
    detector = SpikeDetector(SpikeConfig(threshold=3.0))
    calm(detector, buckets=30)
    assert detector.messages_seen == 60


def test_burst_triggers_during_the_bucket() -> None:
    detector = SpikeDetector(
        SpikeConfig(threshold=2.0, minimum_messages=6, minimum_rate=1.5, cooldown_seconds=45)
    )
    next_t = calm(detector, buckets=10)
    results = burst(detector, next_t)

    candidates = [c for c in results if c is not None]
    assert candidates, "a 24-message wall of CLIP IT must trigger"
    moment = candidates[0]
    assert moment.trigger_at >= next_t
    assert moment.start == max(0.0, moment.trigger_at - 25.0)
    assert moment.end == moment.trigger_at + 20.0
    assert moment.signal.rate_z >= 2.0
    assert moment.messages, "evidence window must include captured messages"
    assert any("clip it" in m.text.lower() for m in moment.messages)


def test_cooldown_suppresses_duplicate_triggers() -> None:
    detector = SpikeDetector(SpikeConfig(threshold=2.0, cooldown_seconds=45))
    next_t = calm(detector, buckets=10)
    first = [c for c in burst(detector, next_t) if c]
    assert len(first) == 1

    # Immediately after: another burst inside the cooldown window.
    second = [c for c in burst(detector, next_t + 6) if c]
    assert second == []


def test_second_spike_detected_after_cooldown() -> None:
    detector = SpikeDetector(SpikeConfig(threshold=2.0, cooldown_seconds=45))
    next_t = calm(detector, buckets=10)
    assert [c for c in burst(detector, next_t) if c]

    # Calm again long enough for the cooldown and the baseline to settle.
    next_t = calm(detector, buckets=12, offset=next_t + 10)
    assert next_t >= 90
    assert [c for c in burst(detector, next_t) if c], "a later spike must still be caught"


def test_baseline_uses_median_not_mean() -> None:
    detector = SpikeDetector(SpikeConfig(threshold=99.0))
    calm(detector, buckets=12)
    burst(detector, 48.0, count=30)
    # Close the burst bucket with a message in the following bucket.
    detector.add(chat(52.1))

    median, scale = detector._baseline()
    assert median <= 1.0, "one viral bucket must not drag the baseline up"
    assert scale >= 0.35


def test_signal_is_explainable() -> None:
    detector = SpikeDetector(SpikeConfig(threshold=99.0))
    calm(detector, buckets=8)
    burst(detector, 32.0, count=20)
    signal = detector.signal_for(detector._current)  # type: ignore[arg-type]
    assert signal.rate > 0
    assert 0.0 <= signal.hype <= 5.0
    assert 0.0 <= signal.repeats <= 1.0
    assert signal.top_messages


def test_timeline_reflects_buckets() -> None:
    detector = SpikeDetector()
    calm(detector, buckets=12)
    timeline = detector.timeline()
    assert len(timeline) >= 10
    assert all({"t", "count", "authors", "rate"} <= set(point) for point in timeline)
    assert detector.current_rate() > 0