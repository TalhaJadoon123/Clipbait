"""Rolling live recorder.

A monitor keeps a short ring buffer of transport-stream segments. When chat
spikes we already have the footage *before* the trigger, which is the whole
point: the reaction starts before anyone types "clip it".

``FileRecorder`` backs the demo mode (and tests) by slicing a local file with
the same interface, so no network is needed to exercise the live pipeline.
"""

from __future__ import annotations

import csv
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .. import media
from ..config import get_settings

log = logging.getLogger("clipbait.live.recorder")


class RecorderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


def select_segments(segments: list[Segment], start: float, end: float) -> tuple[list[Segment], float]:
    """Segments covering [start, end] plus the offset into the first one."""
    if not segments:
        return [], 0.0
    ordered = sorted(segments, key=lambda s: s.start)
    chosen = [s for s in ordered if s.end > start and s.start < end]
    if not chosen:
        return [], 0.0
    offset = max(0.0, start - chosen[0].start)
    return chosen, offset


def concat_file(segments: list[Segment], path: Path) -> Path:
    lines = ["ffconcat version 1.0"]
    for segment in segments:
        lines.append(f"file '{segment.path.as_posix()}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class Recorder:
    kind = "base"

    @property
    def started_at(self) -> float:
        """``time.monotonic()`` when this recorder began writing media.

        Zero until ``start()`` runs. The runner adopts this value as the monitor
        clock so chat, content windows and segment timestamps share one origin.
        """
        return self._started

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def slice(self, start: float, end: float, out: Path) -> Path:
        raise NotImplementedError

    def slice_analysis(self, start: float, end: float, out: Path) -> Path:
        """A cheap slice for the content watcher.

        Analysis runs every few seconds forever, so it must not pay full-price
        encoding. Recorders that can produce a smaller/faster file override
        this; everything else still gets a correct slice.
        """
        return self.slice(start, end, out)

    def stats(self) -> dict:
        return {}


def resolve_live_streams(url: str, height: int = 720) -> list[str]:
    """Resolve a live page to 1-2 direct stream URLs (video, maybe audio).

    YouTube's live HLS exposes *video-only* formats and separate audio-only
    formats, so the old ``best[height<=720]`` selector matched nothing and the
    recorder failed with "Requested format is not available". Selecting
    ``bv*+ba`` gets both streams; when the platform serves a muxed stream there
    is only one URL and the recorder uses it directly.
    """
    settings = get_settings()
    formats = f"bv*[height<={height}]+ba/b[height<={height}]/b"
    proc = media.run(
        [
            settings.ytdlp,
            "-g",
            "-f",
            formats,
            "--no-warnings",
            url,
        ],
        timeout=90,
        check=False,
    )
    lines = [
        line.strip()
        for line in (proc.stdout or "").splitlines()
        if line.strip().startswith(("http://", "https://"))
    ]
    if not lines:
        detail = " ".join((getattr(proc, "stderr", "") or "").strip().splitlines()[-1:])[:220]
        hint = f" ({detail})" if detail else ""
        raise RecorderError(
            "could not resolve the live stream URL. Is the stream live and public?"
            + hint
        )
    return lines[:2]


def resolve_live_stream(url: str, height: int = 720) -> str:
    """Single-URL form, kept for callers that only need the first stream."""
    return resolve_live_streams(url, height)[0]


class HlsRecorder(Recorder):
    """Capture a live HLS stream into 4-second MPEG-TS segments."""

    kind = "hls"

    def __init__(
        self,
        url: str,
        directory: Path,
        segment_seconds: int = 4,
        *,
        origin: float | None = None,
        buffer_seconds: int | None = None,
    ) -> None:
        self.url = url
        self.directory = directory
        self.segment_seconds = segment_seconds
        # How much footage stays on disk. Content analysis (scene detect +
        # whisper) can finish minutes after the window it scored, so a small
        # ring silently turns every late trigger into a skipped clip. The
        # monitor option wins; the setting is the fallback.
        self.buffer_seconds = int(
            buffer_seconds or get_settings().live_buffer_seconds
        )
        # ``origin`` is the monitor clock (time.monotonic) the runner started at.
        # Segment timestamps are rebased onto it so chat times and video times
        # agree even though resolving a live URL takes 20-40 seconds.
        self._origin = origin
        self.directory.mkdir(parents=True, exist_ok=True)
        self._segments: list[Segment] = []
        self._seen: set[str] = set()
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._started = 0.0

    def start(self) -> None:
        streams = resolve_live_streams(self.url)
        # Resolving the URL can take 20-40s. Stamp the clock *after* that, the
        # moment ffmpeg really starts writing, and rebase segments onto it.
        self._started = time.monotonic()
        self._origin = self._started
        list_path = self.directory / "segments.csv"
        cmd = [
            get_settings().ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            # Live HLS drops connections; reconnect instead of ending the buffer.
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            # A live HLS playlist often carries an hour of DVR. Without this,
            # ffmpeg starts at the oldest segment and races through the backlog
            # at 20x realtime, so segment timestamps stop tracking the clock.
            "-live_start_index",
            "-1",
            "-i",
            streams[0],
        ]
        if len(streams) > 1:
            cmd += ["-live_start_index", "-1", "-i", streams[1], "-map", "0:v:0", "-map", "1:a:0?"]
        cmd += [
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(self.segment_seconds),
            "-reset_timestamps",
            "1",
            "-segment_list",
            str(list_path),
            "-segment_list_type",
            "csv",
            str(self.directory / "seg_%06d.ts"),
        ]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self._monitor = threading.Thread(target=self._watch, args=(list_path,), daemon=True)
        self._monitor.start()
        log.info("recording %s", self.url)

    def _watch(self, list_path: Path) -> None:
        while not self._stop.is_set():
            self._scan(list_path)
            time.sleep(1.0)

    def _scan(self, list_path: Path) -> None:
        """Read new rows from ffmpeg's segment list, one pass."""
        try:
            if list_path.exists():
                with list_path.open("r", encoding="utf-8") as handle:
                    for row in csv.reader(handle):
                        if len(row) < 2:
                            continue
                        name = row[0]
                        if name in self._seen:
                            continue
                        # ffmpeg writes ``name,start,end`` for CSV segment lists;
                        # older builds wrote ``name,duration``. Using column 2 as
                        # a duration quadrupled every timestamp, so accept both.
                        try:
                            if len(row) >= 3:
                                seconds = float(row[2]) - float(row[1])
                            else:
                                seconds = float(row[1])
                        except ValueError:
                            continue
                        if seconds <= 0:
                            seconds = float(self.segment_seconds)
                        self._seen.add(name)
                        if self._segments:
                            start = self._segments[-1].end
                        elif self._origin is not None:
                            # First segment: its start is ``duration`` before
                            # it was discovered, measured on the monitor clock.
                            start = max(
                                0.0, time.monotonic() - self._origin - seconds
                            )
                        else:
                            start = 0.0
                        self._segments.append(
                            Segment(self.directory / name, start, seconds)
                        )
                self._prune()
        except OSError:
            pass

    def _prune(self) -> None:
        keep = self.buffer_seconds
        if not self._segments:
            return
        newest = self._segments[-1].end
        for segment in list(self._segments):
            if segment.end < newest - keep:
                segment.path.unlink(missing_ok=True)
                self._segments.remove(segment)

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._monitor:
            self._monitor.join(timeout=3)

    def slice(self, start: float, end: float, out: Path) -> Path:
        return self._slice(start, end, out, analysis=False)

    def slice_analysis(self, start: float, end: float, out: Path) -> Path:
        return self._slice(start, end, out, analysis=True)

    def _slice(self, start: float, end: float, out: Path, *, analysis: bool) -> Path:
        segments, offset = select_segments(self._segments, start, end)
        if not segments:
            window = (
                f"{self._segments[0].start:.1f}-{self._segments[-1].end:.1f}s"
                if self._segments
                else "empty"
            )
            raise RecorderError(
                f"no recorded segment overlaps {start:.1f}-{end:.1f}s "
                f"(buffer {window})"
            )
        # Wait for the last segment to be complete before cutting.
        deadline = time.monotonic() + 90
        while segments[-1].end < end:
            time.sleep(0.5)
            if time.monotonic() > deadline:
                break
        concat = concat_file(segments, self.directory / "slice.ffconcat")
        out.parent.mkdir(parents=True, exist_ok=True)
        # Seek *after* -i, not before it. An input seek (``-ss`` before ``-i``)
        # on a concat of TS segments lands the video on the nearest keyframe --
        # up to a second *after* the audio's seek point -- so the slice opens
        # with audio that has no matching video and every burned caption sits
        # that far off the lips. As an output seek, ffmpeg decodes from the head
        # of the concatenated window (only the offset we already trimmed, so a
        # few seconds at most) and applies one seek point to both streams, which
        # keeps video and audio starting together.
        cmd = [
            get_settings().ffmpeg,
            "-hide_banner",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-ss",
            f"{offset:.3f}",
            "-t",
            f"{max(0.5, end - start):.3f}",
        ]
        if analysis:
            # 480p/ultrafast is enough for a loudness curve and scene cuts. The
            # final capture below stays at full quality.
            cmd += [
                "-vf",
                "scale=-2:480",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "30",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-ar",
                "16000",
            ]
        else:
            cmd += [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-ar",
                "48000",
            ]
        cmd += ["-movflags", "+faststart", str(out)]
        media.run(cmd, timeout=300)
        return out

    def stats(self) -> dict:
        return {
            "kind": self.kind,
            "segments": len(self._segments),
            "buffered_seconds": round(self._segments[-1].end - self._segments[0].start, 1)
            if self._segments
            else 0.0,
            "available_from": round(self._segments[0].start, 1) if self._segments else 0.0,
            "available_until": round(self._segments[-1].end, 1) if self._segments else 0.0,
        }


class FileRecorder(Recorder):
    """Slice a local file on a synthetic clock (demo mode and tests)."""

    kind = "file"

    def __init__(self, source: Path, *, loop_seconds: float | None = None) -> None:
        self.source = source
        self._started = 0.0
        self._info = media.probe(source)
        self._loop_seconds = loop_seconds or self._info.duration

    def start(self) -> None:
        self._started = time.monotonic()

    def stop(self) -> None:  # noqa: B027 - nothing to release
        pass

    def slice(self, start: float, end: float, out: Path) -> Path:
        loop = self._loop_seconds
        span = max(0.5, end - start)
        if loop and start >= loop:
            start = start % loop
        duration = max(0.5, min(span, loop - start)) if loop else span
        out.parent.mkdir(parents=True, exist_ok=True)
        media.run(
            [
                get_settings().ffmpeg,
                "-hide_banner",
                "-y",
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{duration:.3f}",
                "-i",
                str(self.source),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-movflags",
                "+faststart",
                str(out),
            ],
            timeout=300,
        )
        return out

    def stats(self) -> dict:
        return {
            "kind": self.kind,
            "source_seconds": round(self._info.duration, 1),
            "available_from": 0.0,
            "available_until": round(self._loop_seconds or self._info.duration, 1),
        }


def build_recorder(
    url: str,
    directory: Path,
    *,
    origin: float | None = None,
    buffer_seconds: int | None = None,
) -> Recorder:
    if url.startswith("demo://") or url == "demo":
        return DemoRecorder(directory)
    if "twitch.tv" in url or "youtube.com" in url or "youtu.be" in url:
        return HlsRecorder(url, directory, origin=origin, buffer_seconds=buffer_seconds)
    raise RecorderError("unsupported live URL; use YouTube, Twitch or demo://live")


def ensure_demo_source(path: Path, seconds: int = 240) -> Path:
    """Generate a synthetic 720p stream once, for demo mode."""
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    media.run(
        [
            get_settings().ffmpeg,
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=1280x720:rate=25:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:duration={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        timeout=300,
    )
    return path


class DemoRecorder(FileRecorder):
    kind = "demo"

    def __init__(self, directory: Path) -> None:
        source = ensure_demo_source(directory / "demo_source.mp4")
        super().__init__(source, loop_seconds=source and 240.0)


def clear(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)