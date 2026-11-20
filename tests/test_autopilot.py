"""Autopilot: the loop's decisions, and the loop itself on a fake runtime.

The decisions are pure, so they are tested directly. ``run`` performs side
effects, so it is tested against a fake :class:`Runtime` with a fake clock: ten
rounds run in a millisecond, no network, no ffmpeg, no real sleep. The one test
that touches the money layer asserts a round only publishes when ``live`` is set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clipbait import db, storage
from clipbait.autopilot import (
    AutopilotOptions,
    Runtime,
    export_monitor,
    more_rounds,
    pick_stream,
    publish_monitor,
    round_finished,
    run,
)

# ------------------------------------------------------------------ fixtures


class Clock:
    """A monotonic clock that only moves when the loop sleeps."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


class Stream:
    def __init__(self, url: str, title: str = "", channel: str = "", viewers: int = 0) -> None:
        self.url = url
        self.title = title
        self.channel = channel
        self.viewers = viewers


def make_runtime(
    *,
    streams=None,
    clips_per_round: int = 2,
    fail_start: bool = False,
    interrupt_on_clips: bool = False,
    clock: Clock | None = None,
) -> tuple[Runtime, dict, Clock]:
    """A Runtime that records what it was asked to do instead of doing it."""
    clock = clock or Clock()
    calls: dict = {"discover": [], "start": [], "stop": [], "clips": [], "export": [], "publish": [], "sleep": []}
    streams = streams if streams is not None else [Stream("https://x/a", "Room A", "alpha", 900)]

    def discover(query, limit, timeout):
        calls["discover"].append((query, limit, timeout))
        return streams

    def start(source, title, options):
        if fail_start:
            raise RuntimeError("no feed")
        calls["start"].append((source, title))
        return f"mon_{len(calls['start'])}"

    def status(monitor_id):
        return {"status": "live"}

    def stop(monitor_id):
        calls["stop"].append(monitor_id)

    def clips(monitor_id):
        if interrupt_on_clips:
            raise KeyboardInterrupt
        calls["clips"].append(monitor_id)
        return [{"id": f"clip_{i}", "meta": {"monitor_id": monitor_id}} for i in range(clips_per_round)]

    def export(monitor_id, options):
        calls["export"].append(monitor_id)
        return clips_per_round

    def publish(monitor_id, options):
        calls["publish"].append(monitor_id)
        return 1

    def sleep(seconds):
        calls["sleep"].append(seconds)
        clock.sleep(seconds)

    runtime = Runtime(
        discover=discover,
        start=start,
        status=status,
        stop=stop,
        clips=clips,
        export=export,
        publish=publish,
        now=clock.now,
        sleep=sleep,
    )
    return runtime, calls, clock


# ------------------------------------------------------------------ decisions


def test_round_finishes_when_the_clip_cap_is_reached() -> None:
    assert round_finished(elapsed=12, minutes=30, clips=4, max_clips=4) is True


def test_round_finishes_when_the_clock_runs_out() -> None:
    assert round_finished(elapsed=30 * 60, minutes=30, clips=0, max_clips=4) is True
    assert round_finished(elapsed=29 * 60, minutes=30, clips=0, max_clips=4) is False


def test_round_ignores_the_cap_when_it_is_zero() -> None:
    assert round_finished(elapsed=10, minutes=30, clips=99, max_clips=0) is False


def test_rounds_zero_means_run_until_interrupted() -> None:
    assert more_rounds(50, 0) is True
    assert more_rounds(1, 3) is True
    assert more_rounds(3, 3) is False


def test_pick_stream_takes_the_biggest_unwatched_room() -> None:
    streams = [Stream("https://x/a"), Stream("https://x/b"), Stream("https://x/c")]
    assert pick_stream(streams, set()).url == "https://x/a"
    assert pick_stream(streams, {"https://x/a"}).url == "https://x/b"
    assert pick_stream(streams, {"https://x/a", "https://x/b", "https://x/c"}) is None
    assert pick_stream([], set()) is None


# ----------------------------------------------------------------------- loop


def test_run_watches_one_round_then_stops_the_monitor() -> None:
    runtime, calls, _ = make_runtime()
    options = AutopilotOptions(source="https://x/a", rounds=1, minutes=30, max_clips=1)

    rounds = run(options, runtime=runtime, on_event=lambda _: None)

    assert len(rounds) == 1
    assert rounds[0].monitor_id == "mon_1"
    assert rounds[0].clips == 2
    assert rounds[0].exported == 2
    assert rounds[0].published == 0  # not live
    assert calls["stop"] == ["mon_1"]
    assert calls["publish"] == []  # publish is never called in a dry loop


def test_run_publishes_each_round_only_when_live() -> None:
    runtime, calls, _ = make_runtime()
    options = AutopilotOptions(source="https://x/a", rounds=2, minutes=30, max_clips=1, live=True)

    rounds = run(options, runtime=runtime, on_event=lambda _: None)

    assert len(rounds) == 2
    assert calls["publish"] == ["mon_1", "mon_2"]
    assert [item.published for item in rounds] == [1, 1]


def test_run_sleeps_the_cooldown_between_rounds() -> None:
    runtime, calls, _ = make_runtime()
    options = AutopilotOptions(
        source="https://x/a", rounds=2, minutes=30, max_clips=1, cooldown_seconds=90
    )

    run(options, runtime=runtime, on_event=lambda _: None)

    assert 90 in calls["sleep"]


def test_run_discovers_a_new_room_each_round() -> None:
    streams = [Stream("https://x/a", "A"), Stream("https://x/b", "B")]
    runtime, calls, _ = make_runtime(streams=streams)
    options = AutopilotOptions(query="gta 6", rounds=2, minutes=30, max_clips=1)

    rounds = run(options, runtime=runtime, on_event=lambda _: None)

    assert [r.source for r in rounds] == ["https://x/a", "https://x/b"]
    assert calls["discover"]  # discovery actually ran


def test_run_stops_when_no_room_is_found() -> None:
    runtime, calls, _ = make_runtime(streams=[])
    options = AutopilotOptions(query="nothing", rounds=0, minutes=30, max_clips=1)

    rounds = run(options, runtime=runtime, on_event=lambda _: None)

    assert rounds == []
    assert calls["start"] == []


def test_run_survives_a_room_that_will_not_start() -> None:
    streams = [Stream("https://x/a"), Stream("https://x/b")]
    runtime, calls, _ = make_runtime(streams=streams, fail_start=True)
    options = AutopilotOptions(query="gta 6", rounds=2, minutes=30, max_clips=1)

    rounds = run(options, runtime=runtime, on_event=lambda _: None)

    assert len(rounds) == 2
    assert all(r.skipped for r in rounds)
    assert calls["start"] == []


def test_run_stops_the_monitor_when_interrupted() -> None:
    runtime, calls, _ = make_runtime(interrupt_on_clips=True)
    options = AutopilotOptions(source="https://x/a", rounds=0, minutes=30, max_clips=1)

    rounds = run(options, runtime=runtime, on_event=lambda _: None)

    assert len(rounds) == 1
    assert calls["stop"] == ["mon_1"]
    assert calls["publish"] == []


# ------------------------------------------------------------- money integration

# Clips made here live in the session-wide test database, where every other
# money test reads the same queue. Reject them after each test so a finished
# integration test never crowds out a ranking assertion elsewhere.
_CREATED: list[str] = []


@pytest.fixture(autouse=True)
def _no_queue_leak():
    yield
    from clipbait.money import mark_rejected

    for clip_id in _CREATED:
        mark_rejected(clip_id, reason="autopilot test cleanup")
    _CREATED.clear()


def _make_ready(*, monitor_id: str, title: str = "A moment", score: float = 74.0) -> dict:
    project = db.create_project(
        {
            "title": "Autopilot project",
            "source_kind": "local",
            "duration": 300.0,
            "options": {"region": "US", "platform": "tiktok", "language": "en"},
        }
    )
    clip = db.create_clip(
        {
            "project_id": project["id"],
            "title": title,
            "hook": title,
            "description": "why it happened",
            "hashtags": ["#shorts"],
            "start": 5.0,
            "end": 45.0,
            "score": int(score),
            "status": "ready",
            "meta": {
                "region": "US",
                "platform": "tiktok",
                "monitor_id": monitor_id,
                "forecast": {"overall": score, "best_time": "18:00-22:00", "reasons": ["hook"]},
            },
        }
    )
    path = storage.project_dir(project["id"]) / "clips" / f"{clip['id']}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
    _CREATED.append(clip["id"])
    return db.update("clips", clip["id"], {"render_path": storage.relative(path)}) or clip


def test_export_monitor_packages_only_that_monitors_clips() -> None:
    mine = _make_ready(monitor_id="mon_mine")
    _make_ready(monitor_id="mon_other")

    written = export_monitor("mon_mine", AutopilotOptions(min_score=0.0))

    assert written == 1
    bundle = storage.exports_dir()
    folders = [path for path in Path(bundle).glob("*") if path.is_dir()]
    assert folders
    assert any((folder / "clip.mp4").exists() for folder in folders)
    assert db.get_clip(mine["id"])


def test_publish_monitor_is_a_dry_run_unless_live() -> None:
    clip = _make_ready(monitor_id="mon_dry")

    sent = publish_monitor("mon_dry", AutopilotOptions(min_score=0.0, live=False))

    assert sent == 1  # a plan counts, but nothing was sent
    assert not (db.get_clip(clip["id"]).get("meta") or {}).get("uploaded")


def test_publish_monitor_requires_a_source_of_clips() -> None:
    # A monitor with no clips publishes nothing, never fails.
    assert publish_monitor("mon_empty", AutopilotOptions(min_score=0.0)) == 0
