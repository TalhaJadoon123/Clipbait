"""The roster supervisor picks the biggest live rooms and gates what it posts.

The decisions that matter: rank by viewers, honour a minimum audience, keep adult
rooms out unless told otherwise, restrict posting to cleared channels when asked,
and build an autopilot command that posts only the rooms it may.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_supervisor():
    spec = importlib.util.spec_from_file_location(
        "supervise_roster", REPO_ROOT / "scripts" / "supervise_roster.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Register before exec: a frozen dataclass resolves its own string
    # annotations through sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sup = _load_supervisor()


class _Stream:
    def __init__(self, channel: str, title: str, url: str, viewers: int = 1000) -> None:
        self.channel = channel
        self.title = title
        self.url = url
        self.viewers = viewers


def _namespace(**overrides) -> argparse.Namespace:
    base = dict(
        rounds=0,
        minutes=30,
        max_clips=6,
        threshold=2.0,
        content_threshold=1.3,
        platform="shorts",
        min_score=20.0,
        hashtags="#shorts #clips",
        campaign="",
        live=True,
        now=True,
        dry=False,
        passthrough=[],
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_select_rooms_ranks_by_viewers_and_keeps_the_top_n():
    streams = [
        _Stream("Small", "chatting", "u-small", viewers=500),
        _Stream("Big", "GTA 6", "u-big", viewers=40000),
        _Stream("Mid", "valorant", "u-mid", viewers=9000),
    ]
    picked = sup.select_rooms(streams, min_viewers=3000, limit=2)
    assert [d.url for d in picked] == ["u-big", "u-mid"]


def test_select_rooms_drops_rooms_below_the_viewer_floor():
    streams = [_Stream("Tiny", "x", "u-tiny", viewers=120)]
    assert sup.select_rooms(streams, min_viewers=3000, limit=2) == []


def test_select_rooms_skips_mature_rooms_by_default():
    streams = [
        _Stream("AdultCam", "18+ late night", "u-adult", viewers=9000),
        _Stream("Gamer", "ranked grind", "u-game", viewers=9000),
    ]
    picked = sup.select_rooms(streams, min_viewers=100, limit=5)
    assert [d.url for d in picked] == ["u-game"]


def test_select_rooms_can_post_mature_when_asked():
    streams = [_Stream("AdultCam", "18+ late night", "u-adult", viewers=9000)]
    picked = sup.select_rooms(streams, min_viewers=100, mature_policy="post", limit=5)
    assert len(picked) == 1 and picked[0].mature is True


def test_select_rooms_priority_streamer_outranks_bigger_rooms():
    """A favoured streamer is clipped even when someone bigger is live."""
    streams = [
        _Stream("SomeOther", "just chatting", "u-other", viewers=90000),
        _Stream("iShowSpeed", "IRL stream", "u-speed", viewers=8000),
    ]
    picked = sup.select_rooms(streams, priority=["ishowspeed"], min_viewers=100, limit=1)
    assert [d.url for d in picked] == ["u-speed"]
    assert picked[0].priority is True


def test_select_rooms_without_priority_still_ranks_by_viewers():
    streams = [
        _Stream("SomeOther", "just chatting", "u-other", viewers=90000),
        _Stream("iShowSpeed", "IRL stream", "u-speed", viewers=8000),
    ]
    picked = sup.select_rooms(streams, min_viewers=100, limit=1)
    assert [d.url for d in picked] == ["u-other"]


def test_select_rooms_honours_a_named_roster():
    streams = [
        _Stream("Kai Cenat", "GTA 6 finale", "u-kai", viewers=60000),
        _Stream("SomeOther", "just chatting", "u-other", viewers=80000),
    ]
    picked = sup.select_rooms(streams, roster=["kai cenat"], min_viewers=100, limit=5)
    assert [d.url for d in picked] == ["u-kai"]


def test_select_rooms_require_rights_keeps_only_cleared_channels():
    streams = [
        _Stream("Cleared", "GTA 6", "u-cleared", viewers=9000),
        _Stream("Stranger", "GTA 6", "u-stranger", viewers=50000),
    ]
    picked = sup.select_rooms(
        streams, min_viewers=100, rights=["cleared"], require_rights=True, limit=5
    )
    assert [d.url for d in picked] == ["u-cleared"]


def test_build_command_posts_only_when_allowed():
    allowed = sup.Decision(
        url="u1", channel="Kai Cenat", title="t", viewers=1, mature=False, may_post=True
    )
    blocked = sup.Decision(
        url="u2", channel="Stranger", title="t", viewers=1, mature=False, may_post=False
    )
    on = sup.build_autopilot_command(_namespace(), allowed)
    off = sup.build_autopilot_command(_namespace(), blocked)
    assert "--live" in on and "--now" in on
    assert "--live" not in off  # not cleared: package only
    assert on[on.index("--source") + 1] == "u1"
    # The channel drives the title and the leading hashtag.
    assert on[on.index("--title") + 1] == "Kai Cenat"
    assert "#kaicenat" in on[on.index("--hashtags") + 1]


def test_build_command_dry_run_never_posts():
    allowed = sup.Decision(
        url="u1", channel="Kai", title="t", viewers=1, mature=False, may_post=True
    )
    command = sup.build_autopilot_command(_namespace(dry=True), allowed)
    assert "--live" not in command and "--now" not in command


def test_resolve_channels_keeps_only_live_rooms(monkeypatch):
    from clipbait import discover

    live = discover.LiveStream(
        "YouTube", "iShowSpeed", "EXPLORING VICE CITY", "u-speed", 42000, "1h02m"
    )

    def fake(channel, *, timeout=60, runner=discover._runner):
        return live if channel == "IShowSpeed" else None

    monkeypatch.setattr(discover, "resolve_channel_live", fake)
    rooms = sup.resolve_channels(["IShowSpeed", "OfflineGuy"], timeout=5)
    assert [r.url for r in rooms] == ["u-speed"]


def test_resolve_channels_survives_a_channel_that_errors(monkeypatch):
    from clipbait import discover

    def boom(channel, *, timeout=60, runner=discover._runner):
        raise discover.DiscoverError("network")

    monkeypatch.setattr(discover, "resolve_channel_live", boom)
    assert sup.resolve_channels(["Broken"], timeout=5) == []


def test_prune_disk_applies_plan_and_survives_errors(monkeypatch):
    from clipbait import retention

    calls = {"applied": []}
    monkeypatch.setattr(
        retention,
        "plan",
        lambda *, settle_seconds: [
            retention.PruneTarget(sup.Path("/tmp/x"), "monitor", 1024, "idle 7h")
        ],
    )
    monkeypatch.setattr(retention, "apply", lambda targets: calls["applied"].extend(targets) or 1024)

    sup.prune_disk(6 * 3600)  # does not raise
    assert len(calls["applied"]) == 1

    def boom(*, settle_seconds):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(retention, "plan", boom)
    sup.prune_disk(6 * 3600)  # a prune failure must never stop the roster
