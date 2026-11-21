"""The live supervisor pins the autopilot to one streamer's current room.

The decisions that matter: pin to the named channel even when a bigger room is
live, return "offline" cleanly, survive a flaky search, and build an autopilot
command that actually targets the resolved URL.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_supervisor():
    spec = importlib.util.spec_from_file_location(
        "supervise_live", REPO_ROOT / "scripts" / "supervise_live.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sup = _load_supervisor()


class _Stream:
    def __init__(self, channel: str, title: str, url: str) -> None:
        self.channel = channel
        self.title = title
        self.url = url


def _namespace(**overrides) -> argparse.Namespace:
    base = dict(
        rounds=0,
        minutes=30,
        max_clips=4,
        threshold=3.0,
        content_threshold=2.5,
        platform="shorts",
        min_score=35.0,
        title="",
        match="kai cenat",
        hashtags="#kaicenat #gta6",
        campaign="",
        live=True,
        now=True,
        passthrough=[],
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_resolve_room_pins_to_the_named_channel(monkeypatch):
    from clipbait import discover

    streams = [
        _Stream("TheBurntPeanut", "GTA 6 DAY 2", "https://youtu.be/big"),
        _Stream("Kai Cenat Live", "BEATING GTA 6 *FINALE*", "https://youtu.be/kai"),
    ]
    monkeypatch.setattr(discover, "search_live", lambda *a, **k: streams)

    url, label = sup.resolve_room("kai cenat", "gta 6", 12)
    assert url == "https://youtu.be/kai"  # the bigger room is skipped on purpose
    assert "Kai Cenat Live" in label


def test_resolve_room_reports_offline_when_no_room_matches(monkeypatch):
    from clipbait import discover

    monkeypatch.setattr(
        discover,
        "search_live",
        lambda *a, **k: [_Stream("Someone", "GTA 6", "https://youtu.be/x")],
    )
    assert sup.resolve_room("kai cenat", "gta 6", 12) == (None, "")


def test_resolve_room_survives_a_flaky_search(monkeypatch):
    from clipbait import discover

    def boom(*_a, **_k):
        raise RuntimeError("yt-dlp timed out")

    monkeypatch.setattr(discover, "search_live", boom)
    assert sup.resolve_room("kai cenat", "gta 6", 12) == (None, "")


def test_build_autopilot_command_targets_the_source_and_forwards_flags():
    command = sup.build_autopilot_command(_namespace(), "https://youtu.be/kai")
    assert command[1:4] == ["-m", "clipbait.cli", "autopilot"]
    source = command[command.index("--source") + 1]
    assert source == "https://youtu.be/kai"
    assert "--live" in command and "--now" in command
    assert command[command.index("--platform") + 1] == "shorts"
    assert command[command.index("--hashtags") + 1] == "#kaicenat #gta6"
    # Free-form extras ride along untouched.
    extra = sup.build_autopilot_command(_namespace(passthrough=["--min-score", "45"]), "u")
    assert extra[-2:] == ["--min-score", "45"]
    # An empty hashtag string is omitted rather than passed as a blank arg.
    assert "--hashtags" not in sup.build_autopilot_command(_namespace(hashtags=""), "u")
