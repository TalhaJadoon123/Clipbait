"""Meme video generation.

The interesting path is ``ApiMemeProvider``: a text prompt goes to a hosted
text-to-video endpoint and a short vertical clip comes back. Because provider
schemas differ and the network is not trustworthy, the client is deliberately
forgiving: it accepts the first ``http`` URL it can find in the response, polls
if the response is asynchronous, and treats every failure as "no insert" rather
than a failed render.

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
    """Pick a provider: hosted API, local fixture, or nothing.

    ``CLIPBAIT_MEME_PROVIDER`` may force ``api``, ``fixture`` or ``none``.
    With ``auto`` an API key wins; otherwise the local fixture is used so the
    feature works out of the box.
    """
    settings = settings or get_settings()
    preference = (settings.meme_provider or "auto").lower()
    if preference == "none":
        return None
    if preference == "api":
        return ApiMemeProvider(settings)
    if preference == "fixture":
        return FixtureMemeProvider(settings)
    if settings.meme_api_url:
        return ApiMemeProvider(settings)
    return FixtureMemeProvider(settings)


def provider_name(provider: MemeProvider | None) -> str | None:
    return getattr(provider, "name", None) if provider else None


__all__ = [
    "ApiMemeProvider",
    "FixtureMemeProvider",
    "MemeError",
    "MemeProvider",
    "MemeRequest",
    "cache_dir",
    "get_meme_provider",
    "provider_name",
]
