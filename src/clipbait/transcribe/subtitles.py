"""Transcripts straight from the platform's own caption track.

YouTube's auto-generated captions ship as WebVTT with *inline per-word
timestamps*::

    >> Only<00:00:34.239><c> love</c><00:00:34.800><c> never</c><c> hesitate.</c>

yt-dlp can fetch that track in seconds even for a 24-hour VOD. Using it instead
of local ASR removes the single most expensive step on a GPU-less box (an hour
of CPU-per-hour-of-audio) and still gives word-level timing, which is all the
caption builder and the highlight ranker actually need.

Falls back to the speech-to-text providers when there is no caption track.
"""

from __future__ import annotations

import html
import logging
import re
import subprocess
import tempfile
from pathlib import Path

from .. import media
from ..config import Settings, get_settings
from ..schema import Transcript, TranscriptSegment, Word
from ..text import is_caption_noise

log = logging.getLogger("clipbait.transcribe.subtitles")

_CUE_START = re.compile(r"^(?P<start>\d{1,2}:\d{2}:\d{2}\.\d{3})\s+-->")
_TAG = re.compile(r"<(?P<ts>\d{1,2}:\d{2}:\d{2}\.\d{3})><c>(?P<text>[^<]*)</c>")
_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


class SubtitlesUnavailable(RuntimeError):
    """The source has no usable caption track."""


def is_youtube(url: str | None) -> bool:
    return bool(url) and any(host in url for host in _YOUTUBE_HOSTS)


def _seconds(stamp: str) -> float:
    hours, minutes, seconds = stamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_vtt(text: str) -> tuple[list[Word], list[TranscriptSegment]]:
    """Word-level words and coarse segments from a rolling WebVTT caption track.

    Only inline-tagged words are trusted: the untagged text in each cue is the
    rolling context window, which repeats earlier words and would otherwise be
    stamped with the wrong time.
    """
    seen: dict[tuple[float, str], str] = {}
    for line in text.splitlines():
        if not _TAG.search(line):
            continue
        for match in _TAG.finditer(line):
            raw = html.unescape(match.group("text")).strip()
            raw = raw.lstrip(">").strip()
            if not raw or is_caption_noise(raw):
                continue
            seen.setdefault((round(_seconds(match.group("ts")), 3), raw), raw)

    ordered = sorted(seen.items(), key=lambda item: (item[0][0], item[0][1]))
    words: list[Word] = []
    for index, ((start, raw), _) in enumerate(ordered):
        end = ordered[index + 1][0][0] if index + 1 < len(ordered) else start + 0.35
        if end <= start:
            end = start + 0.12
        words.append(Word(text=raw, start=start, end=round(end, 3), prob=0.9))

    return words, _segments(words)


def _segments(words: list[Word], *, gap: float = 1.2, max_span: float = 6.0) -> list[TranscriptSegment]:
    """Group words into speech-sized segments, breaking on pauses."""
    segments: list[TranscriptSegment] = []
    bucket: list[Word] = []
    for word in words:
        if bucket and (word.start - bucket[-1].end > gap or word.end - bucket[0].start > max_span):
            segments.append(_segment(bucket))
            bucket = []
        bucket.append(word)
    if bucket:
        segments.append(_segment(bucket))
    return segments


def _segment(bucket: list[Word]) -> TranscriptSegment:
    return TranscriptSegment(
        text=" ".join(word.text for word in bucket),
        start=bucket[0].start,
        end=bucket[-1].end,
    )


class SubtitlesTranscriber:
    """Fetch the source's caption track and turn it into a transcript."""

    name = "subtitles"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def transcribe(
        self, audio: Path, language: str = "auto", *, source_url: str | None = None
    ) -> Transcript:
        if not source_url:
            raise SubtitlesUnavailable("no source URL to fetch captions for")
        languages = ["en", "en-orig"] if language in {"auto", ""} else [language, "en"]
        payload = self._fetch(source_url, languages)
        words, segments = parse_vtt(payload)
        if len(words) < 20:
            raise SubtitlesUnavailable(f"caption track too sparse ({len(words)} words)")
        duration = 0.0
        try:
            duration = media.probe(audio).duration
        except media.MediaError:
            duration = words[-1].end if words else 0.0
        return Transcript(
            language=languages[0],
            duration=duration or (words[-1].end if words else 0.0),
            words=words,
            segments=segments,
            provider="subtitles",
        )

    def _fetch(self, url: str, languages: list[str]) -> str:
        """Download the best available auto-caption track as WebVTT."""
        with tempfile.TemporaryDirectory(dir=str(get_settings().data_dir)) as tmp:
            template = str(Path(tmp) / "cap.%(ext)s")
            cmd = [
                self.settings.ytdlp,
                "--no-warnings",
                "--no-playlist",
                "--skip-download",
                "--write-auto-subs",
                "--write-subs",
                "--sub-langs",
                ",".join(languages),
                "--sub-format",
                "vtt",
                "-o",
                template,
                url,
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            except (OSError, subprocess.SubprocessError) as exc:
                raise SubtitlesUnavailable(f"could not run yt-dlp: {exc}") from exc
            candidates = sorted(Path(tmp).glob("cap*.vtt"))
            if not candidates:
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                raise SubtitlesUnavailable(
                    "no caption track" + (f": {detail[-1]}" if detail else "")
                )
            # Prefer the exact language requested, then whatever came back.
            candidates.sort(key=lambda path: (languages[0] not in path.name, path.name))
            return candidates[0].read_text(encoding="utf-8", errors="replace")
