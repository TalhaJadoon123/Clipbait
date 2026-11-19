"""HTTP surface: contract stability, validation and SPA fallback."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from clipbait import db
from clipbait.api.app import app


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def make_project(**overrides) -> dict:
    data = {
        "title": "Test project",
        "source_kind": "local",
        "source_url": None,
        "duration": 120.0,
        "options": {"clips": 2, "region": "US", "platform": "tiktok"},
    }
    data.update(overrides)
    return db.create_project(data)


def make_clip(project_id: str, **overrides) -> dict:
    data = {
        "project_id": project_id,
        "title": "A clip",
        "hook": "A clip",
        "start": 10.0,
        "end": 50.0,
        "score": 70,
        "scores": {"hook": 70, "emotion": 60, "value": 60, "share": 65, "overall": 70},
        "meta": {
            "variants": [
                {
                    "id": "organic",
                    "title": "A clip",
                    "hook": "A clip",
                    "description": "",
                    "hashtags": ["#shorts"],
                }
            ]
        },
    }
    data.update(overrides)
    return db.create_clip(data)


def test_health(client: TestClient) -> None:
    response = client.get("/api/v1/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_meta_advertises_the_playbooks(client: TestClient) -> None:
    payload = client.get("/api/v1/meta").json()
    assert payload["version"]
    assert len(payload["regions"]) >= 20
    assert len(payload["memes"]) >= 20
    assert {"default", "hormozi", "clean"} <= {preset["id"] for preset in payload["caption_presets"]}
    assert {"tiktok", "reels", "shorts"} <= set(payload["platforms"])
    assert payload["live_providers"]
    assert "transcriber" in payload["capabilities"]


def test_projects_list_is_a_plain_array(client: TestClient) -> None:
    project = make_project(title="Listed project")
    response = client.get("/api/v1/projects")
    assert response.status_code == 200
    rows = response.json()
    assert isinstance(rows, list)
    row = next(item for item in rows if item["id"] == project["id"])
    assert row["clip_count"] == 0
    assert "job" in row


def test_project_detail_includes_clips_and_transcript_state(client: TestClient) -> None:
    project = make_project(title="Detail project")
    clip = make_clip(project["id"])
    payload = client.get(f"/api/v1/projects/{project['id']}").json()
    assert payload["id"] == project["id"]
    assert payload["has_transcript"] is False
    assert [item["id"] for item in payload["clips"]] == [clip["id"]]
    assert payload["jobs"] == []
    assert payload["options"]["region"] == "US"


def test_missing_project_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/projects/prj_missing").status_code == 404
    assert client.delete("/api/v1/projects/prj_missing").status_code == 404
    assert client.post("/api/v1/projects/prj_missing/retry").status_code == 404


def test_url_validation_rejects_bad_schemes_and_private_hosts(client: TestClient) -> None:
    assert client.post("/api/v1/projects", json={"url": "ftp://example.com/x"}).status_code == 422
    private = client.post("/api/v1/projects", json={"url": "http://127.0.0.1/x"})
    assert private.status_code == 400
    assert "private" in private.json()["detail"].lower() or "allowed" in private.json()["detail"].lower()


def test_upload_rejects_malformed_options(client: TestClient) -> None:
    response = client.post(
        "/api/v1/projects/upload",
        files={"file": ("clip.mp4", b"not really a video", "video/mp4")},
        data={"options": "{not json"},
    )
    assert response.status_code == 400


def test_clip_patch_validates_range(client: TestClient) -> None:
    project = make_project(title="Patch project", duration=120.0)
    clip = make_clip(project["id"])

    bad = client.patch(f"/api/v1/clips/{clip['id']}", json={"start": 40.0, "end": 20.0})
    assert bad.status_code == 400

    outside = client.patch(f"/api/v1/clips/{clip['id']}", json={"start": 0.0, "end": 500.0})
    assert outside.status_code == 400

    good = client.patch(f"/api/v1/clips/{clip['id']}", json={"title": "Renamed", "hook": "Renamed"})
    assert good.status_code == 200
    assert good.json()["title"] == "Renamed"


def test_clip_media_is_404_until_rendered(client: TestClient) -> None:
    project = make_project(title="Media project")
    clip = make_clip(project["id"])
    assert client.get(f"/api/v1/clips/{clip['id']}/video").status_code == 404
    assert client.get(f"/api/v1/clips/{clip['id']}/thumb").status_code == 404
    assert client.get("/api/v1/clips/clip_missing").status_code == 404


def test_variant_selection_requires_an_existing_variant(client: TestClient) -> None:
    project = make_project(title="Variant project")
    clip = make_clip(project["id"])
    assert client.post(f"/api/v1/clips/{clip['id']}/variants/nope").status_code == 404

    response = client.post(f"/api/v1/clips/{clip['id']}/variants/organic")
    assert response.status_code == 200
    assert response.json()["meta"]["selected_variant"] == "organic"


def test_live_monitor_validation_and_404s(client: TestClient) -> None:
    assert isinstance(client.get("/api/v1/live/monitors").json(), list)

    unsafe = client.post(
        "/api/v1/live/monitors", json={"url": "http://169.254.169.254/latest"}
    )
    assert unsafe.status_code == 400

    assert client.get("/api/v1/live/monitors/mon_missing").status_code == 404
    assert client.get("/api/v1/live/monitors/mon_missing/moments").status_code == 404
    assert client.get("/api/v1/live/monitors/mon_missing/chat").status_code == 404
    assert client.delete("/api/v1/live/monitors/mon_missing").status_code == 404


def test_spa_fallback_serves_the_built_app(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "clipbait" in response.text.lower()

    deep_link = client.get("/projects/prj_anything")
    assert deep_link.status_code == 200
    assert "text/html" in deep_link.headers["content-type"]

    assert client.get("/api/v1/does-not-exist").status_code == 404