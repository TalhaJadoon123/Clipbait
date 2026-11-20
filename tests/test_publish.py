"""Publishing: window math, the provider request, and the dry/live service.

The schedule is pure, so it is tested against a fixed clock. The provider is
tested against a stubbed ``httpx`` so no byte leaves the machine. The service is
tested end to end in dry run first, then once live against a fake provider, to
prove a dry run never marks a clip posted and a live send does exactly once.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from clipbait import db, storage
from clipbait.publish import (
    ProviderError,
    PublishOptions,
    UploadPostProvider,
    next_posting_time,
    parse_window,
    parse_windows,
    plan_clip,
    platform_fields,
    publish_clip,
    publish_queue,
    region_timezone,
    spread_minutes,
    upload_platform,
    upload_post_provider,
    windows_from_best_times,
)
from clipbait.publish import provider as provider_module

# ------------------------------------------------------------------ fixtures


def make_ready(
    *,
    title: str = "He lost it on stream",
    score: float = 74.0,
    region: str = "US",
    hashtags: list[str] | None = None,
    write_file: bool = True,
    meta_extra: dict | None = None,
    start: float = 12.0,
    end: float = 44.0,
) -> dict:
    project = db.create_project(
        {
            "title": "Publish project",
            "source_kind": "local",
            "duration": 300.0,
            "options": {"region": region, "platform": "tiktok", "language": "en"},
        }
    )
    meta = {
        "region": region,
        "platform": "tiktok",
        "forecast": {"overall": score, "best_time": "18:00-22:00", "reasons": ["hook"]},
    }
    meta.update(meta_extra or {})
    clip = db.create_clip(
        {
            "project_id": project["id"],
            "title": title,
            "hook": title,
            "description": "the part nobody expected",
            "hashtags": hashtags or ["#shorts", "#gta6"],
            "start": start,
            "end": end,
            "score": int(score),
            "status": "ready",
            "meta": meta,
        }
    )
    if write_file:
        path = storage.project_dir(project["id"]) / "clips" / f"{clip['id']}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
        clip = db.update("clips", clip["id"], {"render_path": storage.relative(path)}) or clip
    return clip


class FakeProvider:
    """A provider that records instead of sending."""

    name = "fake"

    def __init__(self, *, ready: bool = True, user: str = "talha", best_times=None, error=None):
        self.api_key = "test-key" if ready else ""
        self.user = user
        self._best_times = best_times or []
        self.calls: list[dict] = []
        self.error = error

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def best_times(self, platform, *, profile_username=None, timezone="UTC", top=3):
        if self.error:
            raise ProviderError(self.error)
        return {"best_times": [{"hour": hour} for hour in self._best_times]}

    def upload(self, **kwargs):
        self.calls.append(kwargs)
        return {"success": True, "job_id": "job_123", "post_id": "post_9", "url": "https://t.co/x"}


# ------------------------------------------------------------------- windows


def test_parse_window_reads_a_window_and_rejects_junk() -> None:
    assert parse_window("18:00-22:00") == (1080, 1320)
    assert parse_window("21:00-00:30") == (1260, 1470)  # wraps midnight
    assert parse_window("") is None
    assert parse_window("18:00") is None
    assert parse_window("22:00-22:00") is None
    assert parse_window("nonsense") is None


def test_parse_windows_normalises_and_sorts() -> None:
    windows = parse_windows(["18:00-22:00", "07:00-09:00", "18:00-22:00", "bad"])
    assert windows == ((420, 540), (1080, 1320))


def test_region_timezone_maps_the_known_regions() -> None:
    assert region_timezone("US") == "America/New_York"
    assert region_timezone("pk") == "Asia/Karachi"
    assert region_timezone(None) == "UTC"
    assert region_timezone("ZZ") == "UTC"


def test_best_time_hours_become_windows_best_first() -> None:
    windows = windows_from_best_times([{"hour": 19}, {"hour": 13}, {"hour": 99}])
    assert windows == ((780, 840), (1140, 1200))


def test_parse_windows_accepts_already_parsed_pairs() -> None:
    # resolve_schedule hands next_posting_time tuples, so re-parsing them must be
    # a no-op rather than dropping every window (which posted at now + lead).
    assert parse_windows([(1140, 1200), (780, 840)]) == ((780, 840), (1140, 1200))
    assert parse_windows([(1140, 1200), "07:00-09:00", (1140, 1200)]) == (
        (420, 540),
        (1140, 1200),
    )


def test_next_posting_time_honours_parsed_best_time_windows() -> None:
    now = datetime(2026, 11, 20, 15, 47, tzinfo=ZoneInfo("America/New_York"))
    windows = windows_from_best_times([{"hour": 19}, {"hour": 20}, {"hour": 21}])
    moment = next_posting_time(windows, now=now, offset_minutes=0)
    assert moment.hour == 19  # the measured 19:00 window, not "now + lead"


def test_next_posting_time_posts_soon_when_already_inside_a_window() -> None:
    now = datetime(2026, 11, 20, 19, 2, tzinfo=ZoneInfo("America/New_York"))
    moment = next_posting_time(["18:00-22:00"], now=now, offset_minutes=0)
    assert moment == datetime(2026, 11, 20, 19, 15, tzinfo=ZoneInfo("America/New_York"))


def test_next_posting_time_waits_for_the_next_window() -> None:
    now = datetime(2026, 11, 20, 13, 2, tzinfo=ZoneInfo("America/New_York"))
    moment = next_posting_time(["18:00-22:00"], now=now, offset_minutes=4, step_minutes=5)
    assert moment == datetime(2026, 11, 20, 18, 4, tzinfo=ZoneInfo("America/New_York"))


def test_next_posting_time_rolls_to_tomorrow_after_the_last_window() -> None:
    now = datetime(2026, 11, 20, 23, 0, tzinfo=ZoneInfo("America/New_York"))
    moment = next_posting_time(["18:00-22:00"], now=now, offset_minutes=0)
    assert moment.date() == datetime(2026, 11, 21).date()
    assert (moment.hour, moment.minute) == (18, 0)


def test_next_posting_time_without_windows_is_just_the_lead() -> None:
    now = datetime(2026, 11, 20, 13, 2, tzinfo=UTC)
    moment = next_posting_time([], now=now, lead_minutes=10, offset_minutes=0)
    assert moment == datetime(2026, 11, 20, 13, 15, tzinfo=UTC)


def test_spread_minutes_is_deterministic_and_bounded() -> None:
    first = spread_minutes("clip_abc")
    assert first == spread_minutes("clip_abc")
    assert 0 <= first < 15


# ------------------------------------------------------------------ provider


def test_upload_platform_maps_destinations_to_networks() -> None:
    assert upload_platform("reels") == "instagram"
    assert upload_platform("shorts") == "youtube"
    assert upload_platform("tiktok") == "tiktok"
    assert upload_platform("") == "tiktok"


def test_provider_without_a_key_is_not_ready_and_refuses() -> None:
    provider = UploadPostProvider("", user="talha")
    assert provider.ready is False
    with pytest.raises(ProviderError):
        provider.upload(video=Path("x.mp4"), fields={"title": "t"}, platforms=["tiktok"])


def test_provider_best_times_needs_a_profile() -> None:
    provider = UploadPostProvider("key", user="")
    with pytest.raises(ProviderError):
        provider.best_times("tiktok")


class _FakeResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def test_provider_upload_sends_the_expected_multipart(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}

    def fake_post(url, *, headers, data, files, timeout):
        captured.update(url=url, headers=headers, data=data, files=files, timeout=timeout)
        return _FakeResponse(200, {"success": True, "job_id": "job_1"})

    monkeypatch.setattr(provider_module.httpx, "post", fake_post)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"data")

    provider = UploadPostProvider("secret", user="talha")
    payload = provider.upload(
        video=video,
        fields={"title": "a caption", "description": "a caption"},
        platforms=["tiktok", "youtube"],
        scheduled_date="2026-11-21T19:00:00-05:00",
        timezone="America/New_York",
        idempotency_key="clipbait-clip_1",
    )

    assert payload["job_id"] == "job_1"
    assert captured["headers"]["Authorization"] == "Apikey secret"
    assert captured["headers"]["Idempotency-Key"] == "clipbait-clip_1"
    assert captured["data"]["user"] == "talha"
    assert captured["data"]["platform[]"] == ["tiktok", "youtube"]
    assert captured["data"]["scheduled_date"] == "2026-11-21T19:00:00-05:00"
    assert captured["data"]["timezone"] == "America/New_York"
    assert captured["files"]["video"][2] == "video/mp4"


def test_provider_upload_omits_schedule_for_an_immediate_post(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}

    def fake_post(url, *, headers, data, files, timeout):
        captured.update(data=data)
        return _FakeResponse(200, {"success": True})

    monkeypatch.setattr(provider_module.httpx, "post", fake_post)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"data")

    UploadPostProvider("secret", user="talha").upload(
        video=video, fields={"title": "now"}, platforms=["tiktok"], scheduled_date=None
    )
    assert "scheduled_date" not in captured["data"]
    assert "timezone" not in captured["data"]


def test_provider_upload_raises_with_the_reason(monkeypatch, tmp_path: Path) -> None:
    def fake_post(url, *, headers, data, files, timeout):
        return _FakeResponse(401, {"error_code": "reconnect_required", "message": "reconnect"})

    monkeypatch.setattr(provider_module.httpx, "post", fake_post)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"data")

    provider = UploadPostProvider("secret", user="talha")
    with pytest.raises(ProviderError) as excinfo:
        provider.upload(video=video, fields={"title": "x"}, platforms=["tiktok"])
    assert excinfo.value.code == "reconnect_required"
    assert excinfo.value.reconnect_required is True
    assert excinfo.value.status == 401


# -------------------------------------------------------------------- service


def test_plan_clip_prices_the_copy_and_picks_the_region_window() -> None:
    clip = make_ready()
    provider = FakeProvider(best_times=[])  # no measured hours -> region playbook

    plan = plan_clip(clip["id"], options=PublishOptions(), provider=provider)

    assert plan.platform == "tiktok"
    assert plan.timezone == "America/New_York"
    assert plan.source == "region-window"
    assert plan.scheduled_date  # a concrete ISO time
    assert "#gta6" in plan.caption
    assert plan.fields["title"] == plan.caption.strip()
    assert plan.as_dict()["video"].startswith("projects/")


def test_plan_clip_uses_measured_best_times_when_available() -> None:
    clip = make_ready()
    provider = FakeProvider(best_times=[19, 20, 21])

    plan = plan_clip(clip["id"], options=PublishOptions(), provider=provider)

    assert plan.source == "account-best-times"
    moment = datetime.fromisoformat(plan.scheduled_date)
    assert moment.hour in {19, 20, 21}


def test_plan_clip_marks_a_campaign_join() -> None:
    clip = make_ready()
    campaign = Path(storage.exports_dir()).parent / "kai-gta6.md"
    campaign.parent.mkdir(parents=True, exist_ok=True)
    campaign.write_text(
        "required_hashtags: #kaicenat #gta6\nrequired_mention: @kaicenat\nrate_per_1k: 2.00\n",
        encoding="utf-8",
    )

    plan = plan_clip(
        clip["id"], options=PublishOptions(campaign=campaign), provider=FakeProvider()
    )
    assert "@kaicenat" in plan.caption
    assert "#kaicenat" in plan.caption


def test_platform_fields_shapes_youtube_and_x() -> None:
    clip = make_ready(hashtags=["#gta6", "#shorts"])
    from clipbait.money import queue_item

    item = queue_item(clip["id"], rate_per_1k=None)
    assert item is not None
    long_caption = "A very long hook line\n\nbody\n\n#gta6 #shorts"

    youtube = platform_fields(
        item, long_caption, clipbait_platform="shorts", upload_platform_name="youtube", language="en"
    )
    assert youtube["youtube_title"] == "He lost it on stream"
    assert youtube["title"] == "He lost it on stream"
    assert youtube["description"] == long_caption
    assert youtube["tags"] == ["gta6", "shorts"]
    assert youtube["defaultLanguage"] == "en"

    tweet = platform_fields(
        item, "x" * 400, clipbait_platform="x", upload_platform_name="x"
    )
    assert len(tweet["title"]) <= 280


def test_dry_run_plans_without_marking_the_clip_posted() -> None:
    clip = make_ready()
    result = publish_clip(clip["id"], options=PublishOptions(live=False), provider=FakeProvider())

    assert result.ok is True
    assert result.dry_run is True
    assert result.metadata["plan"]["clip_id"] == clip["id"]
    assert not (db.get_clip(clip["id"]).get("meta") or {}).get("uploaded")


def test_live_publish_sends_once_and_marks_the_clip() -> None:
    clip = make_ready()
    provider = FakeProvider()

    result = publish_clip(clip["id"], options=PublishOptions(live=True), provider=provider)

    assert result.ok is True
    assert result.dry_run is False
    assert result.job_id == "job_123"
    assert result.url == "https://t.co/x"
    assert len(provider.calls) == 1
    assert provider.calls[0]["idempotency_key"] == f"clipbait-{clip['id']}"

    uploaded = (db.get_clip(clip["id"]).get("meta") or {}).get("uploaded") or {}
    assert uploaded["platform"] == "tiktok"
    assert uploaded["job_id"] == "job_123"
    assert uploaded["scheduled_for"]


def test_per_platform_result_link_is_recorded() -> None:
    # Upload-Post nests the outcome per network, so a posted clip must pick the
    # link out of results.<platform> rather than storing an empty URL.
    clip = make_ready()
    provider = FakeProvider()
    provider.upload = lambda **kwargs: {
        "success": True,
        "job_id": "job_9",
        "status": "completed",
        "results": {
            "youtube": {
                "success": True,
                "post_id": "ZKgCGH1FP5A",
                "url": "https://www.youtube.com/watch?v=ZKgCGH1FP5A",
            }
        },
    }

    result = publish_clip(clip["id"], options=PublishOptions(live=True), provider=provider)

    assert result.url == "https://www.youtube.com/watch?v=ZKgCGH1FP5A"
    assert result.post_id == "ZKgCGH1FP5A"
    uploaded = (db.get_clip(clip["id"]).get("meta") or {}).get("uploaded") or {}
    assert uploaded["url"] == "https://www.youtube.com/watch?v=ZKgCGH1FP5A"


def test_a_second_live_run_skips_the_already_posted_clip() -> None:
    clip = make_ready()
    first = FakeProvider()
    publish_clip(clip["id"], options=PublishOptions(live=True), provider=first)
    second = FakeProvider()

    result = publish_clip(clip["id"], options=PublishOptions(live=True), provider=second)

    assert result.source == "already-posted"
    assert second.calls == []


def test_live_publish_without_a_key_explains_itself() -> None:
    clip = make_ready()
    provider = FakeProvider(ready=False, user="")

    result = publish_clip(clip["id"], options=PublishOptions(live=True), provider=provider)
    assert result.ok is False
    assert "UPLOAD_POST_API_KEY" in result.error


def test_live_publish_reports_a_provider_refusal() -> None:
    clip = make_ready()
    provider = FakeProvider()
    provider.upload = lambda **kwargs: (_ for _ in ()).throw(ProviderError("quota", status=429))

    result = publish_clip(clip["id"], options=PublishOptions(live=True), provider=provider)
    assert result.ok is False
    assert result.error == "quota"
    assert result.metadata["status"] == 429
    assert not (db.get_clip(clip["id"]).get("meta") or {}).get("uploaded")


def test_post_now_sends_immediately() -> None:
    clip = make_ready()
    provider = FakeProvider()
    result = publish_clip(
        clip["id"], options=PublishOptions(live=True, post_now=True), provider=provider
    )
    assert result.source == "now"
    assert result.scheduled_date == ""
    assert provider.calls[0]["scheduled_date"] is None


def test_explicit_at_time_is_honoured() -> None:
    clip = make_ready()
    provider = FakeProvider()
    result = publish_clip(
        clip["id"],
        options=PublishOptions(live=True, at="2026-11-21T19:30", timezone="America/New_York"),
        provider=provider,
    )
    assert result.source == "manual"
    moment = datetime.fromisoformat(result.scheduled_date)
    assert (moment.hour, moment.minute) == (19, 30)
    assert provider.calls[0]["scheduled_date"] == result.scheduled_date


def test_publish_queue_returns_one_result_per_ready_clip() -> None:
    first = make_ready(title="First moment")
    second = make_ready(title="Second moment")

    results = publish_queue(
        options=PublishOptions(),
        provider=FakeProvider(),
        limit=10,
        project_match="Publish project",
    )
    ids = {result.clip_id for result in results}
    assert {first["id"], second["id"]} <= ids
    assert all(result.ok for result in results)


def test_plan_clip_falls_back_when_best_times_is_unavailable() -> None:
    clip = make_ready()
    # Key present but the account is not connected yet: the provider refuses,
    # the plan must fall back to the region playbook, not fail the run.
    provider = FakeProvider(error="account_not_connected")

    plan = plan_clip(clip["id"], options=PublishOptions(), provider=provider)

    assert plan.source == "region-window"
    assert plan.scheduled_date


def test_no_best_times_flag_skips_the_provider_entirely() -> None:
    clip = make_ready()
    provider = FakeProvider(best_times=[19, 20, 21])

    plan = plan_clip(
        clip["id"], options=PublishOptions(use_best_times=False), provider=provider
    )

    assert plan.source == "region-window"


def test_platform_override_changes_the_destination() -> None:
    clip = make_ready()
    provider = FakeProvider()
    result = publish_clip(
        clip["id"],
        options=PublishOptions(live=True, platforms=("shorts", "reels")),
        provider=provider,
    )
    assert result.platform == "youtube"
    assert provider.calls[0]["platforms"] == ["youtube", "instagram"]


def test_provider_factory_uses_the_configured_timeout() -> None:
    # A vertical clip is tens of megabytes; a home uplink needs more than the
    # library default before the client gives up on the body.
    from dataclasses import replace

    from clipbait.config import get_settings

    settings = replace(
        get_settings(),
        upload_post_api_key="key",
        upload_post_user="me",
        upload_post_timeout=456.0,
    )
    provider = upload_post_provider(settings)
    assert provider.timeout == 456.0
    assert provider.user == "me"
