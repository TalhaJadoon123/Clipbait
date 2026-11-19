"""Live monitor orchestration.

One thread per monitor reads chat and drives a rolling recorder. When the
detector fires, a second thread waits out the post-roll, cuts the moment from
the ring buffer, and queues the normal clip pipeline with chat overlay data.

The chat loop never blocks on rendering: a viral moment should not stop the
next one from being captured.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from .. import db, jobs, storage
from ..events import bus
from ..pipeline import process_live_moment
from ..schema import ChatMessage, MonitorOptions, utcnow
from .chat import ChatSource, resolve_chat_source
from .recorder import Recorder, build_recorder
from .spikes import SpikeConfig, SpikeDetector

log = logging.getLogger("clipbait.live")

_RUNNERS: dict[str, LiveRunner] = {}
_LOCK = threading.Lock()


def _publish(monitor_id: str, event: dict) -> None:
    bus.publish(f"monitor:{monitor_id}", event)


class LiveRunner:
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
        self.started_at = 0.0
        self.clips = 0
        self.total_messages = 0
        self._chat_offset = 0.0
        self._pending: list[ChatMessage] = []
        self._last_flush = 0.0

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
            # One monitor clock: reset it when the recorder actually starts, then
            # rebase chat timestamps onto it. Otherwise a slow stream resolve (or
            # demo asset generation) shifts chat by 30s and clips the wrong part.
            self.started_at = time.monotonic()
            self.recorder = build_recorder(self.url, storage.live_dir(self.monitor_id))
            self.recorder.start()
            self.chat = resolve_chat_source(self.url, self.options.chat_provider, api_key)
            self._chat_offset = max(0.0, self.chat.started - self.started_at)
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
                now = self.elapsed()
                if candidate:
                    self._on_spike(candidate, now)
                if now - self._last_flush > 1.5:
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
        }
        if self.recorder:
            data["recorder"] = self.recorder.stats()
        return data

    # --------------------------------------------------------------------- spike

    def _on_spike(self, candidate, now: float) -> None:
        top = candidate.signal.top_messages[0] if candidate.signal.top_messages else "chat went wild"
        title = f'Chat: "{top}"'[:90]
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
        _publish(
            self.monitor_id,
            {
                "type": "spike",
                "moment": moment,
                "title": title,
                "trigger_at": candidate.trigger_at,
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

    def _capture(self, candidate, moment: dict, title: str) -> None:
        # Let the post-roll finish before cutting the buffer.
        while not self._stop.is_set() and self.elapsed() < candidate.end + 1.5:
            time.sleep(0.5)
        if not self.recorder:
            return
        try:
            moment_id = moment["id"]
            out = storage.live_dir(self.monitor_id) / f"{moment_id}.mp4"
            self.recorder.slice(candidate.start, candidate.end, out)
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
                {"source_path": storage.relative(target), "duration": candidate.end - candidate.start},
            )

            window_start = candidate.start
            chat_messages = [
                {
                    "t": max(0.0, message.t - window_start),
                    "author": message.author,
                    "text": message.text,
                    "kind": message.kind,
                }
                for message in candidate.messages
                if candidate.start <= message.t <= candidate.end
            ]
            moment_payload = {
                **moment,
                "title": title,
                "hook": title,
                "messages": chat_messages,
                "reason": (
                    f"{candidate.signal.rate}/s chat velocity "
                    f"({candidate.signal.rate_z}σ over baseline)"
                ),
                "score": candidate.score,
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
        except Exception as exc:  # noqa: BLE001
            log.exception("live capture failed")
            _publish(
                self.monitor_id,
                {"type": "capture_failed", "moment_id": moment["id"], "error": str(exc)[:300]},
            )


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