"""Live monitor orchestration.

One thread per monitor reads chat and drives a rolling recorder. When the
detector fires, a second thread waits out the post-roll, cuts the moment from
the ring buffer, and queues the normal clip pipeline with chat overlay data.

The chat loop never blocks on rendering: a viral moment should not stop the
next one from being captured.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time

from .. import db, jobs, storage
from ..events import bus
from ..pipeline import process_live_moment
from ..schema import ChatMessage, MonitorOptions, SpikeSignal, utcnow
from .chat import ChatSource, resolve_chat_source
from .content import ContentWatcher
from .recorder import Recorder, RecorderError, build_recorder
from .spikes import SpikeConfig, SpikeDetector

log = logging.getLogger("clipbait.live")

_RUNNERS: dict[str, LiveRunner] = {}
_LOCK = threading.Lock()


def _publish(monitor_id: str, event: dict) -> None:
    bus.publish(f"monitor:{monitor_id}", event)


def _fused_score(stored: dict, candidate) -> float:
    """Two detectors agreeing is stronger evidence than either alone.

    The bonus scales with the weaker signal, so a 9.0 paired with a 0.5 does
    not jump to 10.8 - a single strong signal is already at the top.
    """
    first = float(stored.get("score") or 0.0)
    second = float(getattr(candidate, "score", 0.0) or 0.0)
    base = max(first, second)
    return min(9.9, base + 0.2 * min(first, second))


class LiveRunner:
    # A ring buffer only exists after the stream resolves, so early moments can
    # name footage that was never recorded. Wait this long for the tail, then
    # skip rather than fail when the window is too short to be a clip.
    CAPTURE_WAIT_SECONDS = 90.0
    CAPTURE_MIN_SECONDS = 4.0
    CAPTURE_POLL_SECONDS = 0.5

    def __init__(self, monitor: dict) -> None:
        self.monitor_id = monitor["id"]
        self.url = monitor["url"]
        self.title = monitor.get("title") or "Live stream"
        self.options = MonitorOptions(**(monitor.get("options") or {}))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._clip_threads: list[threading.Thread] = []
        self.detector = SpikeDetector(
            SpikeConfig(
                threshold=self.options.spike_threshold,
                cooldown_seconds=self.options.cooldown,
                pre_roll=self.options.pre_roll,
                post_roll=self.options.post_roll,
            )
        )
        self.recorder: Recorder | None = None
        self.chat: ChatSource | None = None
        self.content: ContentWatcher | None = None
        self.started_at = 0.0
        self.clips = 0
        self.total_messages = 0
        self._chat_offset = 0.0
        self._pending: list[ChatMessage] = []
        self._last_flush = 0.0
        # Fusion: chat and content often fire within seconds of each other for
        # the same beat. Keep one moment, one clip, and merge the evidence.
        self._last_trigger_at = -1e9
        self._last_moment_id: str | None = None
        self._last_origin = "chat"
        self.content_triggers = 0

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"live-{self.monitor_id}"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.chat:
            try:
                self.chat.close()
                terminate = getattr(self.chat, "terminate", None)
                if callable(terminate):
                    terminate()
            except Exception:  # noqa: BLE001
                pass
        if self.recorder:
            try:
                self.recorder.stop()
            except Exception:  # noqa: BLE001
                pass
        db.update_monitor(self.monitor_id, status="stopped", stopped_at=utcnow())
        _publish(self.monitor_id, {"type": "status", "status": "stopped"})

    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    # ---------------------------------------------------------------------- run

    def _run(self) -> None:
        api_key = os.getenv("YOUTUBE_API_KEY")
        try:
            db.update_monitor(self.monitor_id, status="starting")
            self.recorder = build_recorder(
                self.url,
                storage.live_dir(self.monitor_id),
                buffer_seconds=self.options.buffer_seconds,
            )
            self.recorder.start()
            # Adopt the recorder's own clock: it is stamped when ffmpeg starts
            # writing, not when URL resolution began. Chat is rebased onto it so
            # chat time and video time agree even after a 30s resolve.
            self.started_at = self.recorder.started_at or time.monotonic()
            self.chat = resolve_chat_source(self.url, self.options.chat_provider, api_key)
            self._chat_offset = max(0.0, self.chat.started - self.started_at)
            if self.options.content_detection:
                self.content = ContentWatcher(
                    self.recorder,
                    directory=storage.live_dir(self.monitor_id),
                    window_seconds=self.options.speech_window,
                    interval_seconds=self.options.content_interval,
                    pre_roll=self.options.pre_roll,
                    post_roll=self.options.post_roll,
                    cooldown=self.options.cooldown,
                    threshold=self.options.content_threshold,
                    language=self.options.language,
                )
                threading.Thread(
                    target=self._content_loop,
                    daemon=True,
                    name=f"live-content-{self.monitor_id}",
                ).start()
        except Exception as exc:  # noqa: BLE001
            log.exception("monitor %s failed to start", self.monitor_id)
            db.update_monitor(self.monitor_id, status="failed", error=str(exc)[:400])
            _publish(self.monitor_id, {"type": "error", "error": str(exc)[:400]})
            return

        db.update_monitor(
            self.monitor_id,
            status="live",
            started_at=utcnow(),
            error=None,
            stats={"provider": self.chat.name, "recorder": self.recorder.kind},
        )
        _publish(
            self.monitor_id,
            {"type": "status", "status": "live", "provider": self.chat.name},
        )

        failed = False
        try:
            for message in self.chat.messages():
                if self._stop.is_set():
                    break
                if self._chat_offset:
                    message = message.model_copy(
                        update={"t": round(message.t + self._chat_offset, 3)}
                    )
                self.total_messages += 1
                self._pending.append(message)
                candidate = self.detector.add(message)
                if candidate:
                    self._on_candidate(candidate, "chat", self.elapsed())
                if self.elapsed() - self._last_flush > 1.5:
                    self._flush()
        except Exception as exc:  # noqa: BLE001
            log.exception("monitor %s crashed", self.monitor_id)
            failed = True
            db.update_monitor(
                self.monitor_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:400]
            )
            _publish(self.monitor_id, {"type": "error", "error": str(exc)[:400]})
        finally:
            self._flush(force=True)
            if self.recorder:
                self.recorder.stop()
            # A crash already recorded its own terminal state; do not mask it
            # with a healthy-looking "stopped".
            if not self._stop.is_set() and not failed:
                db.update_monitor(self.monitor_id, status="stopped", stopped_at=utcnow())
            _publish(self.monitor_id, {"type": "status", "status": "stopped"})

    def _flush(self, force: bool = False) -> None:
        if not self._pending or (not force and self._last_flush == 0):
            self._last_flush = self.elapsed()
            return
        batch = self._pending
        self._pending = []
        try:
            db.add_chat_events(
                self.monitor_id,
                [
                    {"t": message.t, "author": message.author, "text": message.text, "kind": message.kind}
                    for message in batch
                ],
            )
            db.prune_chat_events(self.monitor_id, keep_seconds=max(1800, self.options.buffer_seconds * 4))
        except Exception:  # noqa: BLE001
            log.warning("chat persistence failed", exc_info=True)
        self._last_flush = self.elapsed()
        stats = self.stats()
        db.update_monitor(self.monitor_id, stats=stats)
        _publish(self.monitor_id, {"type": "stats", **stats})

    def stats(self) -> dict:
        data = {
            "messages": self.total_messages,
            "rate": self.detector.current_rate(),
            "timeline": self.detector.timeline(36),
            "elapsed": round(self.elapsed(), 1),
            "clips": self.clips,
            "chat_provider": getattr(self.chat, "name", None),
            "content_triggers": self.content_triggers,
        }
        if self.recorder:
            data["recorder"] = self.recorder.stats()
        if self.content:
            data["content"] = self.content.stats()
        return data

    # --------------------------------------------------------------------- spike

    def _content_loop(self) -> None:
        """Watch the stream itself, independent of chat volume."""
        if not self.content:
            return
        while not self._stop.is_set():
            if self._stop.wait(self.options.content_interval):
                break
            try:
                candidate = self.content.step(self.elapsed())
            except Exception:  # noqa: BLE001 - content detection is best-effort
                log.debug("content step failed", exc_info=True)
                continue
            if candidate:
                self.content_triggers += 1
                self._on_candidate(candidate, "content", self.elapsed())

    def _on_candidate(self, candidate, origin: str, now: float) -> None:
        trigger = float(candidate.trigger_at if candidate.trigger_at is not None else now)
        if origin == "content" and not candidate.messages:
            # Chat is still useful as overlay/evidence when the *stream* was
            # the trigger, so attach whatever was said around the moment.
            candidate = dataclasses.replace(
                candidate,
                messages=self.detector.messages_between(candidate.start, candidate.end),
            )

        # Same beat, two detectors: merge into the moment we already have
        # instead of cutting the same thirty seconds twice.
        if self._last_moment_id and abs(trigger - self._last_trigger_at) <= 12.0:
            self._fuse(candidate, origin)
            return
        if now - self._last_trigger_at < self.options.cooldown:
            if self._last_origin == "chat" and origin == "content":
                # Chat fired first and started the cooldown. Chat is the weaker
                # signal — a reflex, not the moment — so dropping the on-stream
                # evidence here loses exactly the clip worth posting. Absorb it
                # into the open moment instead.
                self._fuse(candidate, origin)
            return

        title, reason = self._label(candidate, origin)
        moment = db.create_moment(
            {
                "monitor_id": self.monitor_id,
                "start": candidate.start,
                "end": candidate.end,
                "score": candidate.score,
                "signal": candidate.signal.model_dump(),
                "messages": [m.model_dump() for m in candidate.messages],
            }
        )
        self._last_moment_id = moment["id"]
        self._last_trigger_at = trigger
        self._last_origin = origin
        _publish(
            self.monitor_id,
            {
                "type": "spike" if origin == "chat" else "content",
                "moment": moment,
                "title": title,
                "reason": reason,
                "origin": origin,
                "trigger_at": trigger,
                "score": candidate.score,
            },
        )
        if not self.options.auto_clip or self.clips >= self.options.max_clips:
            return
        thread = threading.Thread(
            target=self._capture,
            args=(candidate, moment, title),
            daemon=True,
            name=f"live-clip-{moment['id']}",
        )
        self._clip_threads.append(thread)
        thread.start()

    def _fuse(self, candidate, origin: str) -> None:
        """Merge a second detector's evidence into the current moment."""
        stored = db.get("moments", self._last_moment_id or "")
        if not stored:
            return
        old = SpikeSignal(**(stored.get("signal") or {}))
        new = candidate.signal
        fused = SpikeSignal(
            rate=old.rate,
            rate_z=old.rate_z,
            unique_z=old.unique_z,
            hype=old.hype,
            repeats=old.repeats,
            sentiment=old.sentiment,
            top_messages=old.top_messages,
            speech=max(old.speech, new.speech),
            loudness=max(old.loudness, new.loudness),
            action=max(old.action, new.action),
            quote=old.quote or new.quote,
            score=round(_fused_score(stored, candidate), 3),
            reasons=list(dict.fromkeys([*old.reasons, *new.reasons]))[:10],
            origin="fused",
        )
        merged_messages: list[dict] = []
        seen: set[tuple[float, str]] = set()
        for message in [*(stored.get("messages") or []), *[m.model_dump() for m in candidate.messages]]:
            key = (round(float(message.get("t", 0)), 2), str(message.get("text", "")))
            if key in seen:
                continue
            seen.add(key)
            merged_messages.append(message)
        merged_messages.sort(key=lambda item: float(item.get("t", 0)))

        updated = db.update(
            "moments",
            stored["id"],
            {
                "score": fused.score,
                "signal": fused.model_dump(),
                "messages": merged_messages[-60:],
                "start": min(float(stored["start"]), candidate.start),
                "end": max(float(stored["end"]), candidate.end),
            },
        )
        self._last_trigger_at = max(self._last_trigger_at, float(candidate.trigger_at))
        if updated:
            _publish(
                self.monitor_id,
                {
                    "type": "spike_fused",
                    "moment": updated,
                    "score": updated["score"],
                    "origin": "fused",
                },
            )

    def _label(self, candidate, origin: str) -> tuple[str, str]:
        signal = candidate.signal
        quote = (signal.quote or "").strip().strip('"')
        if origin == "chat":
            top = signal.top_messages[0] if signal.top_messages else "chat went wild"
            title = f'Chat: "{top}"'[:90]
            reason = (
                f"{signal.rate}/s chat velocity "
                f"({signal.rate_z}σ over baseline)"
            )
            return title, reason

        if quote:
            title = f'On stream: "{quote}"'[:90]
        elif signal.action >= max(signal.loudness, signal.speech):
            title = "The stream lost it on camera"
        elif signal.loudness:
            title = "The stream lost it"
        else:
            title = "Something happened on stream"
        reason = "; ".join(signal.reasons[:3]) or "on-stream content signal"
        return title, reason

    def _capture(self, candidate, moment: dict, title: str) -> None:
        # The moment may have been fused with a second detector since trigger.
        stored = db.get("moments", moment["id"]) or moment
        start = float(stored.get("start", candidate.start))
        end = float(stored.get("end", candidate.end))
        signal = SpikeSignal(**(stored.get("signal") or candidate.signal.model_dump()))
        # Let the post-roll finish before cutting the buffer.
        while not self._stop.is_set() and self.elapsed() < end + 1.5:
            time.sleep(0.5)
        if not self.recorder:
            return

        # The ring buffer only starts once the stream resolves, so an early
        # moment can ask for footage that does not exist. Clamp to what was
        # actually recorded, wait briefly for the tail to arrive, and skip
        # cleanly instead of dying on a slice error.
        requested = (start, end)
        start, end = self._clamp_to_buffer(start, end)
        if end - start < self.CAPTURE_MIN_SECONDS:
            self._capture_skipped(moment["id"], stored, signal, requested, start, end)
            return
        if (start, end) != requested:
            stored = db.update("moments", moment["id"], {"start": start, "end": end}) or stored

        try:
            moment_id = moment["id"]
            out = storage.live_dir(self.monitor_id) / f"{moment_id}.mp4"
            self.recorder.slice(start, end, out)
            if not out.exists() or out.stat().st_size < 10_000:
                raise RuntimeError("recording slice came back empty")

            project = db.create_project(
                {
                    "title": title,
                    "source_kind": "live",
                    "source_url": self.url,
                    "status": "queued",
                    "options": {
                        "clips": 1,
                        "captions": self.options.captions,
                        "hook_titles": self.options.hook_titles,
                        "chat_overlay": self.options.chat_overlay,
                        "caption_preset": self.options.caption_preset,
                        "reframe_mode": self.options.reframe_mode,
                        "language": self.options.language,
                        "hashtags": list(self.options.hashtags),
                        "region": self.options.region,
                        "platform": self.options.platform,
                    },
                }
            )
            target = storage.project_dir(project["id"]) / "source" / "video.mp4"
            target.parent.mkdir(parents=True, exist_ok=True)
            out.replace(target)
            db.update(
                "projects",
                project["id"],
                {"source_path": storage.relative(target), "duration": end - start},
            )

            raw_messages = stored.get("messages") or [m.model_dump() for m in candidate.messages]
            chat_messages = [
                {
                    "t": max(0.0, float(message.get("t", 0)) - start),
                    "author": message.get("author", ""),
                    "text": message.get("text", ""),
                    "kind": message.get("kind", "text"),
                }
                for message in raw_messages
                if start <= float(message.get("t", 0)) <= end
            ]
            moment_payload = {
                **stored,
                "title": title,
                "hook": title,
                "messages": chat_messages,
                "reason": self._reason_text(signal, stored.get("score", candidate.score)),
                "quote": signal.quote,
                "origin": signal.origin,
                "score": stored.get("score", candidate.score),
            }

            def on_done(job_row, result):
                clip_id = (result or {}).get("clip_id")
                if clip_id:
                    db.attach_clip_to_moment(moment["id"], clip_id)
                    self.clips += 1
                    _publish(
                        self.monitor_id,
                        {"type": "clip_ready", "moment_id": moment["id"], "clip_id": clip_id},
                    )

            jobs.submit(
                "live_clip",
                process_live_moment,
                project_id=project["id"],
                monitor_id=self.monitor_id,
                args=(project["id"], moment_payload),
                on_done=on_done,
            )
            _publish(
                self.monitor_id,
                {"type": "clip_queued", "moment_id": moment["id"], "project_id": project["id"]},
            )
        except RecorderError as exc:
            log.warning("live capture skipped for %s: %s", moment["id"], exc)
            self._capture_skipped(moment["id"], stored, signal, requested, start, end, note=str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("live capture failed")
            _publish(
                self.monitor_id,
                {"type": "capture_failed", "moment_id": moment["id"], "error": str(exc)[:300]},
            )

    def _clamp_to_buffer(self, start: float, end: float) -> tuple[float, float]:
        """Trim a requested window to the footage the recorder actually has.

        The first segment only appears tens of seconds into a monitor (a live
        URL takes 20-40s to resolve), and the newest one is still filling. Wait
        up to ``CAPTURE_WAIT_SECONDS`` for the buffer to reach ``end``, then
        return the longest window that fits. When nothing is buffered the
        window collapses so the caller can skip instead of slicing.
        """
        if not self.recorder:
            return start, start
        stats = self.recorder.stats() or {}
        if "available_until" not in stats:
            # Doubles without a ring buffer (local files, tests) always satisfy
            # the request; there is nothing to wait for.
            return start, end
        deadline = time.monotonic() + self.CAPTURE_WAIT_SECONDS
        best = (start, start)
        while True:
            stats = self.recorder.stats() or {}
            available_from = float(stats.get("available_from") or 0.0)
            available_until = float(stats.get("available_until") or 0.0)
            if available_until > 0.0:
                best = (max(start, available_from), min(end, available_until))
                # Only the *tail* is worth waiting for; footage before the first
                # segment is gone for good, so trim it and carry on.
                if best[1] >= end - 0.5:
                    return best
            if self._stop.is_set() or time.monotonic() >= deadline:
                return best
            time.sleep(self.CAPTURE_POLL_SECONDS)

    def _capture_skipped(
        self,
        moment_id: str,
        stored: dict,
        signal: SpikeSignal,
        requested: tuple[float, float],
        start: float,
        end: float,
        *,
        note: str | None = None,
    ) -> None:
        """Record why a moment could not be cut, without a stack trace."""
        covered = max(0.0, end - start)
        error = note or (
            f"only {covered:.1f}s of the requested "
            f"{requested[1] - requested[0]:.1f}s was still in the ring buffer"
        )
        payload = {**(stored.get("signal") or signal.model_dump()), "capture_error": error}
        db.update("moments", moment_id, {"signal": payload})
        _publish(
            self.monitor_id,
            {"type": "capture_skipped", "moment_id": moment_id, "error": error[:300]},
        )

    @staticmethod
    def _reason_text(signal: SpikeSignal, score: float) -> str:
        parts = list(signal.reasons)
        if signal.rate_z:
            parts.append(f"{signal.rate}/s chat velocity ({signal.rate_z}σ over baseline)")
        if not parts:
            parts.append(f"combined signal scored {score:.1f}")
        return "; ".join(parts[:4])


# ---------------------------------------------------------------------- registry


def start(monitor_id: str) -> dict:
    monitor = db.get_monitor(monitor_id)
    if not monitor:
        raise ValueError(f"unknown monitor {monitor_id}")
    with _LOCK:
        existing = _RUNNERS.get(monitor_id)
        if existing and existing.alive():
            return monitor
        runner = LiveRunner(monitor)
        _RUNNERS[monitor_id] = runner
        runner.start()
    return db.get_monitor(monitor_id) or monitor


def stop(monitor_id: str) -> dict:
    with _LOCK:
        runner = _RUNNERS.pop(monitor_id, None)
    if runner:
        runner.stop()
    monitor = db.get_monitor(monitor_id)
    if monitor:
        return db.update_monitor(monitor_id, status="stopped", stopped_at=utcnow()) or monitor
    raise ValueError(f"unknown monitor {monitor_id}")


def status(monitor_id: str) -> dict:
    runner = _RUNNERS.get(monitor_id)
    if runner and runner.alive():
        return runner.stats()
    monitor = db.get_monitor(monitor_id) or {}
    return monitor.get("stats") or {}


def stop_all() -> None:
    for monitor_id in list(_RUNNERS):
        stop(monitor_id)


def active_monitors() -> list[str]:
    return [mid for mid, runner in _RUNNERS.items() if runner.alive()]