"""Reframe geometry: layout choice, smoothing and sendcmd de-duplication."""

from __future__ import annotations

from pathlib import Path

import pytest

from clipbait.reframe import ReframePlan, _clamp, _interpolate, _smooth, plan_reframe
from clipbait.reframe.tracker import FaceSample


def test_clamp() -> None:
    assert _clamp(-5, 0, 10) == 0
    assert _clamp(15, 0, 10) == 10
    assert _clamp(5, 0, 10) == 5


def test_smooth_removes_spikes_without_lagging_forever() -> None:
    values = [0.5] * 20 + [1.0] + [0.5] * 20
    smoothed = _smooth(values)
    assert len(smoothed) == len(values)
    assert max(smoothed) < 0.75, "a single outlier must not move the camera fully"
    assert smoothed[-1] == pytest.approx(0.5, abs=0.02)


def test_smooth_handles_empty_input() -> None:
    assert _smooth([]) == []


def test_interpolate_fills_every_frame() -> None:
    samples = [FaceSample(t=0.0, cx=0.2, cy=0.5, scale=0.2), FaceSample(t=2.0, cx=0.8, cy=0.5, scale=0.2)]
    xs, found = _interpolate(samples, fps=10, frames=20)
    assert found is True
    assert len(xs) == 20
    assert xs[0] == pytest.approx(0.2)
    assert xs[-1] == pytest.approx(0.8, abs=0.05)


def test_interpolate_without_faces_defaults_to_centre() -> None:
    xs, found = _interpolate([], fps=10, frames=5)
    assert found is False
    assert xs == [0.5] * 5


def test_interpolate_single_sample_is_static() -> None:
    xs, found = _interpolate([FaceSample(t=0.0, cx=0.3, cy=0.5, scale=0.2)], fps=10, frames=5)
    assert found is True
    assert xs == [0.3] * 5


def test_vertical_source_gets_a_blur_bed() -> None:
    plan = plan_reframe(Path("x.mp4"), 0, 10, 1080, 1920, mode="auto")
    assert plan.mode == "blur"
    assert plan.crop_xs == [0]


def test_wide_source_without_vision_falls_back_to_centre() -> None:
    plan = plan_reframe(Path("x.mp4"), 0, 10, 1920, 1080, mode="auto")
    assert plan.mode in {"track", "center"}
    assert len(plan.crop_xs) >= 1
    assert all(x >= 0 for x in plan.crop_xs)


def test_sendcmd_deduplicates_identical_positions() -> None:
    plan = ReframePlan(
        mode="track",
        out_w=1080,
        out_h=1920,
        source_w=1920,
        source_h=1080,
        crop_xs=[100, 100, 100, 140, 140, 90],
        fps=30,
    )
    assert plan.sendcmd() == "0.0000 crop@cam x 100;\n0.1000 crop@cam x 140;\n0.1667 crop@cam x 90;\n"


def test_sendcmd_file_is_written(tmp_path: Path) -> None:
    plan = ReframePlan(
        mode="center",
        out_w=1080,
        out_h=1920,
        source_w=1080,
        source_h=1920,
        crop_xs=[0],
        fps=30,
    )
    target = plan.write_sendcmd(tmp_path / "moves.txt")
    assert target.read_text(encoding="utf-8").startswith("0.0000 crop@cam x 0;")
    assert plan.with_sendcmd(tmp_path / "b.txt").sendcmd_path.endswith("b.txt")