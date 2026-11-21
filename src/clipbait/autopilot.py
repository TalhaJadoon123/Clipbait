"""The unattended loop: discover, watch, clip, post, repeat.

``earn.sh`` is one pass. This is the loop that keeps going. Each round:

1. finds the biggest live room for a query (or uses a fixed ``--source``);
2. monitors it until the round's minutes are up *or* enough clips **rendered**
   (a clip row appears the instant a moment is detected, but the render runs on
   a thread, so only ready clips count and a round drains what is in flight
   before it stops);
3. stops the monitor, exports what is ready, and -- only when publishing is on --
   hands those clips to :func:`clipbait.publish.publish_queue` at their windows;
4. picks the next room and goes again.

The decisions (when a round is done, whether to go another round, which room is
next) are pure and tested against a fake clock. The side effects sit behind one
small :class:`Runtime` of callables, so a test can run ten rounds in a
millisecond with no network, no ffmpeg and no real sleep. Publishing is off
unless ``live`` is set, and this module never posts on its own.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger("clipbait.autopilot")

POLL_SECONDS = 3.0
# A moment produces a clip row immediately, but the render runs on a thread.
# Stop on *ready* clips, then let any render still in flight finish (bounded) so
# a round never stops mid-render and packages nothing.
DRAIN_SECONDS = 90.0
PENDING_STATUSES = {"draft", "rendering"}


@dataclass(frozen=True)
class AutopilotOptions:
    """One operator's idea of a good unattended run."""

    query: str = "gta 6"
    source: str | None = None  # a fixed URL; disables discovery
    rounds: int = 0  # 0 = keep going until interrupted
    minutes: int = 30  # per round
    cooldown_seconds: int = 60  # between rounds
    max_clips: int = 4  # render cap per round; 0 = no cap
    threshold: float = 3.0
    content_threshold: float = 2.5
    preset: str = "default"
    region: str = "US"
    platform: str = "tiktok"
    language: str = "auto"
    hashtags: tuple[str, ...] = ()
    buffer_seconds: int = 420
    min_score: float = 35.0
    campaign: str | None = None
    live: bool = False  # publish for real (else package only)
    post_now: bool = False  # send immediately; else schedule at the best window
    title: str = ""
    # The channel's look: a stylised picture effect, a music bed, and a sound
    # effect on each meme cutaway. ``none``/``None`` keep a clip plain.
    effect: str = "none"
    music: str | None = None
    music_volume: float = 0.16
    meme_sfx: bool = True


@dataclass(frozen=True)
class Round:
    """What one round of the loop did."""

    index: int
    source: str
    title: str = ""
    monitor_id: str = ""
    clips: int = 0
    exported: int = 0
    published: int = 0
    skipped: str = ""

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "source": self.source,
            "title": self.title,
            "monitor_id": self.monitor_id,
            "clips": self.clips,
            "exported": self.exported,
            "published": self.published,
            "skipped": self.skipped,
        }


# ------------------------------------------------------------------- decisions


def round_finished(*, elapsed: float, minutes: int, clips: int, max_clips: int) -> bool:
    """Stop the round once either bound is reached.

    Whichever comes first: the clock runs out, or enough clips were rendered.
    Landing a burst of clips early should not keep burning bandwidth, and a
    quiet room should not run forever.
    """
    if max_clips > 0 and clips >= max_clips:
        return True
    return elapsed >= max(1, minutes) * 60


def more_rounds(done: int, rounds: int) -> bool:
    """``rounds <= 0`` means run until the operator interrupts."""
    return rounds <= 0 or done < rounds


def _field(item, name: str, default=""):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def pick_stream(streams, used: set[str]):
    """The biggest room not already watched, so rounds do not repeat a channel."""
    for stream in streams:
        url = str(_field(stream, "url", ""))
        if url and url not in used:
            return stream
    return None


def _clip_status(clip) -> str:
    return str(_field(clip, "status", "") or "")


def ready_clips(clips) -> list:
    """Only the clips that finished rendering -- what the cap and export count."""
    return [clip for clip in clips if _clip_status(clip) == "ready"]


def pending_clips(clips) -> list:
    """Clips a render thread is still working on, worth waiting out."""
    return [clip for clip in clips if _clip_status(clip) in PENDING_STATUSES]


# ------------------------------------------------------------------- side effects


@dataclass
class Runtime:
    """Every side effect the loop performs, replaceable in one place.

    Defaults are the real discover/live/money/publish functions; a test passes
    its own and the loop runs against them unchanged.
    """

    discover: Callable = field(default=None)  # type: ignore[assignment]
    start: Callable = field(default=None)  # type: ignore[assignment]
    status: Callable = field(default=None)  # type: ignore[assignment]
    stop: Callable = field(default=None)  # type: ignore[assignment]
    clips: Callable = field(default=None)  # type: ignore[assignment]
    export: Callable = field(default=None)  # type: ignore[assignment]
    publish: Callable = field(default=None)  # type: ignore[assignment]
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def default_runtime() -> Runtime:
    return Runtime(
        discover=_discover,
        start=start_monitor,
        status=_status,
        stop=_stop,
        clips=_clips,
        export=export_monitor,
        publish=publish_monitor,
    )


def _discover(query: str, limit: int, timeout: int):
    from . import discover

    return discover.search_live(query, limit, resolve=True, timeout=timeout)


def _status(monitor_id: str) -> dict:
    from . import live

    return live.status(monitor_id)


def _stop(monitor_id: str) -> None:
    from . import live

    try:
        live.stop(monitor_id)
    except ValueError:
        pass  # already stopped


def _clips(monitor_id: str) -> list[dict]:
    from . import db

    return [
        clip
        for clip in db.list_rows("clips", limit=1000)
        if str((clip.get("meta") or {}).get("monitor_id") or "") == monitor_id
    ]


def start_monitor(source: str, title: str, options: AutopilotOptions) -> str:
    """Create and start a live monitor for one round. Returns its id."""
    from . import db, live
    from .schema import MonitorOptions

    url = "demo://live" if source in {"demo", "demo://live"} else source
    monitor_options = MonitorOptions(
        chat_provider="demo" if url.startswith("demo://") else "auto",
        spike_threshold=options.threshold,
        content_threshold=options.content_threshold,
        caption_preset=options.preset,
        region=options.region,
        platform=options.platform,
        language=options.language,
        hashtags=options.hashtags,
        max_clips=options.max_clips or 8,
        buffer_seconds=options.buffer_seconds,
        auto_publish=options.live,
        post_now=options.post_now,
        publish_min_score=options.min_score,
        campaign=options.campaign,
        effect=options.effect,
        music=options.music,
        music_volume=options.music_volume,
        meme_sfx=options.meme_sfx,
    )
    monitor = db.create_monitor(
        {
            "url": url,
            "title": title or (options.title or "Autopilot"),
            "chat_provider": monitor_options.chat_provider,
            "options": monitor_options.model_dump(),
            "status": "starting",
        }
    )
    live.start(monitor["id"])
    return monitor["id"]


def export_monitor(monitor_id: str, options: AutopilotOptions) -> int:
    """Write an upload bundle for every ready clip this monitor produced."""
    from .money import build_queue, export_bundle, load_campaign

    rules = load_campaign(options.campaign)
    queue = build_queue(
        limit=50,
        min_score=options.min_score,
        rate_per_1k=rules.rate_per_1k,
        monitor_id=monitor_id,
    )
    written = 0
    for item in queue.items:
        try:
            export_bundle(item.clip_id, campaign=options.campaign)
            written += 1
        except FileNotFoundError as exc:
            log.info("skip export %s: %s", item.clip_id, exc)
    return written


def publish_monitor(monitor_id: str, options: AutopilotOptions) -> int:
    """Hand this monitor's ready clips to the publisher. Dry unless live."""
    from .publish import PublishOptions, publish_provider, publish_queue

    results = publish_queue(
        options=PublishOptions(
            live=options.live,
            campaign=options.campaign,
            post_now=options.post_now,
            # Post to the destination this run was set up for, so a loop
            # configured for one connected network never falls back to a
            # clip's recommended platform that the account lacks.
            platforms=(options.platform,),
        ),
        provider=publish_provider(),
        limit=50,
        min_score=options.min_score,
        monitor_id=monitor_id,
    )
    return sum(1 for result in results if result.ok and not result.metadata.get("skipped"))


# ------------------------------------------------------------------------ loop


def run(
    options: AutopilotOptions,
    *,
    runtime: Runtime | None = None,
    on_event: Callable[[str], None] = print,
) -> list[Round]:
    """Run rounds until the round count is reached or the operator stops it."""
    runtime = runtime or default_runtime()
    results: list[Round] = []
    used: set[str] = set()
    index = 0

    while True:
        index += 1
        source, title = _resolve(options, runtime, used)
        if not source:
            on_event("  no live room found; stopping the loop")
            break
        if not options.source:
            used.add(source)

        on_event(f"\n== round {index}: {title or source}")
        try:
            monitor_id = str(runtime.start(source, title, options))
        except Exception as exc:  # noqa: BLE001 - one bad room must not end the loop
            on_event(f"  could not start the monitor: {type(exc).__name__}: {exc}")
            results.append(Round(index=index, source=source, title=title, skipped=str(exc)))
            if not more_rounds(len(results), options.rounds):
                break
            runtime.sleep(max(0, options.cooldown_seconds))
            continue

        started = runtime.now()
        clips: list[dict] = []
        try:
            while not round_finished(
                elapsed=runtime.now() - started,
                minutes=options.minutes,
                clips=len(ready_clips(clips)),
                max_clips=options.max_clips,
            ):
                runtime.sleep(POLL_SECONDS)
                clips = runtime.clips(monitor_id)
                state = str(runtime.status(monitor_id).get("status") or "")
                if state in {"failed", "stopped"}:
                    break

            # A render started just before the round ended should still land.
            # Wait out anything in flight, bounded, so the export is not empty.
            drain_started = runtime.now()
            clips = runtime.clips(monitor_id)
            while pending_clips(clips) and runtime.now() - drain_started < DRAIN_SECONDS:
                runtime.sleep(POLL_SECONDS)
                clips = runtime.clips(monitor_id)
        except KeyboardInterrupt:
            on_event("  interrupted; stopping the monitor")
            runtime.stop(monitor_id)
            results.append(Round(index=index, source=source, title=title, monitor_id=monitor_id))
            break

        runtime.stop(monitor_id)
        clips = runtime.clips(monitor_id)
        rendered = len(ready_clips(clips))
        exported = runtime.export(monitor_id, options)
        published = runtime.publish(monitor_id, options) if options.live else 0
        on_event(
            f"  {rendered} clip(s) rendered, {exported} packaged"
            + (f", {published} sent" if options.live else "")
        )
        results.append(
            Round(
                index=index,
                source=source,
                title=title,
                monitor_id=monitor_id,
                clips=rendered,
                exported=exported,
                published=published,
            )
        )

        if not more_rounds(len(results), options.rounds):
            break
        runtime.sleep(max(0, options.cooldown_seconds))

    return results


def _resolve(options: AutopilotOptions, runtime: Runtime, used: set[str]) -> tuple[str, str]:
    if options.source:
        return options.source, options.title or options.source
    try:
        streams = runtime.discover(options.query, 10, 60)
    except Exception as exc:  # noqa: BLE001 - discovery failure is not fatal
        log.warning("discovery failed: %s", exc)
        return "", ""
    chosen = pick_stream(streams, used)
    if chosen is None:
        return "", ""
    url = str(_field(chosen, "url", ""))
    title = str(_field(chosen, "title", "") or _field(chosen, "channel", "") or url)
    return url, title[:120]
