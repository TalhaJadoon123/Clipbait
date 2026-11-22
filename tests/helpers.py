"""Shared test helpers: synthetic media and transcripts.

Nothing here touches the network: every fixture is generated locally so the
suite stays deterministic on a headless box.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from clipbait.schema import Transcript, TranscriptSegment, Word


def make_video(
    path: Path,
    *,
    seconds: float = 3,
    size: str = "480x270",
    fps: int = 25,
    with_audio: bool = True,
) -> Path:
    """Render a tiny testsrc/sine clip. Fast: ultrafast + tiny frames."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"testsrc2=size={size}:rate={fps}:duration={seconds}",
    ]
    if with_audio:
        cmd += [
            "-f", "lavfi",
            "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        ]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def _add(
    words: list[Word],
    segments: list[TranscriptSegment],
    text: str,
    start: float,
    step: float,
    duration: float,
) -> float:
    cursor = start
    for token in text.split():
        words.append(
            Word(
                text=token,
                start=round(min(cursor, duration), 3),
                end=round(min(cursor + step * 0.82, duration), 3),
            )
        )
        cursor += step
    segments.append(
        TranscriptSegment(text=text, start=round(start, 3), end=round(min(cursor, duration), 3))
    )
    return cursor


def scripted_transcript(duration: float = 45.0) -> Transcript:
    """Speech with a deliberate silence and two meme-friendly signals.

    The first sentence carries a ``failure`` signal ("wrong"), the second a
    ``surprise``/``revelation`` pair ("No way ... actually"), and there is a
    ~1.5s gap between them so the insert planner has a real seam.
    """
    words: list[Word] = []
    segments: list[TranscriptSegment] = []
    cursor = _add(
        words,
        segments,
        "I was completely wrong about the whole thing and nobody told me",
        0.6,
        0.30,
        duration,
    )
    cursor = _add(
        words,
        segments,
        "No way this is real because the system actually works now",
        round(cursor + 1.5, 3),
        0.32,
        duration,
    )
    filler = "and then the numbers kept climbing every single day after that"
    while cursor < duration - 2:
        cursor = _add(words, segments, filler, round(cursor + 0.12, 3), 0.34, duration)
    return Transcript(
        language="en",
        duration=duration,
        words=words,
        segments=segments,
        provider="fixture",
    )


class FixedTranscriber:
    """A transcriber that always returns the same transcript copy."""

    name = "fixture"

    def __init__(self, transcript: Transcript) -> None:
        self.transcript = transcript

    def transcribe(
        self, audio: Path, language: str = "auto", *, source_url: str | None = None
    ) -> Transcript:
        return self.transcript.model_copy(deep=True)
