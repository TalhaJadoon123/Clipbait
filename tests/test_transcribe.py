"""Transcription fallbacks must stay useful with zero keys installed."""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path

import pytest

from clipbait.config import detect_capabilities, get_settings
from clipbait.transcribe import AutoTranscriber, MockTranscriber, get_transcriber


def test_mock_transcriber_is_deterministic_and_word_level(tmp_path: Path) -> None:
    transcriber = MockTranscriber()
    missing = tmp_path / "silence.wav"  # probe fails -> fixed 60s transcript

    first = transcriber.transcribe(missing)
    second = transcriber.transcribe(missing)

    assert first.provider == "mock"
    assert first.duration == 60.0
    assert len(first.words) > 50
    assert len(first.segments) > 3
    assert [w.text for w in first.words] == [w.text for w in second.words]
    assert [w.start for w in first.words] == [w.start for w in second.words]

    starts = [word.start for word in first.words]
    assert starts == sorted(starts)
    assert all(word.end > word.start for word in first.words)
    assert all(word.start < first.duration for word in first.words)
    assert first.text_between(0, 10)


def test_zero_key_install_reports_the_mock_path() -> None:
    if importlib.util.find_spec("faster_whisper") is not None:
        return  # a real model is installed; this assertion is about the fallback
    settings = replace(
        get_settings(), transcriber="auto", openai_api_key=None, gemini_api_key=None
    )
    assert detect_capabilities(settings).transcriber == "mock"


def test_explicit_mock_setting_is_honoured() -> None:
    settings = replace(get_settings(), transcriber="mock")
    assert get_transcriber(settings).resolve().name == "mock"


def test_auto_prefers_local_whisper_when_it_exists() -> None:
    settings = replace(get_settings(), transcriber="auto")
    caps = replace(detect_capabilities(settings), whisper=True)
    assert AutoTranscriber(settings, caps).resolve().name == "whisper"


def test_auto_uses_a_cloud_key_before_mock() -> None:
    settings = replace(
        get_settings(),
        transcriber="auto",
        openai_api_key="sk-test",
        gemini_api_key=None,
    )
    caps = replace(detect_capabilities(settings), whisper=False)
    auto = AutoTranscriber(settings, caps)
    assert auto.resolve().name in {"openai", "whisper"}
    assert auto.resolve().name != "mock"


def test_unknown_preference_falls_back_to_mock() -> None:
    settings = replace(get_settings(), transcriber="something-else")
    assert AutoTranscriber(settings).resolve().name == "mock"


def _write_wav(path: Path, rate: int, channels: int, seconds: float) -> None:
    import struct
    import wave

    frames = int(rate * seconds)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(struct.pack("<h", 1000) for _ in range(frames * channels)))


def test_decode_audio_array_reads_mono_16k(tmp_path: Path) -> None:
    from clipbait.transcribe import _decode_audio_array

    path = tmp_path / "mono16k.wav"
    _write_wav(path, 16000, 1, 0.25)
    samples = _decode_audio_array(path)
    assert samples is not None
    assert len(samples) == 4000
    assert abs(float(samples[0]) - 1000 / 32768) < 1e-6


def test_decode_audio_array_skips_other_shapes(tmp_path: Path) -> None:
    from clipbait.transcribe import _decode_audio_array

    stereo = tmp_path / "stereo.wav"
    _write_wav(stereo, 16000, 2, 0.1)
    assert _decode_audio_array(stereo) is None

    wrong_rate = tmp_path / "44k.wav"
    _write_wav(wrong_rate, 44100, 1, 0.1)
    assert _decode_audio_array(wrong_rate) is None

    assert _decode_audio_array(tmp_path / "missing.wav") is None

# ------------------------------------------------------------------ providers


def _openai_settings(key: str | None = "sk-test"):
    from dataclasses import replace as _replace

    return _replace(get_settings(), openai_api_key=key, gemini_api_key=None)


def _gemini_settings(key: str | None = "g-test"):
    from dataclasses import replace as _replace

    return _replace(get_settings(), gemini_api_key=key, openai_api_key=None)


def test_parse_json_variants():
    from clipbait.transcribe import TranscriptionError, _parse_json

    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('prefix {"a": 2} suffix') == {"a": 2}
    with pytest.raises(TranscriptionError, match="valid JSON"):
        _parse_json("not json at all")


def test_openai_requires_a_key():
    from clipbait.transcribe import OpenAITranscriber, TranscriptionError

    with pytest.raises(TranscriptionError, match="OPENAI_API_KEY"):
        OpenAITranscriber(_openai_settings(None))


def test_openai_transcribes_words_and_segments(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from clipbait.transcribe import OpenAITranscriber

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    captured: dict = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured.update({"url": url, "headers": headers, "data": data})
        return SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {
                "language": "en",
                "duration": 4.0,
                "words": [
                    {"word": " hello ", "start": 0.0, "end": 0.4},
                    {"word": "  ", "start": 0.5, "end": 0.6},
                    {"word": "world", "start": 0.6, "end": 1.0},
                ],
                "segments": [{"text": " hello world ", "start": 0.0, "end": 1.0}],
            },
        )

    monkeypatch.setattr("clipbait.transcribe.httpx.post", fake_post)
    transcript = OpenAITranscriber(_openai_settings()).transcribe(audio, language="en")
    assert [word.text for word in transcript.words] == ["hello", "world"]
    assert transcript.provider == "openai"
    assert transcript.duration == 4.0
    assert transcript.segments[0].text == "hello world"
    assert captured["data"]["language"] == "en"
    assert captured["data"]["model"] == "whisper-1"


def test_openai_auto_language_and_error(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from clipbait.transcribe import OpenAITranscriber, TranscriptionError

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    seen: dict = {}

    def ok(url, headers=None, files=None, data=None, timeout=None):
        seen.update(data or {})
        return SimpleNamespace(status_code=200, text="", json=lambda: {"words": []})

    monkeypatch.setattr("clipbait.transcribe.httpx.post", ok)
    OpenAITranscriber(_openai_settings()).transcribe(audio)
    assert "language" not in seen

    monkeypatch.setattr(
        "clipbait.transcribe.httpx.post",
        lambda *a, **k: SimpleNamespace(status_code=403, text="denied", json=lambda: {}),
    )
    with pytest.raises(TranscriptionError, match="denied"):
        OpenAITranscriber(_openai_settings()).transcribe(audio)


def test_gemini_requires_a_key():
    from clipbait.transcribe import GeminiTranscriber, TranscriptionError

    with pytest.raises(TranscriptionError, match="GEMINI_API_KEY"):
        GeminiTranscriber(_gemini_settings(None))


def test_gemini_rejects_large_audio(tmp_path):
    from clipbait.transcribe import GeminiTranscriber, TranscriptionError

    audio = tmp_path / "big.wav"
    with audio.open("wb") as handle:
        handle.truncate(20 * 1024 * 1024)
    with pytest.raises(TranscriptionError, match="too large"):
        GeminiTranscriber(_gemini_settings()).transcribe(audio)


def test_gemini_parses_nested_words(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from clipbait.transcribe import GeminiTranscriber

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    captured: dict = {}

    def fake_post(url, params=None, json=None, timeout=None):
        captured.update({"url": url, "params": params, "json": json})
        return SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": (
                                        '{"language":"en","segments":[{"text":"hi",'
                                        '"start":0,"end":2,"words":['
                                        '{"text":"hi","start":0,"end":0.5},'
                                        '{"text":"","start":0.6,"end":0.7}]}]}'
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr("clipbait.transcribe.httpx.post", fake_post)
    transcript = GeminiTranscriber(_gemini_settings()).transcribe(audio)
    assert [word.text for word in transcript.words] == ["hi"]
    assert transcript.provider == "gemini"
    assert transcript.duration == 2.0
    assert captured["params"] == {"key": "g-test"}


def test_gemini_error_status(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from clipbait.transcribe import GeminiTranscriber, TranscriptionError

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    monkeypatch.setattr(
        "clipbait.transcribe.httpx.post",
        lambda *a, **k: SimpleNamespace(status_code=429, text="slow down", json=lambda: {}),
    )
    with pytest.raises(TranscriptionError, match="slow down"):
        GeminiTranscriber(_gemini_settings()).transcribe(audio)


# --------------------------------------------------------------------- whisper


def test_whisper_loads_a_model_with_cpu_safe_compute(monkeypatch):
    import sys
    import types

    from clipbait.transcribe import WhisperTranscriber

    created: dict = {}

    class FakeWhisperModel:
        def __init__(self, name, **kwargs):
            created.setdefault("models", []).append((name, kwargs))

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setattr("clipbait.transcribe._cuda_available", lambda: False)

    settings = replace(
        get_settings(),
        whisper_device="auto",
        whisper_compute_type="float16",
        whisper_model="tiny",
    )
    transcriber = WhisperTranscriber(settings)
    model = transcriber._load()
    assert isinstance(model, FakeWhisperModel)
    name, kwargs = created["models"][0]
    assert name == "tiny"
    assert kwargs["device"] == "cpu"
    assert kwargs["compute_type"] == "int8"
    assert transcriber._load() is model  # cached


def test_whisper_transcribe_builds_words(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from clipbait.transcribe import WhisperTranscriber

    word = lambda text, start, end: SimpleNamespace(  # noqa: E731
        word=text, start=start, end=end, probability=0.9
    )
    segment = SimpleNamespace(
        text=" hello world ",
        start=0.0,
        end=1.0,
        words=[word(" hello ", 0.0, 0.4), word("   ", 0.4, 0.5), word("world", 0.5, 1.0)],
    )
    seen: dict = {}

    class FakeModel:
        def transcribe(self, source, **kwargs):
            seen.update({"source": source, **kwargs})
            return iter([segment]), SimpleNamespace(duration=1.0, language="en")

    transcriber = WhisperTranscriber(_openai_settings())
    monkeypatch.setattr(transcriber, "_load", lambda: FakeModel())
    monkeypatch.setattr("clipbait.transcribe._decode_audio_array", lambda path: None)

    transcript = transcriber.transcribe(tmp_path / "a.wav", language="auto")
    assert seen["language"] is None
    assert seen["source"] == str(tmp_path / "a.wav")
    assert [w.text for w in transcript.words] == ["hello", "world"]
    assert transcript.language == "en"
    assert transcript.duration == 1.0
    assert transcript.provider == "whisper"


def test_whisper_uses_sample_duration_and_rejects_silence(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from clipbait.transcribe import TranscriptionError, WhisperTranscriber

    class SilentModel:
        def transcribe(self, source, **kwargs):
            return iter([]), SimpleNamespace(duration=0.0, language=None)

    transcriber = WhisperTranscriber(_openai_settings())
    monkeypatch.setattr(transcriber, "_load", lambda: SilentModel())
    monkeypatch.setattr("clipbait.transcribe._decode_audio_array", lambda path: [0.0] * 32000)
    with pytest.raises(TranscriptionError, match="no words"):
        transcriber.transcribe(tmp_path / "a.wav")

    class OneWord:
        def transcribe(self, source, **kwargs):
            segment = type(
                "S",
                (),
                {
                    "text": "hi",
                    "start": 0.0,
                    "end": 0.5,
                    "words": [
                        type("W", (), {"word": "hi", "start": 0.0, "end": 0.5, "probability": 1.0})
                    ],
                },
            )()
            return iter([segment]), SimpleNamespace(duration=0.0, language=None)

    monkeypatch.setattr(transcriber, "_load", lambda: OneWord())
    transcript = transcriber.transcribe(tmp_path / "a.wav", language="en")
    assert transcript.duration == 2.0  # 32000 samples / 16000
    assert transcript.language == "en"


# ---------------------------------------------------------------- auto resolver


def test_auto_resolver_respects_explicit_preferences():
    from clipbait.transcribe import (
        AutoTranscriber,
        GeminiTranscriber,
        MockTranscriber,
        OpenAITranscriber,
        WhisperTranscriber,
    )

    caps = replace(detect_capabilities(get_settings()), whisper=True)

    whisper = AutoTranscriber(replace(get_settings(), transcriber="whisper"), caps)
    assert isinstance(whisper.resolve(), WhisperTranscriber)
    assert whisper.resolve() is whisper.resolve()  # cached

    openai = AutoTranscriber(replace(get_settings(), transcriber="openai", openai_api_key="k"))
    assert isinstance(openai.resolve(), OpenAITranscriber)

    gemini = AutoTranscriber(replace(get_settings(), transcriber="gemini", gemini_api_key="k"))
    assert isinstance(gemini.resolve(), GeminiTranscriber)

    mock = AutoTranscriber(replace(get_settings(), transcriber="whatever"))
    assert isinstance(mock.resolve(), MockTranscriber)


def test_auto_resolver_auto_order():
    from clipbait.transcribe import (
        AutoTranscriber,
        GeminiTranscriber,
        MockTranscriber,
        OpenAITranscriber,
        WhisperTranscriber,
    )

    no_whisper = replace(detect_capabilities(get_settings()), whisper=False)

    only_openai = replace(get_settings(), transcriber="auto", openai_api_key="k")
    assert isinstance(AutoTranscriber(only_openai, no_whisper).resolve(), OpenAITranscriber)

    only_gemini = replace(
        get_settings(), transcriber="auto", openai_api_key=None, gemini_api_key="g"
    )
    assert isinstance(AutoTranscriber(only_gemini, no_whisper).resolve(), GeminiTranscriber)

    nothing = replace(
        get_settings(), transcriber="auto", openai_api_key=None, gemini_api_key=None
    )
    assert isinstance(AutoTranscriber(nothing, no_whisper).resolve(), MockTranscriber)

    caps_whisper = replace(no_whisper, whisper=True)
    assert isinstance(
        AutoTranscriber(replace(get_settings(), transcriber="auto"), caps_whisper).resolve(),
        WhisperTranscriber,
    )


def test_auto_transcribe_delegates(monkeypatch):
    from clipbait.transcribe import AutoTranscriber, Transcript

    auto = AutoTranscriber(replace(get_settings(), transcriber="mock"))
    sentinel = Transcript(provider="sentinel")

    class Delegate:
        def transcribe(self, audio, language="auto", *, source_url=None):
            return sentinel

    monkeypatch.setattr(auto, "resolve", lambda: Delegate())
    assert auto.transcribe(Path("x.wav")) is sentinel


def test_estimate_credits_and_cuda_flag():
    from clipbait.transcribe import _cuda_available, estimate_credits

    assert estimate_credits(0) == 1
    assert estimate_credits(60) == 1
    assert estimate_credits(61) == 2
    assert isinstance(_cuda_available(), bool)
