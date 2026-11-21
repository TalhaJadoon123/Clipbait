"""Live clipping: chat transports, the rolling recorder and the orchestrator.

Networks are faked at the seam (socket, httpx, pytchat, the yt-dlp shell-out)
so every transport's real parsing code runs without a live stream.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from helpers import make_video

from clipbait import db, live, storage
from clipbait.config import get_settings
from clipbait.live import chat, recorder
from clipbait.live.orchestrator import LiveRunner
from clipbait.live.spikes import MomentCandidate
from clipbait.schema import ChatMessage, SpikeSignal

# ----------------------------------------------------------------------- helpers


def no_sleep(monkeypatch):
    monkeypatch.setattr("clipbait.live.chat.time.sleep", lambda *_: None)
    monkeypatch.setattr("clipbait.live.recorder.time.sleep", lambda *_: None)


class FakeRecorder(recorder.Recorder):
    kind = "fake"

    def __init__(self, window: tuple[float, float] | None = None) -> None:
        self.started = 0
        self.stopped = 0
        self.slices: list[tuple[float, float, Path]] = []
        self.clear_calls = 0
        self.window = window
        self._started = 0.0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def slice(self, start, end, out):
        self.slices.append((start, end, out))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"0" * 20_000)
        return out

    def stats(self) -> dict:
        data = {"kind": self.kind, "slices": len(self.slices)}
        if self.window:
            data["available_from"], data["available_until"] = self.window
        return data


class FakeChat(chat.ChatSource):
    name = "fake"

    def __init__(self, messages=(), error: Exception | None = None) -> None:
        super().__init__()
        self._messages = list(messages)
        self._error = error
        self.closed = False

    def messages(self):
        yield from self._messages
        if self._error:
            raise self._error

    def close(self) -> None:
        self.closed = True


class FakeDetector:
    """Fires exactly once, on the first message."""

    def __init__(self, candidate: MomentCandidate) -> None:
        self.candidate = candidate
        self.fired = False

    def add(self, message):
        if self.fired:
            return None
        self.fired = True
        return self.candidate

    def current_rate(self) -> float:
        return 12.5

    def timeline(self, _buckets: int) -> list:
        return [1, 2, 3]


def confirmed_candidate(**overrides) -> MomentCandidate:
    """A chat spike the stream actually backed up: speech is present on stream.

    A chat-only spike is deliberately *not* clipped (see ``LiveRunner._clipworthy``).
    Capture-path tests use this helper to represent the confirmed moment.
    """
    base = candidate()
    signal = base.signal.model_copy(update={"speech": 3.0})
    return dataclasses.replace(base, signal=signal, **overrides)


def make_monitor(**options) -> dict:
    return db.create_monitor(
        {
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
            "title": "Live test",
            "status": "idle",
            "options": options,
        }
    )


def candidate(**overrides) -> MomentCandidate:
    return MomentCandidate(
        start=5.0,
        end=9.0,
        trigger_at=9.0,
        score=4.2,
        signal=SpikeSignal(rate=11.0, rate_z=4.0, top_messages=["CLIP IT"]),
        messages=[
            ChatMessage(t=5.2, author="a", text="CLIP IT"),
            ChatMessage(t=8.0, author="b", text="W"),
            ChatMessage(t=20.0, author="c", text="outside window"),
        ],
        **overrides,
    )


# ------------------------------------------------------------------- chat: urls


def test_youtube_video_id_variants():
    assert chat.youtube_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert chat.youtube_video_id("https://www.youtube.com/live/abcdefghijk?x=1") == "abcdefghijk"
    assert chat.youtube_video_id("https://www.youtube.com/shorts/abcdefghijk") == "abcdefghijk"
    assert chat.youtube_video_id("https://www.youtube.com/embed/abcdefghijk") == "abcdefghijk"
    assert chat.youtube_video_id("abcdefghijk") == "abcdefghijk"
    with pytest.raises(chat.ChatError):
        chat.youtube_video_id("https://example.com/nope")


def test_module_probe():
    assert chat._module("json") is True
    assert chat._module("definitely_not_a_module_xyz") is False


def test_resolve_chat_source_kinds(monkeypatch):
    assert isinstance(chat.resolve_chat_source("demo://live"), chat.DemoChatSource)
    assert isinstance(chat.resolve_chat_source("https://x", provider="demo"), chat.DemoChatSource)
    assert isinstance(
        chat.resolve_chat_source("https://twitch.tv/someone"), chat.TwitchChatSource
    )
    monkeypatch.setattr(chat, "_module", lambda _name: True)
    assert isinstance(
        chat.resolve_chat_source("https://youtube.com/watch?v=abcdefghijk"),
        chat.YouTubeChatSource,
    )
    assert isinstance(
        chat.resolve_chat_source(
            "https://youtube.com/watch?v=abcdefghijk", provider="api", api_key="k"
        ),
        chat.YouTubeApiChatSource,
    )
    # Keyless fallback: no pytchat, no API key still reads YouTube chat.
    monkeypatch.setattr(chat, "_module", lambda _name: False)
    assert isinstance(
        chat.resolve_chat_source("https://youtube.com/watch?v=abcdefghijk"),
        chat.YouTubeInnerTubeChatSource,
    )
    assert isinstance(
        chat.resolve_chat_source(
            "https://youtube.com/watch?v=abcdefghijk", provider="auto", api_key="k"
        ),
        chat.YouTubeApiChatSource,
    )
    with pytest.raises(chat.ChatError, match="YOUTUBE_API_KEY"):
        chat.resolve_chat_source(
            "https://youtube.com/watch?v=abcdefghijk", provider="api", api_key=None
        )
    with pytest.raises(chat.ChatError):
        chat.resolve_chat_source("https://example.com/stream")


def test_jsonl_chat_source(tmp_path):
    path = tmp_path / "chat.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"t": 0.0, "author": "a", "text": "hi"}),
                "not json",
                json.dumps({"t": 0.001, "author": "b", "text": "there", "kind": "superchat"}),
            ]
        ),
        encoding="utf-8",
    )
    source = chat.JsonlChatSource(path, speed=1000)
    messages = list(source.messages())
    assert [message.text for message in messages] == ["hi", "there"]
    assert messages[1].kind == "superchat"


def test_demo_chat_source_burst_and_calm(monkeypatch):
    no_sleep(monkeypatch)
    source = chat.DemoChatSource(seed=3)
    monkeypatch.setattr(source, "elapsed", lambda: 20.0)
    stream = source.messages()
    burst = [next(stream) for _ in range(3)]
    assert all(message.text for message in burst)
    source.close()
    assert source._stop.is_set()

    calm_source = chat.DemoChatSource(seed=4)
    monkeypatch.setattr(calm_source, "elapsed", lambda: 1.0)
    calm = next(calm_source.messages())
    assert calm.author.startswith("viewer")


def test_pytchat_source(monkeypatch):
    no_sleep(monkeypatch)
    items = [
        SimpleNamespace(message="hello", amountValue=0, author=SimpleNamespace(name="bob")),
        SimpleNamespace(message="", amountValue=0, author=None),
        SimpleNamespace(message="tip", amountValue=5, author=SimpleNamespace(name="ann")),
    ]

    class FakeChat:
        def __init__(self):
            self.reads = 0

        def is_alive(self):
            return self.reads < 2

        def get(self):
            self.reads += 1
            return SimpleNamespace(items=items if self.reads == 1 else [])

    module = types.ModuleType("pytchat")
    module.create = lambda **kwargs: FakeChat()
    monkeypatch.setitem(sys.modules, "pytchat", module)

    source = chat.YouTubeChatSource("https://youtu.be/abcdefghijk")
    messages = list(source.messages())

    assert [message.text for message in messages] == ["hello", "tip"]
    assert messages[0].author == "bob"
    assert messages[1].kind == "superchat"


def test_pytchat_missing_is_explained(monkeypatch):
    monkeypatch.setitem(sys.modules, "pytchat", None)
    source = chat.YouTubeChatSource("https://youtu.be/abcdefghijk")
    with pytest.raises(chat.ChatError, match="pytchat is not installed"):
        list(source.messages())


def test_youtube_api_source(monkeypatch):
    calls: list[str] = []

    class Response:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status_code = status
            self.text = "err"

        def json(self):
            return self._payload

    def fake_get(url, params=None, timeout=None):
        calls.append(url)
        if "videos" in url:
            return Response({"items": [{"liveStreamingDetails": {"activeLiveChatId": "lc"}}]})
        if len(calls) == 2:
            return Response(
                {
                    "items": [
                        {
                            "snippet": {"displayMessage": " hi ", "superChatDetails": {"x": 1}},
                            "authorDetails": {"displayName": "ann"},
                        },
                        {"snippet": {"displayMessage": ""}},
                    ],
                    "nextPageToken": "tok",
                }
            )
        raise chat.ChatError("done")

    monkeypatch.setattr("httpx.get", fake_get)
    source = chat.YouTubeApiChatSource("https://youtu.be/abcdefghijk", "key")
    stream = source.messages()
    first = next(stream)
    assert first.text == "hi"
    assert first.kind == "superchat"
    assert first.author == "ann"
    with pytest.raises(chat.ChatError, match="done"):
        next(stream)


def test_youtube_api_errors(monkeypatch):
    class Response:
        status_code = 403
        text = "quota"

        def json(self):
            return {}

    monkeypatch.setattr("httpx.get", lambda *a, **k: Response())
    source = chat.YouTubeApiChatSource("https://youtu.be/abcdefghijk", "key")
    with pytest.raises(chat.ChatError, match="403"):
        list(source.messages())

    class Empty:
        status_code = 200

        def json(self):
            return {"items": []}

    monkeypatch.setattr("httpx.get", lambda *a, **k: Empty())
    with pytest.raises(chat.ChatError, match="video not found"):
        list(source.messages())

    class NoChat:
        status_code = 200

        def json(self):
            return {"items": [{"liveStreamingDetails": {}}]}

    monkeypatch.setattr("httpx.get", lambda *a, **k: NoChat())
    with pytest.raises(chat.ChatError, match="no active live chat"):
        list(source.messages())


def test_twitch_reader_parses_irc(monkeypatch):
    payload = (
        b"PING :tmi.twitch.tv\r\n"
        b":bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :hello chat\r\n"
        b":garbage line\r\n"
        b":ann!ann@ann.tmi PRIVMSG #chan :second\r\n"
    )
    sent: list[bytes] = []

    class Sock:
        def __init__(self):
            self.chunks = [payload, b""]

        def sendall(self, data):
            sent.append(data)

        def recv(self, _size):
            return self.chunks.pop(0) if self.chunks else b""

        def close(self):
            pass

    class Context:
        def wrap_socket(self, sock, **_kwargs):
            return sock

    monkeypatch.setattr("socket.create_connection", lambda *a, **k: Sock())
    monkeypatch.setattr("ssl.create_default_context", lambda: Context())

    source = chat.TwitchChatSource("https://twitch.tv/chan")
    assert source.channel == "chan"
    source._reader()

    messages = []
    while True:
        item = source._queue.get_nowait()
        if item is None:
            break
        messages.append(item)
    assert [message.text for message in messages] == ["hello chat", "second"]
    assert messages[0].author == "bob"
    assert any(b"PONG" in chunk for chunk in sent)
    assert any(b"JOIN #chan" in chunk for chunk in sent)


# -------------------------------------------------------------------- recorder


def test_select_segments():
    assert recorder.select_segments([], 0, 10) == ([], 0.0)
    segments = [
        recorder.Segment(Path("a.ts"), 0.0, 4.0),
        recorder.Segment(Path("b.ts"), 4.0, 4.0),
    ]
    assert recorder.select_segments(segments, 100, 110) == ([], 0.0)
    chosen, offset = recorder.select_segments(segments, 2.0, 6.0)
    assert [s.path.name for s in chosen] == ["a.ts", "b.ts"]
    assert offset == 2.0


def test_concat_file(tmp_path):
    path = recorder.concat_file(
        [recorder.Segment(tmp_path / "a.ts", 0, 1)], tmp_path / "list.txt"
    )
    text = path.read_text()
    assert text.startswith("ffconcat version 1.0")
    assert "a.ts" in text


def test_build_recorder_variants(monkeypatch, tmp_path):
    tiny = make_video(tmp_path / "tiny.mp4", seconds=1, size="160x90")
    monkeypatch.setattr(recorder, "ensure_demo_source", lambda path, seconds=240: tiny)
    assert isinstance(recorder.build_recorder("demo://live", tmp_path), recorder.DemoRecorder)
    assert isinstance(
        recorder.build_recorder("https://twitch.tv/x", tmp_path), recorder.HlsRecorder
    )
    assert isinstance(
        recorder.build_recorder("https://youtube.com/watch?v=a", tmp_path), recorder.HlsRecorder
    )
    with pytest.raises(recorder.RecorderError):
        recorder.build_recorder("https://example.com/x", tmp_path)


def test_ensure_demo_source(monkeypatch, tmp_path):
    existing = tmp_path / "demo.mp4"
    existing.write_bytes(b"data")
    assert recorder.ensure_demo_source(existing) == existing

    target = tmp_path / "generated.mp4"
    made = recorder.ensure_demo_source(target, seconds=1)
    assert made.exists() and made.stat().st_size > 0


def test_resolve_live_stream(monkeypatch):
    monkeypatch.setattr(
        recorder.media,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="\nnot-a-url\nhttps://cdn/stream.m3u8\n"),
    )
    assert recorder.resolve_live_stream("https://twitch.tv/x") == "https://cdn/stream.m3u8"

    monkeypatch.setattr(recorder.media, "run", lambda *a, **k: SimpleNamespace(stdout=""))
    with pytest.raises(recorder.RecorderError):
        recorder.resolve_live_stream("https://twitch.tv/x")


def test_resolve_live_streams_selects_video_and_audio(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(
            stdout="https://cdn/video.m3u8\nhttps://cdn/audio.m3u8\n", stderr=""
        )

    monkeypatch.setattr(recorder.media, "run", fake_run)
    streams = recorder.resolve_live_streams("https://youtube.com/watch?v=x")
    assert streams == ["https://cdn/video.m3u8", "https://cdn/audio.m3u8"]
    # YouTube live HLS is video-only + audio-only: the selector must merge both.
    selector = captured["cmd"][captured["cmd"].index("-f") + 1]
    assert "bv*" in selector and "+ba" in selector


def test_resolve_live_streams_reports_the_ytdlp_reason(monkeypatch):
    monkeypatch.setattr(
        recorder.media,
        "run",
        lambda *a, **k: SimpleNamespace(
            stdout="", stderr="ERROR: Requested format is not available"
        ),
    )
    with pytest.raises(recorder.RecorderError, match="Requested format is not available"):
        recorder.resolve_live_streams("https://youtube.com/watch?v=x")


def test_innertube_token_and_messages(monkeypatch):
    html = """<script>var ytInitialData = {"a": {"liveChatRenderer": {"continuations":
      [{"reloadContinuationData": {"continuation": "TOKEN123"}}]}}};</script>"""
    assert chat.YouTubeInnerTubeChatSource._token_from_page(html) == "TOKEN123"
    assert chat.YouTubeInnerTubeChatSource._token_from_page("<html>no chat</html>") is None

    source = chat.YouTubeInnerTubeChatSource("https://youtu.be/abcdefghijk")
    payload = {
        "continuationContents": {
            "liveChatContinuation": {
                "actions": [
                    {
                        "addChatItemAction": {
                            "item": {
                                "liveChatTextMessageRenderer": {
                                    "message": {"runs": [{"text": "hello "}, {"text": "chat"}]},
                                    "authorName": {"simpleText": "bob"},
                                }
                            }
                        }
                    },
                    {
                        "addChatItemAction": {
                            "item": {
                                "liveChatPaidMessageRenderer": {
                                    "message": {"simpleText": "tip"},
                                    "authorName": {"runs": [{"text": "ann"}]},
                                }
                            }
                        }
                    },
                    {"addChatItemAction": {"item": {"liveChatViewerEngagementMessageRenderer": {}}}},
                ],
                "continuations": [
                    {"invalidationContinuationData": {"continuation": "NEXT", "timeoutMs": 3000}}
                ],
            }
        }
    }
    messages = source._messages_from(payload)
    assert [m.text for m in messages] == ["hello chat", "tip"]
    assert messages[0].author == "bob"
    assert messages[1].kind == "superchat"
    assert source._advance(payload) == 3.0
    assert source._continuation == "NEXT"


def test_innertube_bootstrap_and_stream(monkeypatch):
    source = chat.YouTubeInnerTubeChatSource("https://youtu.be/abcdefghijk")

    class Response:
        status_code = 200
        text = '"liveChatRenderer": {"continuationCommand": {"token": "T"}}'

        def json(self):
            return {"continuationContents": {"liveChatContinuation": {"actions": []}}}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: Response())
    monkeypatch.setattr(httpx, "post", lambda *a, **k: Response())
    payload = source._poll()
    assert payload["continuationContents"] == {"liveChatContinuation": {"actions": []}}
    assert source._continuation == "T"

    def broken(*_a, **_k):
        return SimpleNamespace(status_code=200, text="nothing", json=lambda: {})

    monkeypatch.setattr(httpx, "get", broken)
    with pytest.raises(chat.ChatError, match="no readable live chat"):
        chat.YouTubeInnerTubeChatSource("https://youtu.be/abcdefghijk")._bootstrap()


def test_hls_start_maps_video_and_audio(monkeypatch, tmp_path):
    monkeypatch.setattr(
        recorder,
        "resolve_live_streams",
        lambda _url: ["https://cdn/v.m3u8", "https://cdn/a.m3u8"],
    )

    class Proc:
        def __init__(self, cmd, **_kwargs):
            self.cmd = cmd

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    procs: list[Proc] = []

    def fake_popen(cmd, **kwargs):
        proc = Proc(cmd, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(recorder.subprocess, "Popen", fake_popen)
    rec = recorder.HlsRecorder("https://youtube.com/watch?v=x", tmp_path)
    rec.start()
    cmd = procs[0].cmd
    assert cmd.count("-i") == 2
    assert "0:v:0" in cmd and "1:a:0?" in cmd
    assert cmd[cmd.index("-i") + 1] == "https://cdn/v.m3u8"
    # Every live input starts at the live edge, not the start of the DVR.
    assert cmd.count("-live_start_index") == 2
    assert cmd[cmd.index("-live_start_index") + 1] == "-1"
    rec.stop()


def test_hls_watch_and_prune(monkeypatch, tmp_path):
    rec = recorder.HlsRecorder("https://twitch.tv/x", tmp_path, segment_seconds=4)
    (tmp_path / "seg_000000.ts").write_bytes(b"a")
    (tmp_path / "seg_000001.ts").write_bytes(b"b")
    (tmp_path / "segments.csv").write_text(
        "seg_000000.ts,4.0\nseg_000001.ts,not-a-number\nseg_000001.ts,4.0\n"
    )

    def stop_after_first(*_args):
        rec._stop.set()

    monkeypatch.setattr("clipbait.live.recorder.time.sleep", stop_after_first)
    rec._watch(tmp_path / "segments.csv")

    assert [segment.path.name for segment in rec._segments] == ["seg_000000.ts", "seg_000001.ts"]
    assert rec._segments[1].start == 4.0


    settings = dataclasses.replace(get_settings(), live_buffer_seconds=2)
    monkeypatch.setattr(recorder, "get_settings", lambda: settings)
    rec.buffer_seconds = 2  # the monitor option, not the process default
    rec._prune()
    assert [segment.path.name for segment in rec._segments] == ["seg_000001.ts"]
    assert not (tmp_path / "seg_000000.ts").exists()
    assert rec.stats()["buffered_seconds"] == 4.0

    rec._segments.clear()
    rec._prune()  # no segments: no-op
    assert rec.stats()["buffered_seconds"] == 0.0

def test_hls_scan_reads_three_column_segment_lists(tmp_path):
    """ffmpeg writes name,start,end; column 2 is a start time, not a duration."""
    rec = recorder.HlsRecorder("https://twitch.tv/x", tmp_path, segment_seconds=4)
    for index in range(2):
        (tmp_path / f"seg_00000{index}.ts").write_bytes(b"x")
    (tmp_path / "segments.csv").write_text(
        "seg_000000.ts,120.000000,124.000000\nseg_000001.ts,124.000000,128.500000\n",
        encoding="utf-8",
    )
    rec._scan(tmp_path / "segments.csv")
    assert [(s.start, s.end) for s in rec._segments] == [(0.0, 4.0), (4.0, 8.5)]


def test_hls_slice(monkeypatch, tmp_path):
    rec = recorder.HlsRecorder("https://twitch.tv/x", tmp_path)
    with pytest.raises(recorder.RecorderError, match="buffer empty"):
        rec.slice(10, 20, tmp_path / "out.mp4")

    (tmp_path / "seg_000000.ts").write_bytes(b"a")
    (tmp_path / "seg_000001.ts").write_bytes(b"b")
    rec._segments = [
        recorder.Segment(tmp_path / "seg_000000.ts", 0.0, 4.0),
        recorder.Segment(tmp_path / "seg_000001.ts", 4.0, 4.0),
    ]

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"video")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(recorder.media, "run", fake_run)
    out = rec.slice(2.0, 6.0, tmp_path / "out.mp4")

    assert out.exists()
    concat = (tmp_path / "slice.ffconcat").read_text()
    assert "seg_000000.ts" in concat and "seg_000001.ts" in concat


def test_hls_slice_seeks_after_input_so_av_stay_aligned(monkeypatch, tmp_path):
    """An input seek lands video on a keyframe and shreds lip-sync.

    Seeking a concat of TS segments before ``-i`` starts the video at its first
    keyframe (up to a second after the audio seek point), so a slice opens with
    video that trails the audio and every caption is that far off the lips. The
    seek must be an output option (after ``-i``) to keep the two in step.
    """
    rec = recorder.HlsRecorder("https://twitch.tv/x", tmp_path)
    (tmp_path / "seg_000000.ts").write_bytes(b"a")
    rec._segments = [recorder.Segment(tmp_path / "seg_000000.ts", 0.0, 4.0)]

    captured: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        captured.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"video")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(recorder.media, "run", fake_run)
    rec.slice(1.0, 3.0, tmp_path / "out.mp4")

    cmd = captured[0]
    assert cmd.index("-i") < cmd.index("-ss"), "slice must seek after -i to stay A/V aligned"


def test_hls_start_and_stop(monkeypatch, tmp_path):
    monkeypatch.setattr(recorder, "resolve_live_streams", lambda _url: ["https://cdn/x.m3u8"])
    monkeypatch.setattr("clipbait.live.recorder.time.sleep", lambda *_: None)

    class Proc:
        def __init__(self, cmd, **_kwargs):
            self.cmd = cmd
            self.terminated = False
            self.killed = False
            self.stdout = None
            self.stderr = None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.killed = True

    procs: list[Proc] = []

    def fake_popen(cmd, **kwargs):
        proc = Proc(cmd, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr("clipbait.live.recorder.subprocess.Popen", fake_popen)
    rec = recorder.HlsRecorder("https://twitch.tv/x", tmp_path)
    rec.start()
    assert procs and "-segment_time" in procs[0].cmd
    rec.stop()
    assert rec._stop.is_set()
    assert procs[0].terminated


def test_file_and_demo_recorder(tmp_path):
    source = make_video(tmp_path / "src.mp4", seconds=2, size="320x180")
    rec = recorder.FileRecorder(source)
    rec.start()
    assert rec.stats()["kind"] == "file"
    out = rec.slice(0.2, 1.2, tmp_path / "cut.mp4")
    assert out.exists() and out.stat().st_size > 0
    beyond = rec.slice(50, 52, tmp_path / "beyond.mp4")
    assert beyond.exists()
    rec.stop()

    demo = recorder.DemoRecorder.__new__(recorder.DemoRecorder)
    recorder.FileRecorder.__init__(demo, source, loop_seconds=30)
    assert demo._loop_seconds == 30


def test_recorder_base_contract():
    base = recorder.Recorder()
    base.start()
    base.stop()
    assert base.stats() == {}
    with pytest.raises(NotImplementedError):
        base.slice(0, 1, Path("x"))


def test_clear(tmp_path):
    directory = tmp_path / "gone"
    directory.mkdir()
    (directory / "f").write_text("x")
    recorder.clear(directory)
    assert not directory.exists()


# ------------------------------------------------------------------ orchestrator


def test_live_runner_captures_a_spike(monkeypatch, tmp_path):
    monitor = make_monitor(auto_clip=True)
    runner = LiveRunner(monitor)
    fake_recorder = FakeRecorder()
    monkeypatch.setattr("clipbait.live.orchestrator.build_recorder", lambda url, d, **kw: fake_recorder)
    monkeypatch.setattr(
        "clipbait.live.orchestrator.resolve_chat_source",
        lambda url, provider, key: FakeChat(
            [ChatMessage(t=5.2, author="a", text="CLIP IT")]
        ),
    )
    spike = confirmed_candidate()
    runner.detector = FakeDetector(spike)
    monkeypatch.setattr(runner, "elapsed", lambda: 1000.0)

    submitted: list[dict] = []

    def fake_submit(kind, fn, **kwargs):
        submitted.append({"kind": kind, "fn": fn, **kwargs})
        return {"id": "job_1"}

    monkeypatch.setattr("clipbait.jobs.submit", fake_submit)

    runner.start()
    runner._thread.join(timeout=5)
    for thread in runner._clip_threads:
        thread.join(timeout=5)

    assert fake_recorder.started == 1
    assert fake_recorder.stopped >= 1
    assert fake_recorder.slices and fake_recorder.slices[0][:2] == (5.0, 9.0)

    monitor_row = db.get_monitor(monitor["id"])
    assert monitor_row["status"] == "stopped"
    moments = db.list_moments(monitor["id"])
    assert len(moments) == 1
    assert moments[0]["score"] == 4.2
    assert moments[0]["signal"]["top_messages"] == ["CLIP IT"]

    assert len(submitted) == 1
    payload = submitted[0]["args"][1]
    assert submitted[0]["kind"] == "live_clip"
    assert submitted[0]["monitor_id"] == monitor["id"]
    assert [message["text"] for message in payload["messages"]] == ["CLIP IT", "W"]
    assert "velocity" in payload["reason"]
    assert payload["score"] == 4.2

    project = db.get_project(submitted[0]["project_id"])
    assert project["source_kind"] == "live"
    assert Path(storage.resolve(project["source_path"])).exists()

    submitted[0]["on_done"]({}, {"clip_id": "clip_done"})
    assert runner.clips == 1
    assert db.list_moments(monitor["id"])[0]["clip_id"] == "clip_done"


def test_live_runner_start_failure(monkeypatch):
    monitor = make_monitor()

    def boom(url, directory, **kw):
        raise recorder.RecorderError("stream offline")

    monkeypatch.setattr("clipbait.live.orchestrator.build_recorder", boom)
    runner = LiveRunner(monitor)
    runner.start()
    runner._thread.join(timeout=5)

    row = db.get_monitor(monitor["id"])
    assert row["status"] == "failed"
    assert "stream offline" in row["error"]


def test_live_runner_survives_a_chat_crash(monkeypatch):
    monitor = make_monitor()
    fake_recorder = FakeRecorder()
    monkeypatch.setattr("clipbait.live.orchestrator.build_recorder", lambda url, d, **kw: fake_recorder)
    monkeypatch.setattr(
        "clipbait.live.orchestrator.resolve_chat_source",
        lambda url, provider, key: FakeChat([ChatMessage(t=1.0, text="hi")], error=RuntimeError("socket died")),
    )
    runner = LiveRunner(monitor)
    monkeypatch.setattr(runner, "elapsed", lambda: 0.0)
    runner.start()
    runner._thread.join(timeout=5)

    row = db.get_monitor(monitor["id"])
    assert row["status"] == "failed"
    assert "socket died" in row["error"]
    assert fake_recorder.stopped == 1


def test_live_runner_records_without_clipping(monkeypatch):
    monitor = make_monitor(auto_clip=False)
    runner = LiveRunner(monitor)
    monkeypatch.setattr(
        "clipbait.live.orchestrator.build_recorder", lambda url, d, **kw: FakeRecorder()
    )
    monkeypatch.setattr(
        "clipbait.live.orchestrator.resolve_chat_source",
        lambda url, provider, key: FakeChat([ChatMessage(t=5.2, text="CLIP IT")]),
    )
    runner.detector = FakeDetector(candidate())
    monkeypatch.setattr(runner, "elapsed", lambda: 1000.0)
    monkeypatch.setattr(
        "clipbait.jobs.submit",
        lambda *a, **k: pytest.fail("auto_clip is off: nothing should be queued"),
    )

    runner.start()
    runner._thread.join(timeout=5)

    assert len(db.list_moments(monitor["id"])) == 1
    assert runner.stats()["messages"] == 1
    assert runner.stats()["recorder"]["kind"] == "fake"


def test_live_runner_respects_max_clips(monkeypatch):
    monitor = make_monitor(max_clips=1)
    runner = LiveRunner(monitor)
    runner.clips = 1
    monkeypatch.setattr(
        "clipbait.live.orchestrator.build_recorder", lambda url, d, **kw: FakeRecorder()
    )
    monkeypatch.setattr(
        "clipbait.live.orchestrator.resolve_chat_source",
        lambda url, provider, key: FakeChat([ChatMessage(t=5.2, text="W")]),
    )
    runner.detector = FakeDetector(candidate())
    monkeypatch.setattr(runner, "elapsed", lambda: 1000.0)
    monkeypatch.setattr(
        "clipbait.jobs.submit",
        lambda *a, **k: pytest.fail("max_clips reached: nothing should be queued"),
    )
    runner.start()
    runner._thread.join(timeout=5)
    assert len(db.list_moments(monitor["id"])) == 1


def test_live_runner_capture_failure_is_published(monkeypatch):
    monitor = make_monitor()
    runner = LiveRunner(monitor)

    class BrokenRecorder(FakeRecorder):
        def slice(self, start, end, out):
            raise recorder.RecorderError("buffer gone")

    monkeypatch.setattr(
        "clipbait.live.orchestrator.build_recorder", lambda url, d, **kw: BrokenRecorder()
    )
    monkeypatch.setattr(
        "clipbait.live.orchestrator.resolve_chat_source",
        lambda url, provider, key: FakeChat([ChatMessage(t=5.2, text="W")]),
    )
    runner.detector = FakeDetector(confirmed_candidate())
    monkeypatch.setattr(runner, "elapsed", lambda: 1000.0)

    events: list[dict] = []
    monkeypatch.setattr(
        "clipbait.live.orchestrator._publish", lambda mid, event: events.append(event)
    )
    runner.start()
    runner._thread.join(timeout=5)
    for thread in runner._clip_threads:
        thread.join(timeout=5)

    # A buffer miss is a skip, not a crash: the reason lands on the moment and
    # nothing is queued for rendering.
    assert any(event["type"] == "capture_skipped" for event in events)
    assert any("buffer gone" in event.get("error", "") for event in events)
    assert not any(event["type"] == "clip_queued" for event in events)
    moment = db.list_moments(monitor["id"])[0]
    assert "buffer gone" in moment["signal"]["capture_error"]


def test_live_runner_unexpected_capture_error_is_published(monkeypatch):
    monitor = make_monitor()
    runner = LiveRunner(monitor)

    class BrokenRecorder(FakeRecorder):
        def slice(self, start, end, out):
            raise RuntimeError("ffmpeg segfaulted")

    monkeypatch.setattr(
        "clipbait.live.orchestrator.build_recorder", lambda url, d, **kw: BrokenRecorder()
    )
    monkeypatch.setattr(
        "clipbait.live.orchestrator.resolve_chat_source",
        lambda url, provider, key: FakeChat([ChatMessage(t=5.2, text="W")]),
    )
    runner.detector = FakeDetector(confirmed_candidate())
    monkeypatch.setattr(runner, "elapsed", lambda: 1000.0)

    events: list[dict] = []
    monkeypatch.setattr(
        "clipbait.live.orchestrator._publish", lambda mid, event: events.append(event)
    )
    runner.start()
    runner._thread.join(timeout=5)
    for thread in runner._clip_threads:
        thread.join(timeout=5)

    assert any(event["type"] == "capture_failed" for event in events)
    assert any("segfaulted" in event.get("error", "") for event in events)


def test_capture_clamps_a_moment_to_the_ring_buffer(monkeypatch):
    """A moment that starts before the first segment is trimmed, not dropped."""
    monitor = make_monitor()
    runner = LiveRunner(monitor)
    fake_recorder = FakeRecorder(window=(6.0, 40.0))
    runner.recorder = fake_recorder
    monkeypatch.setattr(runner, "elapsed", lambda: 1000.0)

    submitted: list[dict] = []
    monkeypatch.setattr(
        "clipbait.jobs.submit",
        lambda kind, fn, **kwargs: submitted.append({"kind": kind, **kwargs}) or {"id": "job_1"},
    )
    # Recorder only has 6.0 onward, so the head of the moment is trimmed.
    spike = dataclasses.replace(confirmed_candidate(), start=2.0, end=12.0, trigger_at=12.0)
    moment = db.create_moment(
        {
            "monitor_id": monitor["id"],
            "start": spike.start,
            "end": spike.end,
            "score": spike.score,
            "signal": spike.signal.model_dump(),
            "messages": [m.model_dump() for m in spike.messages],
        }
    )

    runner._capture(spike, moment, "clamped")

    assert fake_recorder.slices and fake_recorder.slices[0][:2] == (6.0, 12.0)
    stored = db.get("moments", moment["id"])
    assert (stored["start"], stored["end"]) == (6.0, 12.0)
    assert len(submitted) == 1
    payload = submitted[0]["args"][1]
    assert payload["start"] == 6.0
    assert [message["text"] for message in payload["messages"]] == ["W"]


def test_capture_skips_a_moment_the_buffer_never_saw(monkeypatch):
    monitor = make_monitor()
    runner = LiveRunner(monitor)
    fake_recorder = FakeRecorder(window=(120.0, 180.0))
    runner.recorder = fake_recorder
    monkeypatch.setattr(runner, "elapsed", lambda: 1000.0)
    monkeypatch.setattr(runner, "CAPTURE_WAIT_SECONDS", 0.0)

    events: list[dict] = []
    monkeypatch.setattr(
        "clipbait.live.orchestrator._publish", lambda mid, event: events.append(event)
    )
    monkeypatch.setattr(
        "clipbait.jobs.submit", lambda *a, **k: pytest.fail("nothing to render")
    )
    spike = confirmed_candidate()  # 5.0-9.0, long before the buffer starts at 120
    moment = db.create_moment(
        {
            "monitor_id": monitor["id"],
            "start": spike.start,
            "end": spike.end,
            "score": spike.score,
            "signal": spike.signal.model_dump(),
            "messages": [m.model_dump() for m in spike.messages],
        }
    )

    runner._capture(spike, moment, "too early")

    assert not fake_recorder.slices
    assert any(event["type"] == "capture_skipped" for event in events)
    stored = db.get("moments", moment["id"])
    assert "ring buffer" in stored["signal"]["capture_error"]


def test_a_chat_only_spike_is_not_clipped(monkeypatch):
    """Chat says *when* to look; the stream must say something to cut."""
    monitor = make_monitor()
    runner = LiveRunner(monitor)
    runner.recorder = FakeRecorder(window=(0.0, 60.0))
    monkeypatch.setattr(runner, "elapsed", lambda: 1000.0)

    events: list[dict] = []
    monkeypatch.setattr(
        "clipbait.live.orchestrator._publish", lambda mid, event: events.append(event)
    )
    monkeypatch.setattr(
        "clipbait.jobs.submit", lambda *a, **k: pytest.fail("a chat reflex is not a clip")
    )
    spike = candidate()  # chat only: speech, loudness and action are all zero
    moment = db.create_moment(
        {
            "monitor_id": monitor["id"],
            "start": spike.start,
            "end": spike.end,
            "score": spike.score,
            "signal": spike.signal.model_dump(),
            "messages": [m.model_dump() for m in spike.messages],
        }
    )

    runner._capture(spike, moment, "chat only")

    assert not runner.recorder.slices
    assert any(event["type"] == "capture_skipped" for event in events)
    stored = db.get("moments", moment["id"])
    assert "chat spike only" in stored["signal"]["capture_error"]


def test_chat_only_clips_can_be_opted_into():
    runner = LiveRunner(make_monitor(chat_only_clips=True))
    assert runner._clipworthy(SpikeSignal(rate=5.0, origin="chat")) is True


def test_a_chat_signal_with_stream_evidence_is_clipworthy():
    runner = LiveRunner(make_monitor())
    assert runner._clipworthy(SpikeSignal(rate=5.0, origin="chat")) is False
    assert runner._clipworthy(SpikeSignal(rate=5.0, speech=1.0, origin="chat")) is True
    assert runner._clipworthy(SpikeSignal(speech=2.0, origin="speech")) is True


def test_hls_recorder_rebases_the_first_segment_onto_the_monitor_clock(monkeypatch, tmp_path):
    """The first segment is stamped on the monitor clock, not at internal zero."""
    directory = tmp_path / "live"
    rec = recorder.HlsRecorder("https://youtube.com/watch?v=x", directory, origin=None)
    rec._started = 100.0
    rec._origin = 100.0
    # Capture began at 100.0; the first 4s segment is listed 24s later, so it
    # covers 20.0-24.0 on the monitor clock rather than 0.0-4.0.
    monkeypatch.setattr("clipbait.live.recorder.time.monotonic", lambda: 124.0)
    (directory / "seg_000000.ts").write_bytes(b"x")
    (directory / "segments.csv").write_text("seg_000000.ts,4.0\n", encoding="utf-8")
    rec._scan(directory / "segments.csv")
    assert [(s.start, s.end) for s in rec._segments] == [(20.0, 24.0)]

    # Later segments chain directly off the previous end.
    (directory / "seg_000001.ts").write_bytes(b"x")
    (directory / "segments.csv").write_text(
        "seg_000000.ts,4.0\nseg_000001.ts,4.0\n", encoding="utf-8"
    )
    rec._scan(directory / "segments.csv")
    assert [(s.start, s.end) for s in rec._segments] == [(20.0, 24.0), (24.0, 28.0)]


class StubRunner:
    instances: list[StubRunner] = []

    def __init__(self, monitor: dict) -> None:
        self.monitor = monitor
        self.monitor_id = monitor["id"]
        self.started = 0
        self.stopped = 0
        self._alive = True
        StubRunner.instances.append(self)

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1
        self._alive = False

    def alive(self) -> bool:
        return self._alive

    def stats(self) -> dict:
        return {"messages": 7, "clips": 0}


@pytest.fixture()
def stub_runners(monkeypatch):
    StubRunner.instances = []
    monkeypatch.setattr("clipbait.live.orchestrator.LiveRunner", StubRunner)
    monkeypatch.setattr("clipbait.live.orchestrator._RUNNERS", {})
    yield StubRunner
    monkeypatch.setattr("clipbait.live.orchestrator._RUNNERS", {})


def test_registry_lifecycle(stub_runners):
    monitor = make_monitor()
    first = live.start(monitor["id"])
    assert first["id"] == monitor["id"]
    assert len(stub_runners.instances) == 1
    assert stub_runners.instances[0].started == 1

    live.start(monitor["id"])  # already alive: no second runner
    assert len(stub_runners.instances) == 1

    assert live.status(monitor["id"]) == {"messages": 7, "clips": 0}
    assert live.active_monitors() == [monitor["id"]]

    stopped = live.stop(monitor["id"])
    assert stopped["status"] == "stopped"
    assert stub_runners.instances[0].stopped == 1
    assert live.active_monitors() == []
    # No live runner left: status falls back to the persisted row.
    assert live.status(monitor["id"]) == {}

    with pytest.raises(ValueError, match="unknown monitor"):
        live.start("mon_missing")
    with pytest.raises(ValueError, match="unknown monitor"):
        live.stop("mon_missing")


def test_registry_stop_all(stub_runners):
    one = make_monitor()
    two = make_monitor()
    live.start(one["id"])
    live.start(two["id"])
    live.stop_all()
    assert live.active_monitors() == []
    assert all(runner.stopped == 1 for runner in stub_runners.instances)


def test_status_uses_persisted_stats_when_not_alive(stub_runners, monkeypatch):
    monitor = make_monitor()
    live.start(monitor["id"])
    runner = stub_runners.instances[0]
    runner._alive = False
    db.update_monitor(monitor["id"], stats={"messages": 3})
    assert live.status(monitor["id"]) == {"messages": 3}


# --------------------------------------------------------------- content fusion


def test_chat_and_content_within_twelve_seconds_fuse(monkeypatch):
    monitor = make_monitor(auto_clip=False)
    runner = LiveRunner(monitor)
    events: list[dict] = []
    monkeypatch.setattr(
        "clipbait.live.orchestrator._publish", lambda mid, event: events.append(event)
    )

    runner._on_candidate(candidate(), "chat", 9.0)
    content = MomentCandidate(
        start=2.0,
        end=30.0,
        trigger_at=15.0,
        score=3.4,
        signal=SpikeSignal(
            speech=4.2,
            loudness=1.1,
            quote="NO WAY! I can't believe it",
            reasons=["hype language: no way", "loudness spike"],
            origin="speech",
        ),
        messages=[ChatMessage(t=14.0, author="c", text="NO WAY")],
    )
    runner._on_candidate(content, "content", 15.0)

    moments = db.list_moments(monitor["id"])
    assert len(moments) == 1
    signal = moments[0]["signal"]
    assert signal["origin"] == "fused"
    assert signal["speech"] > 0 and signal["rate"] > 0
    assert signal["quote"].startswith("NO WAY")
    assert signal["score"] > content.score
    assert [event["type"] for event in events] == ["spike", "spike_fused"]


def test_distinct_content_moments_are_separate_clips(monkeypatch):
    monitor = make_monitor(auto_clip=False)
    runner = LiveRunner(monitor)
    monkeypatch.setattr("clipbait.live.orchestrator._publish", lambda mid, event: None)

    first = MomentCandidate(
        start=0.0,
        end=20.0,
        trigger_at=10.0,
        score=3.0,
        signal=SpikeSignal(speech=3.0, quote="wait what", origin="speech"),
        messages=[],
    )
    second = MomentCandidate(
        start=80.0,
        end=110.0,
        trigger_at=100.0,
        score=3.2,
        signal=SpikeSignal(action=3.0, origin="action"),
        messages=[],
    )
    runner._on_candidate(first, "content", 10.0)
    runner._on_candidate(second, "content", 100.0)

    moments = db.list_moments(monitor["id"])
    assert len(moments) == 2
    assert {moment["signal"]["origin"] for moment in moments} == {"speech", "action"}


def test_content_candidate_pulls_chat_evidence_from_the_detector(monkeypatch):
    monitor = make_monitor(auto_clip=False)
    runner = LiveRunner(monitor)
    monkeypatch.setattr("clipbait.live.orchestrator._publish", lambda mid, event: None)
    runner.detector._message_log = [
        ChatMessage(t=6.0, author="a", text="inside"),
        ChatMessage(t=40.0, author="b", text="outside"),
    ]

    content = MomentCandidate(
        start=5.0,
        end=30.0,
        trigger_at=12.0,
        score=3.1,
        signal=SpikeSignal(speech=3.1, quote="look at this", origin="speech"),
        messages=[],
    )
    runner._on_candidate(content, "content", 12.0)

    moment = db.list_moments(monitor["id"])[0]
    assert [message["text"] for message in moment["messages"]] == ["inside"]


def test_the_monitor_buffer_option_controls_what_ffmpeg_keeps(tmp_path) -> None:
    """Content analysis can finish minutes late; the ring must outlast it."""
    rec = recorder.HlsRecorder(
        "https://www.youtube.com/watch?v=x", tmp_path, buffer_seconds=600
    )
    assert rec.buffer_seconds == 600
    assert recorder.build_recorder(
        "https://www.youtube.com/watch?v=x", tmp_path, buffer_seconds=500
    ).buffer_seconds == 500


def test_a_chat_reflex_cannot_swallow_the_on_stream_moment(monkeypatch):
    """Chat can start the cooldown; on-stream evidence must still be kept."""
    monitor = make_monitor(auto_clip=False)
    runner = LiveRunner(monitor)
    monkeypatch.setattr("clipbait.live.orchestrator._publish", lambda mid, event: None)

    runner._on_candidate(candidate(), "chat", 9.0)  # reaction spam at t=9
    content = MomentCandidate(
        start=28.0,
        end=60.0,
        trigger_at=40.0,  # 31s later: past the 12s fuse, inside the 45s cooldown
        score=3.9,
        signal=SpikeSignal(
            speech=4.4,
            loudness=1.2,
            quote="oh my god he actually did it",
            reasons=["hype language"],
            origin="speech",
        ),
        messages=[],
    )
    runner._on_candidate(content, "content", 40.0)

    moments = db.list_moments(monitor["id"])
    assert len(moments) == 1, "the real moment must merge, not vanish or duplicate"
    signal = moments[0]["signal"]
    assert signal["origin"] == "fused"
    assert signal["speech"] == pytest.approx(4.4)
    assert signal["quote"] == "oh my god he actually did it"
    assert signal["rate"] > 0


def test_chat_after_content_still_respects_the_cooldown(monkeypatch):
    """The reverse order must not turn one moment into two clips."""
    monitor = make_monitor(auto_clip=False)
    runner = LiveRunner(monitor)
    monkeypatch.setattr("clipbait.live.orchestrator._publish", lambda mid, event: None)

    content = MomentCandidate(
        start=0.0,
        end=30.0,
        trigger_at=12.0,
        score=3.5,
        signal=SpikeSignal(speech=3.5, quote="no way", origin="speech"),
        messages=[],
    )
    runner._on_candidate(content, "content", 12.0)
    runner._on_candidate(candidate(), "chat", 40.0)

    assert len(db.list_moments(monitor["id"])) == 1


def test_a_live_clip_inherits_the_stream_language(monkeypatch):
    """An English stream must not be captioned in whatever whisper guessed."""
    monitor = make_monitor(language="en")
    runner = LiveRunner(monitor)
    runner.recorder = FakeRecorder(window=(0.0, 60.0))
    monkeypatch.setattr(runner, "elapsed", lambda: 1000.0)
    monkeypatch.setattr(
        "clipbait.jobs.submit",
        lambda kind, fn, **kwargs: {"id": "job_1"},
    )
    created: dict = {}
    real_create = db.create_project

    def spy(payload):
        project = real_create(payload)
        created["project"] = project
        return project

    monkeypatch.setattr("clipbait.db.create_project", spy)

    spike = dataclasses.replace(confirmed_candidate(), start=2.0, end=12.0, trigger_at=12.0)
    moment = db.create_moment(
        {
            "monitor_id": monitor["id"],
            "start": spike.start,
            "end": spike.end,
            "score": spike.score,
            "signal": spike.signal.model_dump(),
            "messages": [m.model_dump() for m in spike.messages],
        }
    )

    runner._capture(spike, moment, "lang")

    assert (created["project"].get("options") or {}).get("language") == "en"


def test_a_live_clip_carries_the_stream_hashtags(monkeypatch):
    """Stream tags must reach the clip so its copy is about this stream."""
    monitor = make_monitor(hashtags=["kaicenat", "gta6"])
    runner = LiveRunner(monitor)
    runner.recorder = FakeRecorder(window=(0.0, 60.0))
    monkeypatch.setattr(runner, "elapsed", lambda: 1000.0)
    monkeypatch.setattr(
        "clipbait.jobs.submit",
        lambda kind, fn, **kwargs: {"id": "job_1"},
    )
    created: dict = {}
    real_create = db.create_project

    def spy(payload):
        project = real_create(payload)
        created["project"] = project
        return project

    monkeypatch.setattr("clipbait.db.create_project", spy)

    spike = dataclasses.replace(confirmed_candidate(), start=2.0, end=12.0, trigger_at=12.0)
    moment = db.create_moment(
        {
            "monitor_id": monitor["id"],
            "start": spike.start,
            "end": spike.end,
            "score": spike.score,
            "signal": spike.signal.model_dump(),
            "messages": [m.model_dump() for m in spike.messages],
        }
    )

    runner._capture(spike, moment, "tags")

    assert (created["project"].get("options") or {}).get("hashtags") == ["kaicenat", "gta6"]


# ------------------------------------------------------------- auto-publish


class _SyncThread:
    """A Thread that runs its target on start(), so tests stay deterministic."""

    def __init__(self, target=None, daemon=None, name=None, **_kw):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


def _ready_clip(score: int) -> dict:
    project = db.create_project({"title": "live publish", "source_kind": "live", "duration": 45.0})
    return db.create_clip(
        {
            "project_id": project["id"],
            "title": "moment",
            "start": 5.0,
            "end": 45.0,
            "score": score,
            "status": "ready",
            "render_path": "projects/x/clip.mp4",
        }
    )


def test_autopublish_posts_a_ready_clip_immediately(monkeypatch):
    from clipbait import publish as publish_mod
    from clipbait.publish.provider import PublishResult

    monitor = make_monitor(
        auto_publish=True, post_now=True, publish_min_score=20, platform="shorts"
    )
    runner = LiveRunner(monitor)
    clip = _ready_clip(33)

    calls: dict = {}

    def fake_publish(clip_id, *, options=None, provider=None):
        calls["clip_id"] = clip_id
        calls["live"] = options.live
        calls["post_now"] = options.post_now
        calls["platforms"] = options.platforms
        return PublishResult(ok=True, url="https://youtu.be/x")

    monkeypatch.setattr(publish_mod, "publish_clip", fake_publish)
    monkeypatch.setattr(publish_mod, "publish_provider", lambda *a, **k: object())
    monkeypatch.setattr("clipbait.live.orchestrator.threading.Thread", _SyncThread)

    runner._autopublish(clip["id"], "mom_1")

    # Posts immediately, to the run's chosen destination (the connected channel),
    # not the money layer's recommended platform.
    assert calls == {
        "clip_id": clip["id"],
        "live": True,
        "post_now": True,
        "platforms": ("shorts",),
    }


def test_autopublish_respects_the_score_floor(monkeypatch):
    from clipbait import publish as publish_mod

    monitor = make_monitor(auto_publish=True, publish_min_score=40)
    runner = LiveRunner(monitor)
    clip = _ready_clip(22)

    called: list = []
    monkeypatch.setattr(publish_mod, "publish_clip", lambda *a, **k: called.append(1))
    monkeypatch.setattr(publish_mod, "publish_provider", lambda *a, **k: object())
    monkeypatch.setattr("clipbait.live.orchestrator.threading.Thread", _SyncThread)

    runner._autopublish(clip["id"], "mom_1")

    assert called == []  # below the floor: never sent


def test_autopublish_survives_a_provider_error(monkeypatch):
    from clipbait import publish as publish_mod

    monitor = make_monitor(auto_publish=True)
    runner = LiveRunner(monitor)
    clip = _ready_clip(30)

    def boom(*a, **k):
        raise RuntimeError("postiz down")

    monkeypatch.setattr(publish_mod, "publish_clip", boom)
    monkeypatch.setattr(publish_mod, "publish_provider", lambda *a, **k: object())
    monkeypatch.setattr("clipbait.live.orchestrator.threading.Thread", _SyncThread)

    runner._autopublish(clip["id"], "mom_1")  # must not raise


def test_autopublish_serializes_uploads(monkeypatch):
    """Two clips landing together must not hand the provider two uploads at once."""
    from clipbait import publish as publish_mod
    from clipbait.live import orchestrator as orch
    from clipbait.publish.provider import PublishResult

    monitor = make_monitor(auto_publish=True, platform="shorts")
    runner = LiveRunner(monitor)
    clip = _ready_clip(30)

    seen: dict = {}

    def fake_publish(clip_id, *, options=None, provider=None):
        seen["locked_during_upload"] = orch._PUBLISH_LOCK.locked()
        return PublishResult(ok=True)

    monkeypatch.setattr(publish_mod, "publish_clip", fake_publish)
    monkeypatch.setattr(publish_mod, "publish_provider", lambda *a, **k: object())
    monkeypatch.setattr("clipbait.live.orchestrator.threading.Thread", _SyncThread)

    runner._autopublish(clip["id"], "mom_1")

    assert seen["locked_during_upload"] is True  # held while the provider is called
    assert not orch._PUBLISH_LOCK.locked()  # and released afterwards
