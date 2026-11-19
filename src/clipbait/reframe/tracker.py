"""Face sampling for the virtual camera.

OpenCV only, no model downloads: Haar frontal + profile cascades that ship with
``opencv-python-headless``. Sampling at 4 fps is enough for a 30 fps camera move
once the path is interpolated and smoothed.

If OpenCV is missing the caller falls back to a centre crop, so vision stays an
optional extra instead of a hard dependency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("clipbait.reframe")


@dataclass(frozen=True)
class FaceSample:
    t: float
    cx: float  # normalised 0..1, centre of the chosen face
    cy: float
    scale: float  # face height / frame height
    count: int = 1


def available() -> bool:
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return True


def _cascades(cv2):
    base = cv2.data.haarcascades
    paths = [
        base + "haarcascade_frontalface_default.xml",
        base + "haarcascade_frontalface_alt2.xml",
        base + "haarcascade_profileface.xml",
    ]
    return [cv2.CascadeClassifier(path) for path in paths]


def sample_faces(
    video: Path,
    start: float,
    end: float,
    *,
    sample_fps: float = 4.0,
    max_width: int = 640,
) -> list[FaceSample]:
    """Detect the dominant face every 1/sample_fps seconds in [start, end]."""
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError:
        return []

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        log.warning("cannot open %s for face tracking", video)
        return []
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        first = max(0, int(start * fps))
        last = min(total, int(end * fps)) if total else int(end * fps)
        step = max(1, int(round(fps / sample_fps)))
        cap.set(cv2.CAP_PROP_POS_FRAMES, first)
        detectors = _cascades(cv2)

        samples: list[FaceSample] = []
        frame_index = first
        while frame_index < last:
            ok, frame = cap.read()
            if not ok:
                break
            if (frame_index - first) % step:
                frame_index += 1
                continue
            height, width = frame.shape[:2]
            if width > max_width:
                ratio = max_width / width
                small = cv2.resize(frame, (max_width, int(height * ratio)))
            else:
                small = frame
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)
            sh, sw = gray.shape[:2]
            faces: list[tuple[int, int, int, int]] = []
            for detector in detectors:
                if detector.empty():
                    continue
                found = detector.detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=5, minSize=(max(24, sw // 22), max(24, sh // 22))
                )
                if len(found):
                    faces = [tuple(int(v) for v in box) for box in found]
                    break
            if not faces:
                frame_index += 1
                continue

            def rank(box: tuple[int, int, int, int], sw: int = sw, sh: int = sh) -> float:
                x, y, w, h = box
                centre_bias = 1.0 - abs((x + w / 2) / sw - 0.5)
                return (w * h / (sw * sh)) * (0.6 + 0.4 * centre_bias)

            x, y, w, h = max(faces, key=rank)
            samples.append(
                FaceSample(
                    t=round(frame_index / fps, 3),
                    cx=(x + w / 2) / sw,
                    cy=(y + h / 2) / sh,
                    scale=h / sh,
                    count=len(faces),
                )
            )
            frame_index += 1
        return samples
    finally:
        cap.release()


def two_speaker_positions(samples: list[FaceSample], min_share: float = 0.25) -> tuple[float, float] | None:
    """Median x of faces left/right of centre when two people are present often."""
    left = sorted(s.cx for s in samples if s.cx < 0.44)
    right = sorted(s.cx for s in samples if s.cx > 0.56)
    if len(left) < max(2, len(samples) * min_share) or len(right) < max(2, len(samples) * min_share):
        return None
    median = lambda values: values[len(values) // 2]  # noqa: E731
    return median(left), median(right)


def split_positions(samples: list[FaceSample]) -> tuple[float, float] | None:
    """Find two recurring clusters (for split-screen podcasts)."""
    if len(samples) < 8:
        return None
    centers = sorted(s.cx for s in samples)
    # Simple two-means on x, seeded at the quartiles.
    a, b = centers[len(centers) // 4], centers[(3 * len(centers)) // 4]
    if abs(a - b) < 0.12:
        return None
    for _ in range(8):
        left = [c for c in centers if abs(c - a) <= abs(c - b)]
        right = [c for c in centers if abs(c - a) > abs(c - b)]
        if not left or not right:
            return None
        a = sum(left) / len(left)
        b = sum(right) / len(right)
    return (a, b) if abs(a - b) > 0.15 else None