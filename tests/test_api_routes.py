"""HTTP API routes.

The job runner and live registry are stubbed: these tests pin the *contract*
(status codes, validation, what gets persisted) not ffmpeg.
"""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest
from fastapi.testclient import TestClient

from clipbait import db, storage
from clipbait.api.app import app
from clipbait.events import bus

# ``clipbait.api`` re-exports the FastAPI instance as ``app``, which shadows the
# submodule for string-based monkeypatch targets. Hold the real module instead.
app_module = importlib.import_module("clipbait.api.app")

API = "/api/v1"


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def submitted(monkeypatch):
    """Capture job submissions instead of running them."""
    calls: list[dict] = []

    def fake_submit(kind, fn, **kwargs):
        job = {"id": f"job_{len(calls)}", "kind": kind, "status": "queued"}
        calls.append({"kind": kind, "fn": fn, "job": job, **kwargs})
        return job

    monkeypatch.setattr(app_module.jobs, "submit", fake_submit)
    return calls


def make_project(**overrides) -> dict:
    data = {
        "title": "API project",
        "source_kind": "local",
        "source_path": None,
        "duration": 120.0,
        "options": {"clips": 2, "region": "US"},
    }
    data.update(overrides)
    return db.create_project(data)


def make_clip(project_id: str, **overrides) -> dict:
    data = {
        "project_id": project_id,
        "title": "Clip",
        "hook": "Clip",
        "start": 10.0,
        "end": 40.0,
        "score": 72,
        "scores": {"overall": 72},
        "meta": {
            "variants": [
                {
                    "id": "pov",
                    "title": "POV title",
                    "hook": "POV hook",
                    "description": "desc",
                    "hashtags": ["#one"],
                }
            ]
        },
    }
    data.update(overrides)
    return db.create_clip(data)


# ----------------------------------------------------------------------- meta


def test_health_and_meta(client):
    health = client.get(f"{API}/healthz")
    assert health.status_code == 200
    assert health.json()["ok"] is True

    meta = client.get(f"{API}/meta").json()
    assert meta["version"] == "0.1.0"
    assert "tiktok" in meta["platforms"]
    assert meta["caption_presets"]
    assert meta["regions"]
    assert any(meme["id"] for meme in meta["memes"])
    assert "capabilities" in meta


# ------------------------------------------------------------------- projects


def test_create_project_validation(client, submitted, monkeypatch):
    monkeypatch.delenv("CLIPBAIT_ALLOW_PRIVATE_URLS", raising=False)
    assert client.post(f"{API}/projects", json={"options": {}}).status_code == 400

    blocked = client.post(f"{API}/projects", json={"url": "http://127.0.0.1:9/x"})
    assert blocked.status_code == 400
    assert "127.0.0.1" in blocked.text or "private" in blocked.text.lower()

    bad_scheme = client.post(f"{API}/projects", json={"url": "ftp://example.com/x"})
    assert bad_scheme.status_code == 422


def test_create_project_queues_a_job(client, submitted):
    response = client.post(
        f"{API}/projects",
        json={"url": "https://example.com/my-great_video", "source_kind": "upload"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["project"]["title"] == "my great video"
    assert body["project"]["source_kind"] == "upload"
    assert len(submitted) == 1
    assert submitted[0]["kind"] == "process"
    assert submitted[0]["args"] == (body["project"]["id"],)

    listed = client.get(f"{API}/projects").json()
    assert any(project["id"] == body["project"]["id"] for project in listed)


def test_upload_project(client, submitted):
    response = client.post(
        f"{API}/projects/upload",
        files={"file": ("talk.mp4", b"not really a video", "video/mp4")},
        data={"options": json.dumps({"clips": 1}), "title": "Uploaded"},
    )
    assert response.status_code == 201
    project = response.json()["project"]
    assert project["title"] == "Uploaded"
    assert project["source_kind"] == "upload"
    assert storage.resolve(project["source_path"]).exists()
    assert submitted[0]["kind"] == "process"

    bad_json = client.post(
        f"{API}/projects/upload",
        files={"file": ("x.mp4", b"x", "video/mp4")},
        data={"options": "{not json"},
    )
    assert bad_json.status_code == 400

    bad_shape = client.post(
        f"{API}/projects/upload",
        files={"file": ("x.mp4", b"x", "video/mp4")},
        data={"options": json.dumps({"clips": 999})},
    )
    assert bad_shape.status_code == 400


def test_project_detail_and_transcript_flag(client):
    project = make_project()
    assert client.get(f"{API}/projects/prj_missing").status_code == 404

    detail = client.get(f"{API}/projects/{project['id']}").json()
    assert detail["has_transcript"] is False
    assert detail["clips"] == []

    storage.write_json(
        storage.transcript_path(project["id"]),
        {"language": "en", "duration": 1.0, "words": [], "segments": [], "provider": "whisper"},
    )
    detail = client.get(f"{API}/projects/{project['id']}").json()
    assert detail["has_transcript"] is True
    assert detail["transcriber"] == "whisper"


def test_retry_and_delete_project(client, submitted):
    project = make_project(status="failed")
    assert client.post(f"{API}/projects/prj_missing/retry").status_code == 404

    retried = client.post(f"{API}/projects/{project['id']}/retry")
    assert retried.status_code == 200
    assert db.get_project(project["id"])["status"] == "queued"
    assert submitted[-1]["kind"] == "process"

    project_dir = storage.project_dir(project["id"])
    assert client.delete(f"{API}/projects/{project['id']}").status_code == 204
    assert db.get_project(project["id"]) is None
    assert not project_dir.exists()
    assert client.delete(f"{API}/projects/{project['id']}").status_code == 404


def test_project_clips_endpoint(client):
    project = make_project()
    make_clip(project["id"])
    assert client.get(f"{API}/projects/{project['id']}/clips").json()[0]["id"]
    assert client.get(f"{API}/projects/prj_missing/clips").status_code == 404


# ----------------------------------------------------------------------- clips


def test_clip_detail(client):
    project = make_project()
    clip = make_clip(project["id"])
    assert client.get(f"{API}/clips/clip_missing").status_code == 404
    detail = client.get(f"{API}/clips/{clip['id']}").json()
    assert detail["project"]["id"] == project["id"]


def test_patch_clip_validation_and_range(client):
    project = make_project(duration=120.0)
    clip = make_clip(project["id"], start=10.0, end=40.0)

    assert client.patch(f"{API}/clips/clip_missing", json={"title": "x"}).status_code == 404
    too_short = client.patch(f"{API}/clips/{clip['id']}", json={"start": 10.0, "end": 10.5})
    assert too_short.status_code == 400
    outside = client.patch(f"{API}/clips/{clip['id']}", json={"start": 0.0, "end": 500.0})
    assert outside.status_code == 400
    negative = client.patch(f"{API}/clips/{clip['id']}", json={"start": -5.0, "end": 30.0})
    assert negative.status_code == 400

    ok = client.patch(
        f"{API}/clips/{clip['id']}",
        json={"start": 12.0, "end": 119.9, "title": "Better"},
    )
    assert ok.status_code == 200
    updated = ok.json()
    assert updated["title"] == "Better"
    assert updated["start"] == 12.0
    assert updated["end"] == 119.9


def test_patch_clip_without_duration_uses_end(client):
    project = make_project(duration=None)
    clip = make_clip(project["id"], start=0.0, end=30.0)
    response = client.patch(f"{API}/clips/{clip['id']}", json={"start": 1.0, "end": 31.0})
    assert response.status_code == 200
    assert response.json()["end"] == 31.0


def test_render_clip_endpoint(client, submitted):
    project = make_project()
    clip = make_clip(project["id"])
    assert client.post(f"{API}/clips/clip_missing/render").status_code == 404

    response = client.post(
        f"{API}/clips/{clip['id']}/render",
        json={"caption_preset": "hormozi", "captions": False},
    )
    assert response.status_code == 200
    assert submitted[-1]["kind"] == "render"
    assert submitted[-1]["args"] == (clip["id"],)
    assert db.get_clip(clip["id"])["caption_preset"] == "hormozi"

    # No body at all: still queues a render with the stored settings.
    assert client.post(f"{API}/clips/{clip['id']}/render").status_code == 200


def test_select_variant(client):
    project = make_project()
    clip = make_clip(project["id"])
    assert client.post(f"{API}/clips/clip_missing/variants/pov").status_code == 404
    assert client.post(f"{API}/clips/{clip['id']}/variants/nope").status_code == 404

    response = client.post(f"{API}/clips/{clip['id']}/variants/pov")
    assert response.status_code == 200
    updated = response.json()
    assert updated["title"] == "POV title"
    assert updated["meta"]["selected_variant"] == "pov"


def test_clip_media_endpoints(client, tmp_path):
    project = make_project()
    clip = make_clip(project["id"])

    assert client.get(f"{API}/clips/clip_missing/video").status_code == 404
    assert client.get(f"{API}/clips/{clip['id']}/video").status_code == 404
    assert client.get(f"{API}/clips/{clip['id']}/thumb").status_code == 404

    directory = storage.clip_dir(project["id"], clip["id"])
    video = directory / "clip.mp4"
    video.write_bytes(b"\x00" * 128)
    thumb = directory / "clip.jpg"
    thumb.write_bytes(b"\xff\xd8\xff\xd9")
    db.update(
        "clips",
        clip["id"],
        {"render_path": storage.relative(video), "thumb_path": storage.relative(thumb)},
    )

    served = client.get(f"{API}/clips/{clip['id']}/video")
    assert served.status_code == 200
    assert served.headers["content-type"] == "video/mp4"
    assert served.content == b"\x00" * 128

    image = client.get(f"{API}/clips/{clip['id']}/thumb")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"


# ------------------------------------------------------------------------ live


@pytest.fixture()
def live_stub(monkeypatch):
    calls = {"start": [], "stop": [], "status": {}}

    def fake_start(mid):
        if not db.get_monitor(mid):
            raise ValueError(f"unknown monitor {mid}")
        calls["start"].append(mid)
        return {}

    def fake_stop(mid):
        if not db.get_monitor(mid):
            raise ValueError(f"unknown monitor {mid}")
        calls["stop"].append(mid)
        return {}

    monkeypatch.setattr(app_module.live, "start", fake_start)
    monkeypatch.setattr(app_module.live, "stop", fake_stop)
    monkeypatch.setattr(
        app_module.live, "status", lambda mid: calls["status"].get(mid, {})
    )
    monkeypatch.setattr(app_module.live, "active_monitors", lambda: list(calls["start"]))
    return calls


def make_monitor(**overrides) -> dict:
    data = {
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "title": "Live",
        "status": "live",
        "options": {},
    }
    data.update(overrides)
    return db.create_monitor(data)


def test_live_monitor_crud(client, live_stub, monkeypatch):
    monkeypatch.delenv("CLIPBAIT_ALLOW_PRIVATE_URLS", raising=False)
    created = client.post(
        f"{API}/live/monitors",
        json={"url": "https://www.youtube.com/watch?v=abcdefghijk", "title": "Mine"},
    )
    assert created.status_code == 201
    monitor = created.json()
    assert monitor["title"] == "Mine"
    assert live_stub["start"] == [monitor["id"]]

    assert client.post(f"{API}/live/monitors", json={"url": "http://127.0.0.1/x"}).status_code == 400
    assert client.post(f"{API}/live/monitors", json={"url": "nope"}).status_code == 422

    listed = client.get(f"{API}/live/monitors").json()
    assert any(item["id"] == monitor["id"] for item in listed)

    assert client.get(f"{API}/live/monitors/mon_missing").status_code == 404
    detail = client.get(f"{API}/live/monitors/{monitor['id']}").json()
    assert detail["moments"] == []
    assert detail["active"] is True

    assert client.post(f"{API}/live/monitors/mon_missing/stop").status_code == 404
    assert client.post(f"{API}/live/monitors/{monitor['id']}/stop").status_code == 200
    assert live_stub["stop"] == [monitor["id"]]

    assert client.post(f"{API}/live/monitors/mon_missing/start").status_code == 404
    assert client.post(f"{API}/live/monitors/{monitor['id']}/start").status_code == 200


def test_live_monitor_delete_moments_and_chat(client, live_stub):
    monitor = make_monitor()
    db.create_moment(
        {
            "monitor_id": monitor["id"],
            "start": 1.0,
            "end": 9.0,
            "score": 4.0,
            "signal": {},
            "messages": [],
        }
    )
    db.add_chat_events(
        monitor["id"], [{"t": 1.0, "author": "a", "text": "hi", "kind": "text"}]
    )

    assert client.get(f"{API}/live/monitors/mon_missing/moments").status_code == 404
    moments = client.get(f"{API}/live/monitors/{monitor['id']}/moments").json()
    assert len(moments) == 1

    assert client.get(f"{API}/live/monitors/mon_missing/chat").status_code == 404
    chat = client.get(f"{API}/live/monitors/{monitor['id']}/chat?since=0&limit=10").json()
    assert chat["messages"][0]["text"] == "hi"
    assert isinstance(chat["stats"], dict)

    assert client.delete(f"{API}/live/monitors/mon_missing").status_code == 404
    assert client.delete(f"{API}/live/monitors/{monitor['id']}").status_code == 204
    assert db.get_monitor(monitor["id"]) is None
    assert monitor["id"] in live_stub["stop"]


def test_live_demo_endpoint(client, live_stub):
    response = client.post(f"{API}/live/demo")
    assert response.status_code == 201
    body = response.json()
    assert body["url"] == "demo://live"
    assert body["options"]["spike_threshold"] == 2.5
    assert body["options"]["chat_provider"] == "demo"

    custom = client.post(f"{API}/live/demo", json={"spike_threshold": 4.0, "auto_clip": False})
    assert custom.status_code == 201
    assert custom.json()["options"]["spike_threshold"] == 4.0


# ------------------------------------------------------------------------- sse


def test_sse_replays_history_then_keeps_the_stream_open():
    bus.publish("test:replay", {"type": "hello"})
    bus.publish("test:replay", {"type": "world"})
    response = app_module._sse("test:replay")
    assert response.media_type == "text/event-stream"

    async def read_two() -> list[str]:
        generator = response.body_iterator
        chunks: list[str] = []
        async for chunk in generator:
            chunks.append(chunk)
            if len(chunks) == 2:
                break
        await generator.aclose()
        return chunks

    chunks = asyncio.run(read_two())
    assert chunks[0].startswith("data: ")
    assert json.loads(chunks[0][len("data: ") :])["type"] == "hello"
    assert json.loads(chunks[1][len("data: ") :])["type"] == "world"


# ------------------------------------------------------------------------- spa


def test_resolve_web_dist_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIPBAIT_WEB_DIST", str(tmp_path / "dist"))
    assert app_module._resolve_web_dist() == tmp_path / "dist"


def test_spa_fallback_serves_built_assets(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>clipbait</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setattr(app_module, "WEB_DIST", dist)

    with TestClient(app_module.create_app()) as spa_client:
        root = spa_client.get("/")
        assert root.status_code == 200
        assert "clipbait" in root.text

        deep = spa_client.get("/projects/abc")
        assert deep.status_code == 200
        assert "clipbait" in deep.text

        asset = spa_client.get("/assets/app.js")
        assert asset.status_code == 200
        assert "console.log" in asset.text

        assert spa_client.get("/api/v1/does-not-exist").status_code == 404
