"""Transcription providers.

Order of preference in ``auto`` mode:
1. ``faster-whisper`` locally (private, no key, GPU-aware)
2. OpenAI Audio API (fast, needs key)
3. Gemini (cheap, needs key)
4. mock (deterministic, for demos and tests)

Word-level timestamps are mandatory: captions and highlight boundaries are
built from them.
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import wave
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from .. import media
from ..config import Capabilities, Settings, detect_capabilities, get_settings
from ..schema import Transcript, TranscriptSegment, Word
from .subtitles import SubtitlesTranscriber, is_youtube

log = logging.getLogger("clipbait.transcribe")


class TranscriptionError(RuntimeError):
    pass


class Transcriber(ABC):
    name = "base"

    @abstractmethod
    def transcribe(
        self, audio: Path, language: str = "auto", *, source_url: str | None = None
    ) -> Transcript: ...


class MockTranscriber(Transcriber):
    """Deterministic transcript for tests, demos and render smoke checks.

    Never presented as real transcription: ``provider`` is ``mock`` and the UI
    shows a warning.
    """

    name = "mock"

    SENTENCES = [
        "Nobody talks about this, but the first ninety seconds decide everything.",
        "I spent four years building the wrong product for the right people.",
        "Here is the part that actually changed my mind about all of it.",
        "You do not need more motivation, you need a smaller first step.",
        "The secret is not the tool, it is the boring system behind the tool.",
        "We tested this on three hundred videos and the results were insane.",
        "Wait, say that again, because that is the whole video right there.",
        "Most people quit right before the compounding starts.",
        "If you remember one thing from this, make it this next sentence.",
        "That is how you turn one recording into thirty days of content.",
    ]

    def transcribe(
        self, audio: Path, language: str = "auto", *, source_url: str | None = None
    ) -> Transcript:
        duration = 60.0
        try:
            info = media.probe(audio)
            duration = info.duration or duration
        except media.MediaError:
            pass

        rng = random.Random(7)
        words: list[Word] = []
        segments: list[TranscriptSegment] = []
        cursor = 1.0
        sent_index = 0
        while cursor < max(20.0, duration - 2):
            sentence = self.SENTENCES[sent_index % len(self.SENTENCES)]
            sent_index += 1
            tokens = sentence.split()
            start = cursor
            for token in tokens:
                spoken = max(0.18, len(token) / 13.0) + rng.uniform(0.0, 0.06)
                words.append(
                    Word(
                        text=token,
                        start=round(cursor, 3),
                        end=round(cursor + spoken, 3),
                        prob=0.97,
                    )
                )
                cursor += spoken
            segments.append(
                TranscriptSegment(text=sentence, start=round(start, 3), end=round(cursor, 3))
            )
            cursor += rng.uniform(0.25, 0.7)

        # Never emit a word past the media duration: downstream clipping and
        # caption timing both assume words live inside the transcript window.
        words = [
            Word(text=w.text, start=w.start, end=min(w.end, duration), prob=w.prob)
            for w in words
            if w.start < duration - 0.05
        ]
        segments = [
            TranscriptSegment(text=s.text, start=s.start, end=min(s.end, duration))
            for s in segments
            if s.start < duration - 0.05
        ]
        return Transcript(
            language="en",
            duration=duration,
            words=words,
            segments=segments,
            provider="mock",
        )


class WhisperTranscriber(Transcriber):
    name = "whisper"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - optional dep
                raise TranscriptionError(
                    "faster-whisper is not installed. Run: pip install 'clipbait[whisper]'"
                ) from exc
            device = self.settings.whisper_device
            if device == "auto":
                device = "cuda" if _cuda_available() else "cpu"
            compute = self.settings.whisper_compute_type
            if device == "cpu" and compute in {"float16", "int8_float16"}:
                compute = "int8"
            self._model = WhisperModel(
                self.settings.whisper_model,
                device=device,
                compute_type=compute,
                download_root=str(self.settings.models_dir),
            )
        return self._model

    def transcribe(
        self, audio: Path, language: str = "auto", *, source_url: str | None = None
    ) -> Transcript:
        model = self._load()
        # Decode the pipeline's 16 kHz mono PCM WAV ourselves and hand Whisper
        # a float32 array. That keeps us off PyAV's container API, whose
        # ``av.open`` keyword set drifts between releases; a non-WAV file falls
        # back to letting faster-whisper decode it.
        samples = _decode_audio_array(audio)
        segments_iter, info = model.transcribe(
            samples if samples is not None else str(audio),
            language=None if language in {"auto", ""} else language,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 350},
            beam_size=5,
        )
        words: list[Word] = []
        segments: list[TranscriptSegment] = []
        for segment in segments_iter:
            segments.append(
                TranscriptSegment(text=segment.text.strip(), start=segment.start, end=segment.end)
            )
            for word in segment.words or []:
                text = (word.word or "").strip()
                if not text:
                    continue
                words.append(
                    Word(
                        text=text,
                        start=float(word.start),
                        end=float(word.end),
                        prob=float(getattr(word, "probability", 0.0) or 0.0),
                    )
                )
        if not words:
            raise TranscriptionError("Whisper returned no words; the audio may be silent")
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        if not duration and samples is not None:
            duration = len(samples) / 16000.0
        return Transcript(
            language=getattr(info, "language", None) or "en",
            duration=duration,
            words=words,
            segments=segments,
            provider="whisper",
        )


class OpenAITranscriber(Transcriber):
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise TranscriptionError("OPENAI_API_KEY is not set")
        self.settings = settings

    def transcribe(
        self, audio: Path, language: str = "auto", *, source_url: str | None = None
    ) -> Transcript:
        data = {
            "model": "whisper-1",
            "response_format": "verbose_json",
            "timestamp_granularities[]": "word",
        }
        if language not in {"auto", ""}:
            data["language"] = language
        with audio.open("rb") as handle:
            response = httpx.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                files={"file": (audio.name, handle, "audio/wav")},
                data=data,
                timeout=900,
            )
        if response.status_code >= 400:
            raise TranscriptionError(f"OpenAI transcription failed: {response.text[:300]}")
        payload = response.json()
        words = [
            Word(text=item["word"].strip(), start=float(item["start"]), end=float(item["end"]))
            for item in payload.get("words", [])
            if item.get("word", "").strip()
        ]
        segments = [
            TranscriptSegment(
                text=(item.get("text") or "").strip(),
                start=float(item.get("start", 0)),
                end=float(item.get("end", 0)),
            )
            for item in payload.get("segments", [])
        ]
        return Transcript(
            language=payload.get("language", "en"),
            duration=float(payload.get("duration") or 0),
            words=words,
            segments=segments,
            provider="openai",
        )


class GeminiTranscriber(Transcriber):
    name = "gemini"

    def __init__(self, settings: Settings) -> None:
        if not settings.gemini_api_key:
            raise TranscriptionError("GEMINI_API_KEY is not set")
        self.settings = settings

    def transcribe(
        self, audio: Path, language: str = "auto", *, source_url: str | None = None
    ) -> Transcript:
        # Gemini inline-data limit is 20MB; long audio must go through Whisper.
        if audio.stat().st_size > 19 * 1024 * 1024:
            raise TranscriptionError("audio too large for Gemini inline upload; use Whisper")
        import base64

        encoded = base64.b64encode(audio.read_bytes()).decode()
        prompt = (
            "Transcribe this audio with word-level timestamps. Return strict JSON: "
            '{"language":"en","segments":[{"text":"...","start":0.0,"end":1.0,'
            '"words":[{"text":"...","start":0.0,"end":0.2}]}]}. '
            "No markdown, no commentary."
        )
        response = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.settings.gemini_model}:generateContent",
            params={"key": self.settings.gemini_api_key},
            json={
                "contents": [
                    {
                        "parts": [
                            {"text": prompt},
                            {"inline_data": {"mime_type": "audio/wav", "data": encoded}},
                        ]
                    }
                ],
                "generationConfig": {"responseMimeType": "application/json"},
            },
            timeout=900,
        )
        if response.status_code >= 400:
            raise TranscriptionError(f"Gemini transcription failed: {response.text[:300]}")
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        payload = _parse_json(text)
        words: list[Word] = []
        segments: list[TranscriptSegment] = []
        for segment in payload.get("segments", []):
            segments.append(
                TranscriptSegment(
                    text=segment.get("text", ""),
                    start=float(segment.get("start", 0)),
                    end=float(segment.get("end", 0)),
                )
            )
            for word in segment.get("words", []):
                words.append(
                    Word(
                        text=word.get("text", "").strip(),
                        start=float(word.get("start", 0)),
                        end=float(word.get("end", 0)),
                    )
                )
        return Transcript(
            language=payload.get("language", "en"),
            duration=segments[-1].end if segments else 0.0,
            words=[w for w in words if w.text],
            segments=segments,
            provider="gemini",
        )


class AutoTranscriber(Transcriber):
    """Resolve the best available provider once, with a clear fallback chain."""

    name = "auto"

    def __init__(self, settings: Settings | None = None, caps: Capabilities | None = None) -> None:
        self.settings = settings or get_settings()
        self.caps = caps or detect_capabilities(self.settings)
        self._delegate: Transcriber | None = None

    def resolve(self) -> Transcriber:
        if self._delegate:
            return self._delegate
        preference = self.settings.transcriber
        if preference in {"auto", ""}:
            if self.caps.whisper:
                self._delegate = WhisperTranscriber(self.settings)
            elif self.settings.openai_api_key:
                self._delegate = OpenAITranscriber(self.settings)
            elif self.settings.gemini_api_key:
                self._delegate = GeminiTranscriber(self.settings)
            else:
                self._delegate = MockTranscriber()
        elif preference in {"subtitles", "youtube", "captions"}:
            self._delegate = SubtitlesTranscriber(self.settings)
        elif preference == "whisper":
            self._delegate = WhisperTranscriber(self.settings)
        elif preference == "openai":
            self._delegate = OpenAITranscriber(self.settings)
        elif preference == "gemini":
            self._delegate = GeminiTranscriber(self.settings)
        else:
            self._delegate = MockTranscriber()
        return self._delegate

    def transcribe(
        self, audio: Path, language: str = "auto", *, source_url: str | None = None
    ) -> Transcript:
        """Prefer the platform's own caption track for remote video.

        It is word-accurate, costs seconds instead of hours, and only falls back
        to speech-to-text when there is no caption track to fetch.
        """
        preferred = self.settings.transcriber in {"auto", "", "subtitles", "youtube", "captions"}
        if preferred and is_youtube(source_url):
            try:
                return SubtitlesTranscriber(self.settings).transcribe(
                    audio, language, source_url=source_url
                )
            except Exception as exc:  # noqa: BLE001 - any failure degrades to ASR
                if self.settings.transcriber not in {"auto", ""}:
                    raise
                log.warning("captions unavailable (%s); falling back to speech-to-text", exc)
        return self.resolve().transcribe(audio, language, source_url=source_url)


def get_transcriber(settings: Settings | None = None) -> Transcriber:
    return AutoTranscriber(settings)


def _cuda_available() -> bool:
    try:
        import torch  # type: ignore[import-not-found]

        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False


def _decode_audio_array(path: Path):
    """Return a mono 16 kHz float32 numpy array, or ``None`` to let the
    transcriber decode the file itself (non-WAV, other sample rates)."""
    try:
        with wave.open(str(path), "rb") as handle:
            if handle.getsampwidth() != 2 or handle.getnchannels() != 1:
                return None
            if handle.getframerate() != 16000:
                return None
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, OSError, EOFError):
        return None
    try:
        import numpy as np  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - numpy ships with ctranslate2
        return None
    return np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0


def _parse_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise TranscriptionError("model did not return valid JSON") from None


def estimate_credits(duration: float) -> int:
    return max(1, math.ceil(duration / 60.0))