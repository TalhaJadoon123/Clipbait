"""Source ingestion: yt-dlp orchestration, progress accounting and local copies.

The downloader is exercised against a real shell script standing in for
``yt-dlp``, so ``Popen`` streaming, progress parsing and file normalisation run
for real without touching the network.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import make_video

from clipbait import media, storage
from clipbait.config import get_settings
from clipbait.ingest import downloader
from clipbait.ingest.downloader import (
    DownloadTracker,
    _cleanup_sidecars,
    _download_message,
    _link,
    _ytdlp_base,
    fetch_audio,
    fetch_remote,
    fetch_section,
    ingest_local,
    probe_url,
    source_kind_for,
)

# ------------------------------------------------------------------- url kinds


@pytest.mark.parametrize(
    "url,kind",
    [
        ("https://www.youtube.com/watch?v=x", "youtube"),
        ("https://youtu.be/x", "youtube"),
        ("https://m.youtube.com/watch?v=x", "youtube"),
        ("https://www.twitch.tv/someone", "twitch"),
        ("https://kick.com/someone", "kick"),
        ("https://vimeo.com/123", "upload"),
        ("https://example.com/video.mp4", "upload"),
        ("not a url at all", "upload"),
    ],
)
def test_source_kind_for(url, kind):
    assert source_kind_for(url) == kind


def test_ytdlp_base_flags(monkeypatch):
    settings = dataclasses.replace(get_settings(), ytdlp="/opt/yt-dlp")
    monkeypatch.setattr(downloader, "get_settings", lambda: settings)
    base = _ytdlp_base()
    assert base[0] == "/opt/yt-dlp"
    assert "--no-playlist" in base
    assert "--concurrent-fragments" in base
    assert "--continue" in base


# ------------------------------------------------------------- download tracker


def test_download_tracker_counts_streams():
    tracker = DownloadTracker()
    assert tracker.feed("[info] abc: Downloading 1 format(s): 397+251") is None
    assert tracker.files == 2

    assert tracker.feed("[download] Destination: video.f137.mp4") is None
    fraction, message = tracker.feed(
        "[download]  50.0% of  188.42MiB at    2.34MiB/s ETA 01:20"
    )
    assert fraction == pytest.approx(0.25)
    assert message.startswith("50%")
    assert "2.34MiB/s" in message
    assert "ETA 01:20" in message

    tracker.feed("[download] Destination: video.f251.m4a")
    fraction, _ = tracker.feed("[download] 100.0% of 10.00MiB at 1MiB/s ETA 00:01")
    assert fraction == pytest.approx(0.995)


def test_download_tracker_handles_postprocess_and_noise():
    tracker = DownloadTracker()
    assert tracker.feed("some unrelated line") is None
    assert tracker.feed("[Merger] Merging formats into video.mp4") == (
        0.99,
        "Merging audio and video",
    )
    assert tracker.feed("[download] 100% of 1MiB") is not None


def test_download_tracker_trusts_observed_destinations():
    tracker = DownloadTracker()
    # No info line: the first destination alone must not break the fraction.
    tracker.feed("[download] Destination: a.mp4")
    fraction, _ = tracker.feed("[download]  50.0% of 1MiB at 1MiB/s ETA 00:01")
    assert fraction == pytest.approx(0.5)


def test_download_message_cleans_unknown_parts():
    assert _download_message(42.5, None, None) == "42%"
    assert _download_message(42.5, "unknown", "unknown") == "42%"
    assert _download_message(42.5, " 1MiB/s ", "00:10") == "42% · 1MiB/s · ETA 00:10"


# ----------------------------------------------------------------- fetch_remote

FAKE_YTDLP = """#!/bin/sh
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then out="$arg"; fi
  prev="$arg"
done
dir=$(dirname "$out")
echo "[info] abc: Downloading 1 format(s): 397+251"
echo "[download] Destination: $dir/video.f137.mp4"
echo "[download]  50.0% of  10.00MiB at  2.00MiB/s ETA 00:05"
echo "[download] 100.0% of  10.00MiB at  2.00MiB/s ETA 00:00"
echo "[download] Destination: $dir/video.f251.m4a"
echo "[download] 100.0% of   2.00MiB at  1.00MiB/s ETA 00:00"
echo "[Merger] Merging formats into '$dir/video.mp4'"
printf 'fake-video-bytes' > "$dir/video.mp4"
printf '%s' '{"title":"Fake Title","duration":12.5,"webpage_url":"https://example.com/watch?v=abc"}' > "$dir/source.info.json"
exit 0
"""


def fake_ytdlp(tmp_path: Path, body: str = FAKE_YTDLP) -> Path:
    path = tmp_path / "yt-dlp"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture()
def remote_project():
    from clipbait import db

    return db.create_project(
        {"title": "Remote", "source_kind": "upload", "source_url": "https://example.com/v"}
    )


def test_fetch_remote_downloads_and_normalises(tmp_path, remote_project, monkeypatch):
    monkeypatch.setattr(downloader, "ensure_safe_url", lambda url: url)
    settings = dataclasses.replace(get_settings(), ytdlp=str(fake_ytdlp(tmp_path)))
    monkeypatch.setattr(downloader, "get_settings", lambda: settings)

    updates: list[tuple[float, str]] = []
    result = fetch_remote(
        "https://www.youtube.com/watch?v=abc",
        remote_project["id"],
        on_progress=lambda fraction, message: updates.append((fraction, message)),
    )

    assert result.title == "Fake Title"
    assert result.duration == 12.5
    assert result.source_kind == "youtube"
    assert result.webpage_url == "https://example.com/watch?v=abc"
    assert result.path == storage.source_path(remote_project["id"])
    assert result.path.read_bytes() == b"fake-video-bytes"
    assert updates[0][0] >= 0
    assert updates[-1] == (1.0, "Download complete")
    fractions = [fraction for fraction, _ in updates]
    assert fractions == sorted(fractions)


def test_fetch_remote_falls_back_to_probe_for_duration(tmp_path, remote_project, monkeypatch):
    body = FAKE_YTDLP.replace('"duration":12.5,', "")
    monkeypatch.setattr(downloader, "ensure_safe_url", lambda url: url)
    settings = dataclasses.replace(get_settings(), ytdlp=str(fake_ytdlp(tmp_path, body)))
    monkeypatch.setattr(downloader, "get_settings", lambda: settings)
    monkeypatch.setattr(
        downloader.media, "probe", lambda path: SimpleNamespace(duration=7.0)
    )

    result = fetch_remote("https://example.com/v.mp4", remote_project["id"])
    assert result.duration == 7.0
    assert result.source_kind == "upload"


def test_fetch_remote_survives_broken_info_json(tmp_path, remote_project, monkeypatch):
    body = FAKE_YTDLP.replace(
        """printf '%s' '{"title":"Fake Title","duration":12.5,"webpage_url":"https://example.com/watch?v=abc"}' > "$dir/source.info.json\"""",
        """printf '%s' 'not json' > "$dir/source.info.json\"""",
    )
    monkeypatch.setattr(downloader, "ensure_safe_url", lambda url: url)
    settings = dataclasses.replace(get_settings(), ytdlp=str(fake_ytdlp(tmp_path, body)))
    monkeypatch.setattr(downloader, "get_settings", lambda: settings)
    monkeypatch.setattr(
        downloader.media, "probe", lambda path: SimpleNamespace(duration=3.0)
    )
    result = fetch_remote("https://example.com/v.mp4", remote_project["id"])
    assert result.title == "Untitled"
    assert result.duration == 3.0


def test_fetch_remote_reports_a_failed_command(tmp_path, remote_project, monkeypatch):
    script = fake_ytdlp(tmp_path, "#!/bin/sh\necho 'ERROR: video unavailable'\nexit 3\n")
    monkeypatch.setattr(downloader, "ensure_safe_url", lambda url: url)
    settings = dataclasses.replace(get_settings(), ytdlp=str(script))
    monkeypatch.setattr(downloader, "get_settings", lambda: settings)

    with pytest.raises(media.MediaError, match="yt-dlp failed"):
        fetch_remote("https://example.com/v.mp4", remote_project["id"])


def test_fetch_remote_reports_a_missing_output(tmp_path, remote_project, monkeypatch):
    script = fake_ytdlp(tmp_path, "#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(downloader, "ensure_safe_url", lambda url: url)
    settings = dataclasses.replace(get_settings(), ytdlp=str(script))
    monkeypatch.setattr(downloader, "get_settings", lambda: settings)

    with pytest.raises(media.MediaError, match="no video file"):
        fetch_remote("https://example.com/v.mp4", remote_project["id"])


def test_fetch_remote_missing_binary(tmp_path, remote_project, monkeypatch):
    monkeypatch.setattr(downloader, "ensure_safe_url", lambda url: url)
    settings = dataclasses.replace(get_settings(), ytdlp=str(tmp_path / "nope"))
    monkeypatch.setattr(downloader, "get_settings", lambda: settings)

    with pytest.raises(media.MediaError, match="missing binary"):
        fetch_remote("https://example.com/v.mp4", remote_project["id"])


def test_fetch_remote_without_a_progress_callback(tmp_path, remote_project, monkeypatch):
    monkeypatch.setattr(downloader, "ensure_safe_url", lambda url: url)
    settings = dataclasses.replace(get_settings(), ytdlp=str(fake_ytdlp(tmp_path)))
    monkeypatch.setattr(downloader, "get_settings", lambda: settings)
    result = fetch_remote("https://example.com/v.mp4", remote_project["id"])
    assert result.path.exists()


# --------------------------------------------------------------------- fetch_audio

FAKE_AUDIO_YTDLP = """#!/bin/sh
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then out="$arg"; fi
  prev="$arg"
done
dir=$(dirname "$out")
echo "[info] abc: Downloading 1 format(s): 251"
echo "[download] 100.0% of   2.00MiB at  1.00MiB/s ETA 00:00"
printf 'fake-audio-bytes' > "$dir/link_audio.m4a"
printf '%s' '{"title":"Audio Title","duration":42.5}' > "$dir/link_audio.info.json"
exit 0
"""


def test_fetch_audio_downloads_only_audio(tmp_path, remote_project, monkeypatch):
    monkeypatch.setattr(downloader, "ensure_safe_url", lambda url: url)
    settings = dataclasses.replace(
        get_settings(), ytdlp=str(fake_ytdlp(tmp_path, FAKE_AUDIO_YTDLP))
    )
    monkeypatch.setattr(downloader, "get_settings", lambda: settings)

    updates: list[tuple[float, str]] = []
    result = fetch_audio(
        "https://www.youtube.com/watch?v=abc",
        remote_project["id"],
        on_progress=lambda fraction, message: updates.append((fraction, message)),
    )

    audio = storage.link_audio_path(remote_project["id"])
    assert result.path == audio
    assert audio.read_bytes() == b"fake-audio-bytes"
    assert result.title == "Audio Title"
    assert result.duration == 42.5
    # The whole point of link mode: no video file is created.
    assert not storage.source_path(remote_project["id"]).exists()
    assert updates[-1] == (1.0, "Audio ready")


def test_fetch_audio_renames_a_different_extension(tmp_path, remote_project, monkeypatch):
    body = FAKE_AUDIO_YTDLP.replace('link_audio.m4a"', 'link_audio.opus"')
    monkeypatch.setattr(downloader, "ensure_safe_url", lambda url: url)
    settings = dataclasses.replace(get_settings(), ytdlp=str(fake_ytdlp(tmp_path, body)))
    monkeypatch.setattr(downloader, "get_settings", lambda: settings)
    monkeypatch.setattr(downloader.media, "probe", lambda path: SimpleNamespace(duration=5.0))

    result = fetch_audio("https://example.com/v", remote_project["id"])
    assert result.path.name == "link_audio.m4a"
    assert result.path.read_bytes() == b"fake-audio-bytes"
    assert result.duration == 42.5


def test_fetch_audio_reports_no_audio(tmp_path, remote_project, monkeypatch):
    monkeypatch.setattr(downloader, "ensure_safe_url", lambda url: url)
    settings = dataclasses.replace(
        get_settings(), ytdlp=str(fake_ytdlp(tmp_path, "#!/bin/sh\nexit 0\n"))
    )
    monkeypatch.setattr(downloader, "get_settings", lambda: settings)

    with pytest.raises(media.MediaError, match="no audio file"):
        fetch_audio("https://example.com/v", remote_project["id"])


# ------------------------------------------------------------------- fetch_section

FAKE_SECTION_YTDLP = """#!/bin/sh
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then out="$arg"; fi
  prev="$arg"
done
printf 'section-bytes' > "$out"
exit 0
"""


def test_fetch_section_requests_only_the_window(tmp_path, remote_project, monkeypatch):
    monkeypatch.setattr(downloader, "ensure_safe_url", lambda url: url)
    settings = dataclasses.replace(
        get_settings(), ytdlp=str(fake_ytdlp(tmp_path, FAKE_SECTION_YTDLP))
    )
    monkeypatch.setattr(downloader, "get_settings", lambda: settings)

    target = storage.clip_window_path(remote_project["id"], "clip_test")
    result = fetch_section("https://example.com/v", 12.0, 41.5, target)
    assert result == target
    assert target.read_bytes() == b"section-bytes"


def test_fetch_section_reports_no_output(tmp_path, remote_project, monkeypatch):
    monkeypatch.setattr(downloader, "ensure_safe_url", lambda url: url)
    settings = dataclasses.replace(
        get_settings(), ytdlp=str(fake_ytdlp(tmp_path, "#!/bin/sh\nexit 0\n"))
    )
    monkeypatch.setattr(downloader, "get_settings", lambda: settings)

    with pytest.raises(media.MediaError, match="no file"):
        fetch_section(
            "https://example.com/v", 1, 2, storage.clip_window_path("prj_x", "clip_y")
        )


# --------------------------------------------------------------------- probe_url


def test_probe_url_success_and_failure(monkeypatch):
    monkeypatch.setattr(downloader, "ensure_safe_url", lambda url: url)
    payload = {
        "title": "A talk",
        "duration": 900,
        "thumbnail": "https://img/x.jpg",
        "uploader": "someone",
        "is_live": True,
        "live_status": "is_live",
    }
    monkeypatch.setattr(
        downloader.media,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload)),
    )
    meta = probe_url("https://example.com/v")
    assert meta["title"] == "A talk"
    assert meta["is_live"] is True

    monkeypatch.setattr(
        downloader.media, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="")
    )
    assert probe_url("https://example.com/v") == {}

    monkeypatch.setattr(
        downloader.media, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="{bad")
    )
    assert probe_url("https://example.com/v") == {}


# ------------------------------------------------------------------ ingest_local


def test_ingest_local_copies_a_file(tmp_path):
    source = make_video(tmp_path / "talk.mp4", seconds=1, size="160x90")
    result = ingest_local(source, "prj_local", title="Custom")
    assert result.source_kind == "local"
    assert result.title == "Custom"
    assert result.duration > 0
    assert result.path == storage.source_path("prj_local")
    assert result.path.read_bytes() == source.read_bytes()


def test_ingest_local_defaults_the_title_to_the_stem(tmp_path):
    source = make_video(tmp_path / "my-talk.mp4", seconds=1, size="160x90")
    assert ingest_local(source, "prj_stem").title == "my-talk"


def test_ingest_local_missing_file(tmp_path):
    with pytest.raises(media.MediaError, match="file not found"):
        ingest_local(tmp_path / "missing.mp4", "prj_missing")


def test_ingest_local_skips_a_copy_when_already_in_place(tmp_path):
    target = storage.source_path("prj_inplace")
    make_video(target, seconds=1, size="160x90")
    result = ingest_local(target, "prj_inplace")
    assert result.path == target
    assert result.path.exists()


def test_ingest_local_uses_link_for_large_files(tmp_path, monkeypatch):
    source = tmp_path / "big.mp4"
    source.write_bytes(b"x" * 32)
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(downloader, "_link", lambda src, dst: calls.append((src, dst)))
    # Pretend the file is over the 2GB copy threshold without writing 2GB.
    real_stat = Path.stat

    def fake_stat(self, *args, **kwargs):
        info = real_stat(self, *args, **kwargs)
        if self == source:
            return SimpleNamespace(st_size=3 * 1024**3)
        return info

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(downloader.media, "probe", lambda path: SimpleNamespace(duration=1.0))
    result = ingest_local(source, "prj_big")
    assert calls and calls[0][0] == source
    assert result.duration == 1.0


def test_link_copies_content(tmp_path):
    source = tmp_path / "a.bin"
    source.write_bytes(b"payload")
    target = tmp_path / "b.bin"
    _link(source, target)
    assert target.read_bytes() == b"payload"


# ----------------------------------------------------------------- sidecars


def test_cleanup_sidecars_only_removes_partials(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"keep")
    part = tmp_path / "video.mp4.part"
    part.write_bytes(b"drop")
    ytdl = tmp_path / "video.ytdl"
    ytdl.write_bytes(b"drop")
    (tmp_path / "notes.txt").write_text("keep")

    _cleanup_sidecars(tmp_path)

    assert video.exists()
    assert not part.exists()
    assert not ytdl.exists()
    assert (tmp_path / "notes.txt").exists()
