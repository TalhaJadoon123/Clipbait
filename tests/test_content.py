"""On-stream content detection: speech, audio, action and their fusion.

The point of these tests is that the detector can say *why* a window scored
what it scored, and that a broken signal degrades to the other two instead of
taking the monitor down.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from helpers import make_video

from clipbait.live.content import (
    ActionStats,
    AudioStats,
    ContentWatcher,
    SpeechScore,
    analyze_scene,
    fuse_window,
    parse_scene_times,
    score_action,
    score_audio,
    score_speech,
)
from clipbait.schema import Transcript, TranscriptSegment
from clipbait.transcribe import MockTranscriber


def _empty_speech(duration: float = 12.0) -> SpeechScore:
    return SpeechScore(
        text="", words=0, duration=duration, word_rate=0.0, hype=0.0, exclamations=0, score=0.0
    )


# --------------------------------------------------------------------- speech


def test_speech_hype_beats_calm_speech() -> None:
    calm = score_speech("so anyway i walked to the store and bought some milk", 6.0)
    hype = score_speech("NO WAY! I can't believe it, that was insane!", 3.0)

    assert hype.score > calm.score
    assert any("exclamation" in reason for reason in hype.reasons)
    assert any("hype language" in reason for reason in hype.reasons)


def test_speech_empty_is_zero_and_never_negative() -> None:
    empty = score_speech("", 5.0)
    assert empty.score == 0.0
    assert empty.words == 0


def test_speech_rewards_fast_delivery_and_repetition() -> None:
    slow = score_speech("this is a normal sentence about a normal thing", 10.0)
    fast = score_speech("go go go go go go go go go go go go go go go go go go", 2.0)
    assert fast.score > slow.score
    assert any("repeated" in reason for reason in fast.reasons)
    assert any("fast delivery" in reason for reason in fast.reasons)


# ---------------------------------------------------------------------- audio


def test_audio_loud_jump_scores_above_flat_room() -> None:
    flat = [(index * 0.5, 0.05) for index in range(30)]
    burst = [(index * 0.5, 0.05) for index in range(30)]
    burst[20] = (10.0, 0.7)

    flat_stats = score_audio(flat)
    burst_stats = score_audio(burst)

    assert burst_stats.score > flat_stats.score
    assert burst_stats.jump_db > flat_stats.jump_db
    assert burst_stats.peak_second == 10.0
    assert any("loudness spike" in reason for reason in burst_stats.reasons)


def test_audio_silence_does_not_win_on_ratio_alone() -> None:
    # A near-silent recording has a huge ratio but nothing worth clipping.
    samples = [(index * 0.5, 0.0001) for index in range(20)]
    samples[10] = (5.0, 0.004)
    stats = score_audio(samples)
    assert stats.score <= 0.8


def test_audio_empty_is_zero() -> None:
    assert score_audio([]).score == 0.0


# --------------------------------------------------------------------- action


def test_parse_scene_times_reads_ffmpeg_showinfo() -> None:
    stderr = "... pts_time:2.04 ... pts_time:4.51 ... n:0 pts_time:9"
    assert parse_scene_times(stderr) == [2.04, 4.51, 9.0]
    assert parse_scene_times("") == []


def test_action_burst_scores_above_spread_out_cuts() -> None:
    burst = score_action([2.0, 2.4, 2.9, 3.3, 3.8], 20.0)
    spread = score_action([1.0, 5.0, 9.0, 13.0, 17.0], 20.0)

    assert burst.score > spread.score
    assert burst.peak_second >= 4.0
    assert any("motion burst" in reason for reason in burst.reasons)
    assert score_action([], 20.0).score == 0.0


def test_analyze_scene_counts_real_hard_cuts(tmp_path: Path) -> None:
    target = tmp_path / "cuts.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=red:size=480x270:rate=25:duration=2",
            "-f", "lavfi", "-i", "testsrc2=size=480x270:rate=25:duration=2",
            "-f", "lavfi", "-i", "color=blue:size=480x270:rate=25:duration=2",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    stats = analyze_scene(target, 6.0)
    assert stats.cuts >= 2
    assert stats.score > 0


# ----------------------------------------------------------------------- fuse


def test_fuse_picks_the_strongest_origin_and_explains_itself() -> None:
    speech = score_speech("NO WAY! I cannot believe this is happening!", 3.0)
    window = fuse_window(
        start=10.0,
        end=30.0,
        speech=speech,
        audio=AudioStats(),
        action=ActionStats(),
        quote="NO WAY!",
        speech_offset=13.0,
    )

    assert window.origin == "speech"
    assert window.trigger_offset == pytest.approx(13.0)
    assert window.score > 0
    assert window.quote == "NO WAY!"
    assert window.reasons


def test_fuse_notes_correlation_and_audio_origin() -> None:
    audio = AudioStats(mean=0.02, peak=0.6, jump_db=29.5, peak_second=8.0, score=2.6)
    window = fuse_window(
        start=0.0,
        end=20.0,
        speech=score_speech("wow that was loud", 2.0),
        audio=audio,
        action=ActionStats(),
        audio_offset=8.0,
    )

    assert window.origin == "audio"
    assert window.trigger_offset == pytest.approx(8.0)
    assert window.reasons[0] == "speech and audio both spiked"


# -------------------------------------------------------------------- watcher


class StubRecorder:
    """A recorder whose buffer is one prepared file, for deterministic tests."""

    kind = "stub"

    def __init__(self, source: Path, available_until: float = 60.0) -> None:
        self.source = source
        self.available_until = available_until

    def slice(self, start: float, end: float, out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.source, out)
        return out

    def stats(self) -> dict:
        return {"kind": self.kind, "available_from": 0.0, "available_until": self.available_until}


class HypeTranscriber:
    name = "fixture"

    def transcribe(self, audio: Path, language: str = "auto") -> Transcript:
        text = "NO WAY! I can't believe it, that was insane!"
        return Transcript(
            provider="fixture",
            language="en",
            duration=12.0,
            segments=[TranscriptSegment(start=1.0, end=4.0, text=text)],
            words=[],
        )


def test_watcher_triggers_on_what_the_streamer_said(tmp_path: Path) -> None:
    source = make_video(tmp_path / "stream.mp4", seconds=6)
    watcher = ContentWatcher(
        StubRecorder(source),
        directory=tmp_path / "live",
        window_seconds=12,
        interval_seconds=8,
        threshold=0.1,
        transcriber=HypeTranscriber(),
        allow_mock_speech=True,
    )

    candidate = watcher.step(now=60.0)

    assert candidate is not None
    assert candidate.signal.origin in {"speech", "audio", "action"}
    assert candidate.signal.speech > 0
    assert "believe" in candidate.signal.quote.lower()
    assert watcher.windows == 1
    assert watcher.triggers == 1


def test_watcher_skips_mock_speech_but_still_detects_action(tmp_path: Path) -> None:
    cuts = tmp_path / "cuts.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=red:size=480x270:rate=25:duration=2",
            "-f", "lavfi", "-i", "testsrc2=size=480x270:rate=25:duration=2",
            "-f", "lavfi", "-i", "color=blue:size=480x270:rate=25:duration=2",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(cuts),
        ],
        check=True,
        capture_output=True,
    )
    watcher = ContentWatcher(
        StubRecorder(cuts),
        directory=tmp_path / "live",
        window_seconds=12,
        interval_seconds=8,
        threshold=0.1,
        transcriber=MockTranscriber(),
    )

    assert watcher.speech_available() is False
    candidate = watcher.step(now=60.0)

    assert candidate is not None
    assert candidate.signal.origin == "action"
    assert candidate.signal.speech == 0.0
    assert candidate.signal.action > 0


def test_watcher_respects_cooldown(tmp_path: Path) -> None:
    source = make_video(tmp_path / "stream.mp4", seconds=6)
    watcher = ContentWatcher(
        StubRecorder(source, available_until=400.0),
        directory=tmp_path / "live",
        window_seconds=12,
        interval_seconds=8,
        threshold=0.1,
        cooldown=120.0,
        transcriber=HypeTranscriber(),
        allow_mock_speech=True,
    )

    first = watcher.step(now=60.0)
    second = watcher.step(now=80.0)

    assert first is not None
    assert second is None
    assert watcher.triggers == 1


def test_watcher_survives_a_broken_slice(tmp_path: Path) -> None:
    class Broken(StubRecorder):
        def slice(self, start: float, end: float, out: Path) -> Path:
            raise RuntimeError("ffmpeg died")

    watcher = ContentWatcher(
        Broken(make_video(tmp_path / "stream.mp4", seconds=6)),
        directory=tmp_path / "live",
        window_seconds=12,
        threshold=0.1,
    )
    assert watcher.step(now=60.0) is None

def test_trigger_clip_covers_the_whole_scored_window(tmp_path: Path) -> None:
    """The scored line must be inside the cut, even when it starts the window.

    A whole-window speech score anchors on the end of the 32s window, which is
    later than ``trigger - pre_roll`` (25s). Anchoring only on that dropped the
    opening seconds of the very audio the detector had just scored.
    """
    class WholeWindowTranscriber:
        name = "fixture"

        def transcribe(self, audio: Path, language: str = "auto") -> Transcript:
            words = " ".join(["insane"] * 64)
            return Transcript(
                provider="fixture",
                language="en",
                duration=32.0,
                segments=[TranscriptSegment(start=0.0, end=32.0, text=words)],
                words=[],
            )

    source = make_video(tmp_path / "stream.mp4", seconds=6)
    watcher = ContentWatcher(
        StubRecorder(source),
        directory=tmp_path / "live",
        window_seconds=32,
        interval_seconds=8,
        threshold=0.1,
        pre_roll=25,
        post_roll=20,
        transcriber=WholeWindowTranscriber(),
        allow_mock_speech=True,
    )

    candidate = watcher.step(now=60.0)

    assert candidate is not None
    # Whole-window speech anchors the trigger at the end of the window.
    assert candidate.trigger_at == pytest.approx(58.0)
    window_start = 60.0 - 2.0 - 32.0
    assert candidate.start == pytest.approx(window_start)
    assert candidate.end == pytest.approx(78.0)


def test_speech_anchors_the_cut_on_the_scored_line(tmp_path: Path) -> None:
    """A long quiet window must not bury the line that triggered the clip."""

    class TwoLines:
        name = "fixture"

        def transcribe(self, audio: Path, language: str = "auto") -> Transcript:
            return Transcript(
                provider="fixture",
                language="en",
                duration=32.0,
                segments=[
                    TranscriptSegment(
                        start=0.0,
                        end=18.0,
                        text="we are driving down the road and the radio is on",
                    ),
                    TranscriptSegment(
                        start=18.0,
                        end=22.0,
                        text="no way holy shit insane lets go",
                    ),
                ],
                words=[],
            )

    source = make_video(tmp_path / "stream.mp4", seconds=6)
    watcher = ContentWatcher(
        StubRecorder(source),
        directory=tmp_path / "live",
        window_seconds=32,
        interval_seconds=8,
        threshold=0.1,
        pre_roll=25,
        post_roll=20,
        transcriber=TwoLines(),
        allow_mock_speech=True,
    )

    candidate = watcher.step(now=60.0)

    assert candidate is not None
    window_start = 60.0 - 2.0 - 32.0
    assert candidate.start == pytest.approx(window_start + 16.0)
    assert candidate.trigger_at == pytest.approx(window_start + 22.0)
    assert candidate.end == pytest.approx(window_start + 42.0)
    assert "no way" in candidate.signal.quote.lower()
    assert candidate.signal.origin == "speech"


def test_analysis_uses_the_cheap_slice_when_the_recorder_has_one(tmp_path: Path) -> None:
    """Analysis runs every few seconds; it must not pay full-price encoding."""
    calls: list[str] = []

    class Cheap(StubRecorder):
        def slice_analysis(self, start: float, end: float, out: Path) -> Path:
            calls.append("analysis")
            return super().slice(start, end, out)

        def slice(self, start: float, end: float, out: Path) -> Path:
            calls.append("full")
            return super().slice(start, end, out)

    watcher = ContentWatcher(
        Cheap(make_video(tmp_path / "stream.mp4", seconds=6)),
        directory=tmp_path / "live",
        window_seconds=12,
        interval_seconds=8,
        threshold=0.1,
        transcriber=HypeTranscriber(),
        allow_mock_speech=True,
    )
    assert watcher.step(now=60.0) is not None
    assert calls == ["analysis"]


def test_action_is_measured_against_the_streams_own_pace() -> None:
    """A stream that always cuts is not 'action' every single minute."""
    from clipbait.live.content import relative_action

    busy = ActionStats(cuts=40, per_second=1.2, burst=1.4, score=2.8, reasons=("14 cuts in 4s",))
    assert relative_action(busy, baseline_burst=1.35).score == 0.0
    assert relative_action(busy, baseline_burst=1.35).reasons == ()

    sudden = ActionStats(cuts=60, per_second=1.9, burst=3.0, score=2.8, reasons=("12 cuts in 4s",))
    assert relative_action(sudden, baseline_burst=1.2).score > 1.0
    assert relative_action(sudden, baseline_burst=1.2).reasons

    first = relative_action(busy, baseline_burst=0.0)  # no history yet: unchanged
    assert first.score == 2.8


def test_words_with_no_mic_energy_score_weaker() -> None:
    """Radio lyrics and NPC dialogue are not the streamer shouting."""
    quiet = fuse_window(
        start=0.0,
        end=32.0,
        speech=score_speech("and who's gonna stop us nobody but the cocks come on", 8.0),
        audio=AudioStats(),
        action=ActionStats(),
    )
    loud = fuse_window(
        start=0.0,
        end=32.0,
        speech=score_speech("and who's gonna stop us nobody but the cocks come on", 8.0),
        audio=AudioStats(score=1.0, jump_db=9.0),
        action=ActionStats(),
    )
    assert quiet.speech.score == loud.speech.score
    assert quiet.score < loud.score
    assert quiet.score == pytest.approx(
        round(0.62 * quiet.speech.score * 0.7, 3), abs=0.005
    )
