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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipbait import db  # noqa: E402
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


def publish(limit: int, *, live: bool, min_score: float, project_match: str | None) -> int:
    from clipbait.publish import PublishOptions, publish_provider, publish_queue

    results = publish_queue(
        options=PublishOptions(
            live=live,
            platforms=("shorts",),
            use_best_times=True,
        ),
        provider=publish_provider(),
        limit=limit,
        min_score=min_score,
        project_match=project_match,
    )
    sent = 0
    for result in results:
        if result.ok:
            sent += 1
        log.info(
            "publish %s %s %s %s",
            result.clip_id,
            "OK" if result.ok else "FAIL",
            result.scheduled_date or "now",
            (result.error or result.title or "")[:80],
        )
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
