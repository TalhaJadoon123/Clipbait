"""End-to-end orchestration.

`process_project` is the only function the API/worker/CLI need for long-form
video. Every step reports progress and every failure is persisted, so the UI
can explain exactly where a job died.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from . import analyze, db, jobs, media, storage, transcribe
from .captions.presets import get_preset
from .config import get_settings
from .memes import (
    InsertConfig,
    MemeRequest,
    cache_dir,
    get_meme_provider,
    plan_inserts,
    prepare_insert,
)
from .reframe import ReframePlan, plan_reframe
from .render import render_clip
from .schema import ChatMessage, Highlight, ProjectOptions, Transcript, Word
from .virality import build_variants, forecast, get_region

log = logging.getLogger("clipbait.pipeline")


def _project_options(project: dict) -> ProjectOptions:
    return ProjectOptions(**(project.get("options") or {}))


def _set_status(project_id: str, status: str, **fields) -> None:
    db.update("projects", project_id, {"status": status, **fields})


def _link_mode(project: dict, options: ProjectOptions) -> bool:
    """True when the video itself should never be downloaded whole."""
    return (
        options.source_mode != "download"
        and bool(project.get("source_url"))
        and project.get("source_kind") != "local"
        and not project.get("source_path")
    )


def ensure_source(project: dict, job_id: str | None, progress_base: float = 0.0) -> dict:
    """Make the source available.

    In link mode only the audio track is fetched here; the video is cut straight
    from the remote source per clip. If that path is unavailable (or the site
    cannot serve sections), it degrades to a normal full download.
    """
    project_id = project["id"]
    existing = project.get("source_path")
    if existing:
        path = storage.resolve(existing)
        if path.exists() and path.stat().st_size > 0:
            return project

    if not project.get("source_url"):
        raise media.MediaError("project has no source URL or file")

    def on_download(fraction: float, message: str) -> None:
        # Live download feedback: the ingest step owns 2% -> 9% of the run.
        jobs.progress(job_id, progress_base + 0.02 + 0.07 * fraction, "downloading", message)

    options = _project_options(project)
    if _link_mode(project, options):
        cached_audio = storage.link_audio_path(project_id)
        if cached_audio.exists() and cached_audio.stat().st_size > 0:
            # A retry reuses the audio-only download instead of paying for it twice.
            return project
        jobs.progress(
            job_id, progress_base + 0.02, "downloading", "Fetching audio only (link mode)"
        )
        _set_status(project_id, "downloading")
        try:
            from .ingest import fetch_audio

            result = fetch_audio(project["source_url"], project_id, on_progress=on_download)
        except Exception as exc:  # noqa: BLE001 - fall back to the full video
            log.warning("link mode unavailable (%s); downloading the full source", exc)
        else:
            updated = db.update(
                "projects",
                project_id,
                {
                    "title": result.title or project["title"],
                    "duration": result.duration,
                    "language": project.get("language"),
                    "error": None,
                },
            )
            return updated or project

    jobs.progress(job_id, progress_base + 0.02, "downloading", "Fetching source video")
    _set_status(project_id, "downloading")

    result = ingest_source(project, job_id, on_progress=on_download)
    updated = db.update(
        "projects",
        project_id,
        {
            "title": result.title or project["title"],
            "duration": result.duration,
            "source_path": storage.relative(result.path),
            "language": project.get("language"),
            "error": None,
        },
    )
    return updated or project


def ingest_source(project: dict, job_id: str | None = None, on_progress=None):
    from .ingest import fetch_remote, ingest_local

    project_id = project["id"]
    if project.get("source_kind") == "local" and project.get("source_path"):
        return ingest_local(storage.resolve(project["source_path"]), project_id, project.get("title"))
    url = project.get("source_url")
    if not url:
        raise ValueError("missing source")
    return fetch_remote(url, project_id, on_progress=on_progress)


def _load_transcript(project_id: str) -> Transcript | None:
    payload = storage.read_json(storage.transcript_path(project_id))
    if not payload:
        return None
    try:
        return Transcript(**payload)
    except Exception:  # noqa: BLE001 - stale/corrupt cache should not break a run
        return None


def process_project(project_id: str, job_id: str | None = None) -> dict:
    project = db.get_project(project_id)
    if not project:
        raise ValueError(f"unknown project {project_id}")
    options = _project_options(project)
    settings = get_settings()

    cached = _load_transcript(project_id)
    if settings.transcript_only and cached is not None and cached.duration > 0:
        # Transcript-only run: the full audio exists only to transcribe and to
        # score energy. When the transcript is already cached (a 24h VOD's audio
        # is gigabytes), skip the source entirely and rank without energy.
        transcript = cached
        audio = None
        db.update(
            "projects",
            project_id,
            {"duration": transcript.duration, "status": "transcribing", "error": None},
        )
    else:
        project = ensure_source(project, job_id)
        link = _link_mode(project, options)
        if link:
            audio_input = storage.link_audio_path(project_id)
            if not audio_input.exists():
                raise media.MediaError("link-mode audio missing after ingest")
            info = media.probe(audio_input)
        else:
            source = storage.resolve(project["source_path"]) if project.get("source_path") else None
            if not source or not source.exists():
                raise media.MediaError("source file missing after ingest")
            audio_input = source
            info = media.probe(source)
        db.update(
            "projects",
            project_id,
            {"duration": info.duration, "status": "transcribing", "error": None},
        )

        jobs.progress(job_id, 0.1, "audio", "Extracting audio")
        audio = media.extract_audio(audio_input, storage.audio_path(project_id))

        transcript = _load_transcript(project_id) if info.duration > 0 else None
        if transcript is None:
            provider = transcribe.get_transcriber(settings)
            jobs.progress(
                job_id,
                0.16,
                "transcribing",
                f"Transcribing with {type(provider).__name__.replace('Transcriber', '').lower()}",
            )
            transcript = provider.transcribe(
                audio,
                language="auto" if options.language == "auto" else options.language,
                source_url=project.get("source_url"),
            )
            transcript.duration = transcript.duration or info.duration
            storage.write_json(storage.transcript_path(project_id), transcript.model_dump())
        else:
            jobs.progress(job_id, 0.34, "transcribing", "Reusing cached transcript")

    jobs.progress(job_id, 0.42, "analyzing", "Reading the room")
    energy = media.audio_rms(audio) if audio is not None else []
    if audio is not None:
        # The full-audio intermediates were only needed to transcribe and to
        # score energy; each clip is cut from its own remote window, never from
        # these files. On a 24h VOD they are ~5GB, so drop them before rendering
        # to keep peak disk flat (audio.wav regenerates on any retry).
        for stale in (audio, storage.link_audio_path(project_id)):
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass
    highlights, analysis_meta = analyze.analyze(
        transcript, options, energy=energy, settings=settings
    )
    analysis_meta["transcriber"] = transcript.provider
    if transcript.provider == "mock":
        analysis_meta["warning"] = (
            "No speech model installed or configured - this transcript is synthetic. "
            "Install clipbait[whisper] for real captions."
        )
    jobs.progress(job_id, 0.55, "selecting", f"{len(highlights)} moments selected")

    db.delete_clips(project_id)
    clips = [
        _create_clip(project_id, highlight, options, analysis_meta, transcript)
        for highlight in highlights
    ]
    if not clips:
        raise ValueError(
            "No clip-worthy moments found. The video may be too short or mostly silence."
        )

    db.update("projects", project_id, {"status": "rendering"})
    rendered = 0
    for index, clip in enumerate(clips):
        base = 0.57 + (index / max(1, len(clips))) * 0.40

        def on_progress(fraction: float, message: str, _base=base, _index=index) -> None:
            span = 0.40 / max(1, len(clips))
            jobs.progress(
                job_id,
                _base + fraction * span,
                "rendering",
                f"Clip {_index + 1}/{len(clips)} · {message}",
            )

        try:
            render_clip_record(clip["id"], transcript=transcript, on_progress=on_progress)
            rendered += 1
        except Exception as exc:  # noqa: BLE001 - one bad clip must not kill the batch
            log.exception("clip %s failed", clip["id"])
            db.update("clips", clip["id"], {"status": "failed", "meta": {**clip["meta"], "error": str(exc)}})

    if rendered == 0:
        raise media.MediaError("every clip failed to render; check the log above")

    jobs.progress(job_id, 0.99, "finalising", "Writing project state")
    _set_status(project_id, "completed", language=transcript.language, error=None)
    return {
        "project_id": project_id,
        "clips": rendered,
        "transcriber": transcript.provider,
        "analysis": analysis_meta.get("provider"),
    }


def _create_clip(
    project_id: str,
    highlight: Highlight,
    options: ProjectOptions,
    meta: dict,
    transcript: Transcript,
) -> dict:
    packaging = analyze.packaging_for(highlight, meta)
    text = transcript.text_between(highlight.start, highlight.end) or highlight.hook
    if options.title_style == "speed":
        # Reference-channel packaging. The named-object path is gated by the
        # project's name gazetteer so a sentence-start capital is not mistaken
        # for a proper noun; when no confident shape exists the clip gets a
        # substantive quote rather than a one-word fragment.
        from .analyze import packaging as speed_packaging

        names = frozenset(meta.get("known_names") or ())
        styled = speed_packaging.speed_title(text, highlight.keywords, names=names)
        if not styled:
            styled = speed_packaging.quote_title(text)
        if styled:
            packaging = {**packaging, "title": styled}
        packaging = {**packaging, "hashtags": speed_packaging.speed_tags(highlight.keywords)}
    region = get_region(options.region)
    variants = (
        build_variants(
            highlight,
            text,
            region,
            options.platform,
            limit=options.variants,
            allow_medium_risk=options.allow_medium_risk,
        )
        if options.memes
        else []
    )
    chosen = variants[0] if variants else None
    forecast_data = forecast(
        highlight.scores,
        text,
        region,
        options.platform,
        duration=highlight.duration,
        meme=(chosen or {}).get("meme"),
    )
    return db.create_clip(
        {
            "project_id": project_id,
            "title": (chosen or packaging)["title"],
            "hook": (chosen or packaging)["hook"],
            "description": (chosen or packaging)["description"],
            "hashtags": (chosen or packaging)["hashtags"],
            "start": highlight.start,
            "end": highlight.end,
            "score": highlight.scores.overall,
            "scores": highlight.scores.model_dump(),
            "reason": highlight.reason,
            "quote": highlight.quote,
            "caption_preset": options.caption_preset,
            "reframe_mode": options.reframe_mode,
            "captions": options.captions,
            "hook_titles": options.hook_titles,
            "meta": {
                "analysis": meta.get("provider"),
                "transcriber": meta.get("transcriber"),
                "warning": meta.get("warning"),
                "source": highlight.source,
                "region": region.code,
                "platform": options.platform,
                "variants": variants,
                "forecast": forecast_data,
                "meme_inserts": options.meme_inserts,
                "meme_insert_seconds": options.meme_insert_seconds,
                "meme_max_inserts": options.meme_max_inserts,
            },
        }
    )


def _clip_inserts(
    clip: dict,
    transcript: Transcript,
    plan: ReframePlan,
    *,
    on_progress=None,
) -> tuple[list[tuple[Path, float, float]], list[dict]]:
    """Plan, generate and normalise the meme b-roll inserts for one clip.

    Returns ``(inserts, records)``. ``inserts`` feeds :func:`render_clip`; a
    provider or ffmpeg failure degrades to "no insert" rather than a failed
    render, so it is safe to call on every clip. An empty list is the common,
    correct answer.
    """
    meta = clip.get("meta") or {}
    if not meta.get("meme_inserts"):
        return [], []
    provider = get_meme_provider()
    if provider is None:
        return [], []

    config = InsertConfig(
        max_inserts=int(meta.get("meme_max_inserts") or 2),
        insert_seconds=float(meta.get("meme_insert_seconds") or 2.0),
    )
    planned = plan_inserts(transcript.words, clip["start"], clip["end"], config)
    if not planned:
        return [], []

    fps = int(round(plan.fps)) or 30
    out_dir = storage.clip_dir(clip["project_id"], clip["id"]) / "inserts"
    cache = cache_dir()
    inserts: list[tuple[Path, float, float]] = []
    records: list[dict] = []
    for index, insert in enumerate(planned):
        if on_progress:
            on_progress(0.02, f"meme b-roll {index + 1}/{len(planned)}")
        request = MemeRequest(
            prompt=insert.prompt,
            duration=insert.duration,
            width=plan.out_w,
            height=plan.out_h,
            fps=fps,
        )
        try:
            asset = provider.generate(request, cache)
            if asset is None:
                continue
            prepared = prepare_insert(
                asset,
                out_dir / f"insert-{index}.mp4",
                width=plan.out_w,
                height=plan.out_h,
                duration=insert.duration,
                fps=fps,
            )
        except Exception as exc:  # noqa: BLE001 - a bad insert must not fail the clip
            log.warning("meme insert %d skipped: %s", index, exc)
            continue
        inserts.append((prepared, insert.start, insert.end))
        records.append(
            {
                **insert.as_dict(),
                "path": storage.relative(prepared),
                "provider": provider.name,
            }
        )
    return inserts, records


_AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}


def _audio_files(directory: str | None) -> list[Path]:
    if not directory:
        return []
    path = Path(directory)
    if not path.is_dir():
        return []
    return sorted(
        item
        for item in path.iterdir()
        if item.is_file() and item.suffix.lower() in _AUDIO_SUFFIXES
    )


def _music_track(options: ProjectOptions, clip_id: str) -> Path | None:
    """The music bed for this clip: an explicit track, else a default one.

    A named track is resolved against the working directory first, then against
    the configured music folder. With no name, one file from ``CLIPBAIT_MUSIC_DIR``
    is chosen by clip id, so different clips get different tracks without an
    operator picking each one.
    """
    settings = get_settings()
    if options.music:
        candidate = Path(options.music)
        if not candidate.exists() and settings.music_dir:
            candidate = Path(settings.music_dir) / options.music
        return candidate if candidate.exists() else None
    tracks = _audio_files(settings.music_dir)
    if not tracks:
        return None
    seed = int(hashlib.sha256(clip_id.encode("utf-8")).hexdigest(), 16)
    return tracks[seed % len(tracks)]


def _insert_sfx(
    options: ProjectOptions,
    inserts: list[tuple[Path, float, float]],
    clip_id: str,
) -> list[tuple[Path, float, float]]:
    """A sound effect for each meme insert, drawn from the configured folder.

    Best-effort: no folder, no inserts, or ``meme_sfx`` off means an empty list
    and the clip renders exactly as before.
    """
    if not options.meme_sfx or not inserts:
        return []
    files = _audio_files(get_settings().meme_sfx_dir)
    if not files:
        return []
    seed = int(hashlib.sha256(clip_id.encode("utf-8")).hexdigest(), 16)
    out: list[tuple[Path, float, float]] = []
    for index, (_asset, start, end) in enumerate(inserts):
        out.append((files[(seed + index) % len(files)], start, end))
    return out


def _download_full(project: dict) -> dict:
    """Force the classic whole-file ingest (link-mode fallback)."""
    result = ingest_source(project, None)
    updated = db.update(
        "projects",
        project["id"],
        {
            "title": result.title or project["title"],
            "duration": result.duration,
            "source_path": storage.relative(result.path),
        },
    )
    return updated or project


def _fetch_clip_window(project: dict, clip: dict, on_progress=None) -> tuple[Path, float, bool]:
    """Fetch just this clip's section, falling back to a full download once.

    Returns ``(path, offset, used_link)``. ``offset`` is the source timestamp of
    the fetched window's first frame (``0`` when the whole file was
    downloaded), and ``used_link`` says whether the result really is a short
    window.
    """
    from .ingest import fetch_hls_window, fetch_section

    target = storage.clip_window_path(project["id"], clip["id"])

    def report(base: float):
        return (
            (lambda fraction, message: on_progress(base + 0.08 * fraction, message))
            if on_progress
            else None
        )

    if on_progress:
        on_progress(0.01, "Fetching this moment only")
    # Fast path: pull only the covering HLS fragments, concurrently. The
    # window is fragment-aligned, so the returned offset is where its first
    # frame actually sits in the source.
    try:
        path, offset = fetch_hls_window(
            project["source_url"], clip["start"], clip["end"], target, on_progress=report(0.01)
        )
        return path, offset, True
    except Exception as exc:  # noqa: BLE001 - degrade to the general path
        log.warning("HLS window fetch failed (%s); trying the ffmpeg section", exc)
    # General path: ffmpeg reads an HLS/DASH section sequentially. Slower on a
    # throttled connection but works for sources without fragment playlists.
    try:
        path = fetch_section(
            project["source_url"],
            clip["start"],
            clip["end"],
            target,
            on_progress=report(0.01),
        )
        return path, float(clip["start"]), True
    except Exception as exc:  # noqa: BLE001 - degrade to the reliable path
        # A whole-source download is only sane for a short video. On a
        # transcript-only run the source is a multi-hour livestream VOD, so
        # "download the full source" would pull many gigabytes and can fill the
        # disk mid-batch. Fail the clip instead; a later retry picks it up once
        # the transient cause (usually a bot check) has cleared.
        if get_settings().transcript_only:
            raise media.MediaError(
                f"no remote window for this clip and full download is disabled: {exc}"
            ) from exc
        log.warning("section fetch failed (%s); downloading the full source", exc)
        updated = _download_full(project)
        path = updated.get("source_path")
        if not path:
            raise media.MediaError("could not fetch the section or the full source") from exc
        return storage.resolve(path), 0.0, False


def render_clip_record(
    clip_id: str,
    *,
    transcript: Transcript | None = None,
    on_progress=None,
) -> dict:
    clip = db.get_clip(clip_id)
    if not clip:
        raise ValueError(f"unknown clip {clip_id}")
    project = db.get_project(clip["project_id"])
    if not project:
        raise ValueError("project source missing")
    options = _project_options(project)

    offset = 0.0
    link = _link_mode(project, options)
    if link:
        source, offset, link = _fetch_clip_window(project, clip, on_progress=on_progress)
    else:
        if not project.get("source_path"):
            raise ValueError("project source missing")
        source = storage.resolve(project["source_path"])
        if not source.exists():
            raise ValueError("project source missing")
    if transcript is None:
        transcript = _load_transcript(clip["project_id"])
    if transcript is None:
        raise ValueError("transcript missing for project")

    db.update("clips", clip_id, {"status": "rendering"})
    meta = clip.get("meta") or {}
    source_info = media.probe(source)
    local_start = max(0.0, float(clip["start"]) - offset)
    local_end = max(local_start + 0.2, float(clip["end"]) - offset)
    plan = plan_reframe(
        source,
        local_start,
        local_end,
        source_w=source_info.width,
        source_h=source_info.height,
        mode=clip.get("reframe_mode", "auto"),
        facecam_box=options.facecam_box,
        facecam_ratio=options.facecam_ratio,
    )
    words = transcript.words
    if offset:
        words = [
            Word(text=word.text, start=word.start - offset, end=word.end - offset, prob=word.prob)
            for word in transcript.words
        ]
    chat = None
    if meta.get("chat"):
        chat = [ChatMessage(**message) for message in meta["chat"]]
        if offset:
            chat = [
                ChatMessage(t=message.t - offset, author=message.author, text=message.text, kind=message.kind)
                for message in chat
            ]

    preset = get_preset(clip.get("caption_preset", "default"))
    out_path = storage.clip_dir(clip["project_id"], clip_id) / "clip.mp4"
    inserts, insert_records = _clip_inserts(
        clip, transcript, plan, on_progress=on_progress
    )
    music = _music_track(options, clip_id)
    sfx = _insert_sfx(options, inserts, clip_id)
    result = render_clip(
        source,
        out_path,
        local_start,
        local_end,
        plan,
        words=words,
        caption_preset=clip.get("caption_preset", "default"),
        caption_position=None
        if options.caption_position == "auto"
        else options.caption_position,
        captions=bool(clip.get("captions", True)),
        hook=(clip.get("hook") or clip.get("title")) if clip.get("hook_titles", True) else None,
        chat=chat,
        inserts=inserts or None,
        effect=options.effect,
        music=music,
        music_volume=options.music_volume,
        sfx=sfx or None,
        sfx_volume=get_settings().meme_sfx_volume,
        on_progress=on_progress,
    )
    updated = db.update(
        "clips",
        clip_id,
        {
            "status": "ready",
            "render_path": storage.relative(result.path),
            "thumb_path": storage.relative(result.path.with_suffix(".jpg")),
            "meta": {
                **meta,
                "layout": plan.mode,
                "layout_note": plan.note,
                "duration": result.duration,
                "size_bytes": result.size_bytes,
                "caption_style": preset.font,
                "inserts": insert_records,
                "insert_count": len(inserts),
                "link_mode": link,
                "error": None,
            },
        },
    )
    if link:
        # The fetched window is only needed while encoding. Dropping it here is
        # what keeps a long link-mode run (dozens of clips off one VOD) from
        # filling the disk with footage that is already burned into the render.
        window = storage.clip_window_path(clip["project_id"], clip_id)
        if window.exists() and window.resolve() == Path(source).resolve():
            window.unlink(missing_ok=True)
    return updated or clip


def _live_description(origin: str) -> str:
    if origin == "manual":
        return "Hand-picked moment from the stream."
    if origin == "chat":
        return "Auto-cut from a live chat spike."
    return "Auto-cut from an on-stream moment."


def hms(seconds: float) -> str:
    """1:02:03 / 2:03 - the shape a streamer reads off the timeline."""
    total = max(0, int(round(float(seconds or 0.0))))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _cut_local(source: Path, start: float, end: float, target: Path) -> Path:
    """Cut one window out of a local file, re-encoding so the cut is exact."""
    target.parent.mkdir(parents=True, exist_ok=True)
    media.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{max(0.0, start):.3f}",
            "-i", str(source),
            "-t", f"{max(0.5, end - start):.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart",
            str(target),
        ],
        timeout=1800,
    )
    if not target.exists() or target.stat().st_size < 10_000:
        raise media.MediaError("cut produced no usable file")
    return target


def grab_moment(
    project_id: str,
    start: float,
    end: float,
    *,
    title: str | None = None,
    reason: str | None = None,
    job_id: str | None = None,
) -> dict:
    """Cut exactly the window a human pointed at, then clip it like any other.

    Detection guesses. A person watching the stream does not have to: they know
    the timestamp of the thing worth posting. This fetches only that window (a
    few seconds of transfer even on a two-hour VOD), then runs the same
    transcript, caption, reframe and copy path a detected moment gets - so the
    hand-picked clip is not a second-class citizen.
    """
    project = db.get_project(project_id)
    if not project:
        raise ValueError(f"project {project_id} not found")
    start = max(0.0, float(start))
    end = max(start + 1.0, float(end))
    target = storage.project_dir(project_id) / "source" / "video.mp4"

    existing = project.get("source_path")
    if existing:
        jobs.progress(job_id, 0.05, "fetching", f"Cutting {hms(start)}-{hms(end)}")
        _cut_local(storage.resolve(existing), start, end, target)
    else:
        url = project.get("source_url")
        if not url:
            raise ValueError("project has neither a local source nor a URL")
        from .ingest import fetch_section

        jobs.progress(job_id, 0.05, "fetching", f"Fetching {hms(start)}-{hms(end)}")
        fetch_section(url, start, end, target)

    db.update(
        "projects",
        project_id,
        {
            "source_path": storage.relative(target),
            "duration": end - start,
            "status": "rendering",
        },
    )
    moment = {
        "title": title or f"Clip at {hms(start)}",
        "origin": "manual",
        "analysis": "manual-grab",
        "score": 6.0,
        "reason": reason or f"Hand-picked {hms(start)}-{hms(end)} of the stream.",
        "messages": [],
    }
    result = process_live_moment(project_id, moment, job_id=job_id)
    return db.get_clip(result["clip_id"]) or {}


def _speech_copy(transcript: Transcript) -> tuple[str, str]:
    """The strongest spoken line in the clip, and the full spoken text.

    A chat-triggered moment is labelled by whatever chat shouted, which is
    usually a reaction ("WASH YOUR HANDS") rather than the thing that happened.
    The clip's own audio knows better, so the copy comes from there when there
    are words to use. The scored import is local because ``clipbait.live``
    imports this module.
    """
    segments = [seg for seg in (transcript.segments or []) if (seg.text or "").strip()]
    if not segments:
        return "", ""
    spoken = " ".join((seg.text or "").strip() for seg in segments)
    from .live.content import score_speech

    best: tuple[float, str] | None = None
    for seg in segments:
        text = (seg.text or "").strip()
        if len(text.split()) < 3:
            continue
        length = max(1.0, float((seg.end or 0.0) - (seg.start or 0.0)))
        score = score_speech(text, length).score
        if best is None or score > best[0]:
            best = (score, text)
    if best is None:
        return "", spoken
    return best[1], spoken


def process_live_moment(
    project_id: str,
    moment: dict,
    *,
    job_id: str | None = None,
) -> dict:
    """Render the single clip captured from a live chat spike.

    The recorder has already cut ``source_path`` to the moment window, so the
    transcript, caption and chat-overlay timings all start at zero.
    """
    project = db.get_project(project_id)
    if not project or not project.get("source_path"):
        raise ValueError("live clip project has no source")
    options = _project_options(project)
    source = storage.resolve(project["source_path"])
    info = media.probe(source)

    jobs.progress(job_id, 0.1, "audio", "Extracting moment audio")
    audio = media.extract_audio(source, storage.audio_path(project_id))
    provider = transcribe.get_transcriber()
    jobs.progress(job_id, 0.2, "transcribing", "Captions for the live moment")
    transcript = provider.transcribe(audio, language="auto" if options.language == "auto" else options.language)
    transcript.duration = transcript.duration or info.duration
    storage.write_json(storage.transcript_path(project_id), transcript.model_dump())

    chat = [ChatMessage(**message) for message in moment.get("messages", [])]
    title = moment.get("title") or "The chat went wild"
    origin = moment.get("origin") or "chat"
    quote = (moment.get("quote") or "").strip()
    spoken_line, spoken = _speech_copy(transcript)
    if not quote and spoken_line:
        quote = spoken_line
    if origin == "chat" and spoken_line:
        # The chat message named a reflex; the stream named the moment.
        title = f'On stream: "{spoken_line}"'[:90]
    score = int(max(0, min(100, round(moment.get("score", 0) * 10))))
    default_reason = (
        "On-stream signal: something happened worth clipping."
        if origin != "chat"
        else "Chat velocity spiked here."
    )
    reason = moment.get("reason") or default_reason

    region = get_region(options.region)
    highlight = Highlight(
        start=0.0,
        end=max(1.0, info.duration),
        keywords=list(options.hashtags),
        title=title[:80],
        hook=(moment.get("hook") or title)[:120],
        reason=reason,
        quote=quote[:200],
        scores={"hook": score, "emotion": score, "value": score, "share": score, "overall": score},
        source="live",
    )
    # Score the copy on what was actually said, not on the detector's own
    # explanation. A quote is the strongest hook a stream clip can have.
    live_text = quote or spoken or reason or title
    variants = (
        build_variants(highlight, live_text, region, options.platform, limit=options.variants)
        if options.memes
        else []
    )
    chosen = variants[0] if variants else None
    forecast_data = forecast(
        highlight.scores,
        live_text,
        region,
        options.platform,
        duration=highlight.duration,
        meme=(chosen or {}).get("meme"),
    )

    db.delete_clips(project_id)
    clip = db.create_clip(
        {
            "project_id": project_id,
            "title": chosen["title"] if chosen else title[:80],
            "hook": chosen["hook"] if chosen else (moment.get("hook") or title)[:120],
            "description": chosen["description"]
            if chosen
            else _live_description(origin),
            "hashtags": chosen["hashtags"] if chosen else ["live", "shorts", "reels", "clips"],
            "start": 0.0,
            "end": max(1.0, info.duration),
            "score": score,
            "scores": {"hook": score, "emotion": score, "value": score, "share": score, "overall": score},
            "reason": reason,
            "quote": quote,
            "caption_preset": options.caption_preset,
            "reframe_mode": options.reframe_mode,
            "captions": options.captions,
            "hook_titles": options.hook_titles,
            "meta": {
                "live": True,
                "monitor_id": moment.get("monitor_id"),
                "moment_id": moment.get("moment_id"),
                "origin": origin,
                "signal": moment.get("signal"),
                "chat": [message.model_dump() for message in chat] if options.chat_overlay else [],
                "analysis": moment.get("analysis")
                or ("live-content" if origin != "chat" else "live-chat-spike"),
                "transcriber": transcript.provider,
                "region": region.code,
                "platform": options.platform,
                "variants": variants,
                "forecast": forecast_data,
                "meme_inserts": options.meme_inserts,
                "meme_insert_seconds": options.meme_insert_seconds,
                "meme_max_inserts": options.meme_max_inserts,
            },
        }
    )

    def on_progress(fraction: float, message: str) -> None:
        jobs.progress(job_id, 0.3 + fraction * 0.65, "rendering", message)

    render_clip_record(clip["id"], transcript=transcript, on_progress=on_progress)
    db.update("projects", project_id, {"status": "completed"})
    return {"project_id": project_id, "clip_id": clip["id"]}


def cleanup_outputs(project_id: str) -> None:
    storage.remove_project(project_id)


__all__ = [
    "cleanup_outputs",
    "ensure_source",
    "process_live_moment",
    "process_project",
    "render_clip_record",
]