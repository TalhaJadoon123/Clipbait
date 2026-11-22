"""Camera path construction and reframe layouts.

The output is either a set of sendcmd lines that move ffmpeg's crop window per
frame, or a static filter graph for blur/split layouts. The technique (sendcmd
per frame, de-duplicated to change points) is adapted from the MIT-licensed
openshorts project; the geometry here is Clipbait's own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path

from .. import media
from .tracker import FaceSample, sample_faces, split_positions, two_speaker_positions

OUT_W = 1080
OUT_H = 1920

# Where Speed's webcam box sits in a typical stream: bottom-left, next to the
# chat panel. Fractions of the source frame (x0, y0, x1, y1). The split-screen
# layout crops this for the top half; the game is cropped for the bottom half.
FACE_CAM_BOX = (0.0, 0.43, 0.205, 0.655)
# Share of the 1080x1920 canvas given to the facecam. The reference edits put
# the face a little above half so the seam caption sits at the eye line.
FACE_CAM_RATIO = 0.52


@dataclass(frozen=True)
class ReframePlan:
    mode: str  # track | center | blur | split | facecam
    out_w: int
    out_h: int
    source_w: int
    source_h: int
    crop_xs: list[int]  # one value per output frame
    fps: float
    split: tuple[float, float] | None = None
    facecam_box: tuple[float, float, float, float] | None = None
    facecam_ratio: float = FACE_CAM_RATIO
    note: str = ""
    sendcmd_path: str | None = None


    def with_sendcmd(self, path: Path) -> ReframePlan:
        self.write_sendcmd(path)
        return replace(self, sendcmd_path=str(path))

    def sendcmd(self) -> str:
        """De-duplicated crop-x commands, relative to the trimmed clip."""
        lines: list[str] = []
        previous: int | None = None
        for index, x in enumerate(self.crop_xs):
            if x != previous:
                lines.append(f"{index / self.fps:.4f} crop@cam x {x};")
                previous = x
        return "\n".join(lines) + ("\n" if lines else "")

    def write_sendcmd(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.sendcmd(), encoding="utf-8")
        return path


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _smooth(values: list[float], alpha: float = 0.22, radius: int = 3) -> list[float]:
    """EMA then a short moving average: no jitter, no lurching between faces."""
    if not values:
        return []
    ema: list[float] = []
    current = values[0]
    for value in values:
        current = alpha * value + (1 - alpha) * current
        ema.append(current)
    out: list[float] = []
    for index in range(len(ema)):
        window = ema[max(0, index - radius) : index + radius + 1]
        out.append(sum(window) / len(window))
    return out


def _interpolate(samples: list[FaceSample], fps: float, frames: int) -> tuple[list[float], bool]:
    if not samples:
        return [0.5] * frames, False
    if len(samples) == 1:
        return [samples[0].cx] * frames, True
    xs: list[float] = []
    cursor = 0
    for frame in range(frames):
        t = frame / fps
        while cursor + 1 < len(samples) - 1 and samples[cursor + 1].t < t:
            cursor += 1
        left, right = samples[cursor], samples[min(cursor + 1, len(samples) - 1)]
        span = max(0.001, right.t - left.t)
        ratio = _clamp((t - left.t) / span, 0.0, 1.0)
        xs.append(left.cx + (right.cx - left.cx) * ratio)
    return xs, True


def _no_subject_plan(
    *,
    resolved: str,
    source_w: int,
    source_h: int,
    source_aspect: float,
    target_aspect: float,
    out_w: int,
    out_h: int,
    fps: float,
    max_x: int,
) -> ReframePlan:
    """What to do when there is nothing to follow.

    A face tracker that finds no faces knows one thing: it does not know where
    the subject is. Cropping anyway then guesses, and for gameplay the guess is
    always wrong - a 16:9 game cut to 9:16 throws away two thirds of the
    picture, so the car, the character and the action all leave the frame while
    the centre stays. Showing the whole frame on a blur bed keeps what the
    stream actually showed; a modest crop is still fine when little is lost.
    """
    if source_aspect > target_aspect * 1.6:
        return ReframePlan(
            mode="blur",
            out_w=out_w,
            out_h=out_h,
            source_w=source_w,
            source_h=source_h,
            crop_xs=[max_x // 2],
            fps=fps,
            note="no subject to follow; whole frame on a blur bed",
        )
    return ReframePlan(
        mode="center",
        out_w=out_w,
        out_h=out_h,
        source_w=source_w,
        source_h=source_h,
        crop_xs=[max_x // 2],
        fps=fps,
        note="no faces detected; centre crop",
    )


def plan_reframe(
    video: Path,
    start: float,
    end: float,
    source_w: int,
    source_h: int,
    *,
    mode: str = "auto",
    out_w: int = OUT_W,
    out_h: int = OUT_H,
    fps: float = 30.0,
    facecam_box: tuple[float, float, float, float] | None = None,
    facecam_ratio: float = FACE_CAM_RATIO,
) -> ReframePlan:
    """Decide the layout and, for tracking modes, the per-frame crop x."""
    duration = max(0.1, end - start)
    frames = max(1, int(math.ceil(duration * fps)))
    source_aspect = source_w / source_h if source_h else 1.0
    target_aspect = out_w / out_h
    scale = out_h / source_h if source_h else 1.0
    scaled_w = int(round(source_w * scale))
    crop_w = min(scaled_w, out_w)
    max_x = max(0, scaled_w - crop_w)

    if mode == "facecam":
        # Split-screen: facecam on top, gameplay on the bottom. The game crop is
        # static and biased right so it never frames the webcam overlay (which
        # is bottom-left) or the chat beside it. This is the look of the
        # reference Speed edits; tracking the game would chase faces in the
        # webcam box and re-introduce it.
        return ReframePlan(
            mode="facecam",
            out_w=out_w,
            out_h=out_h,
            source_w=source_w,
            source_h=source_h,
            crop_xs=[int(round(max_x * 0.55))],
            fps=fps,
            facecam_box=facecam_box or FACE_CAM_BOX,
            facecam_ratio=_clamp(facecam_ratio, 0.30, 0.70),
            note="facecam over gameplay",
        )

    resolved = mode
    if mode == "auto":
        if source_aspect <= target_aspect * 1.05:
            resolved = "blur"
        else:
            resolved = "track"

    if resolved == "blur":
        return ReframePlan(
            mode="blur",
            out_w=out_w,
            out_h=out_h,
            source_w=source_w,
            source_h=source_h,
            crop_xs=[max_x // 2],
            fps=fps,
            note="blurred background bed",
        )

    if resolved == "center" or max_x == 0:
        return ReframePlan(
            mode="center",
            out_w=out_w,
            out_h=out_h,
            source_w=source_w,
            source_h=source_h,
            crop_xs=[max_x // 2],
            fps=fps,
            note="static centre",
        )

    samples = sample_faces(video, start, end)
    if resolved == "split":
        positions = split_positions(samples)
        if positions:
            return ReframePlan(
                mode="split",
                out_w=out_w,
                out_h=out_h,
                source_w=source_w,
                source_h=source_h,
                crop_xs=[max_x // 2],
                fps=fps,
                split=positions,
                note="two-speaker stack",
            )
        resolved = "track"

    if not samples:
        return _no_subject_plan(
            resolved=resolved,
            source_w=source_w,
            source_h=source_h,
            source_aspect=source_aspect,
            target_aspect=target_aspect,
            out_w=out_w,
            out_h=out_h,
            fps=fps,
            max_x=max_x,
        )

    centres, _ = _interpolate(samples, fps, frames)
    centres = _smooth(centres)
    xs: list[int] = []
    previous = None
    for centre in centres:
        x = int(round(_clamp(centre * scaled_w - crop_w / 2, 0, max_x)))
        # Deadzone: ignore sub-1% movements that read as micro-jitter.
        if previous is not None and abs(x - previous) < max(2, crop_w * 0.006):
            x = previous
        xs.append(x)
        previous = x
    return ReframePlan(
        mode="track",
        out_w=out_w,
        out_h=out_h,
        source_w=source_w,
        source_h=source_h,
        crop_xs=xs,
        fps=fps,
        note=f"tracked {len(samples)} face samples",
    )


def split_layout_centres(video: Path, start: float, end: float) -> tuple[float, float] | None:
    """Convenience for the UI: does this clip look like a two-person shot?"""
    samples = sample_faces(video, start, end)
    return split_positions(samples) or two_speaker_positions(samples)


def probe_source(video: Path) -> media.MediaInfo:
    return media.probe(video)