"""Command line interface.

Four verbs, no flags to memorise::

    clipbait serve                 # dashboard + API on :8000
    clipbait process <url|file>    # long video -> clips, in the terminal
    clipbait live <url|demo>       # watch live chat, clip the spikes
    clipbait doctor                # what is installed, what is missing

Everything the CLI does is also available over HTTP; the CLI exists so the
engine is usable on a headless box and scriptable in CI.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import db, storage
from .config import detect_capabilities, get_settings
from .schema import MonitorOptions, ProjectOptions


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


# --------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = get_settings()
    caps = detect_capabilities(settings)
    print("Clipbait doctor\n")
    rows = [
        ("ffmpeg", caps.ffmpeg, settings.ffmpeg, "required for every render"),
        ("ffprobe", caps.ffprobe, settings.ffprobe, "required for media metadata"),
        ("yt-dlp", caps.ytdlp, settings.ytdlp, "required for URL ingestion"),
        ("faster-whisper", caps.whisper, "pip install 'clipbait[whisper]'", "real captions"),
        ("opencv", caps.vision, "pip install 'clipbait[vision]'", "face-aware reframing"),
        ("pytchat", caps.pytchat, "pip install 'clipbait[live]'", "keyless YouTube live chat"),
    ]
    for name, ok, hint, why in rows:
        mark = "ok  " if ok else "miss"
        detail = hint if ok else f"{hint}  ({why})"
        print(f"  [{mark}] {name:<16} {detail}")
    print(f"\n  transcriber : {caps.transcriber}")
    print(f"  llm         : {caps.llm_provider or 'none (heuristic ranking still works)'}")
    print(f"  data dir    : {settings.data_dir}")
    print(f"  database    : {settings.db_path}")

    problems = []
    if not caps.ffmpeg:
        problems.append("ffmpeg is missing; install it before rendering")
    if not caps.ytdlp:
        problems.append("yt-dlp is missing; local uploads still work")
    if caps.transcriber == "mock":
        problems.append(
            "no speech model: transcripts will be synthetic. "
            "Install faster-whisper or set OPENAI_API_KEY."
        )
    if problems:
        print("\nWarnings:")
        for problem in problems:
            print(f"  - {problem}")
    return 0 if caps.ffmpeg else 1


# ---------------------------------------------------------------------- serve


def cmd_serve(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.ensure_dirs()
    db.init_db()
    import uvicorn

    from .api.app import app

    print(f"Clipbait on http://{args.host}:{args.port}  ·  data={settings.data_dir}")
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="debug" if args.verbose else "info",
        reload=args.reload,
    )
    return 0


# -------------------------------------------------------------------- process


def cmd_process(args: argparse.Namespace) -> int:
    from .ingest import source_kind_for
    from .pipeline import process_project

    settings = get_settings()
    settings.ensure_dirs()
    db.init_db()

    target = args.source
    is_file = Path(target).expanduser().exists()
    options = ProjectOptions(
        clips=args.clips,
        target_seconds=args.target_seconds,
        caption_preset=args.preset,
        reframe_mode=args.reframe,
        captions=not args.no_captions,
        hook_titles=not args.no_hooks,
        region=args.region,
        platform=args.platform,
        memes=not args.no_memes,
        llm=not args.no_llm,
    )
    project = db.create_project(
        {
            "title": args.title or (Path(target).stem if is_file else "CLI project"),
            "source_kind": "local" if is_file else source_kind_for(target),
            "source_url": None if is_file else target,
            "source_path": None,
            "options": options.model_dump(),
        }
    )
    if is_file:
        from .ingest import ingest_local

        result = ingest_local(Path(target).expanduser(), project["id"], project["title"])
        db.update(
            "projects",
            project["id"],
            {
                "source_path": storage.relative(result.path),
                "duration": result.duration,
                "title": args.title or result.title,
            },
        )

    try:
        result = process_project(project["id"])
    except Exception as exc:  # noqa: BLE001 - the CLI is the error surface here
        print(f"\nFailed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    clips = db.list_clips(project["id"])
    print(f"\n{result['clips']} clip(s) ready  ·  transcriber={result['transcriber']}")
    out_dir = storage.project_dir(project["id"]) / "clips"
    for clip in clips:
        if clip.get("render_path"):
            path = storage.resolve(clip["render_path"])
            score = (clip.get("meta") or {}).get("forecast", {}).get("overall", clip["score"])
            print(f"  {score:>3}  {clip['start']:7.1f}-{clip['end']:<7.1f}  {path}")
            print(f"       {clip['title']}")
    print(f"\nProject page: {settings.public_base_url}/projects/{project['id']}")
    print(f"Files:        {out_dir}")
    if args.json:
        print(json.dumps({"project": project["id"], "clips": clips}, default=str, indent=2))
    return 0


# ----------------------------------------------------------------------- live


def cmd_live(args: argparse.Namespace) -> int:
    from . import live

    settings = get_settings()
    settings.ensure_dirs()
    db.init_db()

    url = "demo://live" if args.source in {"demo", "demo://live"} else args.source
    options = MonitorOptions(
        chat_provider="demo" if url.startswith("demo://") else args.chat_provider,
        spike_threshold=args.threshold,
        auto_clip=not args.no_clip,
        captions=not args.no_captions,
        chat_overlay=not args.no_chat_overlay,
        caption_preset=args.preset,
        region=args.region,
        platform=args.platform,
    )
    monitor = db.create_monitor(
        {
            "url": url,
            "title": args.title or ("Demo live stream" if url.startswith("demo://") else "Live monitor"),
            "chat_provider": options.chat_provider,
            "options": options.model_dump(),
            "status": "starting",
        }
    )
    live.start(monitor["id"])
    print(f"Monitoring {url}")
    print(f"  id: {monitor['id']}   threshold: {options.spike_threshold}σ")
    print("  Ctrl-C to stop.\n")
    import time

    try:
        seen = set()
        while True:
            for moment in db.list_moments(monitor["id"]):
                if moment["id"] in seen:
                    continue
                seen.add(moment["id"])
                top = (moment.get("signal") or {}).get("top_messages") or ["chat went wild"]
                print(
                    f"  spike {moment['start']:7.1f}s  score={moment['score']:.2f}  "
                    f"\"{top[0][:60]}\""
                )
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        try:
            live.stop(monitor["id"])
        except ValueError:
            pass
    clips = db.list_jobs(monitor_id=monitor["id"])
    print(f"Monitored for {int(time.monotonic())}s · {len(seen)} spike(s) · {len(clips)} job(s)")
    return 0


# ---------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipbait",
        description="Turn long videos and live streams into vertical clips.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version="clipbait 0.1.0")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the dashboard and API")
    serve.add_argument("--host", default=os.getenv("CLIPBAIT_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.getenv("CLIPBAIT_PORT", "8000")))
    serve.add_argument("--reload", action="store_true", help="dev auto-reload")
    serve.set_defaults(func=cmd_serve)

    process = sub.add_parser("process", help="clip a video file or URL")
    process.add_argument("source", help="path, YouTube/Vimeo URL, or direct video URL")
    process.add_argument("--clips", type=int, default=5)
    process.add_argument("--target-seconds", type=int, default=55)
    process.add_argument("--preset", default="default")
    process.add_argument("--reframe", default="auto", choices=["auto", "center", "blur", "split"])
    process.add_argument("--region", default="US", help="market profile, e.g. US, IN, BR, PK")
    process.add_argument(
        "--platform", default="tiktok", choices=["tiktok", "reels", "shorts", "linkedin", "x"]
    )
    process.add_argument("--title")
    process.add_argument("--no-captions", action="store_true")
    process.add_argument("--no-hooks", action="store_true")
    process.add_argument("--no-memes", action="store_true")
    process.add_argument("--no-llm", action="store_true", help="force the offline ranker")
    process.add_argument("--json", action="store_true", help="print the result as JSON")
    process.set_defaults(func=cmd_process)

    live_cmd = sub.add_parser("live", help="watch live chat and clip the spikes")
    live_cmd.add_argument("source", help="YouTube/Twitch URL, or 'demo'")
    live_cmd.add_argument("--threshold", type=float, default=3.0, help="spike z-score threshold")
    live_cmd.add_argument("--chat-provider", default="auto", choices=["auto", "youtube", "twitch", "api"])
    live_cmd.add_argument("--preset", default="default")
    live_cmd.add_argument("--region", default="US")
    live_cmd.add_argument(
        "--platform", default="tiktok", choices=["tiktok", "reels", "shorts", "linkedin", "x"]
    )
    live_cmd.add_argument("--title")
    live_cmd.add_argument("--no-clip", action="store_true", help="detect only, do not render")
    live_cmd.add_argument("--no-captions", action="store_true")
    live_cmd.add_argument("--no-chat-overlay", action="store_true")
    live_cmd.set_defaults(func=cmd_live)

    doctor = sub.add_parser("doctor", help="report installed capabilities")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    _setup_logging(getattr(args, "verbose", False))
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())