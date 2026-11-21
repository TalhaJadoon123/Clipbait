#!/usr/bin/env python3
"""Keep Clipbait's autopilot pointed at one streamer's *current* live room.

``clipbait autopilot --source <url>`` watches one fixed URL. That is exactly
what an unattended run wants -- keep clipping the same person, not whichever
room happens to be biggest -- but a watch URL goes stale the moment the
streamer restarts (they get a new video id) and the fixed URL then points at a
dead room forever.

This supervisor closes that gap. Every ``--check`` seconds it re-resolves the
live room whose channel or title matches ``--match`` and (re)starts the
autopilot against it. It restarts on a new URL, on a crashed/exited autopilot,
and stops the stale child when the streamer goes offline. It runs until it is
told to stop, so it is safe to keep alive under systemd.

    python scripts/supervise_live.py --match "kai cenat" --query "gta 6" \
        --platform shorts --live --now --hashtags "#kaicenat #gta6 #shorts"

Anything after ``--`` is passed straight through to ``autopilot`` (for example
``--min-score 45``), for flags this supervisor does not model itself.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_TIMEOUT = 90


def log(message: str) -> None:
    print(f"[supervise] {time.strftime('%H:%M:%S')} {message}", flush=True)


def resolve_room(match: str, query: str, limit: int) -> tuple[str | None, str]:
    """The biggest live room whose channel or title contains ``match``."""
    try:
        from clipbait import discover

        streams = discover.search_live(query, limit, resolve=True, timeout=RESOLVE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - a flaky search must not kill the loop
        log(f"discovery failed: {type(exc).__name__}: {exc}")
        return None, ""

    needle = match.lower()
    for stream in streams:
        if needle in stream.channel.lower() or needle in stream.title.lower():
            return stream.url, f"{stream.channel} - {stream.title}"[:120]
    return None, ""


def build_autopilot_command(args: argparse.Namespace, source: str) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "clipbait.cli",
        "autopilot",
        "--source",
        source,
        "--rounds",
        str(args.rounds),
        "--minutes",
        str(args.minutes),
        "--max-clips",
        str(args.max_clips),
        "--threshold",
        str(args.threshold),
        "--content-threshold",
        str(args.content_threshold),
        "--platform",
        args.platform,
        "--min-score",
        str(args.min_score),
        "--title",
        args.title or args.match,
    ]
    if args.hashtags:
        command += ["--hashtags", args.hashtags]
    if args.campaign:
        command += ["--campaign", args.campaign]
    if args.live:
        command.append("--live")
    if args.now:
        command.append("--now")
    command += list(args.passthrough or [])
    return command


def stop_child(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    log(f"stopping autopilot (pid {child.pid})")
    child.terminate()
    try:
        child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--match", required=True, help="channel/title substring to pin to")
    parser.add_argument("--query", default="gta 6", help="live search that should surface the room")
    parser.add_argument("--check", type=int, default=120, help="seconds between re-resolves")
    parser.add_argument("--resolve-limit", type=int, default=12, help="how many rooms to resolve")
    parser.add_argument("--minutes", type=int, default=30, help="minutes to watch per autopilot round")
    parser.add_argument("--rounds", type=int, default=0, help="rounds the autopilot runs; 0 = forever")
    parser.add_argument("--max-clips", type=int, default=4, help="render cap per round; 0 = no cap")
    parser.add_argument("--threshold", type=float, default=3.0, help="chat spike z-score threshold")
    parser.add_argument("--content-threshold", type=float, default=2.5)
    parser.add_argument("--min-score", type=float, default=35.0, help="export/post above this score")
    parser.add_argument("--platform", default="shorts", help="destination: shorts, tiktok, reels, x ...")
    parser.add_argument("--hashtags", default="", help="tags that drive the copy")
    parser.add_argument("--campaign", default="", help="campaign rules file")
    parser.add_argument("--title", default="", help="monitor title")
    parser.add_argument("--live", action="store_true", help="post for real (else package only)")
    parser.add_argument("--now", action="store_true", help="post immediately, not at the best window")
    parser.add_argument(
        "passthrough",
        nargs=argparse.REMAINDER,
        help="extra args for autopilot, after --",
    )
    args = parser.parse_args(argv)
    args.passthrough = [a for a in args.passthrough if a != "--"]

    running = True

    def handle_stop(signum, _frame):  # noqa: ANN001
        nonlocal running
        log(f"signal {signum}: shutting down")
        running = False

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    child: subprocess.Popen | None = None
    current_url: str | None = None

    try:
        while running:
            url, label = resolve_room(args.match, args.query, args.resolve_limit)
            if url is None:
                log(f"no live room matching {args.match!r} right now")
                if child is not None and child.poll() is None:
                    stop_child(child)
                    child, current_url = None, None
            elif child is None or child.poll() is not None or url != current_url:
                if child is not None and child.poll() is None:
                    stop_child(child)
                log(f"watching {label}  ({url})")
                child = subprocess.Popen(  # noqa: S603 - command is built here, not user input
                    build_autopilot_command(args, url), cwd=str(REPO_ROOT)
                )
                current_url = url

            slept = 0
            while running and slept < max(5, args.check):
                time.sleep(min(5, max(5, args.check) - slept))
                slept += 5
    finally:
        if child is not None:
            stop_child(child)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
