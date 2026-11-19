"""Chat spike detection.

The signal is deliberately explainable: message-rate z-score, unique-author
z-score, hype-lexicon density, duplicate-message ratio and sentiment intensity.
Every trigger stores the evidence, so a clip can say *why* it was cut instead of
"the AI felt something".

Pure logic, no I/O - the highest-value test target in this repo.
"""

from __future__ import annotations

import math
import re
from collections import Counter, deque
from dataclasses import dataclass, field

from ..schema import ChatMessage, SpikeSignal

HYPE_TERMS = {
    "w": 1.2, "ww": 1.4, "www": 1.6, "lul": 1.2, "lulw": 1.5, "lmao": 1.2,
    "omg": 1.2, "no way": 1.8, "clip it": 2.2, "clip that": 2.2, "gg": 0.8,
    "pog": 1.3, "poggers": 1.3, "insane": 1.2, "crazy": 1.0, "holy": 1.3,
    "wtf": 1.1, "fire": 0.9, "goat": 1.2, "sheesh": 1.2, "let's go": 1.3,
    "finally": 0.7, "unbelievable": 1.4, "what": 0.4, "wait": 0.4,
}

EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF" "]+"
)

POSITIVE = {"love", "great", "amazing", "best", "win", "w", "goat", "fire", "yes", "lets go", "🔥", "❤️"}
NEGATIVE = {"hate", "worst", "trash", "boring", "cringe", "no", "bad", "💀", "😡"}

URL_RE = re.compile(r"https?://|www\.")


def _meaningful(texts: list[str]) -> bool:
    """True when at least one message says something a human typed on purpose.

    ``W`` / ``ww`` / emoji-only floods are the most common chat spike on a big
    stream and the least useful clip trigger: nothing happens, the audience just
    reacted. Requiring a few real characters removes those without needing a
    language model.
    """
    for text in texts[:3]:
        stripped = (text or "").strip()
        letters = re.sub(r"[^A-Za-z]", "", stripped).lower()
        if len(stripped) >= 6 and len(set(letters)) >= 3:
            return True
    return False


@dataclass(frozen=True)
class SpikeConfig:
    bucket_seconds: float = 4.0
    baseline_buckets: int = 15  # 60s of history
    threshold: float = 3.0
    cooldown_seconds: float = 45.0
    pre_roll: float = 25.0
    post_roll: float = 20.0
    minimum_rate: float = 1.5  # messages/second in the triggering bucket
    minimum_messages: int = 6


@dataclass
class Bucket:
    start: float
    count: int = 0
    authors: set[str] = field(default_factory=set)
    texts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MomentCandidate:
    start: float
    end: float
    trigger_at: float
    score: float
    signal: SpikeSignal
    messages: list[ChatMessage]


class SpikeDetector:
    def __init__(self, config: SpikeConfig | None = None) -> None:
        self.config = config or SpikeConfig()
        self._buckets: deque[Bucket] = deque()
        self._current: Bucket | None = None
        self._last_trigger = -1e9
        self._message_log: deque[ChatMessage] = deque(maxlen=400)
        self._rate_history: deque[float] = deque(maxlen=120)
        self.messages_seen = 0

    # ------------------------------------------------------------------ ingest

    def add(self, message: ChatMessage) -> MomentCandidate | None:
        self.messages_seen += 1
        bucket_index = int(message.t // self.config.bucket_seconds)
        bucket_start = bucket_index * self.config.bucket_seconds

        if self._current is None:
            self._current = Bucket(start=bucket_start)
        elif bucket_start > self._current.start:
            self._close_bucket()
            self._current = Bucket(start=bucket_start)

        self._current.count += 1
        self._current.authors.add(message.author or f"anon{self.messages_seen}")
        self._current.texts.append(message.text)
        self._message_log.append(message)

        # A burst can be visible inside the first couple of seconds.
        if self._current.count >= self.config.minimum_messages:
            if message.t - self._last_trigger >= self.config.cooldown_seconds:
                rate = self._current.count / max(1.0, self.config.bucket_seconds)
                if rate >= self.config.minimum_rate:
                    return self._maybe_trigger(message.t, self._current)
        return None

    def _close_bucket(self) -> None:
        if not self._current:
            return
        rate = self._current.count / max(0.1, self.config.bucket_seconds)
        self._rate_history.append(rate)
        self._buckets.append(self._current)
        while len(self._buckets) > self.config.baseline_buckets:
            self._buckets.popleft()

    # ----------------------------------------------------------------- scoring

    def _baseline(self) -> tuple[float, float]:
        """Median + MAD, not mean + std.

        A single viral burst must not raise the baseline enough to hide the next
        one ten seconds later. Median/MAD stays anchored to normal chat and is
        the difference between detecting one spike and detecting the stream.
        """
        rates = list(self._rate_history)[-self.config.baseline_buckets :]
        if len(rates) < 4:
            return 0.0, 1.0
        ordered = sorted(rates)
        median = ordered[len(ordered) // 2]
        deviations = sorted(abs(rate - median) for rate in rates)
        mad = deviations[len(deviations) // 2]
        return median, max(0.35, mad * 1.4826)

    def signal_for(self, bucket: Bucket) -> SpikeSignal:
        mean, std = self._baseline()
        rate = bucket.count / max(0.1, self.config.bucket_seconds)
        rate_z = (rate - mean) / std

        unique_counts = [len(b.authors) for b in self._buckets][-self.config.baseline_buckets :]
        if len(unique_counts) >= 3:
            unique_mean = sum(unique_counts) / len(unique_counts)
            unique_var = sum((c - unique_mean) ** 2 for c in unique_counts) / len(unique_counts)
            unique_z = (len(bucket.authors) - unique_mean) / max(0.8, math.sqrt(unique_var))
        else:
            unique_z = 0.0

        lowered = [t.lower() for t in bucket.texts]
        hype_hits = 0.0
        for text in lowered:
            for term, weight in HYPE_TERMS.items():
                if term in text:
                    hype_hits += weight
                    break
            if EMOJI_RE.search(text):
                hype_hits += 0.8
        hype_ratio = hype_hits / max(1, len(lowered))

        counter = Counter(lowered)
        top = counter.most_common(1)[0][1] if counter else 0
        repeat_ratio = top / max(1, len(lowered))

        positive = sum(1 for t in lowered if any(word in t for word in POSITIVE))
        negative = sum(1 for t in lowered if any(word in t for word in NEGATIVE))
        sentiment = (positive - negative) / max(1, len(lowered))

        score = (
            0.55 * max(0.0, rate_z)
            + 0.15 * max(0.0, unique_z)
            + 0.15 * hype_ratio
            + 0.10 * repeat_ratio
            + 0.05 * abs(sentiment)
        )

        evidence = self._top_messages(bucket)
        return SpikeSignal(
            rate=round(rate, 2),
            rate_z=round(rate_z, 2),
            unique_z=round(unique_z, 2),
            hype=round(hype_ratio, 3),
            repeats=round(repeat_ratio, 3),
            sentiment=round(sentiment, 3),
            score=round(score, 3),
            top_messages=evidence,
        )

    def _top_messages(self, bucket: Bucket) -> list[str]:
        scored: list[tuple[float, str]] = []
        for text in bucket.texts:
            lowered = text.lower()
            score = 0.0
            for term, weight in HYPE_TERMS.items():
                if term in lowered:
                    score += weight
            if EMOJI_RE.search(text):
                score += 0.5
            if text.isupper() and len(text) > 2:
                score += 0.4
            if URL_RE.search(text):
                score -= 2.0
            if len(text) < 40:
                score += 0.2
            scored.append((score, text))
        scored.sort(key=lambda item: item[0], reverse=True)
        seen: list[str] = []
        for _, text in scored:
            cleaned = text.strip()[:90]
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
            if len(seen) >= 5:
                break
        return seen

    def _maybe_trigger(self, now: float, bucket: Bucket) -> MomentCandidate | None:
        signal = self.signal_for(bucket)
        if signal.score < self.config.threshold:
            return None
        if not _meaningful(signal.top_messages):
            # A wall of "W", "ww" or a single emoji is an audience reflex, not a
            # moment. Cutting it produces a clip with nothing in it.
            return None
        self._last_trigger = now
        start = max(0.0, now - self.config.pre_roll)
        end = now + self.config.post_roll
        window = [m for m in self._message_log if start <= m.t <= now]
        return MomentCandidate(
            start=round(start, 2),
            end=round(end, 2),
            trigger_at=round(now, 2),
            score=signal.score,
            signal=signal,
            messages=window[-40:],
        )

    # -------------------------------------------------------------------- state

    def timeline(self, buckets: int = 30) -> list[dict]:
        history = [
            {
                "t": round(bucket.start, 1),
                "count": bucket.count,
                "authors": len(bucket.authors),
                "rate": round(bucket.count / max(0.1, self.config.bucket_seconds), 2),
            }
            for bucket in self._buckets
        ]
        if self._current:
            history.append(
                {
                    "t": round(self._current.start, 1),
                    "count": self._current.count,
                    "authors": len(self._current.authors),
                    "rate": round(
                        self._current.count / max(0.1, self.config.bucket_seconds), 2
                    ),
                }
            )
        return history[-buckets:]

    def current_rate(self) -> float:
        if not self._current:
            return 0.0
        return round(self._current.count / max(0.1, self.config.bucket_seconds), 2)

    def messages_between(self, start: float, end: float, limit: int = 40) -> list[ChatMessage]:
        """Recent chat inside ``[start, end]``, for overlaying a content clip."""
        return [m for m in self._message_log if start <= m.t <= end][-limit:]