"""Publishing through a self-hosted Postiz instance.

Postiz is the unmetered replacement for the metered Upload-Post API. The
provider talks to three endpoints -- list channels, upload a file, create a post
-- so every one of them is stubbed at the ``httpx`` seam. The service test is
end to end: a live Postiz send marks the clip posted and records the post id
without ever needing a profile username.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from clipbait import db, storage
from clipbait.config import get_settings
from clipbait.publish import (
    PostizProvider,
    ProviderError,
    PublishOptions,
    UploadPostProvider,
    publish_clip,
    publish_provider,
)
from clipbait.publish import postiz as postiz_module

# ------------------------------------------------------------------ fixtures


def make_ready(*, platforms: tuple[str, ...] = ()) -> dict:
    """A rendered, ready clip in the money queue with a YouTube destination."""
    project = db.create_project(
        {
            "title": "Postiz project",
            "source_kind": "local",
            "duration": 300.0,
            "options": {"region": "US", "platform": "shorts", "language": "en"},
        }
    )
    clip = db.create_clip(
        {
            "project_id": project["id"],
            "title": "He lost it on stream",
            "hook": "He lost it on stream",
            "description": "the part nobody expected",
            "hashtags": ["#shorts", "#gta6"],
            "start": 12.0,
            "end": 44.0,
            "score": 74,
            "status": "ready",
            "meta": {
                "region": "US",
                "platform": "shorts",
                "forecast": {"overall": 74.0, "best_time": "18:00-22:00", "reasons": ["hook"]},
            },
        }
    )
    path = storage.project_dir(project["id"]) / "clips" / f"{clip['id']}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
    return db.update("clips", clip["id"], {"render_path": storage.relative(path)}) or clip


class FakeResponse:
    def __init__(self, status: int, payload, *, raw: bytes | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)
        self.content = raw if raw is not None else self.text.encode()

    def json(self):
        return self._payload


class PostizApi:
    """A tiny stand-in for a Postiz instance, recording every request."""

    def __init__(self, *, channels=None, upload=None, posts=None) -> None:
        self.channels = channels if channels is not None else [
            {"id": "int_yt", "providerIdentifier": "youtube"}
        ]
        self.upload_payload = (
            upload if upload is not None else {"id": "media_1", "path": "https://cdn/media_1.mp4"}
        )
        self.posts_payload = (
            posts if posts is not None else [{"integration": "int_yt", "postId": "post_77"}]
        )
        self.uploads: list[dict] = []
        self.posts: list[dict] = []

    def get(self, url, *, headers, params=None, timeout=None):
        assert url.endswith("/integrations")
        assert headers["Authorization"] == "pz_secret"
        return FakeResponse(200, {"integrations": self.channels})

    def post(self, url, *, headers, files=None, json=None, data=None, timeout=None):
        if url.endswith("/upload"):
            self.uploads.append({"files": files, "headers": headers})
            return FakeResponse(200, self.upload_payload)
        assert url.endswith("/posts")
        self.posts.append(json)
        return FakeResponse(200, self.posts_payload)


# ------------------------------------------------------------------ factory


def test_publish_provider_selects_postiz_from_config() -> None:
    settings = replace(
        get_settings(),
        publish_provider="postiz",
        postiz_api_key="pz_secret",
        postiz_api_url="http://box:4007/api/public/v1",
    )
    provider = publish_provider(settings)
    assert isinstance(provider, PostizProvider)
    assert provider.api_key == "pz_secret"
    assert provider.base_url == "http://box:4007/api/public/v1"
    assert provider.ready is True


def test_publish_provider_defaults_to_upload_post() -> None:
    settings = replace(get_settings(), publish_provider="upload_post", upload_post_api_key="k")
    assert isinstance(publish_provider(settings), UploadPostProvider)


def test_unknown_provider_falls_back_instead_of_failing_live() -> None:
    settings = replace(get_settings(), publish_provider="mystery", upload_post_api_key="k")
    assert isinstance(publish_provider(settings), UploadPostProvider)


def test_postiz_is_not_ready_without_a_key() -> None:
    assert PostizProvider(None).ready is False
    assert PostizProvider("").ready is False
    assert PostizProvider.requires_profile is False


def test_postiz_best_times_is_unavailable() -> None:
    with pytest.raises(ProviderError):
        PostizProvider("pz_secret").best_times("shorts")


# ------------------------------------------------------------------ transport


def test_postiz_upload_uploads_the_file_then_creates_a_post(monkeypatch, tmp_path: Path) -> None:
    api = PostizApi()
    monkeypatch.setattr(postiz_module.httpx, "get", api.get)
    monkeypatch.setattr(postiz_module.httpx, "post", api.post)

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"data")

    payload = PostizProvider("pz_secret").upload(
        video=video,
        fields={
            "youtube_title": "He lost it",
            "youtube_description": "full caption\n#shorts #gta6",
            "tags": ["#shorts", "#gta6"],
        },
        platforms=["shorts"],
        scheduled_date=None,
    )

    # The file went up as multipart with its media id carried into the post.
    assert api.uploads[0]["files"]["file"][2] == "video/mp4"
    body = api.posts[0]
    assert body["type"] == "now"  # no schedule -> publish immediately
    assert len(body["posts"]) == 1
    target = body["posts"][0]
    assert target["integration"]["id"] == "int_yt"
    assert target["value"][0]["content"] == "full caption\n#shorts #gta6"
    assert target["value"][0]["image"][0]["id"] == "media_1"
    assert target["settings"]["__type"] == "youtube"
    assert target["settings"]["title"] == "He lost it"
    assert target["settings"]["type"] == "public"
    assert [t["value"] for t in target["settings"]["tags"]] == ["shorts", "gta6"]

    assert payload["success"] is True
    assert payload["post_id"] == "post_77"
    assert payload["results"]["youtube"]["post_id"] == "post_77"


def test_postiz_schedules_when_a_date_is_given(monkeypatch, tmp_path: Path) -> None:
    api = PostizApi()
    monkeypatch.setattr(postiz_module.httpx, "get", api.get)
    monkeypatch.setattr(postiz_module.httpx, "post", api.post)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"data")

    PostizProvider("pz_secret").upload(
        video=video,
        fields={"title": "x"},
        platforms=["shorts"],
        scheduled_date="2026-11-21T19:00:00-05:00",
    )
    assert api.posts[0]["type"] == "schedule"
    assert api.posts[0]["date"] == "2026-11-21T19:00:00-05:00"


def test_postiz_refuses_when_the_channel_is_not_connected(monkeypatch, tmp_path: Path) -> None:
    # Only an X channel is connected, but the clip is shaped for Shorts.
    api = PostizApi(channels=[{"id": "int_x", "providerIdentifier": "x"}])
    monkeypatch.setattr(postiz_module.httpx, "get", api.get)
    monkeypatch.setattr(postiz_module.httpx, "post", api.post)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"data")

    with pytest.raises(ProviderError) as excinfo:
        PostizProvider("pz_secret").upload(
            video=video, fields={"title": "x"}, platforms=["shorts"]
        )
    assert excinfo.value.code == "account_not_connected"
    assert api.uploads == []  # nothing was uploaded for a channel that cannot post


def test_postiz_upload_media_requires_an_id_and_path(monkeypatch, tmp_path: Path) -> None:
    api = PostizApi(upload={})
    monkeypatch.setattr(postiz_module.httpx, "post", api.post)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"data")

    with pytest.raises(ProviderError):
        PostizProvider("pz_secret").upload_media(video)


# -------------------------------------------------------------------- service


def test_live_postiz_send_marks_the_clip_without_a_profile(monkeypatch) -> None:
    clip = make_ready()
    api = PostizApi()
    monkeypatch.setattr(postiz_module.httpx, "get", api.get)
    monkeypatch.setattr(postiz_module.httpx, "post", api.post)

    provider = PostizProvider("pz_secret")
    result = publish_clip(
        clip["id"],
        options=PublishOptions(live=True, post_now=True, platforms=("shorts",)),
        provider=provider,
    )

    assert result.ok is True
    assert result.post_id == "post_77"
    assert api.posts, "the post was created"
    uploaded = ((db.get_clip(clip["id"]) or {}).get("meta") or {}).get("uploaded") or {}
    assert uploaded.get("dry_run") is False
    assert uploaded.get("platform") == "youtube"
    # A profile-less provider must not be blocked by the Upload-Post profile gate.
    assert provider.user == ""
