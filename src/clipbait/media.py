"""Thin, testable wrappers around ffmpeg / ffprobe."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

from .config import get_settings

log = logging.getLogger("clipbait.media")


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    codec: str = ""

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 1.0


def run(cmd: list[str], *, timeout: int | None = None, check: bool = True) -> subprocess.CompletedProcess:
    log.debug("exec: %s", " ".join(cmd[:14]))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise MediaError(f"missing binary: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"command timed out after {timeout}s: {cmd[0]}") from exc
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise MediaError(f"{cmd[0]} failed ({proc.returncode}): " + " | ".join(tail))
    return proc


def probe(path: Path) -> MediaInfo:
    settings = get_settings()
    proc = run(
        [
            settings.ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError("ffprobe returned invalid JSON") from exc

    streams = payload.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = 0.0
    for candidate in (payload.get("format", {}).get("duration"), (video or {}).get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue

    fps = 30.0
    rate = (video or {}).get("avg_frame_rate") or (video or {}).get("r_frame_rate") or "30/1"
    try:
        num, _, den = rate.partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    except (ValueError, ZeroDivisionError):
        fps = 30.0

    return MediaInfo(
        duration=duration,
        width=int((video or {}).get("width") or 0),
        height=int((video or {}).get("height") or 0),
        fps=round(fps, 3),
        has_audio=audio is not None,
        codec=(video or {}).get("codec_name", ""),
    )


def extract_audio(source: Path, target: Path, *, sample_rate: int = 16000) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    run(
        [
            settings.ffmpeg,
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(target),
        ]
    )
    return target


def audio_rms(path: Path, window: float = 0.5) -> list[tuple[float, float]]:
    """Return [(seconds, rms 0..1)] using only the stdlib.

    Used by the no-LLM ranker to detect laughter, shouting and applause. Reads
    16-bit mono PCM produced by :func:`extract_audio`; anything else returns [].
    """
    try:
        with wave.open(str(path), "rb") as handle:
            if handle.getsampwidth() != 2:
                return []
            rate = handle.getframerate()
            frames_per_window = max(1, int(rate * window))
            samples_per_read = frames_per_window * 2
            out: list[tuple[float, float]] = []
            index = 0
            while True:
                raw = handle.readframes(frames_per_window)
                if not raw:
                    break
                # Fast absolute-mean proxy for RMS; 4x cheaper in pure Python.
                total = 0
                count = 0
                for offset in range(0, len(raw) - 1, 2):
                    value = raw[offset] | (raw[offset + 1] << 8)
                    if value >= 32768:
                        value -= 65536
                    total += abs(value)
                    count += 1
                out.append((index * window, min(1.0, (total / max(1, count)) / 32768.0 * 3.0)))
                index += 1
                if len(raw) < samples_per_read // 2:
                    break
            return out
    except (OSError, wave.Error):
        return []


def thumbnail(video: Path, at: float, target: Path, width: int = 540) -> Path | None:
    target.parent.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    proc = run(
        [
            settings.ffmpeg,
            "-y",
            "-v",
            "error",
            "-ss",
            f"{max(0.0, at):.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2",
            str(target),
        ],
        check=False,
        timeout=60,
    )
    return target if proc.returncode == 0 and target.exists() else None


def parse_progress(line: str) -> float | None:
    """Parse ``out_time_ms`` from ffmpeg -progress output into seconds."""
    match = re.match(r"out_time_ms=(\d+)", line.strip())
    if not match:
        return None
    return int(match.group(1)) / 1_000_000


_CPU_ENCODER = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19"]
_ENCODERS = {
    "nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "20"],
    "vaapi": ["-c:v", "h264_vaapi", "-qp", "20"],
    "qsv": ["-c:v", "h264_qsv", "-global_quality", "20"],
    "videotoolbox": ["-c:v", "h264_videotoolbox", "-q:v", "65"],
}
_ENCODER_CACHE: dict[str, list[str]] = {}


def _encoder_works(args: list[str]) -> bool:
    """Listing an encoder does not mean the GPU is reachable (common in VMs)."""
    try:
        proc = run(
            [
                get_settings().ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=256x256:d=0.3:r=10",
                *args,
                "-frames:v",
                "2",
                "-f",
                "null",
                "-",
            ],
            check=False,
            timeout=30,
        )
    except MediaError:
        return False
    return proc.returncode == 0


def hw_encoder(preference: str = "auto") -> list[str]:
    """Return H.264 encoder flags that actually work on this machine."""
    if preference not in {"auto", "cpu", "nvenc", "vaapi", "qsv", "videotoolbox"}:
        preference = "auto"
    if preference in _ENCODER_CACHE:
        return _ENCODER_CACHE[preference]

    if preference == "cpu":
        result = _CPU_ENCODER
    elif preference == "auto":
        try:
            proc = run([get_settings().ffmpeg, "-hide_banner", "-encoders"], check=False)
            listed = proc.stdout or ""
        except MediaError:
            listed = ""
        result = _CPU_ENCODER
        for name in ("nvenc", "videotoolbox", "qsv", "vaapi"):
            if f"h264_{name}" in listed and _encoder_works(_ENCODERS[name]):
                result = _ENCODERS[name]
                break
    else:
        result = _ENCODERS[preference]
        if not _encoder_works(result):
            log.warning("encoder %s is unusable here; falling back to libx264", preference)
            result = _CPU_ENCODER

    _ENCODER_CACHE[preference] = result
    return result


def escape_filter_path(path: Path) -> str:
    """Escape a path for use inside an ffmpeg filter argument."""
    text = str(path).replace("\\", "\\\\")
    return text.replace(":", r"\:").replace("'", r"\'").replace(",", r"\,")