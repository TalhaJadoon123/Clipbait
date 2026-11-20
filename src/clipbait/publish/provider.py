"""Talk to a publishing provider.

One narrow interface: hand a rendered file plus its copy to a provider and get
back a result. Upload-Post is the first provider because one multipart request
already reaches TikTok, Instagram, YouTube, LinkedIn, X, Facebook and more, and
because it exposes the *measured* best posting hours for the connected account.

The module never posts on its own. ``dry_run`` lives in the caller; here every
method is just HTTP. No credential is ever invented or defaulted: without an API
key the provider reports ``ready == False`` and the caller says so in plain
words instead of failing mid-upload.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# Clipbait's internal platform names are the destinations a clip is *shaped*
# for; Upload-Post's names are the networks a clip is *sent* to. Keeping the
# map in one place stops "reels" being posted as a network that does not exist.
PLATFORM_MAP: dict[str, str] = {
    "tiktok": "tiktok",
    "reels": "instagram",
    "instagram": "instagram",
    "shorts": "youtube",
    "youtube": "youtube",
    "linkedin": "linkedin",
    "x": "x",
    "twitter": "x",
    "facebook": "facebook",
    "threads": "threads",
}

DEFAULT_BASE_URL = "https://api.upload-post.com"


def upload_platform(platform: str | None) -> str:
    """Map a Clipbait destination to the network Upload-Post publishes to."""
    return PLATFORM_MAP.get((platform or "").strip().lower(), "tiktok")


class ProviderError(RuntimeError):
    """A publishing call failed, with what the provider said about why."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str = "",
        body: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body or {}

    @property
    def reconnect_required(self) -> bool:
        return self.code in {"reconnect_required", "account_not_connected"}


@dataclass(frozen=True)
class PublishResult:
    """What happened to one clip, in a shape both the CLI and the API can print."""

    ok: bool = False
    dry_run: bool = True
    clip_id: str = ""
    platform: str = ""
    title: str = ""
    scheduled_date: str = ""
    timezone: str = ""
    source: str = ""
    job_id: str = ""
    post_id: str = ""
    url: str = ""
    error: str = ""
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "clip_id": self.clip_id,
            "platform": self.platform,
            "title": self.title,
            "scheduled_date": self.scheduled_date,
            "timezone": self.timezone,
            "source": self.source,
            "job_id": self.job_id,
            "post_id": self.post_id,
            "url": self.url,
            "error": self.error,
            "metadata": self.metadata,
        }


def _json_body(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = {"raw": response.text[:500]}
    return payload if isinstance(payload, dict) else {"data": payload}


def _error_message(payload: dict, response: httpx.Response) -> tuple[str, str]:
    code = str(payload.get("error_code") or payload.get("code") or "")
    message = str(
        payload.get("message")
        or payload.get("error")
        or payload.get("detail")
        or response.text[:200]
        or f"HTTP {response.status_code}"
    )
    return message, code


class UploadPostProvider:
    """Upload-Post REST client: publish a video, ask for the best hours."""

    name = "upload-post"
    # Upload-Post addresses a *profile* inside the account, so a run needs a
    # username. Postiz addresses a connected channel directly and does not.
    requires_profile = True

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        user: str = "",
        timeout: float = 180.0,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.user = (user or "").strip()
        self.timeout = timeout

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Apikey {self.api_key}"}

    def best_times(
        self,
        platform: str,
        *,
        profile_username: str | None = None,
        timezone: str = "UTC",
        top: int = 3,
    ) -> dict:
        """Measured best hours for a connected account.

        Raises :class:`ProviderError` when the account is not connected or the
        network is out of quota, so the caller can fall back to the region
        playbook instead of guessing hours the account never earned.
        """
        if not self.ready:
            raise ProviderError("no Upload-Post API key configured")
        profile = (profile_username or self.user).strip()
        if not profile:
            raise ProviderError("no Upload-Post profile username configured")
        params = {
            "platform": upload_platform(platform),
            "profile_username": profile,
            "timezone": timezone or "UTC",
            "top": max(1, min(24, int(top))),
        }
        try:
            response = httpx.get(
                f"{self.base_url}/api/uploadposts/best-times",
                headers=self._headers(),
                params=params,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:  # network is the operator's problem too
            raise ProviderError(f"best-times request failed: {exc}") from exc
        payload = _json_body(response)
        if response.status_code >= 400:
            message, code = _error_message(payload, response)
            raise ProviderError(
                message, status=response.status_code, code=code, body=payload
            )
        return payload

    def upload(
        self,
        *,
        video: Path,
        fields: dict,
        platforms,
        user: str | None = None,
        scheduled_date: str | None = None,
        timezone: str | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Publish one video. Returns the parsed response, raises on refusal."""
        if not self.ready:
            raise ProviderError("no Upload-Post API key configured")
        profile = (user or self.user).strip()
        if not profile:
            raise ProviderError("no Upload-Post profile username configured")
        video = Path(video)
        if not video.exists():
            raise ProviderError(f"render is missing: {video}")

        data: dict[str, object] = {"user": profile, "platform[]": list(platforms)}
        data.update({key: value for key, value in fields.items() if value not in (None, "")})
        if scheduled_date:
            data["scheduled_date"] = scheduled_date
        if timezone and scheduled_date:
            data["timezone"] = timezone

        headers = self._headers()
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        try:
            with video.open("rb") as handle:
                files = {"video": (video.name, handle, "video/mp4")}
                response = httpx.post(
                    f"{self.base_url}/api/upload",
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=timeout or self.timeout,
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"upload request failed: {exc}") from exc

        payload = _json_body(response)
        if response.status_code >= 400:
            message, code = _error_message(payload, response)
            raise ProviderError(
                message, status=response.status_code, code=code, body=payload
            )
        return payload
