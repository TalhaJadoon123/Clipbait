"""Transcription fallbacks must stay useful with zero keys installed."""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path

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