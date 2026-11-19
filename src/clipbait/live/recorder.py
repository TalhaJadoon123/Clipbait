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

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def slice(self, start: float, end: float, out: Path) -> Path:
        raise NotImplementedError

    def stats(self) -> dict:
        return {}


def resolve_live_stream(url: str, height: int = 720) -> str:
    settings = get_settings()
    proc = media.run(
        [
            settings.ytdlp,
            "-g",
            "-f",
            f"best[height<={height}]/best",
            "--no-warnings",
            url,
        ],
        timeout=60,
        check=False,
    )
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip().startswith("http")]
    if not lines:
        raise RecorderError(
            "could not resolve the live stream URL. Is the stream live and public?"
        )
    return lines[0]


class HlsRecorder(Recorder):
    """Capture a live HLS stream into 4-second MPEG-TS segments."""

    kind = "hls"

    def __init__(self, url: str, directory: Path, segment_seconds: int = 4) -> None:
        self.url = url
        self.directory = directory
        self.segment_seconds = segment_seconds
        self.directory.mkdir(parents=True, exist_ok=True)
        self._segments: list[Segment] = []
        self._seen: set[str] = set()
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._started = 0.0

    def start(self) -> None:
        stream_url = resolve_live_stream(self.url)
        self._started = time.monotonic()
        list_path = self.directory / "segments.csv"
        cmd = [
            get_settings().ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            stream_url,
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
            try:
                if list_path.exists():
                    with list_path.open("r", encoding="utf-8") as handle:
                        for row in csv.reader(handle):
                            if len(row) < 2:
                                continue
                            name, duration = row[0], row[1]
                            if name in self._seen:
                                continue
                            try:
                                seconds = float(duration)
                            except ValueError:
                                continue
                            self._seen.add(name)
                            start = sum(s.duration for s in self._segments)
                            self._segments.append(
                                Segment(self.directory / name, start, seconds)
                            )
                    self._prune()
            except OSError:
                pass
            time.sleep(1.0)

    def _prune(self) -> None:
        keep = get_settings().live_buffer_seconds
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
        segments, offset = select_segments(self._segments, start, end)
        if not segments:
            raise RecorderError("requested range is older than the ring buffer")
        # Wait for the last segment to be complete before cutting.
        deadline = time.monotonic() + 90
        while segments[-1].end < end:
            time.sleep(0.5)
            if time.monotonic() > deadline:
                break
        concat = concat_file(segments, self.directory / "slice.ffconcat")
        out.parent.mkdir(parents=True, exist_ok=True)
        media.run(
            [
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
            "segments": len(self._segments),
            "buffered_seconds": round(self._segments[-1].end - self._segments[0].start, 1)
            if self._segments
            else 0.0,
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
        duration = max(0.5, min(end, self._loop_seconds) - start)
        if start >= self._loop_seconds:
            start = 0.0
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
        return {"kind": self.kind, "source_seconds": round(self._info.duration, 1)}


def build_recorder(url: str, directory: Path) -> Recorder:
    if url.startswith("demo://") or url == "demo":
        return DemoRecorder(directory)
    if "twitch.tv" in url or "youtube.com" in url or "youtu.be" in url:
        return HlsRecorder(url, directory)
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