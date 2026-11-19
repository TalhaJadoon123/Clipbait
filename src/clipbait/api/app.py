"""HTTP API.

Contract notes:
* every list endpoint returns a plain array (no envelope) - easier for the SPA
* clip media is only served by id, never by path (no traversal surface)
* progress is SSE; polling still works because every event is persisted
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import db, jobs, live, storage
from ..analyze import prompts
from ..captions import preset_catalog
from ..config import detect_capabilities, get_settings
from ..events import bus
from ..net_guard import UnsafeUrl, ensure_safe_url
from ..pipeline import process_project, render_clip_record
from ..schema import (
    ClipPatch,
    CreateMonitorRequest,
    CreateProjectRequest,
    MonitorOptions,
    ProjectOptions,
    RenderRequest,
)
from ..virality import catalog as region_catalog
from ..virality.memes import MEMES

log = logging.getLogger("clipbait.api")
settings = get_settings()

PLATFORMS = ["tiktok", "reels", "shorts", "linkedin", "x"]


def _resolve_web_dist() -> Path:
    """Locate the built SPA for editable installs, wheels and containers."""
    override = os.getenv("CLIPBAIT_WEB_DIST", "").strip()
    if override:
        return Path(override).expanduser()
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "apps" / "web" / "dist",  # editable install / repo checkout
        Path.cwd() / "apps" / "web" / "dist",  # `clipbait serve` from the repo root
        here.parent / "web_dist",  # bundled next to the package
    ]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return candidates[0]


WEB_DIST = _resolve_web_dist()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    db.init_db()
    bus.bind_loop(asyncio.get_running_loop())
    log.info("clipbait %s ready · data=%s", "0.1.0", settings.data_dir)
    yield
    live.stop_all()
    jobs.shutdown(wait=False)


router = APIRouter(prefix="/api/v1")


# ----------------------------------------------------------------------- meta


@router.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "version": "0.1.0"}


@router.get("/meta")
def meta() -> dict:
    caps = detect_capabilities(settings)
    return {
        "version": "0.1.0",
        "capabilities": caps.as_dict(),
        "caption_presets": preset_catalog(),
        "regions": region_catalog(),
        "platforms": PLATFORMS,
        "prompt_version": prompts.PROMPT_VERSION,
        "memes": [
            {
                "id": meme.id,
                "name": meme.name,
                "pattern": meme.pattern,
                "regions": list(meme.regions),
                "signals": list(meme.signals),
                "risk": meme.risk,
                "example": meme.example,
            }
            for meme in MEMES
        ],
        "live_providers": ["auto", "youtube", "twitch", "demo"],
        "data_dir": str(settings.data_dir),
    }


# ------------------------------------------------------------------- projects


def _latest_job(project_id: str) -> dict | None:
    rows = db.list_jobs(project_id=project_id)
    return rows[0] if rows else None


@router.get("/projects")
def list_projects() -> list[dict]:
    projects = db.list_projects()
    for project in projects:
        project["clip_count"] = len(db.list_clips(project["id"]))
        project["job"] = _latest_job(project["id"])
    return projects


@router.post("/projects", status_code=201)
def create_project(request: CreateProjectRequest) -> dict:
    if not request.url:
        raise HTTPException(400, "url is required")
    try:
        ensure_safe_url(request.url)
    except UnsafeUrl as exc:
        raise HTTPException(400, str(exc)) from exc

    project = db.create_project(
        {
            "title": request.title or _title_from_url(request.url),
            "source_kind": request.source_kind or "youtube",
            "source_url": request.url,
            "options": request.options.model_dump(),
        }
    )
    job = jobs.submit("process", process_project, project_id=project["id"], args=(project["id"],))
    bus.publish(f"project:{project['id']}", {"type": "created", "project": project, "job": job})
    return {"project": project, "job": job}


def _title_from_url(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1].split("?")[0]
    return tail.replace("-", " ").replace("_", " ")[:80] or "Untitled project"


@router.post("/projects/upload", status_code=201)
async def upload_project(
    file: UploadFile = File(...),
    options: str = Form("{}"),
    title: str | None = Form(None),
) -> dict:
    try:
        parsed = json.loads(options or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "options must be valid JSON") from exc
    try:
        project_options = ProjectOptions(**parsed)
    except Exception as exc:  # noqa: BLE001 - pydantic message is the useful part
        raise HTTPException(400, str(exc)) from exc

    project = db.create_project(
        {
            "title": title or Path(file.filename or "Upload").stem,
            "source_kind": "upload",
            "source_url": None,
            "options": project_options.model_dump(),
        }
    )
    saved = storage.save_upload(project["id"], file.filename or "upload.mp4", file.file)
    db.update("projects", project["id"], {"source_path": storage.relative(saved)})
    job = jobs.submit("process", process_project, project_id=project["id"], args=(project["id"],))
    return {"project": db.get_project(project["id"]), "job": job}


def _get_project_or_404(project_id: str) -> dict:
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "project not found")
    return project


@router.get("/projects/{project_id}")
def get_project(project_id: str) -> dict:
    project = _get_project_or_404(project_id)
    clips = db.list_clips(project_id)
    project["clips"] = clips
    project["job"] = _latest_job(project_id)
    project["jobs"] = db.list_jobs(project_id=project_id)
    transcript_meta = storage.read_json(storage.transcript_path(project_id), default=None)
    project["has_transcript"] = storage.transcript_path(project_id).exists()
    project["transcriber"] = (transcript_meta or {}).get("provider")
    return project


@router.post("/projects/{project_id}/retry")
def retry_project(project_id: str) -> dict:
    project = _get_project_or_404(project_id)
    db.update("projects", project_id, {"status": "queued", "error": None})
    job = jobs.submit("process", process_project, project_id=project_id, args=(project_id,))
    bus.publish(f"project:{project_id}", {"type": "retry", "job": job})
    return {"project": project, "job": job}


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str) -> JSONResponse:
    _get_project_or_404(project_id)
    db.delete_project(project_id)
    storage.remove_project(project_id)
    return JSONResponse(status_code=204, content=None)


@router.get("/projects/{project_id}/events")
async def project_events(project_id: str) -> StreamingResponse:
    _get_project_or_404(project_id)
    return _sse(f"project:{project_id}")


@router.get("/projects/{project_id}/clips")
def project_clips(project_id: str) -> list[dict]:
    _get_project_or_404(project_id)
    return db.list_clips(project_id)


# ---------------------------------------------------------------------- clips


def _get_clip_or_404(clip_id: str) -> dict:
    clip = db.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "clip not found")
    return clip


@router.get("/clips/{clip_id}")
def get_clip(clip_id: str) -> dict:
    clip = _get_clip_or_404(clip_id)
    clip["project"] = db.get_project(clip["project_id"])
    return clip


@router.patch("/clips/{clip_id}")
def patch_clip(clip_id: str, patch: ClipPatch) -> dict:
    clip = _get_clip_or_404(clip_id)
    project = db.get_project(clip["project_id"]) or {}
    fields = patch.model_dump(exclude_none=True)
    if "start" in fields or "end" in fields:
        start = float(fields.get("start", clip["start"]))
        end = float(fields.get("end", clip["end"]))
        if end <= start + 1:
            raise HTTPException(400, "end must be at least 1s after start")
        duration = project.get("duration") or end
        if start < 0 or end > duration + 0.5:
            raise HTTPException(400, "clip range is outside the source video")
        fields["start"], fields["end"] = round(start, 3), min(round(end, 3), duration)
    updated = db.update("clips", clip_id, fields)
    if not updated:
        raise HTTPException(404, "clip not found")
    return updated


@router.post("/clips/{clip_id}/render")
def render_clip_endpoint(clip_id: str, request: RenderRequest | None = None) -> dict:
    clip = _get_clip_or_404(clip_id)
    if request:
        fields = request.model_dump(exclude_none=True)
        if fields:
            db.update("clips", clip_id, fields)
    job = jobs.submit(
        "render", render_clip_record, project_id=clip["project_id"], args=(clip_id,)
    )
    return {"clip": db.get_clip(clip_id), "job": job}


@router.post("/clips/{clip_id}/variants/{variant_id}")
def select_variant(clip_id: str, variant_id: str) -> dict:
    clip = _get_clip_or_404(clip_id)
    variants = (clip.get("meta") or {}).get("variants") or []
    variant = next((item for item in variants if item.get("id") == variant_id), None)
    if not variant:
        raise HTTPException(404, "variant not found")
    meta = {**(clip.get("meta") or {}), "selected_variant": variant_id}
    updated = db.update(
        "clips",
        clip_id,
        {
            "title": variant["title"],
            "hook": variant["hook"],
            "description": variant["description"],
            "hashtags": variant["hashtags"],
            "meta": meta,
        },
    )
    return updated or clip


@router.get("/clips/{clip_id}/video")
def clip_video(clip_id: str) -> FileResponse:
    clip = _get_clip_or_404(clip_id)
    if not clip.get("render_path"):
        raise HTTPException(404, "clip has not been rendered yet")
    path = storage.resolve(clip["render_path"])
    if not path.exists():
        raise HTTPException(404, "render file missing")
    return FileResponse(path, media_type="video/mp4", filename=f"{clip['id']}.mp4")


@router.get("/clips/{clip_id}/thumb")
def clip_thumb(clip_id: str) -> FileResponse:
    clip = _get_clip_or_404(clip_id)
    thumb = clip.get("thumb_path")
    path = storage.resolve(thumb) if thumb else None
    if not path or not path.exists():
        raise HTTPException(404, "thumbnail missing")
    return FileResponse(path, media_type="image/jpeg")


# ----------------------------------------------------------------------- live


@router.get("/live/monitors")
def list_monitors() -> list[dict]:
    monitors = db.list_monitors()
    for monitor in monitors:
        monitor["moments"] = len(db.list_moments(monitor["id"], limit=200))
        monitor["active"] = monitor["id"] in live.active_monitors()
    return monitors


@router.post("/live/monitors", status_code=201)
def create_monitor(request: CreateMonitorRequest) -> dict:
    if not (request.url.startswith("demo://") or request.url == "demo"):
        try:
            ensure_safe_url(request.url)
        except UnsafeUrl as exc:
            raise HTTPException(400, str(exc)) from exc
    monitor = db.create_monitor(
        {
            "url": request.url,
            "title": request.title or "Live monitor",
            "chat_provider": request.options.chat_provider,
            "options": request.options.model_dump(),
            "status": "starting",
        }
    )
    live.start(monitor["id"])
    return db.get_monitor(monitor["id"]) or monitor


@router.get("/live/monitors/{monitor_id}")
def get_monitor(monitor_id: str) -> dict:
    monitor = db.get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(404, "monitor not found")
    runner_stats = live.status(monitor_id)
    monitor["live_stats"] = runner_stats
    monitor["moments"] = db.list_moments(monitor_id)
    monitor["active"] = monitor_id in live.active_monitors()
    return monitor


@router.post("/live/monitors/{monitor_id}/stop")
def stop_monitor(monitor_id: str) -> dict:
    try:
        return live.stop(monitor_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/live/monitors/{monitor_id}/start")
def start_monitor(monitor_id: str) -> dict:
    try:
        live.start(monitor_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return db.get_monitor(monitor_id) or {}


@router.delete("/live/monitors/{monitor_id}", status_code=204)
def delete_monitor(monitor_id: str) -> JSONResponse:
    if not db.get_monitor(monitor_id):
        raise HTTPException(404, "monitor not found")
    try:
        live.stop(monitor_id)
    except ValueError:
        pass
    db.delete("monitors", monitor_id)
    storage.remove_project(monitor_id)
    return JSONResponse(status_code=204, content=None)


@router.get("/live/monitors/{monitor_id}/moments")
def monitor_moments(monitor_id: str) -> list[dict]:
    if not db.get_monitor(monitor_id):
        raise HTTPException(404, "monitor not found")
    return db.list_moments(monitor_id)


@router.get("/live/monitors/{monitor_id}/chat")
def monitor_chat(monitor_id: str, since: float = Query(0.0, ge=0), limit: int = 500) -> dict:
    if not db.get_monitor(monitor_id):
        raise HTTPException(404, "monitor not found")
    return {
        "messages": db.list_chat_events(monitor_id, since=since, limit=min(1000, limit)),
        "stats": live.status(monitor_id),
    }


@router.get("/live/monitors/{monitor_id}/events")
async def monitor_events(monitor_id: str) -> StreamingResponse:
    if not db.get_monitor(monitor_id):
        raise HTTPException(404, "monitor not found")
    return _sse(f"monitor:{monitor_id}")


@router.post("/live/demo", status_code=201)
def live_demo(options: MonitorOptions | None = None) -> dict:
    opts = options or MonitorOptions(
        chat_provider="demo", auto_clip=True, chat_overlay=True, spike_threshold=2.5
    )
    monitor = db.create_monitor(
        {
            "url": "demo://live",
            "title": "Demo live stream",
            "chat_provider": "demo",
            "options": opts.model_dump(),
            "status": "starting",
        }
    )
    live.start(monitor["id"])
    return db.get_monitor(monitor["id"]) or monitor


def _sse(channel: str, replay: int = 15) -> StreamingResponse:
    async def generator():
        queue = bus.subscribe(channel)
        try:
            for event in bus.snapshot(channel)[-replay:]:
                yield f"data: {json.dumps(event)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            bus.unsubscribe(channel, queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="Clipbait",
        version="0.1.0",
        description="Local-first engine that turns long videos and live streams into vertical clips.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    if WEB_DIST.exists():
        assets = WEB_DIST / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            if full_path.startswith(("api/", "docs", "openapi.json")):
                raise HTTPException(404, "not found")
            candidate = WEB_DIST / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(WEB_DIST / "index.html")

    return app


app = create_app()