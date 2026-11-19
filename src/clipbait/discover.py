"""Find live streams worth clipping, ranked by the audience already watching.

The hard part of live clipping is not the cut, it is being in the right room at
the right time. A stream with 50k concurrent viewers turns one good moment into
a distribution event; the same moment on a 40-viewer stream earns nothing.

YouTube is the reliable source here because ``yt-dlp`` can read it without an
API key. Search is filtered to live results and then sorted by the viewer count
YouTube itself reports, so nothing is invented.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import quote_plus

from .config import get_settings

log = logging.getLogger("clipbait.discover")

# YouTube's "live now" search filter. Percent-encoded twice in a URL that itself
# gets quoted, so keep it as the raw token and quote once.
LIVE_FILTER = "EgJAAQ%3D%3D"
SEARCH_URL = "https://www.youtube.com/results?search_query={query}&sp=" + LIVE_FILTER


class DiscoverError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveStream:
    platform: str
    channel: str
    title: str
    url: str
    viewers: int
    live_for: str = ""
    stream_id: str = ""
    thumbnail: str = ""

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "channel": self.channel,
            "title": self.title,
            "url": self.url,
            "viewers": self.viewers,
            "live_for": self.live_for,
            "stream_id": self.stream_id,
            "thumbnail": self.thumbnail,
        }


def _runner(args: list[str], timeout: int) -> dict:
    """Run yt-dlp and parse one JSON blob. The seam every test replaces."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise DiscoverError(f"missing binary: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DiscoverError(f"yt-dlp timed out after {timeout}s") from exc
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        tail = " | ".join((proc.stderr or "").strip().splitlines()[-3:])
        raise DiscoverError(f"yt-dlp failed ({proc.returncode}): {tail}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise DiscoverError("yt-dlp returned invalid JSON") from exc


def _base() -> list[str]:
    settings = get_settings()
    return [
        settings.ytdlp,
        "--no-warnings",
        "--no-check-certificates",
        "--socket-timeout",
        "15",
    ]


def fmt_duration(seconds: float | int | None) -> str:
    try:
        value = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    hours, rest = divmod(value, 3600)
    minutes = rest // 60
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _entry_viewers(entry: dict) -> int:
    for key in ("concurrent_view_count", "view_count"):
        value = entry.get(key)
        try:
            if value is not None:
                return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _live_for(entry: dict) -> str:
    if entry.get("live_for"):
        return str(entry["live_for"])
    for key in ("release_timestamp", "timestamp"):
        start = entry.get(key)
        if isinstance(start, (int, float)) and start > 0:
            return fmt_duration(time.time() - start)
    return ""


def _to_stream(entry: dict, *, require_live: bool = True) -> LiveStream | None:
    if not entry:
        return None
    if require_live and not entry.get("is_live"):
        return None
    stream_id = str(entry.get("id") or "")
    url = str(entry.get("webpage_url") or entry.get("url") or "")
    if not url and stream_id:
        url = f"https://www.youtube.com/watch?v={stream_id}"
    if not url:
        return None
    channel = str(entry.get("channel") or entry.get("uploader") or "")
    return LiveStream(
        platform="YouTube",
        channel=channel,
        title=str(entry.get("title") or ""),
        url=url,
        viewers=_entry_viewers(entry),
        live_for=_live_for(entry),
        stream_id=stream_id,
        thumbnail=str(entry.get("thumbnail") or ""),
    )


def search_live(
    query: str = "gta 6",
    limit: int = 20,
    *,
    resolve: bool = True,
    timeout: int = 60,
    runner=_runner,
) -> list[LiveStream]:
    """Search YouTube's live filter, then sort by concurrent viewers.

    Search results are only candidates: the flat playlist omits both
    ``is_live`` and ``concurrent_view_count``, so each candidate is resolved
    against its watch page before it is ranked. That is a handful of requests
    and it is what makes the ordering real instead of alphabetical.

    A candidate whose watch page cannot be read is kept (the search was already
    filtered to live) with zero known viewers, so it ranks last rather than
    silently disappearing.
    """
    limit = max(1, min(100, int(limit)))
    url = SEARCH_URL.format(query=quote_plus(query))
    args = _base() + ["--flat-playlist", "-J", "--playlist-end", str(limit), url]
    try:
        payload = runner(args, timeout)
    except DiscoverError:
        raise
    except Exception as exc:  # pragma: no cover - defensive around a subprocess seam
        raise DiscoverError(str(exc)) from exc

    entries = [entry for entry in (payload.get("entries") or []) if isinstance(entry, dict)]
    candidates: list[LiveStream] = []
    for entry in entries:
        if entry.get("is_live") is False:  # an explicit "not live" is trustworthy
            continue
        stream = _to_stream(entry, require_live=False)
        if stream:
            candidates.append(stream)
    if not candidates:
        return []
    if not resolve:
        candidates.sort(key=lambda stream: (stream.viewers, stream.channel), reverse=True)
        return candidates[:limit]

    def one(stream: LiveStream) -> LiveStream | None:
        watch = f"https://www.youtube.com/watch?v={stream.stream_id}"
        try:
            detail = runner(_base() + ["-J", "--no-playlist", watch], timeout)
        except Exception as exc:  # a dead stream must not sink the list
            log.debug("resolve failed for %s: %s", stream.stream_id, exc)
            return stream
        if not detail or not detail.get("is_live"):
            return None
        return _to_stream(detail, require_live=False)

    with ThreadPoolExecutor(max_workers=6) as pool:
        resolved = list(pool.map(one, candidates))
    streams = [stream for stream in resolved if stream]
    streams.sort(key=lambda stream: (stream.viewers, stream.channel), reverse=True)
    return streams[:limit]


def best(streams: list[LiveStream]) -> LiveStream | None:
    return streams[0] if streams else None


def as_markdown(query: str, streams: list[LiveStream], *, stamp: str | None = None) -> str:
    when = stamp or time.strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Live streams: {query}",
        "",
        f"Checked {when}. Viewer counts are what YouTube returned at that moment.",
        "",
    ]
    if not streams:
        lines.append("No live streams found. Try a broader query or a different hour.")
        return "\n".join(lines) + "\n"
    lines += ["| # | Channel | Title | Viewers | Live for | URL |", "|---|---|---|---|---|---|"]
    for index, stream in enumerate(streams, start=1):
        title = stream.title.replace("|", "/")
        lines.append(
            f"| {index} | {stream.channel} | {title} | {stream.viewers:,} | "
            f"{stream.live_for} | {stream.url} |"
        )
    return "\n".join(lines) + "\n"