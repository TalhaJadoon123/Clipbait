"""Ingest download-progress parsing and streaming."""

from __future__ import annotations

import sys

import pytest

from clipbait import media
from clipbait.ingest.downloader import DownloadTracker, _download_message, _run_ytdlp

INFO_TWO = "[info] aqz-KE-bpKQ: Downloading 1 format(s): 397+251"
DEST_VIDEO = "[download] Destination: /data/v.f397.mp4"
DEST_AUDIO = "[download] Destination: /data/v.f251.webm"


def feed(tracker: DownloadTracker, *lines: str) -> list[tuple[float, str]]:
    return [update for line in lines if (update := tracker.feed(line)) is not None]


def test_single_format_download_maps_percent_to_fraction() -> None:
    tracker = DownloadTracker()
    updates = feed(
        tracker,
        "[info] x: Downloading 1 format(s): 22",
        "[download] Destination: v.mp4",
        "[download]  25.0% of 10.00MiB at 1.00MiB/s ETA 00:08",
        "[download] 100% of 10.00MiB in 00:07",
    )
    assert [round(fraction, 3) for fraction, _ in updates] == [0.25, 0.995]


def test_two_stream_download_stays_monotonic() -> None:
    # The point of the tracker: video hits 100%, then audio restarts at 0%.
    # Naively reading the percent would make the bar jump back to zero.
    tracker = DownloadTracker()
    updates = feed(
        tracker,
        INFO_TWO,
        DEST_VIDEO,
        "[download]  50.0% of 20.00MiB at 2.00MiB/s ETA 00:05",
        "[download] 100% of 20.00MiB in 00:09",
        DEST_AUDIO,
        "[download]  50.0% of  5.00MiB at 1.00MiB/s ETA 00:02",
        "[download] 100% of  5.00MiB in 00:04",
    )
    fractions = [fraction for fraction, _ in updates]
    assert fractions == sorted(fractions), "progress must never move backwards"
    assert fractions[0] == pytest.approx(0.25)  # half of the first stream
    assert fractions[-1] == pytest.approx(0.995)


def test_stream_count_is_inferred_without_the_info_line() -> None:
    tracker = DownloadTracker()
    updates = feed(
        tracker,
        DEST_VIDEO,
        "[download]  50.0% of 20.00MiB at 2.00MiB/s ETA 00:05",
        DEST_AUDIO,
        "[download]  50.0% of  5.00MiB at 1.00MiB/s ETA 00:02",
    )
    assert [round(f, 3) for f, _ in updates] == [0.5, 0.75]


def test_postprocessing_reports_merging() -> None:
    tracker = DownloadTracker()
    updates = feed(tracker, INFO_TWO, '[Merger] Merging formats into "v.mp4"')
    assert updates == [(0.99, "Merging audio and video")]


def test_unrelated_lines_are_ignored() -> None:
    tracker = DownloadTracker()
    assert tracker.feed("[youtube] aqz: Downloading webpage") is None
    assert tracker.feed("") is None


def test_message_includes_speed_and_eta_but_hides_unknown() -> None:
    assert _download_message(45.3, "2.34MiB/s", "01:20") == "45% · 2.34MiB/s · ETA 01:20"
    assert _download_message(10.0, "Unknown B/s", "Unknown") == "10%"


def test_run_ytdlp_streams_progress_lines() -> None:
    lines = [INFO_TWO, DEST_VIDEO, "[download]  50.0% of 20.00MiB at 2.00MiB/s ETA 00:05"]
    script = "; ".join(f"print({line!r}, flush=True)" for line in lines)
    seen: list[tuple[float, str]] = []
    _run_ytdlp([sys.executable, "-c", script], lambda f, m: seen.append((f, m)))
    assert seen, "the callback should have fired for the progress line"
    assert seen[-1][0] == pytest.approx(0.25)


def test_run_ytdlp_raises_with_tail_on_failure() -> None:
    with pytest.raises(media.MediaError) as excinfo:
        _run_ytdlp([sys.executable, "-c", "import sys; print('ERROR: nope'); sys.exit(3)"], None)
    assert "yt-dlp failed (3)" in str(excinfo.value)
    assert "ERROR: nope" in str(excinfo.value)


def test_run_ytdlp_terminates_child_when_callback_cancels() -> None:
    class Cancelled(Exception):
        pass

    def cancel(fraction: float, message: str) -> None:
        raise Cancelled

    script = (
        "import time; "
        "print('[download]  10.0% of 1.00MiB at 1.00MiB/s ETA 00:01', flush=True); "
        "time.sleep(30)"
    )
    with pytest.raises(Cancelled):
        _run_ytdlp([sys.executable, "-c", script], cancel)
