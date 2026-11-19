"""ffmpeg render engine.

One clip = one ffmpeg process. The filter graph is assembled from the reframe
plan so tracking, blur beds and split screens share the encode/caption tail.
Progress comes from ``-progress pipe:1`` rather than stderr scraping.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import media
from ..captions import write_ass
from ..config import get_settings
from ..reframe import ReframePlan
from ..schema import ChatMessage, Word

log = logging.getLogger("clipbait.render")

ProgressFn = Callable[[float, str], None]


@dataclass(frozen=True)
class RenderResult:
    path: Path
    duration: float
    size_bytes: int
    command: list[str]
    layout: str


def _even(value: int) -> int:
    return value + (value % 2)


def _esc(path: Path) -> str:
    return media.escape_filter_path(path)


def _graph(
    plan: ReframePlan,
    ass_path: Path | None,
    *,
    fps: float,
    has_audio: bool,
) -> tuple[str, bool]:
    out_w, out_h = plan.out_w, plan.out_h
    scaled_w = _even(round(plan.source_w * out_h / plan.source_h)) if plan.source_h else out_w
    crop_w = min(out_w, scaled_w)
    base = (
        f"[0:v]setpts=PTS-STARTPTS,fps={fps},"
        f"scale={scaled_w}:{out_h}:flags=lanczos,setsar=1"
    )

    if plan.mode in {"track", "center"}:
        if crop_w < out_w:
            chain = (
                f"{base},crop={crop_w}:{out_h}:x=0:y=0,"
                f"pad={out_w}:{out_h}:(ow-iw)/2:0:color=black[layout]"
            )
        elif plan.mode == "track" and plan.sendcmd_path and len(set(plan.crop_xs)) > 1:
            start_x = plan.crop_xs[0] if plan.crop_xs else 0
            chain = (
                f"{base},sendcmd=f={_esc(Path(plan.sendcmd_path))},"
                f"crop@cam={out_w}:{out_h}:x={start_x}:y=0[layout]"
            )
        else:
            start_x = plan.crop_xs[0] if plan.crop_xs else max(0, (scaled_w - out_w) // 2)
            chain = f"{base},crop={out_w}:{out_h}:x={start_x}:y=0[layout]"
    elif plan.mode == "blur":
        chain = (
            f"{base},split=2[bga][fga];"
            f"[bga]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},gblur=sigma=32[bg];"
            f"[fga]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[layout]"
        )
    elif plan.mode == "split":
        chain = (
            f"{base},split=2[sa][sb];"
            f"[sa]crop=iw/2:ih:0:0,scale={out_w}:{out_h // 2}:"
            f"force_original_aspect_ratio=increase,crop={out_w}:{out_h // 2}[t];"
            f"[sb]crop=iw/2:ih:iw/2:0,scale={out_w}:{out_h // 2}:"
            f"force_original_aspect_ratio=increase,crop={out_w}:{out_h // 2}[b];"
            f"[t][b]vstack=inputs=2,setsar=1[layout]"
        )
    else:
        chain = f"{base},crop={out_w}:{out_h}:x=(iw-{out_w})/2:y=0[layout]"

    if ass_path:
        chain += f";[layout]ass=f={_esc(ass_path)},setsar=1[v]"
    else:
        chain += ";[layout]null[v]"

    if has_audio:
        chain += ";[0:a]asetpts=PTS-STARTPTS,aresample=48000[a]"
    return chain, has_audio


def render_clip(
    source: Path,
    out_path: Path,
    start: float,
    end: float,
    plan: ReframePlan,
    *,
    words: list[Word] | None = None,
    caption_preset: str = "default",
    captions: bool = True,
    hook: str | None = None,
    chat: list[ChatMessage] | None = None,
    encoder: str | None = None,
    on_progress: ProgressFn | None = None,
) -> RenderResult:
    settings = get_settings()
    duration = max(0.2, end - start)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ass_path: Path | None = None
    if captions and words:
        ass_path = out_path.with_suffix(".ass")
        write_ass(ass_path, words, start, end, caption_preset, hook=hook, chat=chat)

    info = media.probe(source)
    if plan.mode == "track":
        plan = plan.with_sendcmd(out_path.parent / "crop.cmd")
    graph, has_audio = _graph(plan, ass_path, fps=plan.fps, has_audio=info.has_audio)

    encoder_args = media.hw_encoder(encoder or settings.hw_encoder)
    cmd = [
        settings.ffmpeg,
        "-hide_banner",
        "-y",
        "-ss",
        f"{max(0.0, start):.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source),
        "-filter_complex",
        graph,
        "-map",
        "[v]",
    ]
    if has_audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    else:
        cmd += ["-an"]
    cmd += [
        *encoder_args,
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-level",
        "4.2",
        "-movflags",
        "+faststart",
        "-shortest",
        "-progress",
        "pipe:1",
        "-nostats",
        str(out_path),
    ]

    log.debug("render %s -> %s (%s)", start, out_path.name, plan.mode)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )
    assert proc.stdout is not None and proc.stderr is not None
    stderr_chunks: list[str] = []

    def drain_stderr() -> None:
        for line in proc.stderr:
            stderr_chunks.append(line)
            if len(stderr_chunks) > 200:
                del stderr_chunks[:100]

    thread = threading.Thread(target=drain_stderr, daemon=True)
    thread.start()
    for line in proc.stdout:
        seconds = media.parse_progress(line)
        if seconds is not None and on_progress:
            on_progress(min(0.99, seconds / duration), f"encoding {seconds:.1f}s / {duration:.1f}s")
    code = proc.wait()
    thread.join(timeout=2)
    if code != 0 or not out_path.exists():
        tail = "".join(stderr_chunks).strip().splitlines()[-10:]
        raise media.MediaError(f"ffmpeg render failed ({code}): " + " | ".join(tail))

    media.thumbnail(out_path, min(duration / 2, 8.0), out_path.with_suffix(".jpg"))
    if on_progress:
        on_progress(1.0, "rendered")
    return RenderResult(
        path=out_path,
        duration=duration,
        size_bytes=out_path.stat().st_size,
        command=cmd,
        layout=plan.mode,
    )