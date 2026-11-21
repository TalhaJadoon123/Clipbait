"""Publish through a self-hosted Postiz instance.

Postiz is the open-source alternative to a metered posting API: one install,
unlimited posts, no per-upload fee. It exposes a small public API -- upload a
file, then create a post that names the integration (connected channel) to send
it to.

This provider speaks that API and answers in the same shape the Upload-Post
provider does, so the publish service can drive either backend without knowing
which one is behind it. Nothing here posts on its own: without an API key the
provider reports ``ready == False`` and the caller says so in plain words.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx

from .provider import ProviderError, _error_message, _json_body

# Clipbait destination -> Postiz provider ``__type``.
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
    "bluesky": "bluesky",
    "mastodon": "mastodon",
    "telegram": "telegram",
    "reddit": "reddit",
    "pinterest": "pinterest",
}

DEFAULT_API_URL = "http://localhost:4007/api/public/v1"


def postiz_platform(platform: str | None) -> str:
    """Map a Clipbait destination to a Postiz provider type."""
    return PLATFORM_MAP.get((platform or "").strip().lower(), "youtube")


def _tag_objects(tags) -> list[dict]:
    out: list[dict] = []
    for tag in tags or ():
        label = str(tag).lstrip("#").strip()
        if label:
            out.append({"value": label, "label": label})
    return out


def _build_settings(postiz_type: str, fields: dict) -> dict:
    """Provider settings block for one destination, from the planner's fields."""
    if postiz_type == "youtube":
        title = str(fields.get("youtube_title") or fields.get("title") or "").strip()
        return {
            "__type": "youtube",
            "title": (title or "Clip")[:100],
            "type": "public",
            "selfDeclaredMadeForKids": "no",
            "tags": _tag_objects(fields.get("tags")),
        }
    if postiz_type == "tiktok":
        return {
            "__type": "tiktok",
            "title": str(fields.get("title") or "")[:90],
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "duet": True,
            "stitch": True,
            "comment": True,
            "autoAddMusic": "no",
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
            "content_posting_method": "DIRECT_POST",
        }
    if postiz_type == "instagram":
        return {"__type": "instagram", "post_type": "post"}
    if postiz_type == "x":
        return {"__type": "x", "who_can_reply_post": "everyone"}
    return {"__type": postiz_type}


def _content_for(postiz_type: str, fields: dict) -> str:
    if postiz_type == "youtube":
        return str(
            fields.get("youtube_description")
            or fields.get("description")
            or fields.get("title")
            or ""
        )
    return str(fields.get("title") or fields.get("description") or "")


class PostizProvider:
    """Postiz public-API client: upload a file, create a post."""

    name = "postiz"
    # A post names a connected channel id, not a profile inside an account, so
    # the publish service must not demand a profile username for Postiz.
    requires_profile = False

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = DEFAULT_API_URL,
        user: str = "",
        timeout: float = 300.0,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_API_URL).rstrip("/")
        self.user = (user or "").strip()
        self.timeout = timeout
        self._integrations: dict[str, str] | None = None

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self.api_key}

    # ------------------------------------------------------------- channels

    def integrations(self) -> list[dict]:
        if not self.ready:
            raise ProviderError("no Postiz API key configured")
        try:
            response = httpx.get(
                f"{self.base_url}/integrations",
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"integrations request failed: {exc}") from exc
        payload = _json_body(response)
        if response.status_code >= 400:
            message, code = _error_message(payload, response)
            raise ProviderError(message, status=response.status_code, code=code, body=payload)
        return self._integration_items(payload)

    @staticmethod
    def _integration_items(payload) -> list[dict]:
        """Pull the channel list out of whatever envelope the API answered with.

        The public API returns a *bare JSON array*, but ``_json_body`` wraps a
        list as ``{"data": [...]}`` to keep its own shape uniform, and some
        builds nest the list under ``integrations``. Reading only
        ``{"integrations": [...]}`` silently saw the real instance as empty.
        """
        items = payload
        if isinstance(items, dict):
            items = items.get("integrations") or items.get("data") or []
        if isinstance(items, dict):
            items = items.get("integrations") or []
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def _integration_id(self, postiz_type: str) -> str:
        """The connected channel id for a destination, cached across a run."""
        if self._integrations is None:
            found: dict[str, str] = {}
            for item in self.integrations():
                provider = str(
                    item.get("providerIdentifier") or item.get("identifier") or ""
                ).lower()
                ident = str(item.get("id") or "")
                if provider and ident and provider not in found:
                    found[provider] = ident
            self._integrations = found
        ident = self._integrations.get(postiz_type)
        if not ident:
            raise ProviderError(
                f"no {postiz_type} channel connected in Postiz",
                code="account_not_connected",
            )
        return ident

    # ---------------------------------------------------------------- media

    def upload_media(self, path: Path, *, timeout: float | None = None) -> dict:
        path = Path(path)
        if not path.exists():
            raise ProviderError(f"render is missing: {path}")
        try:
            with path.open("rb") as handle:
                response = httpx.post(
                    f"{self.base_url}/upload",
                    headers=self._headers(),
                    files={"file": (path.name, handle, "video/mp4")},
                    timeout=timeout or self.timeout,
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"media upload failed: {exc}") from exc
        payload = _json_body(response)
        if response.status_code >= 400:
            message, code = _error_message(payload, response)
            raise ProviderError(message, status=response.status_code, code=code, body=payload)
        if not payload.get("id") or not payload.get("path"):
            raise ProviderError("upload returned no media id/path", body=payload)
        return payload

    # ---------------------------------------------------------------- posts

    def create_post(self, body: dict, *, timeout: float | None = None) -> list[dict]:
        try:
            response = httpx.post(
                f"{self.base_url}/posts",
                headers={**self._headers(), "Content-Type": "application/json"},
                json=body,
                timeout=timeout or self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"create-post request failed: {exc}") from exc
        payload = response.json() if response.content else []
        if response.status_code >= 400:
            message, code = _error_message(
                payload if isinstance(payload, dict) else {"data": payload}, response
            )
            raise ProviderError(message, status=response.status_code, code=code)
        return payload if isinstance(payload, list) else [payload]

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
        """Upload the file, then create a post for each destination.

        Returns a dict in the same shape Upload-Post answers with -- ``success``
        plus a per-network ``results`` map -- so the service reads one format.
        """
        if not self.ready:
            raise ProviderError("no Postiz API key configured")

        wanted = list(dict.fromkeys(postiz_platform(p) for p in (platforms or ("shorts",))))
        # Resolve every channel before uploading: a missing channel should fail
        # fast, not after a tens-of-megabytes body is already on the wire.
        targets = [(postiz_type, self._integration_id(postiz_type)) for postiz_type in wanted]

        media = self.upload_media(video, timeout=timeout)
        posts = [
            {
                "integration": {"id": integration_id},
                "value": [
                    {
                        "content": _content_for(postiz_type, fields),
                        "image": [{"id": media["id"], "path": media["path"]}],
                    }
                ],
                "settings": _build_settings(postiz_type, fields),
            }
            for postiz_type, integration_id in targets
        ]

        body = {
            "type": "schedule" if scheduled_date else "now",
            # Postiz still expects a real timestamp on a "now" post; an epoch
            # date reads as a long-past schedule. Send the current instant.
            "date": scheduled_date or datetime.now(UTC).isoformat(),
            "shortLink": False,
            "tags": [],
            "posts": posts,
        }
        created = self.create_post(body, timeout=timeout)

        by_integration = {
            str(item.get("integration") or ""): str(item.get("postId") or "")
            for item in created
            if isinstance(item, dict)
        }
        results: dict[str, dict] = {}
        first_post_id = ""
        for postiz_type, integration_id in targets:
            post_id = by_integration.get(integration_id, "")
            first_post_id = first_post_id or post_id
            results[postiz_type] = {
                "success": True,
                "post_id": post_id,
                "url": "",
                "status": "queued",
                "integration_id": integration_id,
            }
        return {
            "success": True,
            "post_id": first_post_id,
            "job_id": "",
            "url": "",
            "results": results,
        }

    def best_times(self, platform: str, **kwargs) -> dict:
        """Postiz has no measured-best-hours endpoint; fall back to the playbook."""
        raise ProviderError("Postiz has no best-times endpoint")
