"""Live discovery: YouTube's live filter, viewer ranking, no invented numbers."""

from __future__ import annotations

import pytest

from clipbait import discover


def fake_runner(details: dict[str, dict] | None = None, *, flat: dict | None = None):
    details = details or {}
    listed = {
        str(row["id"]): row
        for row in (flat or {}).get("entries", [])
        if isinstance(row, dict) and row.get("id")
    }

    def runner(args: list[str], timeout: int) -> dict:
        if "--flat-playlist" in args:
            return flat or {"entries": []}
        call = " ".join(args)
        for stream_id, payload in details.items():
            if stream_id in call:
                return payload
        for stream_id, row in listed.items():
            if stream_id in call:
                return row
        return {"is_live": False}

    return runner


def entry(stream_id: str, title: str, channel: str, *, viewers: int | None = None) -> dict:
    row = {
        "id": stream_id,
        "title": title,
        "channel": channel,
        "is_live": True,
        "webpage_url": f"https://www.youtube.com/watch?v={stream_id}",
        "live_for": "1h05m",
    }
    if viewers is not None:
        row["concurrent_view_count"] = viewers
    return row


def test_live_search_keeps_only_live_and_sorts_by_viewers() -> None:
    flat = {
        "entries": [
            entry("aaa", "small room", "Nobody", viewers=900),
            {"id": "vod", "title": "old vod", "channel": "X", "is_live": False},
            entry("bbb", "big room", "Kai", viewers=55000),
        ]
    }
    streams = discover.search_live("gta 6", limit=10, runner=fake_runner(flat=flat))

    assert [stream.channel for stream in streams] == ["Kai", "Nobody"]
    assert streams[0].viewers == 55000
    assert streams[0].url.endswith("bbb")
    assert discover.best(streams) is streams[0]
    assert all(stream.platform == "YouTube" for stream in streams)


def test_missing_viewer_count_is_resolved_per_stream() -> None:
    flat = {"entries": [entry("ccc", "unresolved", "Someone")]}
    details = {
        "ccc": {
            "id": "ccc",
            "title": "unresolved",
            "channel": "Someone",
            "is_live": True,
            "webpage_url": "https://www.youtube.com/watch?v=ccc",
            "concurrent_view_count": 12345,
        }
    }
    streams = discover.search_live("gta", limit=5, runner=fake_runner(details, flat=flat))

    assert len(streams) == 1
    assert streams[0].viewers == 12345


def test_flat_search_entries_without_is_live_are_still_ranked() -> None:
    """YouTube's live-filtered flat result omits is_live: resolving is required."""
    flat = {
        "entries": [
            {
                "id": "fff",
                "url": "https://www.youtube.com/watch?v=fff",
                "title": "PLAYING GTA 6 RIGHT NOW",
                "channel": "IShowSpeed",
                "duration": None,
            }
        ]
    }
    details = {
        "fff": {
            "id": "fff",
            "title": "PLAYING GTA 6 RIGHT NOW",
            "channel": "IShowSpeed",
            "is_live": True,
            "webpage_url": "https://www.youtube.com/watch?v=fff",
            "concurrent_view_count": 54679,
        }
    }
    streams = discover.search_live("gta 6", limit=5, runner=fake_runner(details, flat=flat))
    assert len(streams) == 1
    assert streams[0].viewers == 54679


def test_a_dead_stream_does_not_sink_the_list() -> None:
    flat = {"entries": [entry("ddd", "one", "A"), entry("eee", "two", "B", viewers=5)]}

    def runner(args: list[str], timeout: int) -> dict:
        if "--flat-playlist" in args:
            return flat
        raise discover.DiscoverError("gone")

    streams = discover.search_live("gta", limit=5, runner=runner)
    assert [stream.channel for stream in streams] == ["B", "A"]


def test_empty_search_is_reported_not_faked() -> None:
    assert discover.search_live("nothing", runner=fake_runner()) == []
    markdown = discover.as_markdown("nothing", [], stamp="2026-01-01 00:00")
    assert "No live streams found" in markdown


def test_markdown_table_carries_the_evidence() -> None:
    streams = [discover.LiveStream("YouTube", "Kai", "GTA 6 | day 2", "https://x", 55000, "2h10m")]
    markdown = discover.as_markdown("gta 6", streams, stamp="2026-01-01 00:00")
    assert "| 1 | Kai | GTA 6 / day 2 | 55,000 | 2h10m | https://x |" in markdown


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, ""), (59, "0m"), (3600, "1h00m"), (3900, "1h05m"), (None, "")],
)
def test_duration_format(seconds, expected) -> None:
    assert discover.fmt_duration(seconds) == expected


def test_resolve_channel_live_builds_the_live_url_from_a_handle() -> None:
    seen: list[str] = []

    def runner(args: list[str], timeout: int) -> dict:
        seen.append(args[-1])
        return entry("live1", "EXPLORING VICE CITY", "iShowSpeed", viewers=42000)

    stream = discover.resolve_channel_live("IShowSpeed", runner=runner)

    # The handle becomes the channel's /live URL so a title that omits the
    # streamer's name still resolves to the right room.
    assert seen == ["https://www.youtube.com/@IShowSpeed/live"]
    assert stream is not None and stream.channel == "iShowSpeed"
    assert stream.url.endswith("live1")


def test_resolve_channel_live_accepts_a_full_url_without_doubling_live() -> None:
    seen: list[str] = []

    def runner(args: list[str], timeout: int) -> dict:
        seen.append(args[-1])
        return entry("live2", "stream", "Speed", viewers=10)

    discover.resolve_channel_live("https://www.youtube.com/@IShowSpeed/live", runner=runner)
    assert seen == ["https://www.youtube.com/@IShowSpeed/live"]


def test_resolve_channel_live_is_none_when_offline() -> None:
    def runner(args: list[str], timeout: int) -> dict:
        raise discover.DiscoverError("This live event will begin in 7 hours.")

    assert discover.resolve_channel_live("IShowSpeed", runner=runner) is None


def test_resolve_channel_live_is_none_when_not_live() -> None:
    def runner(args: list[str], timeout: int) -> dict:
        return {"id": "vod", "is_live": False, "title": "old"}

    assert discover.resolve_channel_live("IShowSpeed", runner=runner) is None


def test_resolve_channel_live_blank_handle_is_none_without_a_call() -> None:
    def runner(args: list[str], timeout: int) -> dict:  # pragma: no cover - must not run
        raise AssertionError("blank channel should not hit yt-dlp")

    assert discover.resolve_channel_live("   ", runner=runner) is None