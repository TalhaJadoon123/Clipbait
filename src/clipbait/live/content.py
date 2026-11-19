"""On-stream moment detection: what the streamer says and does.

Chat spikes are lagging evidence. By the time 40 people type "CLIP IT" the
moment is over, and if the stream is small nobody types at all. This module
watches the stream itself:

* **speech** - transcribes the rolling buffer and scores hype language, shouting
  punctuation, rapid delivery, repetition and profanity,
* **audio** - finds a sudden loud peak against the window's own baseline
  (screams, hits, explosions, a mic drop),
* **action** - counts hard scene changes and motion bursts (jumping, standing
  up, throwing something) from the video track.

All three are explainable numbers, fused into one score. Nothing here decides
that a moment is *good*; it only says "something happened here", and the normal
clip pipeline still has to make the render worth posting.

The detector is deliberately failure-tolerant: a missing transcriber, a corrupt
slice or an ffmpeg hiccup degrades to the remaining signals, never to a crashed
monitor.
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path

from .. import media
from ..config import Settings, get_settings
from ..schema import SpikeSignal
from ..transcribe import Transcriber, get_transcriber
from .recorder import Recorder
from .spikes import MomentCandidate

log = logging.getLogger("clipbait.live.content")

# Words and phrases that precede a clip. Weights are "how much would a stranger
# stop scrolling", not sentiment: "I lost everything" beats "I am happy".
SPEECH_LEXICON: dict[str, float] = {
    "no way": 2.0,
    "what the": 1.2,
    "holy": 1.3,
    "insane": 1.4,
    "crazy": 1.0,
    "unbelievable": 1.6,
    "i can't believe": 1.9,
    "cant believe": 1.6,
    "can't believe": 1.6,
    "oh my god": 1.4,
    "omg": 1.2,
    "look at this": 1.2,
    "watch this": 1.2,
    "are you serious": 1.7,
    "let's go": 1.1,
    "we did it": 1.3,
    "i did it": 1.2,
    "finally": 0.7,
    "impossible": 1.3,
    "record": 1.0,
    "million": 1.1,
    "broke": 0.8,
    "lost": 0.8,
    "won": 1.0,
    "quit": 0.7,
    "secret": 0.9,
    "nobody": 0.7,
    "everyone": 0.5,
    "truth": 0.7,
    "never": 0.4,
    "stop": 0.5,
    "wait": 0.5,
    "look": 0.4,
}

INTENSIFIERS = {
    "very", "really", "so", "literally", "actually", "absolutely", "totally",
    "completely", "insanely", "super",
}
PROFANITY = {"fuck", "fucking", "shit", "damn", "hell", "bitch", "ass", "asshole"}
LAUGHTER = {"haha", "hahaha", "hahahaha", "lol", "lmao", "hehe", "hah"}
FILLER = {"um", "uh", "like", "you", "know", "kind", "of", "sort"}

_WORD_RE = re.compile(r"[A-Za-z']+")


@dataclass(frozen=True)
class SpeechScore:
    text: str
    words: int
    duration: float
    word_rate: float
    hype: float
    exclamations: int
    score: float
    reasons: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()
    # Seconds from the start of the analysed window to the beginning of the
    # strongest sentence. None when the score covers the whole window, so the
    # caller keeps the conservative pre-roll.
    anchor: float | None = None


@dataclass(frozen=True)
class AudioStats:
    mean: float = 0.0
    peak: float = 0.0
    jump_db: float = 0.0
    peak_second: float = 0.0
    silence_ratio: float = 0.0
    score: float = 0.0
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionStats:
    cuts: int = 0
    per_second: float = 0.0
    burst: float = 0.0
    peak_second: float = 0.0
    score: float = 0.0
    reasons: tuple[str, ...] = ()


@dataclass
class ContentWindow:
    """One analysed slice of the rolling buffer."""

    start: float
    end: float
    speech: SpeechScore
    audio: AudioStats
    action: ActionStats
    text: str = ""
    quote: str = ""
    score: float = 0.0
    origin: str = "content"
    reasons: list[str] = field(default_factory=list)
    trigger_offset: float = 0.0
    anchor: float | None = None


# --------------------------------------------------------------------- speech


def score_speech(text: str, duration: float) -> SpeechScore:
    """Score a spoken window for clip-worthiness. Pure function."""
    cleaned = (text or "").strip()
    tokens = _WORD_RE.findall(cleaned)
    words = len(tokens)
    duration = max(1.0, float(duration or 1.0))
    word_rate = words / duration
    lowered = cleaned.lower()

    hype = 0.0
    hits: list[str] = []
    for phrase, weight in SPEECH_LEXICON.items():
        if phrase in lowered:
            hype += weight
            hits.append(phrase)
    hype = min(4.5, hype)

    exclamations = cleaned.count("!")
    exclaim = min(2.4, exclamations * 0.85)
    questions = min(0.9, cleaned.count("?") * 0.3)
    caps_words = [t for t in tokens if len(t) > 1 and t.isupper()]
    caps_ratio = len(caps_words) / max(1, words)
    caps = 0.9 if caps_ratio > 0.3 else 0.0

    # Fast delivery is excitement; very slow delivery is dead air.
    pace = min(1.8, max(0.0, (word_rate - 4.2) * 0.55))
    if words >= 4 and word_rate < 0.8:
        pace -= 0.6

    repeats = sum(
        1 for index in range(1, len(tokens)) if tokens[index].lower() == tokens[index - 1].lower()
    )
    counts: dict[str, int] = {}
    for token in tokens:
        key = token.lower()
        counts[key] = counts.get(key, 0) + 1
    top_share = max(counts.values(), default=0) / max(1, words)
    repeat = min(1.2, repeats * 0.4 + max(0.0, top_share - 0.2) * 1.2)

    intensifiers = sum(1 for token in tokens if token.lower() in INTENSIFIERS)
    intensity = min(0.9, intensifiers * 0.18)
    profanity = min(1.4, sum(1 for token in tokens if token.lower() in PROFANITY) * 0.7)
    laughter = min(1.0, sum(1 for token in tokens if token.lower() in LAUGHTER) * 0.8)

    score = (
        0.55 * hype
        + 0.85 * exclaim
        + 0.4 * questions
        + caps
        + pace
        + repeat
        + intensity
        + profanity
        + laughter
    )
    score = max(0.0, round(min(9.9, score), 3))

    reasons: list[str] = []
    if exclamations:
        reasons.append(f"{exclamations} exclamation(s)")
    if hits:
        reasons.append("hype language: " + ", ".join(hits[:3]))
    if pace >= 0.8:
        reasons.append(f"fast delivery ({word_rate:.1f} words/s)")
    if repeat >= 0.6:
        reasons.append("repeated words")
    if profanity:
        reasons.append("profanity")
    if laughter:
        reasons.append("laughter")
    if caps:
        reasons.append("shouted (ALL CAPS)")

    return SpeechScore(
        text=cleaned,
        words=words,
        duration=duration,
        word_rate=round(word_rate, 2),
        hype=round(hype, 2),
        exclamations=exclamations,
        score=score,
        reasons=tuple(reasons),
        signals=tuple(hits),
    )


# ---------------------------------------------------------------------- audio


def score_audio(samples: list[tuple[float, float]]) -> AudioStats:
    """Sudden loudness against the window's own baseline.

    ``samples`` is ``[(seconds, rms 0..1)]`` from :func:`clipbait.media.audio_rms`.
    A scream in an otherwise quiet room is a 20+ dB jump; a constantly loud
    stream is not, which is exactly why the baseline is local.
    """
    values = [value for _, value in samples if value >= 0]
    if not values:
        return AudioStats()

    mean = sum(values) / len(values)
    peak = max(values)
    peak_second = next((at for at, value in samples if value >= peak), 0.0)
    jump_db = 20.0 * math.log10(peak / mean) if mean > 1e-5 else 0.0
    silence = sum(1 for value in values if value < 0.01) / len(values)

    score = max(0.0, min(2.6, (jump_db - 7.0) / 5.0))
    if peak < 0.015:
        # Near-silent audio: nothing to cut, whatever the ratio says.
        score *= 0.3

    reasons: list[str] = []
    if score >= 0.8:
        reasons.append(f"loudness spike (+{jump_db:.0f} dB over the window)")

    return AudioStats(
        mean=round(mean, 4),
        peak=round(peak, 4),
        jump_db=round(jump_db, 2),
        peak_second=round(peak_second, 2),
        silence_ratio=round(silence, 3),
        score=round(score, 3),
        reasons=tuple(reasons),
    )


# --------------------------------------------------------------------- action


def parse_scene_times(stderr: str) -> list[float]:
    return [float(match) for match in re.findall(r"pts_time:([0-9.]+)", stderr or "")]


def score_action(times: list[float], duration: float) -> ActionStats:
    duration = max(1.0, float(duration or 1.0))
    times = sorted(t for t in times if 0.0 <= t <= duration + 0.5)
    cuts = len(times)
    per_second = cuts / duration
    if not times:
        return ActionStats()

    # Action comes in bursts: six cuts in two seconds is a fight, six cuts over
    # a minute is just editing. Score the densest four-second window.
    best_count, best_at = 0, times[0]
    for start in times:
        window = [t for t in times if start <= t < start + 4.0]
        if len(window) > best_count:
            best_count, best_at = len(window), start
    burst = best_count / 4.0
    score = max(0.0, min(2.8, burst * 2.0))

    reasons = (
        [f"{best_count} cuts in 4s (motion burst)"] if score >= 0.8 else []
    )
    return ActionStats(
        cuts=cuts,
        per_second=round(per_second, 3),
        burst=round(burst, 3),
        peak_second=round(best_at + 2.0, 2),
        score=round(score, 3),
        reasons=tuple(reasons),
    )


def relative_action(action: ActionStats, baseline_burst: float) -> ActionStats:
    """Score an action burst against the stream's own recent pace.

    Gameplay cuts constantly: a GTA drive can hit five scene changes in four
    seconds all night, which saturates the absolute score and makes every window
    look busy (0.45 x 2.8 = 1.26 of free score, enough to trigger on nothing).
    Two times the usual pace is action; the usual pace is not.
    """
    if action.burst <= 0 or baseline_burst <= 0.01:
        return action
    ratio = action.burst / baseline_burst
    if ratio <= 1.15:
        return replace(action, score=0.0, reasons=())
    scaled = min(2.8, action.score * min(1.6, (ratio - 1.0) * 1.5))
    return replace(
        action,
        score=round(scaled, 3),
        reasons=action.reasons if scaled >= 0.8 else (),
    )


def analyze_scene(path: Path, duration: float) -> ActionStats:
    """Count hard scene changes with ffmpeg. Never raises."""
    settings = get_settings()
    try:
        proc = subprocess.run(
            [
                settings.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "info",
                "-i",
                str(path),
                "-an",
                "-vf",
                "select='gt(scene,0.22)',showinfo",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("scene analysis failed: %s", exc)
        return ActionStats()
    return score_action(parse_scene_times(proc.stderr), duration)


# ---------------------------------------------------------------------- fuse


def fuse_window(
    *,
    start: float,
    end: float,
    speech: SpeechScore,
    audio: AudioStats,
    action: ActionStats,
    quote: str = "",
    speech_offset: float | None = None,
    speech_anchor: float | None = None,
    audio_offset: float | None = None,
    action_offset: float | None = None,
) -> ContentWindow:
    """Combine the three signals into one explainable moment.

    Weights are intentionally close: a scream with no words, a wild sentence
    with no movement and a silent action beat are all valid clips. Correlation
    is not required - it only raises the score.
    """
    score = 0.62 * speech.score + 0.55 * audio.score + 0.45 * action.score
    if speech.score and not audio.score and not action.score:
        # Words with no mic energy and no movement are usually the game: radio
        # lyrics, NPC dialogue, a cutscene. Demand a much louder spoken signal
        # before a transcript alone can start a clip.
        score *= 0.7
    score = round(min(9.9, score), 3)

    reasons = [*speech.reasons, *audio.reasons, *action.reasons]
    if speech.score and audio.score:
        reasons.insert(0, "speech and audio both spiked")

    parts = {
        "speech": (speech.score, speech_offset),
        "audio": (audio.score, audio_offset),
        "action": (action.score, action_offset),
    }
    origin = max(parts, key=lambda key: parts[key][0])
    offset = parts[origin][1]
    if offset is None:
        offset = {"speech": speech.duration, "audio": audio.peak_second, "action": action.peak_second}[origin]
    trigger_offset = max(0.0, min(end - start, float(offset)))

    return ContentWindow(
        start=start,
        end=end,
        speech=speech,
        audio=audio,
        action=action,
        text=speech.text,
        quote=(quote or speech.text)[:160],
        score=score,
        origin=origin if score > 0 else "content",
        reasons=reasons,
        trigger_offset=trigger_offset,
        anchor=speech_anchor if origin == "speech" else None,
    )


# ------------------------------------------------------------------- watcher


class ContentWatcher:
    """Analyse the newest slice of the ring buffer every ``interval`` seconds.

    Transcription is the expensive step, so one call covers the whole window and
    the peak is found inside it. Failed windows are logged and skipped; they must
    never take the monitor down.
    """

    def __init__(
        self,
        recorder: Recorder,
        *,
        directory: Path,
        window_seconds: int = 32,
        interval_seconds: int = 18,
        pre_roll: float = 25.0,
        post_roll: float = 20.0,
        cooldown: float = 45.0,
        threshold: float = 2.5,
        language: str = "auto",
        transcriber: Transcriber | None = None,
        allow_mock_speech: bool = False,
        settings: Settings | None = None,
    ) -> None:
        self.recorder = recorder
        self.directory = directory / "analysis"
        self.window_seconds = max(12, int(window_seconds))
        self.interval_seconds = max(8, int(interval_seconds))
        self.pre_roll = float(pre_roll)
        self.post_roll = float(post_roll)
        self.cooldown = float(cooldown)
        self.threshold = float(threshold)
        self.language = language
        self.settings = settings or get_settings()
        self._transcriber = transcriber
        self._allow_mock_speech = allow_mock_speech
        self._last_analyzed_end = 0.0
        self._last_trigger = -1e9
        self._speech_ready: bool | None = None
        self._action_bursts: deque[float] = deque(maxlen=60)
        self.windows = 0
        self.triggers = 0

    # ------------------------------------------------------------------ setup

    def _resolve_transcriber(self) -> Transcriber:
        if self._transcriber is None:
            self._transcriber = get_transcriber(self.settings)
        return self._transcriber

    def speech_available(self) -> bool:
        if self._speech_ready is None:
            provider = self._resolve_transcriber()
            name = getattr(provider, "name", "")
            if hasattr(provider, "resolve"):
                try:
                    name = getattr(provider.resolve(), "name", name)
                except Exception:  # noqa: BLE001 - capability probe only
                    name = "unknown"
            self._speech_ready = self._allow_mock_speech or name not in {"mock", "auto", ""}
        return bool(self._speech_ready)

    def stats(self) -> dict:
        return {
            "speech": self.speech_available(),
            "windows": self.windows,
            "triggers": self.triggers,
            "interval": self.interval_seconds,
            "window": self.window_seconds,
        }

    # ------------------------------------------------------------------- work

    def step(self, now: float) -> MomentCandidate | None:
        available_until = self._available_until(now)
        end = min(now - 2.0, available_until - 1.0)
        start = max(0.0, end - self.window_seconds)
        if end - start < 12.0:
            return None
        if start < self._last_analyzed_end - 1.0:
            # New audio has not arrived yet; do not re-score the same seconds.
            return None

        window = self._analyze(start, end)
        self._last_analyzed_end = end
        self.windows += 1
        if window is None:
            return None
        if window.score < 0.01:
            return None
        if window.score < self.threshold:
            return None

        trigger = start + window.trigger_offset
        if trigger - self._last_trigger < self.cooldown:
            return None
        self._last_trigger = trigger
        self.triggers += 1

        if window.anchor is not None:
            # Speech: start two seconds before the sentence the detector actually
            # scored. The clip is then ~line + post-roll long and opens on the
            # line instead of on half a minute of driving.
            clip_start = max(start, start + window.anchor - 2.0)
        else:
            # Chat/audio/action: the trigger is a single beat, so keep the full
            # pre-roll to give the moment room to happen.
            clip_start = min(trigger - self.pre_roll, start)
        return MomentCandidate(
            start=round(max(0.0, clip_start), 2),
            end=round(trigger + self.post_roll, 2),
            trigger_at=round(trigger, 2),
            score=window.score,
            signal=SpikeSignal(
                speech=window.speech.score,
                loudness=window.audio.score,
                action=window.action.score,
                score=window.score,
                quote=window.quote,
                reasons=window.reasons[:8],
                origin=window.origin,
            ),
            messages=[],
        )

    def _available_until(self, now: float) -> float:
        try:
            stats = self.recorder.stats() or {}
        except Exception:  # noqa: BLE001
            return now
        value = stats.get("available_until")
        try:
            return float(value) if value is not None else now
        except (TypeError, ValueError):
            return now

    def _analyze(self, start: float, end: float) -> ContentWindow | None:
        self.directory.mkdir(parents=True, exist_ok=True)
        video: Path | None = None
        audio: Path | None = None
        try:
            with tempfile.TemporaryDirectory(dir=self.directory, prefix="win-") as temp:
                video = Path(temp) / "window.mp4"
                audio = Path(temp) / "window.wav"
                cutter = getattr(self.recorder, "slice_analysis", self.recorder.slice)
                cutter(start, end, video)
                if not video.exists() or video.stat().st_size < 5_000:
                    return None
                duration = max(0.5, end - start)
                try:
                    media.extract_audio(video, audio)
                except media.MediaError:
                    audio = None

                action = self._relative_action(analyze_scene(video, duration))
                audio_stats = AudioStats()
                samples: list[tuple[float, float]] = []
                if audio is not None:
                    samples = media.audio_rms(audio)
                    audio_stats = score_audio(samples)

                speech = SpeechScore(text="", words=0, duration=duration, word_rate=0.0, hype=0.0, exclamations=0, score=0.0)
                speech_offset = duration
                if audio is not None and self.speech_available():
                    speech, speech_offset = self._transcribe(audio, start, duration)

                quote = self._quote(speech)
                window = fuse_window(
                    start=start,
                    end=end,
                    speech=speech,
                    audio=audio_stats,
                    action=action,
                    quote=quote,
                    speech_offset=speech_offset,
                    speech_anchor=speech.anchor,
                    audio_offset=audio_stats.peak_second,
                    action_offset=action.peak_second,
                )
                return window
        except Exception as exc:  # noqa: BLE001 - one bad window must not kill live
            log.debug("content window %s-%s skipped: %s", start, end, exc)
            return None
        finally:
            for path in (video, audio):
                if path is not None:
                    path.unlink(missing_ok=True)

    def _relative_action(self, action: ActionStats) -> ActionStats:
        history = sorted(self._action_bursts)
        baseline = history[len(history) // 2] if len(history) >= 3 else 0.0
        if action.burst > 0:
            self._action_bursts.append(action.burst)
        return relative_action(action, baseline)

    def _transcribe(self, audio: Path, start: float, duration: float):
        try:
            transcript = self._resolve_transcriber().transcribe(audio, self.language)
        except Exception as exc:  # noqa: BLE001
            log.debug("live transcript skipped: %s", exc)
            return (
                SpeechScore(text="", words=0, duration=duration, word_rate=0.0, hype=0.0, exclamations=0, score=0.0),
                duration,
            )

        best = None
        for segment in transcript.segments or []:
            relative = max(0.0, min(duration, float(segment.start or 0.0)))
            length = max(1.0, float((segment.end or 0.0) - (segment.start or 0.0)))
            scored = score_speech(segment.text, length)
            if best is None or scored.score > best[0].score:
                best = (scored, relative + length, relative)

        full_text = " ".join(segment.text for segment in transcript.segments or []).strip()
        if best is None or not full_text:
            return score_speech(full_text, duration), duration

        # Score the whole window for evidence, but quote the strongest sentence
        # and remember where it starts. A 32s window can hold four sentences:
        # cutting from the window start buries the line behind 25s of setup.
        best_score, best_end, best_start = best
        overall = score_speech(full_text, duration)
        if overall.score >= best_score.score * 1.25:
            # The build-up genuinely carries the moment; keep the wide window.
            return overall, duration
        return replace(best_score, anchor=best_start), best_end

    def _quote(self, speech: SpeechScore) -> str:
        text = re.sub(r"\s+", " ", speech.text or "").strip()
        if not text:
            return ""
        words = text.split()
        return " ".join(words[:18])[:120]