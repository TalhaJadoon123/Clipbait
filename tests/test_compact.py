"""Compacting a clip for upload: smaller body, same content, one transcode."""

from __future__ import annotations

from helpers import make_video

from clipbait import media
from clipbait.publish import compact_for_upload


def test_a_small_clip_is_sent_untouched(tmp_path) -> None:
    video = make_video(tmp_path / "clip.mp4", seconds=1)
    assert compact_for_upload(video) == video


def test_a_large_clip_gets_a_720p_upload_copy(tmp_path) -> None:
    video = make_video(tmp_path / "clip.mp4", seconds=1, size="1080x1920")

    out = compact_for_upload(video, max_bytes=1)  # force the shrink

    assert out != video
    assert out.name == "clip.upload.mp4"
    assert out.exists()
    info = media.probe(out)
    assert (info.width, info.height) == (720, 1280)
    # the original render is left alone
    assert media.probe(video).width == 1080


def test_the_compact_copy_is_reused_not_rebuilt(tmp_path) -> None:
    video = make_video(tmp_path / "clip.mp4", seconds=1, size="1080x1920")

    first = compact_for_upload(video, max_bytes=1)
    stamp = first.stat().st_mtime

    second = compact_for_upload(video, max_bytes=1)

    assert second == first
    assert second.stat().st_mtime == stamp  # no second transcode
