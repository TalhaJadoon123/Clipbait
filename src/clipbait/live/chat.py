"""Chat sources.

Three real transports plus a deterministic replay source:

* YouTube live chat via ``pytchat`` (no API key, optional extra)
* YouTube Data API polling (needs ``YOUTUBE_API_KEY``)
* Twitch IRC over TLS (anonymous, no key)
* ``demo`` - a synthetic stream that spikes on cue, for demos and tests

Every source yields :class:`ChatMessage` with ``t`` = seconds since monitor
start. Connectors own reconnection; the orchestrator only sees a clean stream.
"""

from __future__ import annotations

import json
import logging
import queue
import random
import re
import socket
import ssl
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from ..schema import ChatMessage

log = logging.getLogger("clipbait.live.chat")


class ChatError(RuntimeError):
    pass


class ChatSource(ABC):
    name = "base"

    def __init__(self) -> None:
        self.started = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @abstractmethod
    def messages(self) -> Iterator[ChatMessage]: ...

    def close(self) -> None:  # noqa: B027 - optional hook
        pass


def youtube_video_id(url: str) -> str:
    patterns = [
        r"(?:v=|/live/|/shorts/|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ChatError(f"cannot extract a YouTube video id from {url!r}")


class YouTubeChatSource(ChatSource):
    """Keyless live chat through pytchat."""

    name = "youtube-pytchat"

    def __init__(self, url: str, poll_seconds: float = 1.0) -> None:
        super().__init__()
        self.video_id = youtube_video_id(url)
        self.poll_seconds = poll_seconds

    def messages(self) -> Iterator[ChatMessage]:
        try:
            import pytchat  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ChatError(
                "pytchat is not installed. Run: pip install 'clipbait[live]' "
                "or set YOUTUBE_API_KEY to use the Data API source."
            ) from exc
        chat = pytchat.create(video_id=self.video_id, interruptable=False)
        while chat.is_alive():
            try:
                batch = chat.get()
            except Exception as exc:  # noqa: BLE001 - network hiccups are expected
                log.warning("pytchat read failed: %s", exc)
                time.sleep(2)
                continue
            items = getattr(batch, "items", None) or []
            for item in items:
                text = getattr(item, "message", "") or ""
                if not text:
                    continue
                kind = "text"
                if getattr(item, "amountValue", 0):
                    kind = "superchat"
                yield ChatMessage(
                    t=round(self.elapsed(), 3),
                    author=(getattr(getattr(item, "author", None), "name", "") or "")[:24],
                    text=text[:240],
                    kind=kind,
                )
            time.sleep(self.poll_seconds)
        self.close()

    def close(self) -> None:
        pass


class YouTubeApiChatSource(ChatSource):
    """Official Data API. Needs YOUTUBE_API_KEY; public streams only."""

    name = "youtube-api"

    def __init__(self, url: str, api_key: str, poll_seconds: float = 2.0) -> None:
        super().__init__()
        self.video_id = youtube_video_id(url)
        self.api_key = api_key
        self.poll_seconds = poll_seconds

    def _get(self, path: str, params: dict) -> dict:
        import httpx

        response = httpx.get(
            f"https://www.googleapis.com/youtube/v3/{path}",
            params={**params, "key": self.api_key},
            timeout=20,
        )
        if response.status_code >= 400:
            raise ChatError(f"YouTube API {response.status_code}: {response.text[:200]}")
        return response.json()

    def messages(self) -> Iterator[ChatMessage]:
        details = self._get(
            "videos", {"part": "liveStreamingDetails", "id": self.video_id}
        ).get("items", [])
        if not details:
            raise ChatError("video not found")
        live_chat_id = details[0].get("liveStreamingDetails", {}).get("activeLiveChatId")
        if not live_chat_id:
            raise ChatError("this video has no active live chat")
        page_token = None
        while True:
            params = {
                "liveChatId": live_chat_id,
                "part": "snippet,authorDetails",
                "maxResults": 200,
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._get("liveChatMessages", params)
            for item in payload.get("items", []):
                snippet = item.get("snippet", {})
                text = (snippet.get("displayMessage") or "").strip()
                if text:
                    yield ChatMessage(
                        t=round(self.elapsed(), 3),
                        author=(item.get("authorDetails", {}).get("displayName") or "")[:24],
                        text=text[:240],
                        kind="superchat" if snippet.get("superChatDetails") else "text",
                    )
            page_token = payload.get("nextPageToken")
            time.sleep(max(1.0, payload.get("pollingIntervalMillis", 2000) / 1000))


class TwitchChatSource(ChatSource):
    """Anonymous read-only Twitch IRC. No credentials, no rate-limit surprises."""

    name = "twitch-irc"

    def __init__(self, url: str, nick: str = "justinfan7331") -> None:
        super().__init__()
        channel = url.rstrip("/").split("/")[-1].split("?")[0].lower()
        if not channel:
            raise ChatError("cannot read the Twitch channel from the URL")
        self.channel = channel
        self.nick = nick
        self._stop = threading.Event()
        self._queue: queue.Queue[ChatMessage | None] = queue.Queue(maxsize=5000)
        self._thread: threading.Thread | None = None

    def _reader(self) -> None:
        try:
            context = ssl.create_default_context()
            sock = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=20)
            sock = context.wrap_socket(sock, server_hostname="irc.chat.twitch.tv")
            sock.sendall(f"PASS oauth:anonymous\r\nNICK {self.nick}\r\n".encode())
            sock.sendall(f"JOIN #{self.channel}\r\n".encode())
            buffer = b""
            while not self._stop.is_set():
                chunk = sock.recv(8192)
                if not chunk:
                    break
                buffer += chunk
                while b"\r\n" in buffer:
                    raw, buffer = buffer.split(b"\r\n", 1)
                    line = raw.decode("utf-8", "ignore")
                    if line.startswith("PING"):
                        sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                        continue
                    if "PRIVMSG" not in line:
                        continue
                    try:
                        prefix, body = line.split("PRIVMSG", 1)
                        author = prefix.split("!", 1)[0].lstrip(":")
                        text = body.split(":", 1)[1]
                    except (IndexError, ValueError):
                        continue
                    self._queue.put(
                        ChatMessage(
                            t=round(self.elapsed(), 3),
                            author=author[:24],
                            text=text.strip()[:240],
                        )
                    )
            sock.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("twitch chat reader stopped: %s", exc)
        finally:
            self._queue.put(None)

    def messages(self) -> Iterator[ChatMessage]:
        self._thread = threading.Thread(target=self._reader, daemon=True, name="twitch-chat")
        self._thread.start()
        while True:
            item = self._queue.get()
            if item is None:
                return
            yield item

    def close(self) -> None:
        self._stop.set()


class JsonlChatSource(ChatSource):
    """Replay recorded chat: one JSON object per line with t/text/author."""

    name = "replay"

    def __init__(self, path: Path, speed: float = 1.0) -> None:
        super().__init__()
        self.path = path
        self.speed = max(0.05, speed)

    def messages(self) -> Iterator[ChatMessage]:
        last_t = 0.0
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = float(payload.get("t", last_t))
                delay = max(0.0, (t - last_t) / self.speed)
                if delay:
                    time.sleep(min(delay, 5.0))
                last_t = t
                yield ChatMessage(
                    t=round(self.elapsed(), 3),
                    author=str(payload.get("author", ""))[:24],
                    text=str(payload.get("text", ""))[:240],
                    kind=str(payload.get("kind", "text")),
                )


class DemoChatSource(ChatSource):
    """Deterministic crowd simulation: calm, then a wave every ~35 seconds.

    Exists so ``/live`` can be demoed and the spike detector can be exercised
    without a live stream. Clearly labelled in the UI.
    """

    name = "demo"

    PEAKS = [20, 75, 130]
    HYPE = ["W", "W", "W", "LUL", "LULW", "NO WAY", "clip it", "CLIP IT", "🔥", "🔥🔥", "💀", "GG", "pog", "insane"]

    def __init__(self, seed: int = 11) -> None:
        super().__init__()
        self.rng = random.Random(seed)
        self._stop = threading.Event()

    def messages(self) -> Iterator[ChatMessage]:
        while not self._stop.is_set():
            tick = self.elapsed()
            burst = any(abs(tick - peak) < 5 for peak in self.PEAKS)
            if burst:
                text = self.rng.choice(self.HYPE)
                wait = self.rng.uniform(0.09, 0.18)
            else:
                text = self.rng.choice(
                    ["hello from Brazil", "this is great", "first time here", "audio is good",
                     "chat is quiet", "wait what", "true", "lol"]
                )
                wait = self.rng.uniform(1.6, 3.4)
            yield ChatMessage(
                t=round(self.elapsed(), 3),
                author=f"viewer{self.rng.randint(1, 400)}",
                text=text,
                kind="text",
            )
            time.sleep(wait)

    def close(self) -> None:
        self._stop.set()


def resolve_chat_source(url: str, provider: str = "auto", api_key: str | None = None) -> ChatSource:
    if url.startswith("demo://") or url == "demo":
        return DemoChatSource()
    if provider == "demo":
        return DemoChatSource()
    if "twitch.tv" in url:
        return TwitchChatSource(url)
    if "youtube.com" in url or "youtu.be" in url:
        if provider == "api" and api_key:
            return YouTubeApiChatSource(url, api_key)
        if provider == "auto" and not _module("pytchat") and api_key:
            return YouTubeApiChatSource(url, api_key)
        return YouTubeChatSource(url)
    raise ChatError("unsupported live URL: use YouTube, Twitch or demo://live")


def _module(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None