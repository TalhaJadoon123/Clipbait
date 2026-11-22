"""Reclaim disk from finished runs so a 24/7 roster does not fill the disk.

A roster never stops. Raw HLS segments, fetched sources and per-clip windows
pile up until the recorder can no longer write -- which takes the whole
pipeline down, not just one job. This module plans a conservative prune and
applies it.

It is deliberately database-aware, because filesystem age alone lies: a long
transcription can leave a project's ``source/`` file untouched for hours while
that source is still needed to render the clips. So:

* A live monitor directory is pruned only when *nothing inside it* has been
  written for ``settle_seconds``. The recorder appends to exactly one monitor
  at a time, so a quiet directory is a finished one.
* A project's ``source/`` and any link-mode ``window.mp4`` are pruned only when
  the project is terminal (``completed``/``failed``) *and* has been idle for
  ``settle_seconds``.

``plan`` never deletes. ``apply`` does, and returns the bytes reclaimed.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import db
from .config import get_settings

# A project in one of these states will never render another clip from its
# source, so the source (and any fetched window) is safe to drop once idle.
TERMINAL_PROJECT_STATES = frozenset({"completed", "failed"})

# Six hours is long enough that an in-flight transcription or render is never
# mistaken for a finished one, short enough to keep a busy day's disk flat.
DEFAULT_SETTLE_SECONDS = 6 * 3600


@dataclass(frozen=True)
class PruneTarget:
    path: Path
    kind: str  # "monitor" | "source" | "window"
    bytes: int
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "bytes": self.bytes,
            "reason": self.reason,
        }


def _tree_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _newest_mtime(path: Path) -> float:
    """The most recent write anywhere under ``path`` (the whole tree)."""
    try:
        newest = path.stat().st_mtime
    except OSError:
        return 0.0
    for root, dirs, files in os.walk(path):
        for name in (*dirs, *files):
            try:
                newest = max(newest, (Path(root) / name).stat().st_mtime)
            except OSError:
                pass
    return newest


def plan(
    *,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    now: float | None = None,
) -> list[PruneTarget]:
    """List what could be reclaimed. Reads only; deletes nothing."""
    settings = get_settings()
    reference = time.time() if now is None else now
    targets: list[PruneTarget] = []

    live = settings.live_dir
    if live.is_dir():
        for entry in sorted(live.iterdir()):
            if not entry.is_dir() or not entry.name.startswith("mon_"):
                continue
            idle = reference - _newest_mtime(entry)
            if idle < settle_seconds:
                continue
            targets.append(
                PruneTarget(entry, "monitor", _tree_bytes(entry), f"idle {idle / 3600:.1f}h")
            )

    projects = settings.projects_dir
    if projects.is_dir():
        terminal = {
            row["id"]
            for row in db.list_rows("projects", limit=1000)
            if row.get("status") in TERMINAL_PROJECT_STATES
        }
        for project_id in sorted(terminal):
            base = projects / project_id
            if not base.is_dir():
                continue
            source = base / "source"
            if source.is_dir():
                idle = reference - _newest_mtime(source)
                if idle >= settle_seconds:
                    targets.append(
                        PruneTarget(
                            source,
                            "source",
                            _tree_bytes(source),
                            f"terminal project, idle {idle / 3600:.1f}h",
                        )
                    )
            for window in sorted((base / "clips").glob("*/window.mp4")):
                try:
                    idle = reference - window.stat().st_mtime
                    size = window.stat().st_size
                except OSError:
                    continue
                if idle >= settle_seconds:
                    targets.append(
                        PruneTarget(
                            window,
                            "window",
                            size,
                            f"terminal project, idle {idle / 3600:.1f}h",
                        )
                    )
    return targets


def apply(targets: list[PruneTarget]) -> int:
    """Delete every target. Returns the bytes reclaimed (measured by plan)."""
    freed = 0
    for target in targets:
        try:
            if target.path.is_dir():
                shutil.rmtree(target.path, ignore_errors=True)
            else:
                target.path.unlink(missing_ok=True)
        except OSError:
            continue
        freed += target.bytes
    return freed


__all__ = ["DEFAULT_SETTLE_SECONDS", "TERMINAL_PROJECT_STATES", "PruneTarget", "apply", "plan"]
