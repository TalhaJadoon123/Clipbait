"""End-to-end orchestration.

`process_project` is the only function the API/worker/CLI need for long-form
video. Every step reports progress and every failure is persisted, so the UI
can explain exactly where a job died.
"""

from __future__ import annotations

import logging

from . import analyze, db, jobs, media, storage, transcribe
from .captions.presets import get_preset
from .config import get_settings
from .reframe import plan_reframe
from .render import render_clip
from .schema import ChatMessage, Highlight, ProjectOptions, Transcript
from .virality import build_variants, forecast, get_region

log = logging.getLogger("clipbait.pipeline")


def _project_options(project: dict) -> ProjectOptions:
    return ProjectOptions(**(project.get("options") or {}))


def _set_status(project_id: str, status: str, **fields) -> None:
    db.update("projects", project_id, {"status": status, **fields})


def ensure_source(project: dict, job_id: str | None, progress_base: float = 0.0) -> dict:
    """Download or copy the source if it is not already on disk."""
    project_id = project["id"]
    existing = project.get("source_path")
    if existing:
        path = storage.resolve(existing)
        if path.exists() and path.stat().st_size > 0:
            return project

    if not project.get("source_url"):
        raise media.MediaError("project has no source URL or file")

    jobs.progress(job_id, progress_base + 0.04, "downloading", "Fetching source video")
    _set_status(project_id, "downloading")
    result = ingest_source(project, job_id)
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


def ingest_source(project: dict, job_id: str | None = None):
    from .ingest import fetch_remote, ingest_local

    project_id = project["id"]
    if project.get("source_kind") == "local" and project.get("source_path"):
        return ingest_local(storage.resolve(project["source_path"]), project_id, project.get("title"))
    url = project.get("source_url")
    if not url:
        raise ValueError("missing source")
    return fetch_remote(url, project_id)


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
    project = ensure_source(project, job_id)

    source = storage.resolve(project["source_path"]) if project.get("source_path") else None
    if not source or not source.exists():
        raise media.MediaError("source file missing after ingest")

    info = media.probe(source)
    db.update(
        "projects",
        project_id,
        {"duration": info.duration, "status": "transcribing", "error": None},
    )

    jobs.progress(job_id, 0.1, "audio", "Extracting audio")
    audio = media.extract_audio(source, storage.audio_path(project_id))

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
            audio, language="auto" if options.language == "auto" else options.language
        )
        transcript.duration = transcript.duration or info.duration
        storage.write_json(storage.transcript_path(project_id), transcript.model_dump())
    else:
        jobs.progress(job_id, 0.34, "transcribing", "Reusing cached transcript")

    jobs.progress(job_id, 0.42, "analyzing", "Reading the room")
    energy = media.audio_rms(audio)
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
            },
        }
    )


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
    if not project or not project.get("source_path"):
        raise ValueError("project source missing")
    source = storage.resolve(project["source_path"])
    if transcript is None:
        transcript = _load_transcript(clip["project_id"])
    if transcript is None:
        raise ValueError("transcript missing for project")

    db.update("clips", clip_id, {"status": "rendering"})
    meta = clip.get("meta") or {}
    source_info = media.probe(source)
    plan = plan_reframe(
        source,
        clip["start"],
        clip["end"],
        source_w=source_info.width,
        source_h=source_info.height,
        mode=clip.get("reframe_mode", "auto"),
    )
    chat = None
    if meta.get("chat"):
        chat = [ChatMessage(**message) for message in meta["chat"]]

    preset = get_preset(clip.get("caption_preset", "default"))
    out_path = storage.clip_dir(clip["project_id"], clip_id) / "clip.mp4"
    result = render_clip(
        source,
        out_path,
        clip["start"],
        clip["end"],
        plan,
        words=transcript.words,
        caption_preset=clip.get("caption_preset", "default"),
        captions=bool(clip.get("captions", True)),
        hook=(clip.get("hook") or clip.get("title")) if clip.get("hook_titles", True) else None,
        chat=chat,
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
                "error": None,
            },
        },
    )
    return updated or clip


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
    score = int(max(0, min(100, round(moment.get("score", 0) * 10))))

    region = get_region(options.region)
    highlight = Highlight(
        start=0.0,
        end=max(1.0, info.duration),
        title=title[:80],
        hook=(moment.get("hook") or title)[:120],
        reason=moment.get("reason", "Chat velocity spiked here."),
        scores={"hook": score, "emotion": score, "value": score, "share": score, "overall": score},
        source="live",
    )
    live_text = moment.get("reason") or title
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
            "description": chosen["description"] if chosen else "Auto-cut from a live chat spike.",
            "hashtags": chosen["hashtags"] if chosen else ["live", "shorts", "reels", "clips"],
            "start": 0.0,
            "end": max(1.0, info.duration),
            "score": score,
            "scores": {"hook": score, "emotion": score, "value": score, "share": score, "overall": score},
            "reason": moment.get("reason", "Chat velocity spiked here."),
            "caption_preset": options.caption_preset,
            "reframe_mode": options.reframe_mode,
            "captions": options.captions,
            "hook_titles": options.hook_titles,
            "meta": {
                "live": True,
                "monitor_id": moment.get("monitor_id"),
                "moment_id": moment.get("moment_id"),
                "chat": [message.model_dump() for message in chat] if options.chat_overlay else [],
                "analysis": "live-chat-spike",
                "transcriber": transcript.provider,
                "region": region.code,
                "platform": options.platform,
                "variants": variants,
                "forecast": forecast_data,
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