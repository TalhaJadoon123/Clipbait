"""Thread-based job runner.

Why threads, not Celery: the workload is ffmpeg/Whisper subprocesses and C
extensions that release the GIL, and a single-binary install is a feature.
Set ``CLIPBAIT_RENDER_CONCURRENCY`` to raise throughput on bigger boxes.

Swap this module for a Redis queue later: the only contract is ``submit`` and
the ``job_*`` helpers below.
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import db
from .events import bus

log = logging.getLogger("clipbait.jobs")

_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_inflight: dict[str, threading.Event] = {}
_cancelled: set[str] = set()


class JobCancelled(RuntimeError):
    pass


def executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        from .config import get_settings

        _executor = ThreadPoolExecutor(
            max_workers=max(2, get_settings().render_concurrency),
            thread_name_prefix="clipbait-job",
        )
    return _executor


def is_cancelled(job_id: str) -> bool:
    with _lock:
        return job_id in _cancelled


def cancel(job_id: str) -> None:
    with _lock:
        _cancelled.add(job_id)
        event = _inflight.get(job_id)
    if event:
        event.set()


def progress(job_id: str | None, value: float, step: str, message: str = "") -> None:
    """Report progress 0..1. Safe to call from worker threads."""
    if not job_id:
        return
    if is_cancelled(job_id):
        raise JobCancelled(f"job {job_id} cancelled")
    value = max(0.0, min(1.0, value))
    job = db.update_job(job_id, progress=value, step=step, message=message, status="running")
    if job:
        event = {
            "type": "progress",
            "job": job,
            "progress": value,
            "step": step,
            "message": message,
        }
        bus.publish(f"job:{job_id}", event)
        if job.get("project_id"):
            bus.publish(f"project:{job['project_id']}", {"job_id": job_id, **event})
        if job.get("monitor_id"):
            bus.publish(f"monitor:{job['monitor_id']}", {"job_id": job_id, **event})


def submit(
    kind: str,
    fn: Callable[..., Any],
    *,
    project_id: str | None = None,
    monitor_id: str | None = None,
    args: tuple = (),
    kwargs: dict[str, Any] | None = None,
    on_done: Callable[[dict[str, Any], Any], None] | None = None,
) -> dict[str, Any]:
    job = db.create_job(
        {
            "kind": kind,
            "project_id": project_id,
            "monitor_id": monitor_id,
            "status": "queued",
            "step": "queued",
            "message": "",
        }
    )
    job_id = job["id"]
    event = threading.Event()
    with _lock:
        _inflight[job_id] = event

    def run() -> None:
        if is_cancelled(job_id):
            db.update_job(job_id, status="cancelled", step="cancelled")
            _finish()
            return
        db.update_job(job_id, status="running", step="starting", progress=0.01)
        bus.publish(f"job:{job_id}", {"type": "started", "job": db.get_job(job_id)})
        try:
            call_kwargs = dict(kwargs or {})
            call_kwargs.setdefault("job_id", job_id)
            result = fn(*args, **call_kwargs)
            job_row = db.update_job(
                job_id, status="completed", progress=1.0, step="completed", message=""
            )
            bus.publish(f"job:{job_id}", {"type": "completed", "job": job_row})
            if on_done:
                on_done(job_row or job, result)
        except JobCancelled:
            db.update_job(job_id, status="cancelled", step="cancelled")
            bus.publish(f"job:{job_id}", {"type": "cancelled", "job": db.get_job(job_id)})
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            log.exception("job %s (%s) failed", job_id, kind)
            detail = f"{type(exc).__name__}: {exc}"
            job_row = db.update_job(job_id, status="failed", step="failed", error=detail)
            bus.publish(
                f"job:{job_id}",
                {
                    "type": "failed",
                    "job": job_row,
                    "error": detail,
                    "traceback": traceback.format_exc(limit=4),
                },
            )
        finally:
            _finish()

    def _finish() -> None:
        with _lock:
            _inflight.pop(job_id, None)
            _cancelled.discard(job_id)
        event.set()

    executor().submit(run)
    return job


def shutdown(wait: bool = False) -> None:
    global _executor
    if _executor:
        _executor.shutdown(wait=wait, cancel_futures=True)
        _executor = None