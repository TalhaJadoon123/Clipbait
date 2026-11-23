"""Source ingestion: YouTube / Twitch / Kick / direct URLs, and local files."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _ytdlp_base(fragments: int = 4) -> list[str]:
    settings = get_settings()
    cmd = [
        settings.ytdlp,
        "--no-playlist",
        "--no-warnings",
        "--newline",
        # Resume a partial `.part` file instead of starting a long download again.
        "--continue",
        # Remote media arrives as many small fragments (DASH or HLS). Fetching
        # several at once is a large, safe speedup over the serial default, and
        # HLS section fetches in particular go from ~150 KB/s to the full pipe
        # at eight.
        "--concurrent-fragments",
        str(max(1, int(fragments))),
        "--retries",
        "10",
        "--fragment-retries",
        "10",
    ]
    # YouTube requires a PO token for anything above 360p. yt-dlp mints one by
    # running its BotGuard JavaScript, which needs a JS runtime present. Without
    # this every client is silently degraded to 360p on a flagged IP.
    if shutil.which("node"):
        cmd[1:1] = ["--js-runtimes", "node"]
    return cmd


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

    Why this is not obvious: YouTube's default formats are fragmented DASH mp4
    served as one huge file. ``--download-sections`` hands those to ffmpeg,
    which cannot seek a fragmented mp4, so it silently downloads from byte 0 —
    minutes of random data before the requested moment, which is what made long
    VODs stall. The **HLS (``m3u8``) renditions** are segmented, so ffmpeg can
    seek straight to ``start``; pinning AVC HLS plus the native HLS downloader
    (which fetches fragments in parallel) turns a 40 s window into ~35 s of
    transfer at full 1080p instead of tens of minutes.

    Falls back to the DASH/progressive formats for sources without HLS.
    """
    ensure_safe_url(url)
    start = max(0.0, float(start))
    end = max(start + 0.5, float(end))
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        *_ytdlp_base(fragments=8),
        # Seek straight to the moment: AVC HLS first (ffmpeg can seek it and it
        # remuxes cleanly to mp4), then any HLS, then the unseekable fallbacks.
        "--hls-prefer-native",
        "-f",
        (
            "bv*[height<=1080][protocol^=m3u8][vcodec^=avc1]+ba[protocol^=m3u8]"
            "/bv*[height<=1080][protocol^=m3u8]+ba[protocol^=m3u8]"
            "/bv*[height<=1080]+ba/b[height<=1080]/b"
        ),
        "--download-sections",
        f"*{start:.3f}-{end:.3f}",
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


# --------------------------------------------------------------- HLS windows
#
# ``fetch_section`` pipes an ffmpeg ``--download-sections`` call, and ffmpeg
# reads HLS fragments one after another. On a throttled connection (each
# connection capped, aggregate not) that is the difference between a 40 s
# window taking 30 s and taking 10 minutes. YouTube serves every rendition as
# plain 6 s MPEG-TS fragments, so the fast path is: read the media playlist,
# take only the fragments covering the window, fetch them concurrently, then
# concatenate and remux with stream copy. Video quality is untouched.

# ``(fetched_at, video_segments, audio_segments)`` keyed by source URL.
_HLS_CACHE: dict[str, tuple[float, list[tuple[float, str]], list[tuple[float, str]]]] = {}
# Sources with no usable fragment playlist, so the slow path is not re-tried
# on every clip of the same project.
_HLS_FAILED: dict[str, float] = {}
_HLS_TTL_SECONDS = 2 * 3600
_HLS_FAIL_TTL_SECONDS = 1800
_HLS_WORKERS = 16
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _capture_ytdlp(cmd: list[str], *, timeout: int = 120) -> str:
    """Run yt-dlp and return stdout (used to resolve playlist URLs)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise media.MediaError(f"missing binary: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise media.MediaError(f"yt-dlp timed out after {timeout}s") from exc
    if proc.returncode != 0:
        detail = " | ".join((proc.stderr or proc.stdout or "").strip().splitlines()[-8:])
        raise media.MediaError(f"yt-dlp failed ({proc.returncode}): {detail}")
    return proc.stdout


def _fetch_text(url: str, *, timeout: int = 120) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - vetted media host
        return response.read().decode("utf-8", "replace")


def _parse_manifest(text: str) -> list[tuple[float, str]]:
    """Return ``[(duration, fragment_url)]`` from an HLS media playlist."""
    segments: list[tuple[float, str]] = []
    duration = 0.0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                duration = 0.0
        elif line and not line.startswith("#"):
            segments.append((duration, line))
    return segments


def _hls_streams(source_url: str) -> tuple[list[tuple[float, str]], list[tuple[float, str]]]:
    """Resolve and parse the best HLS video/audio playlists for ``source_url``.

    The default client is enough most days, but a flagged IP gets the "confirm
    you're not a bot" wall on it. yt-dlp tries several player clients when asked
    to, and the ``tv``/``web_safari`` pair usually still serves the full 1080p
    HLS ladder, so a ladder of client sets is tried in turn before giving up.
    """
    cached = _HLS_CACHE.get(source_url)
    if cached and time.monotonic() - cached[0] < _HLS_TTL_SECONDS:
        return cached[1], cached[2]
    failed_at = _HLS_FAILED.get(source_url)
    if failed_at and time.monotonic() - failed_at < _HLS_FAIL_TTL_SECONDS:
        raise media.MediaError("no HLS playlist for this source (cached)")
    selector = (
        "bv*[height<=1080][protocol^=m3u8][vcodec^=avc1]+ba[protocol^=m3u8]"
        "/bv*[height<=1080][protocol^=m3u8]+ba[protocol^=m3u8]"
    )
    base = [*_ytdlp_base(), "--skip-download", "-g", "-f", selector, source_url]
    clients = ("", "tv,web_safari,default", "web_safari,tv,android", "android,web_safari")
    youtube = source_kind_for(source_url) == "youtube"
    manifests: list[str] = []
    last_error: Exception | None = None
    for client in clients:
        args = [*base]
        if client and youtube:
            args[1:1] = ["--extractor-args", f"youtube:player_client={client}"]
        try:
            out = _capture_ytdlp(args)
        except media.MediaError as exc:
            last_error = exc
            continue
        manifests = [line.strip() for line in out.splitlines() if line.strip().startswith("http")]
        if manifests:
            break
    if not manifests:
        _HLS_FAILED[source_url] = time.monotonic()
        raise media.MediaError(f"no HLS playlist for this source: {last_error}")
    video = _parse_manifest(_fetch_text(manifests[0]))
    audio = _parse_manifest(_fetch_text(manifests[1])) if len(manifests) > 1 else []
    if not video:
        _HLS_FAILED[source_url] = time.monotonic()
        raise media.MediaError("empty HLS video playlist")
    _HLS_FAILED.pop(source_url, None)
    _HLS_CACHE[source_url] = (time.monotonic(), video, audio)
    return video, audio


def _cumulative_starts(segments: list[tuple[float, str]]) -> list[float]:
    starts: list[float] = []
    clock = 0.0
    for duration, _ in segments:
        starts.append(clock)
        clock += duration
    return starts


def _segment_range(
    segments: list[tuple[float, str]], start: float, end: float
) -> tuple[int, int, float]:
    """Fragments covering ``[start, end]`` plus one of tail headroom.

    Returns ``(first_index, last_index, fragment_start_seconds)``.
    """
    starts = _cumulative_starts(segments)
    total = starts[-1] + segments[-1][0]
    start = max(0.0, min(float(start), max(0.0, total - 0.5)))
    end = min(max(float(end), start + 0.2), total)
    first = 0
    for index, begin in enumerate(starts):
        if begin <= start:
            first = index
        else:
            break
    last = len(segments) - 1
    for index, (duration, _) in enumerate(segments):
        if starts[index] + duration >= end:
            last = index
            break
    last = min(last + 1, len(segments) - 1)
    return first, last, starts[first]


def _download_segment(url: str, path: Path, *, attempts: int = 3) -> None:
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=120) as response:  # noqa: S310
                data = response.read()
            if data:
                path.write_bytes(data)
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1 + attempt)
    raise media.MediaError("HLS fragment fetch failed")


def _fetch_fragments(
    segments: list[tuple[float, str]],
    first: int,
    last: int,
    directory: Path,
    *,
    on_progress: ProgressFn | None = None,
    done_base: int = 0,
    done_total: int = 1,
) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    indices = list(range(first, last + 1))
    paths = {index: directory / f"{index:07d}.ts" for index in indices}
    failures = 0
    completed = 0
    with ThreadPoolExecutor(max_workers=min(_HLS_WORKERS, len(indices))) as pool:
        futures = {
            pool.submit(_download_segment, segments[index][1], paths[index]): index
            for index in indices
        }
        for future in as_completed(futures):
            completed += 1
            try:
                future.result()
            except media.MediaError:
                failures += 1
            if on_progress:
                fraction = (done_base + completed) / max(1, done_total)
                on_progress(min(0.999, fraction), "Fetching fragments")
    if failures:
        raise media.MediaError(f"{failures} HLS fragments failed")
    return [paths[index] for index in indices]


def fetch_hls_window(
    url: str,
    start: float,
    end: float,
    target: Path,
    *,
    on_progress: ProgressFn | None = None,
) -> tuple[Path, float]:
    """Fetch ``[start, end]`` by pulling only its HLS fragments concurrently.

    Returns ``(path, offset)`` where ``offset`` is the source timestamp of the
    file's first frame. The file keeps a few seconds of headroom on either side
    because fragments are 6 s; the renderer seeks within it to a frame-accurate
    cut, so no second generation is spent trimming here.
    """
    ensure_safe_url(url)
    start = max(0.0, float(start))
    end = max(start + 0.5, float(end))
    target.parent.mkdir(parents=True, exist_ok=True)

    def attempt() -> tuple[Path, float]:
        video, audio = _hls_streams(url)
        first, last, offset = _segment_range(video, start, end)
        a_first, a_last, _ = _segment_range(audio, start, end) if audio else (first, last, offset)
        total = (last - first + 1) + (a_last - a_first + 1 if audio else 0)
        temp = target.parent / f".{target.stem}.hls"
        video_parts = _fetch_fragments(
            video, first, last, temp / "v", on_progress=on_progress,
            done_base=0, done_total=total,
        )
        audio_parts = (
            _fetch_fragments(
                audio, a_first, a_last, temp / "a", on_progress=on_progress,
                done_base=(last - first + 1), done_total=total,
            )
            if audio
            else []
        )
        if on_progress:
            on_progress(0.92, "Joining fragments")
        video_ts = temp / "video.ts"
        video_ts.write_bytes(b"".join(part.read_bytes() for part in video_parts))
        cmd = [get_settings().ffmpeg, "-y", "-v", "error", "-i", str(video_ts)]
        if audio_parts:
            audio_ts = temp / "audio.ts"
            audio_ts.write_bytes(b"".join(part.read_bytes() for part in audio_parts))
            cmd += ["-i", str(audio_ts), "-map", "0:v:0", "-map", "1:a:0"]
        cmd += ["-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(target)]
        media.run(cmd)
        shutil.rmtree(temp, ignore_errors=True)
        if not target.exists() or target.stat().st_size == 0:
            raise media.MediaError("HLS window produced no file")
        if on_progress:
            on_progress(1.0, "Moment ready")
        return target, offset

    had_cache = url in _HLS_CACHE
    try:
        return attempt()
    except media.MediaError:
        # A cached playlist expires every few hours; a stale cache is the usual
        # cause of a mid-run 403. When the very first resolve fails there is
        # nothing stale to refresh, so do not pay for the same failure twice.
        _HLS_FAILED.pop(url, None)
        if not had_cache:
            raise
        _HLS_CACHE.pop(url, None)
        return attempt()


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