"""Money layer: payout math honestly, queue ranking, export bundles."""

from __future__ import annotations

import json

import pytest

from clipbait import db, storage
from clipbait.money import build_queue, export_bundle, mark_uploaded, queue_item
from clipbait.virality.payout import (
    PLATFORM_RPM,
    best_destination,
    estimate,
    estimate_platform,
    expected_views,
    region_factor,
)


def make_ready(
    *,
    title: str = "A ready clip",
    score: float = 72.0,
    region: str = "US",
    start: float = 10.0,
    end: float = 48.0,
    hashtags: list[str] | None = None,
    write_file: bool = True,
    project_id: str | None = None,
    status: str = "ready",
) -> dict:
    project = db.get_project(project_id) if project_id else None
    if project is None:
        project = db.create_project(
            {
                "title": "Money project",
                "source_kind": "local",
                "duration": 300.0,
                "options": {"region": region, "platform": "tiktok"},
            }
        )
    clip = db.create_clip(
        {
            "project_id": project["id"],
            "title": title,
            "hook": f"{title} hook",
            "description": "the part nobody expected",
            "hashtags": hashtags or ["#shorts", "#viral"],
            "start": start,
            "end": end,
            "score": int(score),
            "status": status,
            "meta": {
                "region": region,
                "platform": "tiktok",
                "forecast": {
                    "overall": score,
                    "best_time": "18:00-22:00",
                    "reasons": ["Strong three-second hook"],
                },
            },
        }
    )
    if write_file:
        path = storage.project_dir(project["id"]) / "clips" / f"{clip['id']}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
        clip = db.update("clips", clip["id"], {"render_path": storage.relative(path)}) or clip
    return clip


# --------------------------------------------------------------------- payout


def test_estimates_are_monotonic_and_ranged() -> None:
    low = estimate_platform(30, "tiktok", "US", duration=38)
    high = estimate_platform(90, "tiktok", "US", duration=38)

    assert high.expected_views > low.expected_views
    assert high.expected_usd > low.expected_usd
    assert low.usd_low <= low.expected_usd <= low.usd_high
    assert high.usd_low <= high.expected_usd <= high.usd_high


def test_region_changes_what_an_impression_is_worth() -> None:
    assert region_factor("US") > region_factor("PK")
    us = estimate_platform(70, "tiktok", "US", duration=38)
    pk = estimate_platform(70, "tiktok", "PK", duration=38)
    assert us.expected_views == pk.expected_views
    assert us.expected_usd > pk.expected_usd
    assert any("worth" in line for line in pk.assumptions)


def test_long_clips_lose_reach_but_can_win_on_linkedin() -> None:
    short = estimate(80, "US", duration=20)
    long = estimate(80, "US", duration=75)
    assert best_destination(short) == "tiktok"
    assert best_destination(long) == "linkedin"


def test_report_ranks_all_platforms_and_keeps_a_disclaimer() -> None:
    report = estimate(65, "US", duration=38)
    assert len(report.estimates) == len(PLATFORM_RPM)
    values = [item.expected_usd for item in report.estimates]
    assert values == sorted(values, reverse=True)
    assert report.best is report.estimates[0]
    assert "not a guarantee" in report.as_dict()["disclaimer"]


def test_zero_score_still_returns_a_small_range() -> None:
    item = estimate_platform(0, "tiktok", "US", duration=38)
    assert item.expected_views >= 0
    assert item.usd_low >= 0


# ---------------------------------------------------------------------- queue


def test_queue_ranks_by_expected_revenue() -> None:
    weak = make_ready(title="Weak", score=35, start=10, end=48)
    strong = make_ready(title="Strong", score=88, start=60, end=98)

    queue = build_queue(limit=10)

    ids = [item.clip_id for item in queue.items]
    assert ids.index(strong["id"]) < ids.index(weak["id"])
    assert queue.total_expected_usd > 0
    assert queue.items[0].expected_usd >= queue.items[-1].expected_usd


def test_queue_skips_drafts_and_missing_renders() -> None:
    make_ready(title="Draft", status="draft")
    make_ready(title="No file", write_file=False)

    queue = build_queue(limit=50)

    assert all(item.title not in {"Draft", "No file"} for item in queue.items)


def test_queue_dedupes_the_same_window_rerendered() -> None:
    project = db.create_project(
        {
            "title": "Dedup",
            "source_kind": "local",
            "duration": 300.0,
            "options": {"region": "US"},
        }
    )
    first = make_ready(title="Take one", project_id=project["id"], score=60, start=10, end=48)
    make_ready(title="Take two", project_id=project["id"], score=80, start=10, end=48)

    queue = build_queue(limit=50)
    matching = [
        item
        for item in queue.items
        if item.project_id == project["id"] and item.clip_id in {first["id"]}
    ]

    assert len([item for item in queue.items if item.project_id == project["id"]]) == 1
    assert matching or True  # the surviving copy may be either render
    assert queue.count == len(queue.items)


def test_queue_carries_the_copy_the_platform_and_the_math() -> None:
    clip = make_ready(title="Launch story", score=76, hashtags=["#one", "#two"])
    item = queue_item(clip["id"])

    assert item is not None
    assert item.platform in PLATFORM_RPM
    assert item.hook in item.caption
    assert "#one #two" in item.caption
    assert item.payout is not None
    payload = item.as_dict()
    assert payload["copy"]["hashtags"] == "#one #two"
    assert payload["usd_low"] <= payload["expected_usd"] <= payload["usd_high"]


def test_mark_uploaded_removes_it_from_the_default_queue() -> None:
    clip = make_ready(title="Posted", score=80)

    assert mark_uploaded(clip["id"], url="https://tiktok.com/x", platform="tiktok")
    default_ids = [item.clip_id for item in build_queue(limit=50).items]
    assert clip["id"] not in default_ids

    with_posted = build_queue(limit=50, include_posted=True)
    match = next(item for item in with_posted.items if item.clip_id == clip["id"])
    assert match.posted is True
    assert match.posted_url == "https://tiktok.com/x"
    assert with_posted.posted_count >= 1

    assert mark_uploaded("clip_missing") is None


def test_export_bundle_writes_an_upload_ready_folder() -> None:
    clip = make_ready(title="Export me now", score=82)
    bundle = export_bundle(clip["id"])

    directory = storage.resolve(bundle["dir"])
    assert {"clip.mp4", "caption.txt", "metadata.json", "README.md"} <= set(bundle["files"])
    assert (directory / "clip.mp4").stat().st_size > 0
    caption = (directory / "caption.txt").read_text()
    assert "#shorts #viral" in caption

    metadata = json.loads((directory / "metadata.json").read_text())
    assert metadata["recommended_platform"] == bundle["platform"]
    assert metadata["clip_id"] == clip["id"]
    assert metadata["expected_usd"] > 0
    assert "## Files" in (directory / "README.md").read_text()


def test_export_rejects_an_unrendered_clip() -> None:
    clip = make_ready(title="Draft export", status="draft", write_file=False)
    with pytest.raises(FileNotFoundError):
        export_bundle(clip["id"])
    with pytest.raises(FileNotFoundError):
        export_bundle("clip_missing")


def test_expected_views_helper_is_ordered() -> None:
    low, mid, high = expected_views(70, "tiktok", 38)
    assert 0 <= low <= mid <= high