"""Source ingestion: YouTube / Twitch / Kick / direct URLs, and local files."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .. import media, storage
from ..config import get_settings
from ..net_guard import ensure_safe_url

log = logging.getLogger("clipbait.ingest")


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
    return [settings.ytdlp, "--no-playlist", "--no-warnings", "--newline"]


def fetch_remote(url: str, project_id: str) -> IngestResult:
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
    proc = media.run(cmd, timeout=None)
    log.debug("yt-dlp: %s", (proc.stdout or "")[-500:])

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