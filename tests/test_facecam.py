"""Finding the webcam box in a stream: a still rectangle inside a moving game.

The pictures are drawn, not recorded: a moving game with a still portrait box in
it, on the small grid the detector looks at. Everything ffmpeg would have
produced is supplied directly, so the test measures the geometry, not the codec.
"""

from __future__ import annotations

import numpy as np
import pytest

from clipbait import pipeline
from clipbait.media import MediaInfo
from clipbait.reframe import facecam
from clipbait.reframe.facecam import FacecamBox
from clipbait.schema import ProjectOptions

W, H = facecam.GRID_W, facecam.GRID_H
# A portrait box in the bottom-left, as fractions of the grid: 0.20 x 0.31.
BOX = (2, 92, 66, 148)
SOURCE_W, SOURCE_H = 1280, 720


def _stream(box: tuple[int, int, int, int] | None = BOX, count: int = 24) -> np.ndarray:
    """A game that moves everywhere, with a still portrait box drawn over it."""
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    frames = np.zeros((count, H, W), dtype=np.float32)
    for index in range(count):
        blob = 70 * np.exp(-((xs - (40 + index * 12)) ** 2 + (ys - 40) ** 2) / (2 * 22.0**2))
        texture = 35 * np.sin((xs + index * 7) / 9.0) * np.cos((ys + index * 5) / 11.0)
        frames[index] = 100 + blob + texture
    if box is not None:
        x0, y0, x1, y1 = box
        portrait = 70 + 25 * ((xs[: y1 - y0, : x1 - x0] - x0) / 64.0)
        for index in range(count):
            frames[index][y0:y1, x0:x1] = portrait
    return np.clip(frames, 0, 255).astype(np.uint8)


@pytest.fixture
def stream(monkeypatch):
    """Point the detector at drawn pictures instead of a video, at 4x the grid."""
    frames = _stream()
    full = np.repeat(np.repeat(frames[0].astype(np.float32), 4, axis=0), 4, axis=1)
    monkeypatch.setattr(facecam, "_frames", lambda path, count=facecam.MAX_FRAMES: frames)
    monkeypatch.setattr(
        facecam,
        "_full_frame",
        lambda path, width, height, duration: full,
    )
    monkeypatch.setattr(
        facecam.media,
        "probe",
        lambda path: MediaInfo(
            duration=60.0, width=SOURCE_W, height=SOURCE_H, fps=30.0, has_audio=True
        ),
    )
    return frames


def test_finds_the_still_box_in_a_moving_game(stream) -> None:
    found = facecam.detect("stream.mp4")
    assert found is not None
    # The box was drawn at (2, 92, 66, 148) on the grid, i.e. 4x that in source
    # pixels; a border that sits on the frame's own edge is reported as the edge.
    assert found.corner in {"left", "bottom-left"}
    assert found.conf == "high"
    x0, y0, x1, y1 = found.fractions(SOURCE_W, SOURCE_H)
    assert x0 <= 0.02
    assert abs(y0 - 92 / H) < 0.02
    assert abs(x1 - 66 / W) < 0.02
    assert abs(y1 - 148 / H) < 0.02


def test_a_stream_with_no_webcam_reports_nothing(monkeypatch) -> None:
    frames = _stream(box=None)
    monkeypatch.setattr(facecam, "_frames", lambda path, count=facecam.MAX_FRAMES: frames)
    monkeypatch.setattr(
        facecam.media,
        "probe",
        lambda path: MediaInfo(
            duration=60.0, width=SOURCE_W, height=SOURCE_H, fps=30.0, has_audio=True
        ),
    )
    assert facecam.detect("game-only.mp4") is None


def test_too_few_frames_is_no_box(monkeypatch) -> None:
    monkeypatch.setattr(facecam, "_frames", lambda path, count=facecam.MAX_FRAMES: _stream(count=2))
    assert facecam.detect("short.mp4") is None


def test_without_numpy_it_reports_no_box(monkeypatch) -> None:
    monkeypatch.setattr(facecam, "np", None)
    assert facecam.available() is False
    assert facecam.detect("stream.mp4") is None


def test_the_game_itself_is_never_taken_for_a_camera() -> None:
    frames = _stream(box=None).astype(np.float32)
    still = facecam._stillness(frames)
    light = frames.mean(axis=0)
    game = float(np.median(still))
    # Half the frame, right where the game is moving: not a camera.
    assert facecam._plausible(10, 10, 170, 90, still, light, game) is None


def test_corner_names_the_position_in_the_frame() -> None:
    assert facecam._corner(0, 0, 100, 100, 1280, 720) == "top-left"
    assert facecam._corner(1180, 0, 1280, 100, 1280, 720) == "top-right"
    assert facecam._corner(0, 620, 100, 720, 1280, 720) == "bottom-left"
    assert facecam._corner(1180, 620, 1280, 720, 1280, 720) == "bottom-right"
    assert facecam._corner(400, 300, 700, 500, 1280, 720) == "middle"


def test_fractions_are_frame_relative() -> None:
    box = FacecamBox(x=128, y=72, w=256, h=144, corner="bottom-left", conf="high", score=4.0)
    assert box.fractions(1280, 720) == (0.1, 0.1, 0.3, 0.3)
    # A missing frame size must not divide by zero.
    assert box.fractions(0, 0) == (0.0, 0.0, 1.0, 1.0)


def test_pipeline_only_measures_when_asked_and_then_caches(monkeypatch, tmp_path) -> None:
    project = {"id": "prj_facecam"}
    source = tmp_path / "source.mp4"
    source.write_bytes(b"not really a video")

    calls: list[str] = []

    def fake_detect(path):
        calls.append(str(path))
        return FacecamBox(x=0, y=360, w=256, h=256, corner="bottom-left", conf="high", score=5.0)

    monkeypatch.setattr(pipeline.facecam, "detect", fake_detect)
    monkeypatch.setattr(
        pipeline.media,
        "probe",
        lambda path: MediaInfo(duration=60.0, width=1280, height=720, fps=30.0, has_audio=True),
    )

    off = ProjectOptions(facecam_detect=False, reframe_mode="facecam")
    assert pipeline._facecam_override(project, source, off) is None
    assert calls == []  # nothing measured when the operator did not ask

    # On, but only for the split-screen layout.
    tracking = ProjectOptions(facecam_detect=True, reframe_mode="auto")
    assert pipeline._facecam_override(project, source, tracking) is None
    assert calls == []

    on = ProjectOptions(facecam_detect=True, reframe_mode="facecam")
    first = pipeline._facecam_override(project, source, on)
    assert first == (0.0, 0.5, 0.2, 0.8556)
    assert len(calls) == 1

    # The second clip of the same project reuses the answer instead of decoding
    # the source again.
    assert pipeline._facecam_override(project, source, on) == first
    assert len(calls) == 1
