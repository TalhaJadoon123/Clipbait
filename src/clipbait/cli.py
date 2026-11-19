"""Command line interface.

The verbs, shortest first::

    clipbait serve                 # dashboard + API on :8000
    clipbait process <url|file>    # long video -> clips, in the terminal
    clipbait live <url|demo>       # watch live chat, clip the spikes
    clipbait streams --query       # find the biggest live rooms right now
    clipbait queue --export        # rank ready clips, write upload bundles
    clipbait check <clip>          # pre-post check: will it be accepted?
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
        content_detection=not args.no_content,
        content_threshold=args.content_threshold,
        max_clips=args.max_clips,
        buffer_seconds=args.buffer_seconds,
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
    if options.content_detection:
        print(
            f"  content detection on (speech/audio/action, threshold {options.content_threshold})"
        )
    print("  Ctrl-C to stop.\n")
    import time

    try:
        seen = set()
        while True:
            for moment in db.list_moments(monitor["id"]):
                if moment["id"] in seen:
                    continue
                seen.add(moment["id"])
                signal = moment.get("signal") or {}
                top = signal.get("top_messages") or []
                label = signal.get("quote") or (top[0] if top else "chat went wild")
                origin = signal.get("origin") or "chat"
                kind = "spike" if origin == "chat" else "content"
                print(
                    f"  {kind:7s} {moment['start']:7.1f}s  score={moment['score']:.2f}  "
                    f"\"{str(label)[:60]}\""
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


# ---------------------------------------------------------------------- check


def cmd_check(args: argparse.Namespace) -> int:
    from .money.check import check_clip, default_campaign, load_campaign

    db.init_db()
    caption = args.caption or ""
    hook = ""
    target = args.clip
    clip = db.get_clip(args.clip) if args.clip.startswith("clip_") else None
    if clip:
        if not clip.get("render_path"):
            print(f"clip {args.clip} has no render yet")
            return 1
        target = storage.resolve(clip["render_path"])
        hook = str(clip.get("hook") or "")
        if not caption:
            body = str(clip.get("hook") or clip.get("title") or "").strip()
            description = str(clip.get("description") or "").strip()
            tags = " ".join(clip.get("hashtags") or ())
            caption = "\n\n".join(part for part in (body, description, tags) if part)

    rules = load_campaign(args.campaign) if args.campaign else default_campaign()
    report = check_clip(target, caption=caption, hook=hook, rules=rules)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print("\n".join(report.lines()))
        if report.rules.source != "defaults":
            print(f"rules: {report.rules.source}")
    return 0 if report.passed else 1


def cmd_check_template(args: argparse.Namespace) -> int:
    from .money.check import campaigns_dir, template

    target = Path(args.out).expanduser() if args.out else campaigns_dir() / "campaign.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template(), encoding="utf-8")
    print(f"wrote {target}")
    print("fill in the real numbers from the campaign brief, then: clipbait check <clip>")
    return 0


# -------------------------------------------------------------------- streams


def cmd_streams(args: argparse.Namespace) -> int:
    from . import discover

    try:
        streams = discover.search_live(
            args.query, args.limit, resolve=not args.no_resolve, timeout=args.timeout
        )
    except discover.DiscoverError as exc:
        print(f"discovery failed: {exc}")
        return 1

    if args.json:
        print(json.dumps([stream.as_dict() for stream in streams], indent=2))
    else:
        markdown = discover.as_markdown(args.query, streams)
        if not streams:
            print(markdown.strip())
        for index, stream in enumerate(streams, start=1):
            print(
                f"  {index:2d}. {stream.viewers:>7,} viewers  {stream.channel[:24]:24}  "
                f"{stream.title[:52]:52}  {stream.url}"
            )
        if streams:
            print(f"\n  {len(streams)} live stream(s). Biggest room: {streams[0].channel}")
    if args.out:
        target = Path(args.out).expanduser()
        target.write_text(discover.as_markdown(args.query, streams), encoding="utf-8")
        print(f"wrote {target}")

    if args.watch:
        if not streams:
            print("nothing to watch")
            return 1
        chosen = streams[0]
        print(f"\nwatching {chosen.channel} ({chosen.viewers:,} viewers): {chosen.url}\n")
        return cmd_live(
            argparse.Namespace(
                source=chosen.url,
                threshold=args.threshold,
                chat_provider="auto",
                preset=args.preset,
                region=args.region,
                platform=args.platform,
                title=chosen.title[:120],
                no_clip=False,
                no_captions=False,
                no_chat_overlay=False,
                no_content=False,
                content_threshold=args.content_threshold,
                max_clips=4,
                buffer_seconds=420,
            )
        )
    return 0


# ---------------------------------------------------------------------- queue


def cmd_queue(args: argparse.Namespace) -> int:
    from .money import build_queue, export_bundle, mark_uploaded

    settings = get_settings()
    settings.ensure_dirs()
    db.init_db()

    if args.mark:
        updated = mark_uploaded(args.mark, url=args.url, platform=args.platform)
        if not updated:
            print(f"clip not found: {args.mark}")
            return 1
        print(f"marked {args.mark} uploaded" + (f" -> {args.url}" if args.url else ""))
        return 0

    # A campaign brief carries the rate that actually pays, so it wins over the
    # platform RPM guess. ``--rate`` is the manual override for a brief that
    # was read in Discord and not written to disk yet.
    rate = args.rate
    if rate is None and args.campaign:
        from .money.check import load_campaign

        rate = load_campaign(args.campaign).rate_per_1k
    queue = build_queue(
        limit=args.limit,
        min_score=args.min_score,
        include_posted=args.include_posted,
        rate_per_1k=rate,
    )
    if args.json:
        print(json.dumps(queue.as_dict(), indent=2))
    else:
        basis = (
            f"campaign rate ${rate:.2f}/1K views"
            if rate
            else "platform RPM medians; pass --rate or --campaign for real campaign pricing"
        )
        print(
            f"{queue.count} ready clip(s) · ${queue.total_expected_usd:.2f} expected "
            f"({basis})"
        )
        if not queue.items:
            print("  nothing ready. render a clip first: clipbait process <url>")
        for index, item in enumerate(queue.items, start=1):
            print(
                f"  {index:2d}. ${item.expected_usd:7.2f}  "
                f"(${item.usd_low:.2f}-${item.usd_high:.2f})  "
                f"{item.platform:8s} {item.score:3d}  {item.duration:4.0f}s  {item.title[:58]}"
            )

    if args.export:
        for item in queue.items:
            try:
                bundle = export_bundle(item.clip_id, campaign=args.campaign)
            except FileNotFoundError as exc:
                print(f"  skip {item.clip_id}: {exc}")
                continue
            print(f"  exported {bundle['slug']} -> {bundle['dir']}")
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
    live_cmd.add_argument(
        "--no-content",
        action="store_true",
        help="chat only; skip speech/audio/action detection on the stream itself",
    )
    live_cmd.add_argument(
        "--content-threshold",
        type=float,
        default=2.5,
        help="on-stream signal threshold (0.5-12, lower = more clips)",
    )
    live_cmd.add_argument(
        "--max-clips", type=int, default=8, help="stop clipping after this many moments"
    )
    live_cmd.add_argument(
        "--buffer-seconds",
        type=int,
        default=420,
        help="footage kept on disk; must outlast content analysis",
    )
    live_cmd.set_defaults(func=cmd_live)

    queue_cmd = sub.add_parser("queue", help="rank ready clips by expected revenue")
    queue_cmd.add_argument("--limit", type=int, default=20)
    queue_cmd.add_argument("--min-score", type=float, default=0.0)
    queue_cmd.add_argument("--include-posted", action="store_true")
    queue_cmd.add_argument("--json", action="store_true")
    queue_cmd.add_argument("--export", action="store_true", help="write upload bundles")
    queue_cmd.add_argument(
        "--campaign", help="campaign rules file: gates the check and sets the rate"
    )
    queue_cmd.add_argument(
        "--rate", type=float, help="campaign payout per 1000 views, e.g. 2.0"
    )
    queue_cmd.add_argument("--mark", metavar="CLIP_ID", help="mark one clip uploaded")
    queue_cmd.add_argument("--url", help="posted URL for --mark")
    queue_cmd.add_argument("--platform", help="platform for --mark")
    queue_cmd.set_defaults(func=cmd_queue)

    check_cmd = sub.add_parser("check", help="pre-post check of a finished clip")
    check_cmd.add_argument("clip", help="clip id (clip_...) or path to an mp4")
    check_cmd.add_argument("--campaign", help="campaign rules file (min_seconds, required_hashtags, ...)")
    check_cmd.add_argument("--caption", help="caption that will be posted; defaults to the clip's copy")
    check_cmd.add_argument("--json", action="store_true")
    check_cmd.set_defaults(func=cmd_check)

    template_cmd = sub.add_parser("campaign", help="write a campaign rules template")
    template_cmd.add_argument("--out", help="where to write it (default: data/campaigns/campaign.md)")
    template_cmd.set_defaults(func=cmd_check_template)

    streams_cmd = sub.add_parser("streams", help="find live streams ranked by viewers")
    streams_cmd.add_argument("--query", default="gta 6")
    streams_cmd.add_argument("--limit", type=int, default=20)
    streams_cmd.add_argument("--timeout", type=int, default=60)
    streams_cmd.add_argument("--no-resolve", action="store_true", help="skip per-stream viewer lookup")
    streams_cmd.add_argument("--json", action="store_true")
    streams_cmd.add_argument("--out", help="also write a markdown table here")
    streams_cmd.add_argument("--watch", action="store_true", help="monitor the biggest room now")
    streams_cmd.add_argument("--threshold", type=float, default=3.0)
    streams_cmd.add_argument("--content-threshold", type=float, default=2.5)
    streams_cmd.add_argument("--preset", default="default")
    streams_cmd.add_argument("--region", default="US")
    streams_cmd.add_argument(
        "--platform", default="tiktok", choices=["tiktok", "reels", "shorts", "linkedin", "x"]
    )
    streams_cmd.set_defaults(func=cmd_streams)

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