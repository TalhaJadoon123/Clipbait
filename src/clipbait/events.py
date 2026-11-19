"""In-process pub/sub used by the SSE endpoints.

The bus is intentionally simple: subscribers get an asyncio queue, publishers
never block. Slow clients drop events instead of stalling the pipeline.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict, deque
from typing import Any


class EventBus:
    def __init__(self, history: int = 200) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=history))
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called from FastAPI startup so worker threads can publish safely."""
        self._loop = loop

    def publish(self, channel: str, event: dict[str, Any]) -> None:
        payload = {"ts": time.time(), **event}
        with self._lock:
            self._history[channel].append(payload)
            subscribers = list(self._subscribers.get(channel, ()))
        for queue in subscribers:
            self._put(queue, payload)

    def _put(self, queue: asyncio.Queue, payload: dict[str, Any]) -> None:
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._safe_put, queue, payload)
        else:
            self._safe_put(queue, payload)

    @staticmethod
    def _safe_put(queue: asyncio.Queue, payload: dict[str, Any]) -> None:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(payload)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def subscribe(self, channel: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers[channel].add(queue)
        return queue

    def unsubscribe(self, channel: str, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers[channel].discard(queue)

    def snapshot(self, channel: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history.get(channel, ()))


bus = EventBus()