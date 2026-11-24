"""Where the facecam sits in a stream.

Ported from PeakCut's ``studio/facecam.py`` (MIT, (c) 2026 PeakCut contributors)
into Clipbait's idiom. A gaming stream is a game with a small webcam picture in
one corner of it; a short is better when the game fills the screen and the webcam
sits in the corner of it, instead of the whole 16:9 frame squeezed into a band
with blurred fill above and below.

Clipbait's split-screen mode ships with one hard-coded box, which is where
Speed's webcam sits and nowhere else. This finds the box instead: the webcam is
found by the one thing a webcam always has and a game almost never has, a
straight border that stays in the same place in every frame. A wall in the game
moves and a HUD bar changes, but the edge of the webcam box does not move at all.
A second thing tells the webcam apart from the game's own panels, which have
straight borders too: the picture inside a webcam is almost still, while the game
behind a panel keeps moving.

Metric: for every column and every row, how much the picture changes at that
line, counted over frames spread through the video, with the median taken so one
busy moment cannot invent a border. A border of the webcam box stands out.

Only numpy and ffmpeg, both already here for other reasons; without numpy the
detector reports no box and the caller keeps its configured one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import media
from ..config import get_settings

try:  # numpy ships with the vision/dev extras, not the core install
    import numpy as np
except ImportError:  # pragma: no cover - core install has no numpy
    np = None  # type: ignore[assignment]

# The picture is looked at small: only the lines matter, not the pixels.
GRID_W, GRID_H = 320, 180
MAX_FRAMES = 24  # frames spread through the video
MIN_SIZE = 0.12  # the smallest a webcam may be, as a part of the width or height
MAX_SIZE = 0.60  # and the largest
ASPECT_MIN = 0.55  # the narrowest: a phone held upright is about 0.56
ASPECT_MAX = 3.20  # the widest: a banner or a ticker is wider than this
STILL_FRAC = 0.35  # how much of the game's own movement a webcam picture may have
STILL_MIN = 6.0  # and the floor, for a game that itself is almost still
STILL_SHARE = (
    0.30  # at most this much of a webcam picture may be moving, or the game is showing through
)
EDGE_FLUSH = 0.02  # a side this close to the frame's own edge is the edge
EDGE = 2.2  # how much brighter than the average line a border must be


@dataclass(frozen=True)
class FacecamBox:
    """A webcam picture, in source pixels."""

    x: int
    y: int
    w: int
    h: int
    corner: str  # top-left | top-right | bottom-left | bottom-right | left | right | top | bottom | middle
    conf: str  # "high" when the picture inside hardly moves, "low" otherwise
    score: float  # how strongly its weakest border stood out from the rest

    def fractions(self, width: int, height: int) -> tuple[float, float, float, float]:
        """The same box as (x0, y0, x1, y1) fractions of the frame.

        That is the shape :func:`clipbait.reframe.plan_reframe` crops from, so a
        measured box needs no special case at the render call.
        """
        if not width or not height:
            return (0.0, 0.0, 1.0, 1.0)
        return (
            round(self.x / width, 4),
            round(self.y / height, 4),
            round(min(1.0, (self.x + self.w) / width), 4),
            round(min(1.0, (self.y + self.h) / height), 4),
        )


def available() -> bool:
    """True when the detector can run on this machine (numpy is installed)."""
    return np is not None


def _frames(path: Path, count: int = MAX_FRAMES):
    """The video as a few grey frames, spread through it."""
    settings = get_settings()
    info = None
    try:
        info = media.probe(path)
    except media.MediaError:
        return np.zeros((0, GRID_H, GRID_W), dtype=np.uint8)
    duration = info.duration
    rate = max(count / duration, 0.02) if duration > 0 else 0.2
    try:
        raw = media.pipe(
            [
                settings.ffmpeg,
                "-v",
                "error",
                "-i",
                str(path),
                "-vf",
                f"fps={rate:.4f},scale={GRID_W}:{GRID_H},format=gray",
                "-frames:v",
                str(count),
                "-f",
                "rawvideo",
                "-",
            ],
            timeout=180,
            check=False,
        )
    except media.MediaError:  # no ffmpeg or an unreadable file: no facecam, not a failure
        return np.zeros((0, GRID_H, GRID_W), dtype=np.uint8)
    size = GRID_W * GRID_H
    frames = len(raw) // size
    if frames < 4:
        return np.zeros((0, GRID_H, GRID_W), dtype=np.uint8)
    return np.frombuffer(raw[: frames * size], dtype=np.uint8).reshape(frames, GRID_H, GRID_W)


def _profiles(frames):
    """(vertical lines, horizontal lines): how much changes at each column and row.

    The median over the frames is used, so one busy moment cannot invent a line.
    """
    if len(frames) < 4:
        return np.zeros(GRID_W), np.zeros(GRID_H)
    stack = frames.astype(np.int16)
    # Change across each column boundary, then across each row boundary.
    vertical = np.median(np.abs(np.diff(stack, axis=2)).sum(axis=1), axis=0)
    horizontal = np.median(np.abs(np.diff(stack, axis=1)).sum(axis=2), axis=0)
    return vertical, horizontal


def _peaks(profile, limit: int) -> list[tuple[int, float]]:
    """The lines that stand out: (where, how much)."""
    if len(profile) < 3:
        return []
    median = float(np.median(profile)) or 1.0
    found: list[tuple[int, float]] = []
    for index in range(1, len(profile) - 1):
        if (
            profile[index] >= profile[index - 1]
            and profile[index] > profile[index + 1]
            and profile[index] > EDGE * median
        ):
            found.append((index, float(profile[index] / median)))
    return sorted(found, key=lambda peak: -peak[1])[:limit]


def _stillness(frames):
    """How much each pixel changes over the frames.

    A webcam picture barely changes: a person sits in a room and shifts a little.
    The game changes every frame. This is the one thing that tells a webcam apart
    from the game's own panels, which have straight borders too and are drawn
    over the game with the game showing through them.
    """
    return frames.astype(np.float32).std(axis=0)


def _plausible(x0, y0, x1, y1, still, light, game, share: float = STILL_SHARE):
    """Could this rectangle be a webcam picture? Judged on shape, stillness and light.

    Returns how much it moves, or None. Nothing here says which corner it is in:
    a webcam may sit in a corner, along one edge, or with a margin all round.
    """
    box_w, box_h = (x1 - x0) / GRID_W, (y1 - y0) / GRID_H
    if not (MIN_SIZE <= box_w <= MAX_SIZE and MIN_SIZE <= box_h <= MAX_SIZE):
        return None
    # Measured in pixels: a 4:3 picture is 1.33, a phone held upright is 0.56.
    aspect = (box_w * GRID_W) / max(1e-6, box_h * GRID_H)
    if not (ASPECT_MIN <= aspect <= ASPECT_MAX):
        return None
    left, top, right, bottom = int(x0), int(y0), int(x1), int(y1)
    inside = still[top:bottom, left:right]
    if not inside.size:
        return None
    cut = max(STILL_MIN, STILL_FRAC * game)
    moving = float(np.median(inside))
    if moving > cut:
        return None  # it changes with the game: it is the game
    if float((inside > cut).mean()) > share:
        return None  # a good part of it moves: the game is showing through
    if float(light[top:bottom, left:right].mean()) < 0.30 * float(light.mean()):
        return None  # a black hole, not a picture
    return moving


def _local(vertical_peaks, horizontal_peaks, still, light) -> list[dict]:
    """Every rectangle the strong straight lines can make that still looks like a camera.

    Returned best first: the one whose weakest border is the strongest, and of
    those the bigger.
    """
    game = float(np.median(still))
    xs = sorted({0.0, float(GRID_W)} | {float(x) for x, _ in vertical_peaks[:10]})
    ys = sorted({0.0, float(GRID_H)} | {float(y) for y, _ in horizontal_peaks[:10]})
    vertical = {float(x): score for x, score in vertical_peaks}
    horizontal = {float(y): score for y, score in horizontal_peaks}
    out: list[dict] = []
    for first, x0 in enumerate(xs):
        left_scores = [vertical[v] for v in (x0,) if v in vertical]
        for x1 in xs[first + 1 :]:
            width_scores = left_scores + [vertical[v] for v in (x1,) if v in vertical]
            for second, y0 in enumerate(ys):
                top_scores = [horizontal[v] for v in (y0,) if v in horizontal]
                for y1 in ys[second + 1 :]:
                    scores = (
                        width_scores
                        + top_scores
                        + [horizontal[v] for v in (y1,) if v in horizontal]
                    )
                    if len(scores) < 2:
                        continue  # a box needs two borders of its own, or it is just the frame
                    moving = _plausible(x0, y0, x1, y1, still, light, game)
                    if moving is None:
                        continue
                    key = (min(scores), (x1 - x0) * (y1 - y0))
                    out.append(
                        {
                            "key": key,
                            "x": x0,
                            "y": y0,
                            "x1": x1,
                            "y1": y1,
                            "score": round(min(scores), 2),
                            "moving": moving,
                        }
                    )
    return sorted(out, key=lambda box: box["key"], reverse=True)


def _full_frame(path: Path, width: int, height: int, duration: float):
    """One grey frame at full size, from the middle of the video: where borders are measured."""
    if not width or not height:
        return None
    try:
        raw = media.pipe(
            [
                get_settings().ffmpeg,
                "-v",
                "error",
                "-ss",
                f"{max(0.0, duration / 2.0):.1f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "-",
            ],
            timeout=120,
            check=False,
        )
    except media.MediaError:
        return None
    if len(raw) < width * height:
        return None
    return (
        np.frombuffer(raw[: width * height], dtype=np.uint8)
        .reshape(height, width)
        .astype(np.float32)
    )


def _best(profile, approx: float, span: float) -> float:
    """The strongest change near a guessed line."""
    low, high = max(0, int(approx - span)), min(len(profile), int(approx + span) + 1)
    segment = profile[low:high]
    return float(low + segment.argmax()) if len(segment) else float(approx)


def _snap(path: Path, box, width: int, height: int, duration: float, span: float = 0.0):
    """A box on the small grid, put onto the borders that are really in the picture.

    A border the small grid found is a pixel or two out; a border guessed on a
    frame can be further out, which is what ``span`` is for. A side within a hair
    of the frame's own edge is the edge.
    """
    frame = _full_frame(path, width, height, duration)
    if frame is None:
        return None
    frame_h, frame_w = frame.shape
    column = np.abs(np.diff(frame, axis=1)).sum(axis=0)
    row = np.abs(np.diff(frame, axis=0)).sum(axis=1)
    # The small grid is a fraction of the picture, so a border found on it is a
    # few pixels out; ``span`` widens the search for a border that was guessed.
    spread = max(4.0, span * max(frame_w, frame_h), 1.5 * max(frame_w / GRID_W, frame_h / GRID_H))

    def line(profile, approx: float, edge: float, extent: float) -> float:
        if abs(approx - edge) <= EDGE_FLUSH * extent:
            return float(edge)
        return _best(profile, approx, spread)

    x0, y0, x1, y1 = box
    return (
        line(column, x0 * frame_w / GRID_W, 0, frame_w),
        line(row, y0 * frame_h / GRID_H, 0, frame_h),
        line(column, x1 * frame_w / GRID_W, frame_w, frame_w),
        line(row, y1 * frame_h / GRID_H, frame_h, frame_h),
    )


def _corner(x0, y0, x1, y1, width: int, height: int) -> str:
    """What part of the frame the box is on: its corner, its edge, or the middle."""
    near = EDGE_FLUSH
    left, right = x0 <= width * near, x1 >= width * (1 - near)
    top, bottom = y0 <= height * near, y1 >= height * (1 - near)
    if left and top:
        return "top-left"
    if right and top:
        return "top-right"
    if left and bottom:
        return "bottom-left"
    if right and bottom:
        return "bottom-right"
    if left or right:
        return "right" if right else "left"
    if top or bottom:
        return "bottom" if bottom else "top"
    return "middle"


def _finish(
    path: Path, found: dict, width: int, height: int, duration: float, still
) -> FacecamBox | None:
    """The box in source pixels, with how sure it is that this is really a camera."""
    snapped = _snap(
        path, (found["x"], found["y"], found["x1"], found["y1"]), width, height, duration
    )
    if not snapped:
        return None
    x0, y0, x1, y1 = snapped
    if x1 - x0 < width * MIN_SIZE or y1 - y0 < height * MIN_SIZE:
        return None
    inside = still[int(found["y"]) : int(found["y1"]), int(found["x"]) : int(found["x1"])]
    game = float(np.median(still))
    moving = float(np.median(inside)) if inside.size else 99.0
    # Sure of it when the picture inside hardly moves while the game around it races.
    confidence = "high" if moving <= max(3.0, 0.25 * game) else "low"
    return FacecamBox(
        x=int(round(x0)),
        y=int(round(y0)),
        w=int(round(x1 - x0)),
        h=int(round(y1 - y0)),
        corner=_corner(x0, y0, x1, y1, width, height),
        conf=confidence,
        score=float(found.get("score", 0.0)),
    )


def detect(path: str | Path) -> FacecamBox | None:
    """The facecam's box in the source, or None when the stream has no webcam.

    Best-effort by design: no numpy, no ffmpeg, an unreadable file or a stream
    that is only game all return None, and the caller keeps the box it was
    configured with.
    """
    if np is None:
        return None
    source = Path(path)
    frames = _frames(source)
    if len(frames) < 4:
        return None
    try:
        info = media.probe(source)
    except media.MediaError:
        return None
    width, height = info.width, info.height
    if not width or not height:
        return None
    still = _stillness(frames)
    light = frames.astype(np.float32).mean(axis=0)
    vertical, horizontal = _profiles(frames)
    vertical_peaks, horizontal_peaks = _peaks(vertical, 40), _peaks(horizontal, 40)
    found = (
        _local(vertical_peaks, horizontal_peaks, still, light)
        if (vertical_peaks and horizontal_peaks)
        else []
    )
    if not found:
        return None
    return _finish(source, found[0], width, height, info.duration, still)
