"""Source ingestion: YouTube / Twitch / Kick / direct URLs, and local files."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import media, storage
from ..config import get_settings
from ..net_guard import ensure_safe_url

log = logging.getLogger("clipbait.ingest")

# ``(fraction, message)`` - the same shape the render and pipeline callbacks use.
ProgressFn = Callable[[float, str], None]


@dataclass(frozen=True)
class IngestResult:
    path: Path
    title: str
    duration: float
    source_kind: str
    webpage_url: str | None = None


HOST_KINDS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "twitch.tv": "twitch",
    "kick.com": "kick",
    "vimeo.com": "upload",
}


def source_kind_for(url: str) -> str:
    host = re.sub(r"^www\.", "", (url.split("//", 1)[-1].split("/", 1)[0]).lower())
    for suffix, kind in HOST_KINDS.items():
        if host == suffix or host.endswith("." + suffix):
            return kind
    return "upload"


def _ytdlp_base() -> list[str]:
    settings = get_settings()
    return [
        settings.ytdlp,
        "--no-playlist",
        "--no-warnings",
        "--newline",
        # Resume a partial `.part` file instead of starting a long download again.
        "--continue",
        # 1080p is almost always a fragmented (DASH) download; fetching several
        # fragments at once is a large, safe speedup over the serial default.
        "--concurrent-fragments",
        "4",
        "--retries",
        "10",
        "--fragment-retries",
        "10",
    ]


# ``[info] <id>: Downloading 1 format(s): 397+251`` - the ``+`` list tells us how
# many streams (video + audio) are coming, which is what keeps multi-file
# progress honest instead of resetting to zero on the second file.
_FORMAT_LINE = re.compile(r"Downloading\s+\d+\s+format\(s\):\s*(?P<ids>\S+)")
# ``[download]  45.3% of  188.42MiB at    2.34MiB/s ETA 01:20``
_DOWNLOAD_LINE = re.compile(
    r"\[download\]\s+(?P<pct>\d{1,3}(?:\.\d+)?)%\s+of\s+~?\s*(?P<size>[0-9.]+\s*[A-Za-z]+)"
    r"(?:\s+at\s+(?P<speed>.+?)\s+ETA\s+(?P<eta>\S+))?"
)
_POSTPROCESS_LINE = re.compile(r"\[(Merger|FixupM3u8|VideoConvertor|ExtractAudio|Fixup\w*)\]")


class DownloadTracker:
    """Turn yt-dlp's stdout into a single monotonic overall fraction.

    A 1080p YouTube download is usually two files (video-only + audio-only), so
    simply showing the percent from the current line makes the bar fall back to
    zero when the audio stream starts. Knowing the stream count up front lets us
    report ``(streams_done + current_percent) / streams_total`` instead.
    """

    def __init__(self) -> None:
        self.files = 1
        self.started = 0

    def feed(self, line: str) -> tuple[float, str] | None:
        """Return ``(fraction, message)`` for a progress line, else ``None``."""
        match = _FORMAT_LINE.search(line)
        if match:
            streams = [part for part in match.group("ids").split("+") if part]
            if streams:
                self.files = max(1, len(streams))
            return None

        if "[download] Destination:" in line:
            self.started += 1
            # Trust what we have observed if the info line never appeared.
            self.files = max(self.files, self.started)
            return None

        if _POSTPROCESS_LINE.search(line):
            return 0.99, "Merging audio and video"

        match = _DOWNLOAD_LINE.search(line)
        if not match:
            return None

        percent = float(match.group("pct"))
        completed = max(0, self.started - 1)
        fraction = min(0.995, (completed + percent / 100.0) / self.files)
        return fraction, _download_message(percent, match.group("speed"), match.group("eta"))


def _download_message(percent: float, speed: str | None, eta: str | None) -> str:
    parts = [f"{percent:.0f}%"]
    cleaned_speed = (speed or "").strip()
    if cleaned_speed and "unknown" not in cleaned_speed.lower():
        parts.append(cleaned_speed)
    cleaned_eta = (eta or "").strip()
    if cleaned_eta and cleaned_eta.lower() != "unknown":
        parts.append(f"ETA {cleaned_eta}")
    return " · ".join(parts)


def _run_ytdlp(cmd: list[str], on_progress: ProgressFn | None) -> None:
    """Run yt-dlp, streaming stdout so download progress is measured, not guessed.

    Uses its own Popen loop (rather than ``media.run``) because the download is
    the one step that can take minutes on a long video, and the UI needs a real
    fraction while it happens. Cancellation reaches here as an exception from
    ``on_progress`` and terminates the child in ``finally``.
    """
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
    except FileNotFoundError as exc:
        raise media.MediaError(f"missing binary: {cmd[0]}") from exc

    tail: list[str] = []
    tracker = DownloadTracker()
    last_fraction = -1.0
    last_emit = 0.0
    code = None
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            tail.append(line.rstrip())
            if len(tail) > 200:
                del tail[:100]
            update = tracker.feed(line)
            if update is None or on_progress is None:
                continue
            fraction, message = update
            now = time.monotonic()
            # yt-dlp emits many lines per second; don't hammer SQLite and SSE.
            if fraction - last_fraction >= 0.005 or now - last_emit >= 1.0:
                last_fraction, last_emit = fraction, now
                on_progress(fraction, message)
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    if code != 0:
        detail = " | ".join(tail[-8:]) or "no output"
        raise media.MediaError(f"yt-dlp failed ({code}): {detail}")


def fetch_remote(
    url: str, project_id: str, on_progress: ProgressFn | None = None
) -> IngestResult:
    """Download a remote video at <=1080p and normalise it to mp4."""
    ensure_safe_url(url)
    target_dir = storage.project_dir(project_id) / "source"
    target_dir.mkdir(parents=True, exist_ok=True)
    template = target_dir / "video.%(ext)s"
    info_path = target_dir / "source.info.json"

    cmd = [
        *_ytdlp_base(),
        "-f",
        "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format",
        "mp4",
        "--write-info-json",
        "-o",
        str(template),
        url,
    ]
    _run_ytdlp(cmd, on_progress)
    if on_progress:
        on_progress(1.0, "Download complete")

    candidates = sorted(target_dir.glob("video.*"))
    video = next((p for p in candidates if p.suffix in {".mp4", ".mkv", ".webm", ".mov"}), None)
    if not video:
        raise media.MediaError("download finished but no video file was produced")

    title = "Untitled"
    duration = 0.0
    webpage = url
    # yt-dlp names the info json after the template only when -o has no ext;
    # fall back to a sidecar glob.
    info_file = info_path if info_path.exists() else next(iter(target_dir.glob("*.info.json")), None)
    if info_file and info_file.exists():
        try:
            payload = json.loads(info_file.read_text(encoding="utf-8"))
            title = payload.get("title") or title
            duration = float(payload.get("duration") or 0)
            webpage = payload.get("webpage_url") or webpage
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    if not duration:
        duration = media.probe(video).duration

    normalised = storage.source_path(project_id)
    if video.resolve() != normalised.resolve():
        video.replace(normalised)
    _cleanup_sidecars(target_dir)
    return IngestResult(
        path=normalised,
        title=title,
        duration=duration,
        source_kind=source_kind_for(url),
        webpage_url=webpage,
    )


def fetch_audio(
    url: str, project_id: str, on_progress: ProgressFn | None = None
) -> IngestResult:
    """Download only the audio track, for link mode.

    This is what makes "no full download" possible: a two-hour 1080p talk is
    gigabytes, while its audio is tens of megabytes. The video itself is fetched
    later, one short section per kept clip, in :func:`fetch_section`.
    """
    ensure_safe_url(url)
    target_dir = storage.project_dir(project_id) / "source"
    target_dir.mkdir(parents=True, exist_ok=True)
    template = target_dir / "link_audio.%(ext)s"
    cmd = [
        *_ytdlp_base(),
        "-f",
        "bestaudio/best",
        "--extract-audio",
        "--audio-format",
        "m4a",
        "--audio-quality",
        "5",
        "--write-info-json",
        "-o",
        str(template),
        url,
    ]
    _run_ytdlp(cmd, on_progress)

    audio = storage.link_audio_path(project_id)
    if not (audio.exists() and audio.stat().st_size > 0):
        produced = next(
            (
                path
                for path in sorted(target_dir.glob("link_audio.*"))
                if path.suffix in {".m4a", ".mp3", ".aac", ".ogg", ".opus", ".wav", ".webm"}
            ),
            None,
        )
        if not produced:
            raise media.MediaError("audio download finished but produced no audio file")
        produced.replace(audio)

    title = "Untitled"
    duration = 0.0
    info_file = target_dir / "link_audio.info.json"
    if info_file.exists():
        try:
            payload = json.loads(info_file.read_text(encoding="utf-8"))
            title = payload.get("title") or title
            duration = float(payload.get("duration") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    if not duration:
        duration = media.probe(audio).duration
    if on_progress:
        on_progress(1.0, "Audio ready")
    return IngestResult(
        path=audio,
        title=title,
        duration=duration,
        source_kind=source_kind_for(url),
        webpage_url=url,
    )


def fetch_section(
    url: str,
    start: float,
    end: float,
    target: Path,
    *,
    on_progress: ProgressFn | None = None,
) -> Path:
    """Download one ``[start, end]`` window of a remote video.

    yt-dlp's ``--download-sections`` uses the platform's range/segment support,
    so a 40-second clip out of a two-hour stream costs roughly 40 seconds of
    transfer instead of two hours. Formats are merged to mp4 so the renderer
    gets the same shape as a full download.
    """
    ensure_safe_url(url)
    start = max(0.0, float(start))
    end = max(start + 0.5, float(end))
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        *_ytdlp_base(),
        "-f",
        "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--download-sections",
        f"*{start:.3f}-{end:.3f}",
        "--force-keyframes-at-cuts",
        "--merge-output-format",
        "mp4",
        "-o",
        str(target),
        url,
    ]
    _run_ytdlp(cmd, on_progress)
    if not target.exists() or target.stat().st_size == 0:
        raise media.MediaError("section download produced no file")
    if on_progress:
        on_progress(1.0, "Section ready")
    return target


def _cleanup_sidecars(directory: Path) -> None:
    for path in directory.glob("video.*"):
        if path.suffix in {".part", ".ytdl", ".temp"}:
            path.unlink(missing_ok=True)


def ingest_local(path: Path, project_id: str, title: str | None = None) -> IngestResult:
    if not path.exists():
        raise media.MediaError(f"file not found: {path}")
    target = storage.source_path(project_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if path.resolve() != target.resolve():
        target.write_bytes(path.read_bytes()) if path.stat().st_size < 2 * 1024**3 else _link(path, target)
    info = media.probe(target)
    return IngestResult(
        path=target,
        title=title or path.stem,
        duration=info.duration,
        source_kind="local",
    )


def _link(source: Path, target: Path) -> None:
    import shutil

    shutil.copy2(source, target)


def probe_url(url: str) -> dict:
    """Cheap metadata lookup used by the New Clip screen."""
    ensure_safe_url(url)
    proc = media.run(
        [*_ytdlp_base(), "--dump-single-json", "--skip-download", url],
        check=False,
        timeout=45,
    )
    if proc.returncode != 0:
        return {}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    return {
        "title": payload.get("title"),
        "duration": payload.get("duration"),
        "thumbnail": payload.get("thumbnail"),
        "uploader": payload.get("uploader"),
        "is_live": bool(payload.get("is_live")),
        "live_status": payload.get("live_status"),
    }