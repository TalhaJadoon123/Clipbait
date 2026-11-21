#!/usr/bin/env python3
"""Keep Clipbait clipping the biggest *famous* live rooms, around the clock.

``supervise_live.py`` pins one autopilot to a single streamer. This supervisor
watches a *roster*: it searches YouTube's live filter for a set of topics (and,
optionally, a list of named streamers), ranks whatever is live right now by
concurrent viewers, and keeps the autopilot on the top ``--concurrency`` rooms.
As rooms end and new ones open it swaps children in and out, so an unattended box
clips the biggest live moments continuously instead of going idle when one
streamer stops.

    python scripts/supervise_roster.py \
        --queries "gta 6,just chatting,fortnite,valorant,music" \
        --min-viewers 3000 --concurrency 2 \
        --platform shorts --live --now --threshold 2.0 --content-threshold 1.3 \
        --min-score 20

Compliance is explicit, not implied:

* ``--min-viewers`` keeps the roster to streamers with a real audience.
* ``--mature-terms`` + ``--mature-policy`` keep adult rooms out of the channel
  by default (``skip``); YouTube decides true age-gating, not the upload API.
* ``--rights-file`` + ``--require-rights`` (off by default) restrict *posting*
  to streamers you have cleared, so an unlicensed room is never uploaded to your
  channel by accident. Clipping someone else's stream is not automatically
  monetizable; that is a rights question only you can answer.

Anything after ``--`` is passed straight through to ``autopilot``.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_TIMEOUT = 90

DEFAULT_QUERIES = ["gta 6", "just chatting", "fortnite", "valorant", "minecraft", "music"]

# Rooms whose channel or title contains one of these are treated as adult and,
# by default, skipped entirely: an explicit stream is the fastest way to lose a
# channel, and true age-gating is a platform decision, not an upload flag.
DEFAULT_MATURE_TERMS = [
    "adult",
    "18+",
    "nsfw",
    "onlyfans",
    "porn",
    "xxx",
    "hentai",
    "stripper",
    "camgirl",
]

# Printed once at startup. Clipping third-party streams for money is a rights
# question; say so plainly rather than letting the operator assume it is safe.
COMPLIANCE_NOTICE = (
    "notice: this clips and posts other people's live streams. Only streamers you "
    "own or are cleared to reuse are safe to monetize -- adding them to the rights "
    "file (and --require-rights) is the difference between a clip channel and a "
    "strike. Earnings are not guaranteed; the money layer is directional only."
)


def log(message: str) -> None:
    print(f"[roster] {time.strftime('%H:%M:%S')} {message}", flush=True)


def _slug(channel: str) -> str:
    cleaned = "".join(ch for ch in (channel or "").lower() if ch.isalnum() or ch in " ")
    return "".join(cleaned.split())[:24] or "clip"


@dataclass(frozen=True)
class Decision:
    """One live room the supervisor chose, and whether it may be posted."""

    url: str
    channel: str
    title: str
    viewers: int
    mature: bool
    may_post: bool
    priority: bool = False


def select_rooms(
    streams,
    *,
    roster=(),
    priority=(),
    min_viewers: int = 3000,
    mature_terms=DEFAULT_MATURE_TERMS,
    mature_policy: str = "skip",
    rights=(),
    require_rights: bool = False,
    limit: int = 2,
) -> list[Decision]:
    """Rank live rooms and keep the ones worth watching.

    Pure so it can be tested without a network: give it stream-like objects with
    ``channel``, ``title``, ``url`` and ``viewers`` and it returns the decisions.

    ``priority`` names channels to favour: a room whose channel matches a
    priority needle sorts ahead of every non-priority room even when it has
    fewer viewers, so "as many clips of this streamer as possible" holds while
    they are live and the roster still fills with the biggest other rooms when
    they are not.
    """
    roster_needles = [r.lower().strip() for r in roster if r and r.strip()]
    priority_needles = [p.lower().strip() for p in priority if p and p.strip()]
    terms = [t.lower().strip() for t in mature_terms if t and t.strip()]
    rights_needles = [r.lower().strip() for r in rights if r and r.strip()]

    chosen: list[Decision] = []
    for stream in streams:
        channel = str(getattr(stream, "channel", "") or "")
        title = str(getattr(stream, "title", "") or "")
        url = str(getattr(stream, "url", "") or "")
        viewers = int(getattr(stream, "viewers", 0) or 0)
        if not url:
            continue
        if min_viewers > 0 and viewers < min_viewers:
            continue
        lowered_channel = channel.lower()
        if roster_needles and not any(n in lowered_channel for n in roster_needles):
            continue
        mature = any(t in f"{lowered_channel} {title.lower()}" for t in terms)
        if mature and mature_policy == "skip":
            continue
        may_post = (not require_rights) or any(r in lowered_channel for r in rights_needles)
        if require_rights and not may_post:
            # Watch it only if it can be used; a room we may never post is CPU
            # spent for nothing.
            continue
        is_priority = any(p in lowered_channel for p in priority_needles)
        chosen.append(
            Decision(
                url=url,
                channel=channel,
                title=title,
                viewers=viewers,
                mature=mature,
                may_post=may_post,
                priority=is_priority,
            )
        )

    chosen.sort(key=lambda d: (d.priority, d.viewers, d.channel), reverse=True)
    return chosen[: max(1, limit)]


def build_autopilot_command(args: argparse.Namespace, decision: Decision) -> list[str]:
    """The autopilot command for one chosen room, posting only if allowed."""
    hashtags = args.hashtags
    slug = _slug(decision.channel)
    if slug not in hashtags.lower():
        hashtags = (f"#{slug} {hashtags}").strip()
    command = [
        sys.executable,
        "-m",
        "clipbait.cli",
        "autopilot",
        "--source",
        decision.url,
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
        decision.channel or "Live",
    ]
    if hashtags:
        command += ["--hashtags", hashtags]
    if args.campaign:
        command += ["--campaign", args.campaign]
    if args.live and decision.may_post and not args.dry:
        command.append("--live")
        if args.now:
            command.append("--now")
    command += list(args.passthrough or [])
    return command


def discover_streams(queries, per_query: int, timeout: int):
    """Search every query, de-duplicated by URL. A flaky query is skipped."""
    from clipbait import discover

    seen: dict[str, object] = {}
    for query in queries:
        try:
            for stream in discover.search_live(query, per_query, resolve=True, timeout=timeout):
                seen.setdefault(stream.url, stream)
        except Exception as exc:  # noqa: BLE001 - one bad query must not stop the loop
            log(f"discovery failed for {query!r}: {type(exc).__name__}: {exc}")
    return list(seen.values())


def load_terms(value: str, path: str) -> list[str]:
    """Comma-separated terms plus any lines in a file (blank/# skipped)."""
    terms = [t.strip() for t in (value or "").split(",") if t.strip()]
    if path:
        try:
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    terms.append(line)
        except FileNotFoundError:
            log(f"rights/terms file not found: {path} (ignored)")
    return terms


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
    parser.add_argument("--roster", default="", help="comma-separated streamer names to pin to")
    parser.add_argument(
        "--priority",
        default="",
        help="comma-separated streamer names to favour over bigger rooms while live",
    )
    parser.add_argument("--queries", default="", help="comma-separated live search topics")
    parser.add_argument("--concurrency", type=int, default=2, help="how many rooms to clip at once")
    parser.add_argument("--min-viewers", type=int, default=3000, help="only clip rooms at least this big")
    parser.add_argument("--per-query", type=int, default=12, help="rooms to rank per query")
    parser.add_argument("--check", type=int, default=120, help="seconds between re-ranks")
    parser.add_argument("--resolve-limit", type=int, default=12, help="(kept for symmetry)")
    parser.add_argument("--mature-terms", default="", help="extra comma-separated adult/deny terms")
    parser.add_argument(
        "--mature-policy",
        choices=("skip", "post"),
        default="skip",
        help="what to do with a room matching --mature-terms",
    )
    parser.add_argument("--rights-file", default="", help="file of cleared channel names")
    parser.add_argument(
        "--require-rights",
        action="store_true",
        help="only post rooms listed in the rights file",
    )
    parser.add_argument("--dry", action="store_true", help="package clips, never post")

    # Autopilot flags, mirrored so a run is configured in one place.
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--rounds", type=int, default=0)
    parser.add_argument("--max-clips", type=int, default=6)
    parser.add_argument("--threshold", type=float, default=2.0)
    parser.add_argument("--content-threshold", type=float, default=1.3)
    parser.add_argument("--min-score", type=float, default=20.0)
    parser.add_argument("--platform", default="shorts")
    parser.add_argument("--hashtags", default="#shorts #clips")
    parser.add_argument("--campaign", default="")
    parser.add_argument("--live", action="store_true", help="post for real")
    parser.add_argument("--now", action="store_true", help="post immediately")
    parser.add_argument("passthrough", nargs=argparse.REMAINDER, help="extra autopilot args after --")

    args = parser.parse_args(argv)
    args.passthrough = [a for a in args.passthrough if a != "--"]

    roster = [r.strip() for r in args.roster.split(",") if r.strip()]
    priority = [p.strip() for p in args.priority.split(",") if p.strip()]
    queries = [q.strip() for q in args.queries.split(",") if q.strip()] or list(DEFAULT_QUERIES)
    # Searching each named streamer is how a roster of specific people is found;
    # priority names are searched too, so a favoured streamer is discovered even
    # when no topic query happens to match their room.
    search_terms = list(dict.fromkeys(queries + roster + priority))
    mature_terms = list(DEFAULT_MATURE_TERMS) + load_terms(args.mature_terms, "")
    rights = load_terms("", args.rights_file)

    log(COMPLIANCE_NOTICE)
    log(
        f"roster: {args.concurrency} room(s), viewers>={args.min_viewers}, "
        f"priority={priority or 'none'}, mature={args.mature_policy}, "
        f"require_rights={args.require_rights}, "
        f"posting={'off (dry)' if args.dry else ('on' if args.live else 'packaged only')}"
    )

    running = True

    def handle_stop(signum, _frame):  # noqa: ANN001
        nonlocal running
        log(f"signal {signum}: shutting down")
        running = False

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    children: dict[str, subprocess.Popen] = {}

    try:
        while running:
            streams = discover_streams(search_terms, args.per_query, RESOLVE_TIMEOUT)
            decisions = select_rooms(
                streams,
                roster=roster,
                priority=priority,
                min_viewers=args.min_viewers,
                mature_terms=mature_terms,
                mature_policy=args.mature_policy,
                rights=rights,
                require_rights=args.require_rights,
                limit=args.concurrency,
            )
            desired = {d.url: d for d in decisions}

            # Drop children whose room is gone or no longer in the top set.
            for url in [u for u in children if u not in desired]:
                stop_child(children.pop(url))

            for decision in decisions:
                if len(children) >= args.concurrency:
                    break
                existing = children.get(decision.url)
                if existing is not None and existing.poll() is None:
                    continue
                children.pop(decision.url, None)
                note = " (mature)" if decision.mature else ""
                gate = "" if decision.may_post else " [package only: no rights on file]"
                log(
                    f"watching {decision.channel} - {decision.title[:50]} "
                    f"({decision.viewers:,} viewers){note}{gate}  {decision.url}"
                )
                children[decision.url] = subprocess.Popen(  # noqa: S603 - built here
                    build_autopilot_command(args, decision), cwd=str(REPO_ROOT)
                )

            if not decisions:
                log("no live room on the roster right now")

            slept = 0
            while running and slept < max(5, args.check):
                time.sleep(min(5, max(5, args.check) - slept))
                slept += 5
    finally:
        for child in children.values():
            stop_child(child)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
