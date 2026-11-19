"""Zero-key highlight detection.

This is the fallback that makes Clipbait usable without an LLM: sentence-aware
windowing plus a small, hand-tuned virality model. It will never beat a good
LLM on nuance, but it is deterministic, free and private - and it is the
baseline the LLM path has to beat in evals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..schema import ClipScores, Highlight, Transcript, Word

HOOK_PATTERNS = [
    (r"\bnobody (talks about|tells you|does this)\b", 18),
    (r"\bthe (secret|truth|problem|reason) (is|was|behind)\b", 15),
    (r"\bi was (wrong|completely wrong|today years old)\b", 16),
    (r"\b(most|almost all|99% of) (people|creators|founders)\b", 13),
    (r"\b(here'?s|this is) (the|what|how|why)\b", 10),
    (r"\b(never|stop|don'?t) (do|use|buy|post|start)\b", 12),
    (r"\bif you (only|remember|do) (one|this)\b", 12),
    (r"\b(i|we) (spent|lost|made|built|tested)\b", 11),
    (r"\bwhat nobody\b", 14),
    (r"^\s*(why|how|what)\b", 6),
    (r"\?", 5),
    (r"\b\d+([.,]\d+)?\s?(%|percent|x|million|thousand|k\b)", 8),
]

EMOTION_WORDS = {
    "insane": 5, "crazy": 4, "wild": 4, "unbelievable": 5, "shocked": 5,
    "terrified": 4, "furious": 5, "hilarious": 5, "love": 2, "hate": 4,
    "cried": 5, "laughed": 4, "screamed": 4, "brutal": 3, "painful": 3,
    "honestly": 1, "literally": 1, "finally": 2, "worst": 3, "best": 3,
    "disaster": 4, "miracle": 4, "regret": 3, "afraid": 3, "proud": 2,
}

LAUGHTER = {"haha", "hahaha", "lol", "lmao", "hah", "hehe", "😂", "🤣"}

VALUE_WORDS = {
    "because": 2, "reason": 2, "framework": 4, "system": 3, "step": 2,
    "process": 2, "method": 3, "how": 2, "learned": 3, "lesson": 4,
    "mistake": 3, "result": 2, "data": 3, "tested": 3, "example": 2,
    "actually": 1, "specifically": 2, "rule": 3, "principle": 3,
}

SHARE_WORDS = {
    "never": 3, "always": 2, "everyone": 3, "nobody": 4, "most": 2,
    "wrong": 3, "truth": 3, "lie": 4, "overrated": 5, "underrated": 5,
    "stop": 3, "secret": 4, "hate": 3, "controversial": 5, "unpopular": 4,
    "changed": 2, "forever": 2, "warning": 3,
}

FILLER = re.compile(
    r"\b(um+|uh+|erm|you know|like,|i mean|sort of|kind of|basically|actually,)\b",
    re.IGNORECASE,
)
TOKEN = re.compile(r"[A-Za-z0-9']+")

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "so", "that", "this",
    "these", "those", "i", "you", "he", "she", "it", "we", "they", "me", "him",
    "her", "us", "them", "my", "your", "his", "its", "our", "their", "is", "are",
    "was", "were", "be", "been", "being", "do", "does", "did", "have", "has",
    "had", "will", "would", "can", "could", "should", "of", "in", "on", "at",
    "to", "for", "with", "about", "from", "by", "as", "not", "no", "yes", "just",
    "very", "really", "what", "which", "who", "when", "where", "why", "how", "all",
    "there", "here", "out", "up", "down", "one", "get", "got", "like", "know",
    "think", "go", "going", "make", "made", "say", "said", "thing", "things",
}


@dataclass(frozen=True)
class Sentence:
    start: float
    end: float
    text: str


def split_sentences(transcript: Transcript) -> list[Sentence]:
    """Prefer provider segments, fall back to punctuation on word stream."""
    if transcript.segments:
        sentences = [
            Sentence(seg.start, seg.end, seg.text.strip())
            for seg in transcript.segments
            if seg.text.strip()
        ]
        if sentences:
            return _merge_short(sentences)
    words = transcript.words
    if not words:
        return []
    out: list[Sentence] = []
    current: list[Word] = []
    for word in words:
        current.append(word)
        if re.search(r"[.!?]$", word.text) or (word.end - current[0].start) > 22:
            out.append(
                Sentence(current[0].start, current[-1].end, " ".join(w.text for w in current))
            )
            current = []
    if current:
        out.append(Sentence(current[0].start, current[-1].end, " ".join(w.text for w in current)))
    return _merge_short(out)


def _merge_short(sentences: list[Sentence], min_duration: float = 1.2) -> list[Sentence]:
    merged: list[Sentence] = []
    for sentence in sentences:
        if merged and sentence.end - sentence.start < min_duration:
            previous = merged[-1]
            merged[-1] = Sentence(previous.start, sentence.end, f"{previous.text} {sentence.text}")
        else:
            merged.append(sentence)
    return merged


def _text_scores(text: str, energy: float = 0.5) -> ClipScores:
    lowered = text.lower()
    tokens = [t.lower() for t in TOKEN.findall(text)]
    length = max(1, len(tokens))

    hook = 20.0
    for pattern, weight in HOOK_PATTERNS:
        if re.search(pattern, lowered):
            hook += weight
    first = " ".join(tokens[:14])
    if re.search(r"\b(you|your)\b", first):
        hook += 6

    emotion = 18.0 + sum(EMOTION_WORDS.get(t, 0) for t in tokens) * 1.4
    emotion += sum(3 for t in tokens if t in LAUGHTER)
    emotion += min(18.0, lowered.count("!") * 5.0)
    emotion += energy * 22.0

    value = 20.0 + sum(VALUE_WORDS.get(t, 0) for t in tokens) * 1.2
    value += min(12.0, len(re.findall(r"\d", text)) * 1.5)
    if re.search(r"\b(first|second|third|step \d)\b", lowered):
        value += 6

    share = 16.0 + sum(SHARE_WORDS.get(t, 0) for t in tokens) * 1.3
    share += min(10.0, lowered.count("?") * 3.0)

    # Filler and over-length punish every dimension.
    filler_penalty = min(12.0, len(FILLER.findall(lowered)) * 2.0)
    density_penalty = 0.0 if 8 <= length / 15 <= 3.2 else 4.0

    def clamp(number: float) -> int:
        return int(max(0, min(100, round(number - filler_penalty - density_penalty))))

    hook_s, emotion_s = clamp(hook), clamp(emotion)
    value_s, share_s = clamp(value), clamp(share)
    overall = round(0.34 * hook_s + 0.24 * emotion_s + 0.22 * value_s + 0.20 * share_s)
    return ClipScores(
        hook=hook_s, emotion=emotion_s, value=value_s, share=share_s, overall=overall
    )


def _energy_in(energy: list[tuple[float, float]], start: float, end: float) -> float:
    if not energy:
        return 0.5
    values = [value for t, value in energy if start <= t <= end]
    if not values:
        return 0.5
    peak = max(values)
    mean = sum(values) / len(values)
    return min(1.0, 0.6 * peak + 0.4 * min(1.0, mean * 2.5))


def _hook_line(text: str, limit: int = 88) -> str:
    sentence = re.split(r"(?<=[.!?])\s+", text.strip())[0]
    sentence = re.sub(r"\s+", " ", sentence).strip(" -")
    if len(sentence) <= limit:
        return sentence
    return sentence[: limit - 1].rsplit(" ", 1)[0] + "…"


def _title_from(text: str) -> str:
    hook = _hook_line(text, 70)
    hook = re.sub(r"^(so|okay|ok|and|but|well|yeah)[, ]+", "", hook, flags=re.IGNORECASE)
    hook = hook.rstrip(".!?…")
    words = hook.split()
    if len(words) > 8:
        hook = " ".join(words[:8])
    return hook[:1].upper() + hook[1:] if hook else "Untitled clip"


def keywords(text: str, limit: int = 6) -> list[str]:
    counts: dict[str, int] = {}
    for token in TOKEN.findall(text.lower()):
        if len(token) < 4 or token in STOPWORDS:
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [word for word, _ in ranked[:limit]]


def candidate_highlights(
    transcript: Transcript,
    *,
    target_seconds: int = 55,
    max_clips: int = 8,
    energy: list[tuple[float, float]] | None = None,
) -> list[Highlight]:
    sentences = split_sentences(transcript)
    if not sentences:
        return []

    energy = energy or []
    min_len = max(18.0, target_seconds * 0.55)
    max_len = min(180.0, target_seconds * 1.9)
    candidates: list[Highlight] = []

    for index, first in enumerate(sentences):
        collected: list[Sentence] = [first]
        end = first.end
        for following in sentences[index + 1 :]:
            if end - first.start >= target_seconds and end - first.start >= min_len:
                break
            if following.start - end > 3.0:
                break
            collected.append(following)
            end = following.end
            if end - first.start >= max_len:
                break
        duration = end - first.start
        if duration < min_len:
            continue
        text = " ".join(s.text for s in collected)
        if len(TOKEN.findall(text)) < 30:
            continue
        scores = _text_scores(text, _energy_in(energy, first.start, end))
        candidates.append(
            Highlight(
                start=round(first.start, 3),
                end=round(end, 3),
                title=_title_from(text),
                hook=_hook_line(text),
                quote=_hook_line(text, 140),
                reason="Strong opening line with a clear payoff, detected by the offline ranker.",
                keywords=keywords(text),
                scores=scores,
                source="heuristic",
            )
        )

    return candidates