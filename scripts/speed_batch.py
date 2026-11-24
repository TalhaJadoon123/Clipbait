#!/usr/bin/env python
"""Unattended Speed-VOD batch: transcribe, cut, render, publish.

Policy decisions this script encodes (each one is a deliberate, documented
choice rather than a default that drifted):

* **Captions over ASR.**  For a YouTube source the platform's own caption track
  gives word-level timings in seconds, so a 24-hour VOD becomes 200+ candidate
  moments without an hour of CPU-only speech recognition. Only sources without
  a caption track fall back to faster-whisper.
* **Link mode + window cleanup.**  Clips are cut straight from the remote
  source; each fetched window is deleted after it has been encoded, so disk use
  stays flat across a long run.
* **One VOD at a time.**  The box has four cores and no GPU. Serial work is the
  only way the renders stay near real time.
* **Bounded publishing.**  Uploads are capped per run and scheduled into the
  platform's best windows, so a 200-clip backlog is spread instead of dumped.
* **Flat disk while publishing.**  A live upload copies the file into the
  publisher; with ``--prune-uploaded`` the local render is deleted the moment
  its upload lands, so a 200-clip publish never needs a second copy on disk.

Usage::

    python scripts/speed_batch.py --url <vod> [--url <vod> ...] \
        --clips 120 --publish-limit 8 --live

Without ``--live`` the publish step is a dry run that prints the plan.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipbait import db, storage  # noqa: E402
from clipbait.config import get_settings  # noqa: E402
from clipbait.ingest import source_kind_for  # noqa: E402
from clipbait.pipeline import process_project  # noqa: E402
from clipbait.schema import ProjectOptions  # noqa: E402

log = logging.getLogger("speed_batch")

SPEED_OPTIONS = {
    "target_seconds": 40,
    "caption_preset": "speed",
    "caption_position": "middle",
    "reframe_mode": "facecam",
    "facecam_box": [0.0, 0.43, 0.205, 0.655],
    "facecam_ratio": 0.52,
    "title_style": "speed",
    "platform": "shorts",
    "effect": "punch",
    "hook_titles": False,
    "memes": False,
    "meme_inserts": False,
    "llm": False,
    "captions": True,
    "hashtags": ["ishowspeed", "speed", "gta6"],
}


def project_for(url: str, clips: int, title: str | None = None) -> dict:
    """Reuse a finished project for this URL, otherwise create one."""
    for row in db.list_rows("projects", limit=500):
        if row.get("source_url") == url:
            return row
    options = {**SPEED_OPTIONS, "clips": clips}
    # Validate before writing: a bad option should fail here, not mid-render.
    ProjectOptions(**options)
    return db.create_project(
        {
            "title": title or f"Speed VOD · {url.rsplit('=', 1)[-1]}",
            "source_kind": source_kind_for(url),
            "source_url": url,
            "source_path": None,
            "options": options,
        }
    )


def run_vod(url: str, clips: int, *, title: str | None = None) -> dict:
    project = project_for(url, clips, title)
    db.update("projects", project["id"], {"options": {**SPEED_OPTIONS, "clips": clips}})
    started = time.time()
    log.info("project %s (%s): processing %s clips", project["id"], url, clips)
    result = process_project(project["id"])
    log.info(
        "project %s done in %.0fs: %s clips, transcriber=%s",
        project["id"],
        time.time() - started,
        result.get("clips"),
        result.get("transcriber"),
    )
    return {**result, "project_id": project["id"]}


# Stop before the disk fills: below this much free space a render would leave a
# truncated file, so the run halts cleanly and can be resumed.
_MIN_FREE_BYTES = 2 * 1024**3


def _free_bytes(path: Path) -> int:
    import shutil as _shutil

    try:
        return _shutil.disk_usage(path).free
    except OSError:
        return _MIN_FREE_BYTES


def resume_vod(url: str, clips: int, *, title: str | None = None) -> dict:
    """Render only the clips that are not finished yet, keeping good renders.

    ``process_project`` rebuilds the whole clip list, which discards renders
    that already succeeded. A crash (or a full disk) mid-batch leaves a mix of
    ``ready``/``failed``/``draft`` rows, so this path renders just the unfinished
    ones and leaves the completed work untouched.
    """
    from clipbait.pipeline import render_clip_record

    project = project_for(url, clips, title)
    db.update("projects", project["id"], {"options": {**SPEED_OPTIONS, "clips": clips}})
    rows = db.list_clips(project["id"])
    pending = [clip for clip in rows if clip["status"] != "ready"]
    log.info(
        "resume %s: %d ready, %d to render",
        project["id"],
        len(rows) - len(pending),
        len(pending),
    )
    rendered = 0
    for index, clip in enumerate(pending):
        if _free_bytes(get_settings().data_dir) < _MIN_FREE_BYTES:
            log.error("stopping: free space below %dG", _MIN_FREE_BYTES // 1024**3)
            break
        log.info("render %d/%d %s", index + 1, len(pending), clip["id"])
        try:
            render_clip_record(clip["id"])
            rendered += 1
        except Exception as exc:  # noqa: BLE001 - one bad clip must not stop the batch
            log.exception("clip %s failed", clip["id"])
            db.update(
                "clips",
                clip["id"],
                {"status": "failed", "meta": {**clip["meta"], "error": str(exc)}},
            )
    ready = [clip for clip in db.list_clips(project["id"]) if clip["status"] == "ready"]
    db.update("projects", project["id"], {"status": "completed"})
    log.info("resume %s done: %d rendered, %d ready total", project["id"], rendered, len(ready))
    return {"project_id": project["id"], "clips": rendered, "ready": len(ready), "transcriber": "cached"}


def _prune_render(clip_id: str) -> int:
    """Delete a clip's local render once it is safely on the publisher.

    Publishing copies the file into Postiz, so the local render is redundant the
    moment the upload succeeds. Removing it keeps disk use flat across a
    200-clip publish instead of needing a second ~6GB copy of every clip on a
    box that was already at 98% full. Returns the bytes reclaimed.
    """
    clip = db.get_clip(clip_id) or {}
    freed = 0
    for key in ("render_path", "thumb_path"):
        stored = clip.get(key)
        if not stored:
            continue
        try:
            path = storage.resolve(stored)
            freed += path.stat().st_size
        except OSError:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return freed


def _spread_at(region: str, index: int, per_day: int) -> str:
    """An explicit local posting time, ``per_day`` clips a day, starting tomorrow.

    YouTube caps a channel at roughly 15 uploads a day (its API answers "You have
    reached your daily upload limit" past that), so a 200-clip backlog cannot all
    land on one day. Each clip gets its own future day and a slot inside a
    17:00-23:00 local window, which both respects the quota and spreads the posts
    for reach instead of dumping them. Times are in the audience's own zone.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from clipbait.publish.windows import region_timezone

    per_day = max(1, per_day)
    tz = ZoneInfo(region_timezone(region))
    today = datetime.now(tz).date()
    day = today + timedelta(days=1 + index // per_day)
    slot = index % per_day
    gap = max(1, (6 * 60) // per_day)  # a 6-hour evening window
    minute_of_day = min(17 * 60 + slot * gap, 23 * 60 + 30)
    moment = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(
        minutes=minute_of_day
    )
    return moment.isoformat()


def publish(
    limit: int,
    *,
    live: bool,
    min_score: float,
    project_match: str | None,
    prune: bool = False,
    per_day: int = 0,
    chunk: int = 25,
) -> int:
    """Publish up to ``limit`` clips, best-expected first.

    A live run goes out in small chunks rather than one call so each render can
    be pruned as soon as its upload lands (``prune``), keeping the disk flat.
    ``per_day`` spreads the posts across future days (empty means let the
    best-time/region playbook pick, which packs them onto the current day).

    A dry run just prints the plan -- one pass -- since nothing is recorded and
    the queue would otherwise hand back the same clips again.
    """
    from clipbait.money import build_queue, load_campaign
    from clipbait.publish import (
        PublishOptions,
        publish_clip,
        publish_provider,
        publish_queue,
    )

    options = PublishOptions(live=live, platforms=("shorts",), use_best_times=True)
    provider = publish_provider()

    def _log(result) -> bool:
        log.info(
            "publish %s %s %s %s",
            result.clip_id,
            "OK" if result.ok else "FAIL",
            result.scheduled_date or "now",
            (result.error or result.title or "")[:80],
        )
        return result.ok

    if not live:
        results = publish_queue(
            options=options,
            provider=provider,
            limit=limit,
            min_score=min_score,
            project_match=project_match,
        )
        return sum(1 for result in results if _log(result))

    rules = load_campaign(options.campaign)
    sent = 0
    handled = 0
    while handled < limit:
        queue = build_queue(
            limit=min(chunk, limit - handled),
            min_score=min_score,
            project_match=project_match,
            rate_per_1k=rules.rate_per_1k,
        )
        items = queue.items
        if not items:
            break
        progressed = False
        for item in items:
            # Only a clip that actually goes out consumes a day's slot, so a
            # failed upload does not leave a hole in the schedule.
            clip_options = options
            if per_day:
                clip_options = replace(options, at=_spread_at(item.region, sent, per_day))
            result = publish_clip(item.clip_id, options=clip_options, provider=provider)
            if _log(result):
                # A queue rebuild after a prune would otherwise loop on the
                # same clips, so a real send -- not merely a plan -- is what
                # counts as progress and advances the schedule.
                if not result.metadata.get("skipped"):
                    sent += 1
                    progressed = True
            if prune and result.ok and not result.metadata.get("skipped"):
                freed = _prune_render(item.clip_id)
                if freed:
                    log.info("pruned %s render (%.1fMB reclaimed)", item.clip_id, freed / 1e6)
        handled += len(items)
        if not progressed:
            log.info("publish stalled after %d clip(s); stopping", sent)
            break
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", action="append", required=True, help="VOD URL (repeatable)")
    parser.add_argument("--clips", type=int, default=60, help="clip budget per VOD")
    parser.add_argument("--title", help="project title override (single URL only)")
    parser.add_argument("--publish-limit", type=int, default=0, help="clips to publish (0 = skip)")
    parser.add_argument("--min-score", type=float, default=0.0)
    parser.add_argument("--live", action="store_true", help="actually upload")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="render only unfinished clips of an existing project (keeps good renders)",
    )
    parser.add_argument(
        "--prune-uploaded",
        action="store_true",
        help="delete each render once its live upload succeeds (keeps disk flat)",
    )
    parser.add_argument(
        "--per-day",
        type=int,
        default=0,
        help="spread live posts across future days, N per day (0 = pack onto today)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    settings = get_settings()
    settings.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(settings.data_dir / "speed_batch.log"),
        ],
    )
    db.init_db()

    summary = []
    for url in args.url:
        try:
            if args.resume:
                summary.append(resume_vod(url, args.clips, title=args.title))
            else:
                summary.append(run_vod(url, args.clips, title=args.title))
        except Exception as exc:  # noqa: BLE001 - one bad VOD must not stop the batch
            log.exception("VOD failed: %s", url)
            summary.append({"source_url": url, "error": f"{type(exc).__name__}: {exc}"})

    if args.publish_limit:
        for url in args.url:
            project = project_for(url, args.clips)
            try:
                sent = publish(
                    args.publish_limit,
                    live=args.live,
                    min_score=args.min_score,
                    # Match on the URL: it is unique, whereas the title is not.
                    project_match=project.get("source_url") or project.get("title"),
                    prune=args.prune_uploaded,
                    per_day=args.per_day,
                )
                log.info("published %s clip(s) for %s", sent, project["id"])
            except Exception:  # noqa: BLE001 - publishing must not lose renders
                log.exception("publish failed for %s", project["id"])

    for row in summary:
        print(f"  {row.get('project_id', '?')}  clips={row.get('clips', 0)}  {row.get('error', '')}")
    if args.json:
        import json

        print(json.dumps(summary, indent=2, default=str))
    return 0 if all("error" not in row for row in summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
