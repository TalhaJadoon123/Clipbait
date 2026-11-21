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
    inserts: int = 0


def _even(value: int) -> int:
    return value + (value % 2)


def _esc(path: Path) -> str:
    return media.escape_filter_path(path)


def _effect_label(
    effect: str | None, final: str, *, fps: float, out_w: int, out_h: int
) -> tuple[str, str]:
    """Appended filter chain for a stylised look, and its output label.

    Effects run *after* the layout and meme inserts but *before* the caption
    pass, so burned captions never judder or zoom with the picture.
    """
    style = (effect or "none").lower()
    if style in {"", "none"}:
        return "", final
    if style == "stopmotion":
        # Drop to a low update rate, then resample back to the clip rate so each
        # held frame repeats two or three times: the judder reads as stop-motion
        # rather than the smooth motion of the source.
        low = max(6, int(round(fps / 2.5)))
        return f";[{final}]fps={low},fps={fps},setsar=1[fx]", "fx"
    if style == "punch":
        # A constant, subtle punch-in: crop 8% and scale back up. Cheap (no
        # zoompan) and it makes a flat talking-head slice read as framed.
        zoom_w = _even(int(round(out_w * 1.08)))
        zoom_h = _even(int(round(out_h * 1.08)))
        return f";[{final}]scale={zoom_w}:{zoom_h},crop={out_w}:{out_h},setsar=1[fx]", "fx"
    return "", final


def _audio_label(
    *,
    has_audio: bool,
    duration: float,
    music_index: int | None,
    music_volume: float,
    sfx: list[tuple[int, float, float, float]],
) -> tuple[str, bool]:
    """Mix the source audio, an optional music bed and insert sound effects.

    One audio output ``[a]`` regardless of how many sources there are, so the
    caller's ``-map [a]`` never changes. ``normalize=0`` keeps the speech at
    full level (amix would otherwise halve it); a mild final ``volume`` guards
    against the summed peaks clipping.
    """
    parts: list[str] = []
    labels: list[str] = []
    sources: list[tuple[str, str]] = []
    if has_audio:
        sources.append(("[0:a]asetpts=PTS-STARTPTS,aresample=48000", "[a0]"))
    if music_index is not None:
        sources.append(
            (
                f"[{music_index}:a]atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,"
                f"aresample=48000,volume={music_volume:.3f}",
                "[mus]",
            )
        )
    for i, (index, start, end, volume) in enumerate(sfx):
        delay = int(round(max(0.0, start) * 1000))
        span = max(0.05, end - start)
        sources.append(
            (
                f"[{index}:a]atrim=0:{span:.3f},asetpts=PTS-STARTPTS,aresample=48000,"
                f"adelay={delay}|{delay},volume={volume:.3f}",
                f"[sfx{i}]",
            )
        )
    if not sources:
        return "", False
    if len(sources) == 1:
        # One audio source: name it ``[a]`` directly, so a plain clip's graph is
        # byte-for-byte what it always was.
        return ";" + sources[0][0] + "[a]", True
    for part, label in sources:
        parts.append(part + label)
        labels.append(label)
    parts.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0"
        + "[mixed];[mixed]volume=0.95[a]"
    )
    return ";" + ";".join(parts), True


def _graph(
    plan: ReframePlan,
    ass_path: Path | None,
    *,
    fps: float,
    has_audio: bool,
    inserts: list[tuple[float, float]] | None = None,
    effect: str | None = None,
    duration: float = 0.0,
    music_index: int | None = None,
    music_volume: float = 0.16,
    sfx: list[tuple[int, float, float, float]] | None = None,
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

    # Meme inserts cover the picture for their window, then hand back. They are
    # composited onto the reframed video and *before* the caption pass, so the
    # spoken audio and its word-level captions run continuously underneath and
    # can never drift. Input index i is the i-th extra -i (0 is the source).
    final = "layout"
    for index, (start, end) in enumerate(inserts or [], start=1):
        # Shift the insert onto the clip timeline: its own PTS start at 0, but
        # the overlay window opens at ``start`` seconds.
        chain += (
            f";[{index}:v]setpts=PTS-STARTPTS+{start:.3f}/TB,"
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},fps={fps},setsar=1[m{index}]"
        )
        chain += (
            f";[{final}][m{index}]overlay=(W-w)/2:(H-h)/2:"
            f"enable='between(t,{start:.3f},{end:.3f})'[o{index}]"
        )
        final = f"o{index}"

    effect_chain, final = _effect_label(
        effect, final, fps=fps, out_w=out_w, out_h=out_h
    )
    chain += effect_chain

    if ass_path:
        chain += f";[{final}]ass=f={_esc(ass_path)},setsar=1[v]"
    else:
        chain += f";[{final}]null[v]"

    audio_chain, audio_out = _audio_label(
        has_audio=has_audio,
        duration=duration,
        music_index=music_index,
        music_volume=music_volume,
        sfx=sfx or [],
    )
    chain += audio_chain
    return chain, audio_out


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
    inserts: list[tuple[Path, float, float]] | None = None,
    effect: str | None = None,
    music: Path | None = None,
    music_volume: float = 0.16,
    sfx: list[tuple[Path, float, float]] | None = None,
    sfx_volume: float = 0.5,
    on_progress: ProgressFn | None = None,
) -> RenderResult:
    settings = get_settings()
    duration = max(0.2, end - start)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Drop any insert whose file vanished, so a bad asset degrades to "no meme"
    # instead of a failed render. Times are clip-local and clipped to the clip.
    windows: list[tuple[float, float]] = []
    insert_paths: list[Path] = []
    for path, ins_start, ins_end in inserts or []:
        if not path.exists():
            log.warning("insert asset missing, skipping: %s", path)
            continue
        local_start = max(0.0, min(duration, ins_start))
        local_end = max(local_start, min(duration, ins_end))
        if local_end - local_start < 0.2:
            continue
        insert_paths.append(path)
        windows.append((local_start, local_end))

    ass_path: Path | None = None
    if captions and words:
        ass_path = out_path.with_suffix(".ass")
        write_ass(ass_path, words, start, end, caption_preset, hook=hook, chat=chat)

    info = media.probe(source)
    if plan.mode == "track":
        plan = plan.with_sendcmd(out_path.parent / "crop.cmd")

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
    ]
    for path in insert_paths:
        cmd += ["-i", str(path)]

    # Music and insert sound effects follow the source and the inserts, so their
    # input indexes are computed from what is already on the command line.
    next_index = 1 + len(insert_paths)
    music_index: int | None = None
    if music is not None and Path(music).exists():
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        music_index = next_index
        next_index += 1
    sfx_entries: list[tuple[int, float, float, float]] = []
    for path, sfx_start, sfx_end in sfx or []:
        if not Path(path).exists():
            continue
        cmd += ["-i", str(path)]
        sfx_entries.append((next_index, sfx_start, sfx_end, sfx_volume))
        next_index += 1

    graph, has_audio = _graph(
        plan,
        ass_path,
        fps=plan.fps,
        has_audio=info.has_audio,
        inserts=windows,
        effect=effect,
        duration=duration,
        music_index=music_index,
        music_volume=music_volume,
        sfx=sfx_entries,
    )

    cmd += [
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
        inserts=len(insert_paths),
    )