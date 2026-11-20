"""Meme video generation.

The interesting path is ``ApiMemeProvider``: a text prompt goes to a hosted
text-to-video endpoint and a short vertical clip comes back. Because provider
schemas differ and the network is not trustworthy, the client is deliberately
forgiving: it accepts the first ``http`` URL it can find in the response, polls
if the response is asynchronous, and treats every failure as "no insert" rather
than a failed render.

``ApiMemeProvider`` generates an insert from a prompt. ``HttpMemeProvider``
searches a meme *library* the user already has access to (Giphy, Tenor, or their
own catalogue) and downloads a matching video instead. Both speak plain HTTP and
are configured entirely by environment variables.

``FixtureMemeProvider`` renders a labelled card locally with ffmpeg. It exists so
the feature is usable, and testable, with no API key at all - the same
zero-key philosophy as the mock transcriber. It is always labelled synthetic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from .. import media
from ..config import Settings, get_settings
from ..net_guard import ensure_safe_url

log = logging.getLogger("clipbait.memes")

# Hard ceiling on a downloaded insert; a generator that returns a full movie is
# a bug or a hostile response, not a b-roll clip.
MAX_DOWNLOAD_BYTES = 120 * 1024 * 1024

# Words that make a useless library query; the insert prompt is spoken English,
# not a tag list, so this is a small hand-tuned list rather than an NLP stoplist.
_QUERY_STOPWORDS = {
    "and", "the", "that", "this", "then", "with", "from", "for", "you", "your",
    "but", "was", "were", "are", "its", "his", "her", "they", "them", "have",
    "has", "had", "not", "what", "when", "how", "who", "why", "all", "just",
    "can", "will", "would", "could", "should", "into", "about", "there", "here",
}


class MemeError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemeRequest:
    prompt: str
    duration: float
    width: int
    height: int
    fps: int = 30

    @property
    def cache_key(self) -> str:
        basis = (
            f"{self.prompt}|{self.duration:.2f}|{self.width}x{self.height}|{self.fps}"
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


class MemeProvider(Protocol):
    name: str

    def generate(self, request: MemeRequest, dest_dir: Path) -> Path | None: ...


def _find_url(payload: object) -> str | None:
    """First http(s) URL anywhere in a JSON-ish payload."""
    if isinstance(payload, str):
        return payload if payload.startswith(("http://", "https://")) else None
    if isinstance(payload, dict):
        for key in ("video_url", "url", "output", "result", "video", "asset_url"):
            found = _find_url(payload.get(key))
            if found:
                return found
        for value in payload.values():
            found = _find_url(value)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _find_url(item)
            if found:
                return found
    return None


def _status_done(payload: object) -> bool:
    if isinstance(payload, dict):
        status = str(payload.get("status") or payload.get("state") or "").lower()
        if status in {"succeeded", "success", "completed", "complete", "ready", "done"}:
            return True
        if status in {"failed", "error", "canceled", "cancelled"}:
            raise MemeError(f"generation failed: {payload.get('error') or status}")
    return False


def _dot_get(payload: object, path: str) -> object:
    """Read ``a.b.0.c`` out of nested JSON without raising on a miss."""
    current: object = payload
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current


def _download_stream(url: str, target: Path) -> None:
    """Stream one video to ``target`` under the SSRF and size guards."""
    ensure_safe_url(url)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".part")
    written = 0
    with httpx.stream("GET", url, timeout=300, follow_redirects=True) as response:
        response.raise_for_status()
        with temp.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 256):
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    handle.close()
                    temp.unlink(missing_ok=True)
                    raise MemeError("meme download exceeded the size limit")
                handle.write(chunk)
    temp.replace(target)


class ApiMemeProvider:
    """Generate an insert through a hosted text-to-video API.

    Configured entirely by environment: ``CLIPBAIT_MEME_API_URL`` (required),
    ``CLIPBAIT_MEME_API_KEY`` (sent as a bearer token), ``CLIPBAIT_MEME_MODEL``.
    The response may be synchronous (a URL in the body) or asynchronous (an id
    that is polled at ``<url>/<id>``).
    """

    name = "api"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def url(self) -> str | None:
        return self.settings.meme_api_url

    def available(self) -> bool:
        return bool(self.settings.meme_api_url)

    def generate(self, request: MemeRequest, dest_dir: Path) -> Path | None:
        if not self.url:
            raise MemeError("CLIPBAIT_MEME_API_URL is not set")
        target = dest_dir / f"{request.cache_key}.mp4"
        if target.exists() and target.stat().st_size > 0:
            return target

        payload: dict[str, object] = {
            "prompt": request.prompt,
            "duration": round(request.duration, 2),
            "aspect_ratio": "9:16",
            "width": request.width,
            "height": request.height,
            "fps": request.fps,
        }
        if self.settings.meme_model:
            payload["model"] = self.settings.meme_model

        headers = {"content-type": "application/json"}
        if self.settings.meme_api_key:
            headers["authorization"] = f"Bearer {self.settings.meme_api_key}"

        response = httpx.post(self.url, headers=headers, json=payload, timeout=180)
        if response.status_code >= 400:
            raise MemeError(f"meme api {response.status_code}: {response.text[:200]}")
        body = self._json_or_text(response)

        video_url = _find_url(body)
        if not video_url:
            request_id = self._request_id(body)
            if request_id:
                video_url = self._poll(request_id)
        if not video_url:
            raise MemeError("meme api returned no video URL")

        self._download(video_url, target)
        return target

    # ---------------------------------------------------------------- helpers

    def _json_or_text(self, response: httpx.Response) -> object:
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return _find_url(response.text)

    def _request_id(self, body: object) -> str | None:
        if isinstance(body, dict):
            for key in ("request_id", "id", "job_id", "task_id"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    def _poll(self, request_id: str) -> str | None:
        base = (self.url or "").rstrip("/")
        status_url = f"{base}/{request_id}"
        headers = {}
        if self.settings.meme_api_key:
            headers["authorization"] = f"Bearer {self.settings.meme_api_key}"
        deadline = time.monotonic() + self.settings.meme_poll_seconds
        while time.monotonic() < deadline:
            time.sleep(2.0)
            try:
                response = httpx.get(status_url, headers=headers, timeout=60)
            except httpx.HTTPError as exc:
                log.debug("meme poll failed: %s", exc)
                continue
            if response.status_code >= 400:
                continue
            body = self._json_or_text(response)
            if _status_done(body):
                return _find_url(body)
            found = _find_url(body)
            if found:
                return found
        raise MemeError("meme api did not finish in time")

    def _download(self, url: str, target: Path) -> None:
        # The URL comes from a third party, so it gets the same SSRF treatment
        # as any other user-supplied URL.
        _download_stream(url, target)


class HttpMemeProvider:
    """Download an existing meme video from a searchable library.

    This is the "I already have an API" path: the user points
    ``CLIPBAIT_MEME_SEARCH_URL`` at any JSON search endpoint (Giphy, Tenor, or
    their own catalogue) and maps two dot paths:
    ``CLIPBAIT_MEME_SEARCH_RESULTS`` (where the result list lives) and
    ``CLIPBAIT_MEME_SEARCH_VIDEO`` (where each result's video URL lives).
    Everything else - query param, auth style, result limit - is configurable.

    Results are tried in order. A dead CDN, an image-only result or a size-cap
    breach skips to the next candidate, and an empty list raises :class:`MemeError`
    so the pipeline degrades to "no insert".
    """

    name = "search"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def url(self) -> str | None:
        return self.settings.meme_search_url

    def available(self) -> bool:
        return bool(self.settings.meme_search_url)

    def query_for(self, request: MemeRequest) -> str:
        """Turn an insert prompt into a short library search.

        Insert prompts look like ``reaction b-roll for shock: and then he...``.
        The production prefix is dropped, but its signal words ("shock",
        "failure") are kept because they are excellent meme queries; stopwords
        are removed so the search is not literally "and then the".
        """
        head, _, tail = request.prompt.partition(":")
        text = "".join(
            ch if ch.isalnum() or ch in " -'" else " " for ch in f"{head} {tail}"
        ).replace("reaction b-roll", " ")
        words = [
            word
            for word in text.split()
            if len(word) > 2 and word.lower() not in _QUERY_STOPWORDS
        ][:6]
        return " ".join(words) or "reaction"

    def generate(self, request: MemeRequest, dest_dir: Path) -> Path | None:
        if not self.url:
            raise MemeError("CLIPBAIT_MEME_SEARCH_URL is not set")
        target = dest_dir / f"{request.cache_key}.mp4"
        if target.exists() and target.stat().st_size > 0:
            return target

        candidates = self._search(self.query_for(request))
        if not candidates:
            raise MemeError("meme search returned no results")
        for url in candidates:
            try:
                _download_stream(url, target)
            except (httpx.HTTPError, MemeError, OSError) as exc:
                log.debug("meme candidate skipped (%s): %s", url, exc)
                continue
            if target.exists() and target.stat().st_size > 0:
                return target
        raise MemeError("meme search returned no playable video")

    # ---------------------------------------------------------------- helpers

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json"}
        key = self.settings.meme_search_key
        if not key:
            return headers
        style = (self.settings.meme_search_auth or "bearer").lower()
        if style == "bearer":
            headers["authorization"] = f"Bearer {key}"
        elif style == "header":
            headers[self.settings.meme_search_header or "Authorization"] = key
        return headers

    def _params(self, query: str) -> dict[str, str]:
        params = {
            self.settings.meme_search_query_param or "q": query,
            "limit": str(max(1, self.settings.meme_search_limit)),
        }
        if self.settings.meme_search_rating:
            params["rating"] = self.settings.meme_search_rating
        key = self.settings.meme_search_key
        if key and (self.settings.meme_search_auth or "").lower() == "query":
            params[self.settings.meme_search_key_param or "api_key"] = key
        return params

    def _search(self, query: str) -> list[str]:
        url = self.url or ""
        headers = self._headers()
        params = self._params(query)
        # A template URL may already place the query itself, e.g.
        # ``https://api.example.com/memes?term={query}``.
        if "{query}" in url:
            url = url.replace("{query}", query.replace(" ", "+"))
            params.pop(self.settings.meme_search_query_param or "q", None)

        response = httpx.get(url, headers=headers, params=params, timeout=60)
        if response.status_code >= 400:
            raise MemeError(f"meme search {response.status_code}: {response.text[:200]}")
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise MemeError("meme search did not return JSON") from exc

        results = _dot_get(body, self.settings.meme_search_results)
        if isinstance(results, dict):
            # One level of tolerance for {"data": {"results": [...]}} shapes.
            results = _dot_get(results, self.settings.meme_search_results)
        if not isinstance(results, list):
            raise MemeError("meme search response had no result list")

        found: list[str] = []
        paths = self._video_paths()
        for item in results:
            value: str | None = None
            # Try the paths in order: the first that yields a real URL wins, so
            # a high-res asset (e.g. Giphy ``hd.mp4``) is preferred over the
            # small thumbnail when a search returns both.
            for path in paths:
                candidate = _dot_get(item, path)
                if isinstance(candidate, dict):
                    candidate = (
                        candidate.get("url") or candidate.get("mp4") or _find_url(candidate)
                    )
                if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                    value = candidate
                    break
            if value and value not in found:
                found.append(value)
            elif not value and not found:
                # Fall back to any URL in the result, so a slightly different
                # provider shape still contributes a candidate.
                fallback = _find_url(item)
                if fallback:
                    found.append(fallback)
        return found[: max(1, self.settings.meme_search_limit)]

    def _video_paths(self) -> list[str]:
        """The candidate video dot-paths, in preference order.

        ``CLIPBAIT_MEME_SEARCH_VIDEO`` may name one path (``url``) or several,
        best first (``hd.mp4,original.mp4``), so a provider that offers more
        than one resolution contributes the largest it has.
        """
        raw = self.settings.meme_search_video or "url"
        paths = [part.strip() for part in raw.split(",") if part.strip()]
        return paths or ["url"]


class FixtureMemeProvider:
    """Render a labelled placeholder card locally. No network, no key."""

    name = "fixture"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def generate(self, request: MemeRequest, dest_dir: Path) -> Path | None:
        target = dest_dir / f"{request.cache_key}.mp4"
        if target.exists() and target.stat().st_size > 0:
            return target
        dest_dir.mkdir(parents=True, exist_ok=True)
        text = _drawtext_safe(request.prompt[:42])
        base = f"color=c=0x0b0f14:s={request.width}x{request.height}:r={request.fps}"
        label = (
            f"drawtext=text='{text}':fontcolor=0xd7ff3f:fontsize={max(24, request.width // 22)}:"
            f"x=(w-text_w)/2:y=(h-text_h)/2"
        )
        filters = f"{base},drawbox=x=0:y=0:w=iw:h=ih:color=0xd7ff3f@0.08:t=fill,{label}"
        cmd = [
            self.settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", filters, "-t", f"{request.duration:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-an", str(target),
        ]
        try:
            media.run(cmd)
        except media.MediaError:
            # No drawtext in this ffmpeg build: a plain coloured card still
            # exercises the whole insert path.
            fallback = [
                self.settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", base, "-t", f"{request.duration:.2f}",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-an", str(target),
            ]
            media.run(fallback)
        return target


def _drawtext_safe(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in " -_.,!?" else " " for ch in text)
    return " ".join(cleaned.split())[:42] or "clipbait"


def cache_dir(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = settings.data_dir / "memes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_meme_provider(settings: Settings | None = None) -> MemeProvider | None:
    """Pick a provider: search library, generated API, local fixture, or nothing.

    ``CLIPBAIT_MEME_PROVIDER`` may force ``search``, ``api``, ``fixture`` or
    ``none``. With ``auto`` a configured search library wins (it returns real
    memes and needs no generation wait), then a generation API, then the local
    fixture so the feature works out of the box.
    """
    settings = settings or get_settings()
    preference = (settings.meme_provider or "auto").lower()
    if preference == "none":
        return None
    if preference == "search":
        return HttpMemeProvider(settings)
    if preference == "api":
        return ApiMemeProvider(settings)
    if preference == "fixture":
        return FixtureMemeProvider(settings)
    if settings.meme_search_url:
        return HttpMemeProvider(settings)
    if settings.meme_api_url:
        return ApiMemeProvider(settings)
    return FixtureMemeProvider(settings)


def provider_name(provider: MemeProvider | None) -> str | None:
    return getattr(provider, "name", None) if provider else None


__all__ = [
    "ApiMemeProvider",
    "FixtureMemeProvider",
    "HttpMemeProvider",
    "MemeError",
    "MemeProvider",
    "MemeRequest",
    "cache_dir",
    "get_meme_provider",
    "provider_name",
]
