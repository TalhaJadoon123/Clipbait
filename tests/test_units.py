"""Unit coverage for the foundation modules.

These are the small, dependency-free pieces every other layer leans on: the
event bus, the job runner, storage paths, SQLite encoding, settings parsing,
ffmpeg wrappers, reframe geometry, the meme providers and the SSRF guard.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import make_video

from clipbait import config, db, events, jobs, media, net_guard, storage
from clipbait.memes import assets as meme_assets
from clipbait.memes import providers as mp
from clipbait.reframe import (
    ReframePlan,
    _clamp,
    _interpolate,
    _smooth,
    plan_reframe,
    probe_source,
    split_layout_centres,
)
from clipbait.reframe.tracker import (
    FaceSample,
    available,
    sample_faces,
    split_positions,
    two_speaker_positions,
)


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------- events


def test_event_bus_history_and_subscribers():
    bus = events.EventBus(history=3)
    assert bus.snapshot("none") == []

    bus.publish("chan", {"type": "a"})
    queue = bus.subscribe("chan")
    bus.publish("chan", {"type": "b"})

    pushed = queue.get_nowait()
    assert pushed["type"] == "b"
    assert "ts" in pushed

    assert [event["type"] for event in bus.snapshot("chan")] == ["a", "b"]
    bus.unsubscribe("chan", queue)
    bus.publish("chan", {"type": "c"})
    assert queue.empty()


def test_event_bus_history_is_bounded():
    bus = events.EventBus(history=2)
    for index in range(5):
        bus.publish("chan", {"type": str(index)})
    assert [event["type"] for event in bus.snapshot("chan")] == ["3", "4"]


def test_event_bus_slow_subscriber_drops_oldest():
    bus = events.EventBus()
    queue = bus.subscribe("chan")
    for index in range(300):
        bus.publish("chan", {"type": str(index)})
    assert queue.qsize() == 256
    assert queue.get_nowait()["type"] == "44"  # first 44 were dropped


def test_event_bus_delivers_on_a_running_loop():
    bus = events.EventBus()

    async def scenario():
        bus.bind_loop(asyncio.get_running_loop())
        queue = bus.subscribe("chan")
        bus.publish("chan", {"type": "hello"})
        await asyncio.sleep(0.01)
        return queue.get_nowait()

    assert asyncio.run(scenario())["type"] == "hello"


# ------------------------------------------------------------------------ jobs


@pytest.fixture()
def job_runner():
    yield
    jobs.shutdown(wait=True)


def test_progress_without_a_job_is_a_noop():
    jobs.progress(None, 0.5, "step", "message")


def test_job_success_and_on_done(job_runner):
    done: list[dict] = []

    def work(job_id=None):
        jobs.progress(job_id, 0.5, "halfway", "working")
        return {"ok": job_id}

    job = jobs.submit("unit", work, on_done=lambda row, result: done.append(result))

    assert wait_for(lambda: db.get_job(job["id"])["status"] == "completed")
    row = db.get_job(job["id"])
    assert row["progress"] == 1.0
    assert row["step"] == "completed"
    assert done and done[0]["ok"] == job["id"]


def test_job_failure_is_persisted(job_runner):
    def explode(job_id=None):
        raise ValueError("nope")

    job = jobs.submit("unit", explode)
    assert wait_for(lambda: db.get_job(job["id"])["status"] == "failed")
    row = db.get_job(job["id"])
    assert "ValueError: nope" in row["error"]


def test_job_cancellation(job_runner):
    gate = threading.Event()

    def blocking(job_id=None):
        gate.wait(5)
        jobs.progress(job_id, 0.5, "step")  # must raise JobCancelled

    job = jobs.submit("unit", blocking)
    jobs.cancel(job["id"])
    assert jobs.is_cancelled(job["id"])
    gate.set()

    assert wait_for(lambda: db.get_job(job["id"])["status"] == "cancelled")
    assert wait_for(lambda: not jobs.is_cancelled(job["id"]))


def test_cancel_before_start_skips_work(job_runner, monkeypatch):
    ran = []

    class Deferred:
        def __init__(self):
            self.fn = None

        def submit(self, fn):
            self.fn = fn

    deferred = Deferred()
    monkeypatch.setattr(jobs, "executor", lambda: deferred)

    def work(job_id=None):
        ran.append(job_id)

    job = jobs.submit("unit", work)
    jobs.cancel(job["id"])
    assert deferred.fn is not None
    deferred.fn()  # the job starts *after* the cancel

    assert ran == []
    assert db.get_job(job["id"])["status"] == "cancelled"
    assert not jobs.is_cancelled(job["id"])


def test_executor_is_lazy_and_restartable():
    jobs.shutdown(wait=True)
    executor = jobs.executor()
    assert executor is not None
    assert jobs.executor() is executor
    jobs.shutdown(wait=True)
    assert jobs.executor() is not executor
    jobs.shutdown(wait=True)


# --------------------------------------------------------------------- storage


def test_safe_filename():
    assert storage.safe_filename("../../etc/passwd") == "..-..-etc-passwd"
    assert storage.safe_filename("") == "file"
    assert storage.safe_filename("a" * 300).__len__() == 120


def test_upload_and_json_roundtrip(tmp_path):
    saved = storage.save_upload("prj_up", "my video.mp4", _BytesReader(b"abc"))
    assert saved.exists() and saved.read_bytes() == b"abc"
    assert " " not in saved.name

    path = tmp_path / "nested" / "data.json"
    storage.write_json(path, {"é": 1})
    assert storage.read_json(path) == {"é": 1}

    path.write_text("{broken", encoding="utf-8")
    assert storage.read_json(path, default="fallback") == "fallback"
    assert storage.read_json(tmp_path / "missing.json", default=None) is None


class _BytesReader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, _size: int = -1) -> bytes:
        payload, self.payload = self.payload, b""
        return payload


def test_storage_paths_and_relative(tmp_path, monkeypatch):
    settings = dataclasses.replace(config.get_settings(), data_dir=tmp_path)
    monkeypatch.setattr(storage, "get_settings", lambda: settings)

    assert storage.relative(tmp_path / "a" / "b.mp4") == "a/b.mp4"
    outside = Path("/tmp/somewhere-else.mp4")
    assert storage.relative(outside) == str(outside)
    assert storage.resolve("a/b.mp4") == tmp_path / "a" / "b.mp4"
    assert storage.resolve(str(outside)) == outside

    assert storage.transcript_path("p").name == "transcript.json"
    assert storage.source_path("p").name == "video.mp4"
    assert storage.audio_path("p").name == "audio.wav"
    assert storage.clip_dir("p", "c").is_dir()

    directory = storage.project_dir("p")
    assert directory.is_dir()
    storage.remove_project("p")
    assert not directory.exists()


# -------------------------------------------------------------------------- db


def test_project_crud_and_defaults():
    project = db.create_project({"title": "T", "source_kind": "local"})
    assert project["status"] == "queued"
    assert project["options"] == {}

    assert db.get_project(project["id"])["title"] == "T"
    assert any(row["id"] == project["id"] for row in db.list_projects())

    updated = db.update("projects", project["id"], {"title": "New", "duration": 9.5})
    assert updated["title"] == "New"
    assert updated["duration"] == 9.5
    assert db.update("projects", "prj_missing", {"title": "x"}) is None

    db.delete_project(project["id"])
    assert db.get_project(project["id"]) is None


def test_clips_cascade_on_project_delete():
    project = db.create_project({"title": "T", "source_kind": "local"})
    db.create_clip(
        {"project_id": project["id"], "title": "c", "start": 0.0, "end": 5.0, "score": 10}
    )
    db.create_clip(
        {"project_id": project["id"], "title": "c2", "start": 0.0, "end": 5.0, "score": 90}
    )
    ordered = db.list_clips(project["id"])
    assert [clip["title"] for clip in ordered] == ["c2", "c"]

    db.delete_project(project["id"])
    assert db.list_clips(project["id"]) == []


def test_bool_and_json_field_encoding():
    project = db.create_project({"title": "T", "source_kind": "local"})
    clip = db.create_clip(
        {
            "project_id": project["id"],
            "title": "c",
            "start": 0.0,
            "end": 5.0,
            "captions": False,
            "hook_titles": True,
            "meta": {"nested": [1, 2]},
            "hashtags": ["#a"],
        }
    )
    stored = db.get_clip(clip["id"])
    assert stored["captions"] is False
    assert stored["hook_titles"] is True
    assert stored["meta"] == {"nested": [1, 2]}
    assert stored["hashtags"] == ["#a"]


def test_corrupt_json_decodes_defensively():
    project = db.create_project({"title": "T", "source_kind": "local"})
    with db.connect() as conn:
        conn.execute(
            "UPDATE projects SET options = ?, error = NULL WHERE id = ?",
            ("{not json", project["id"]),
        )
    assert db.get_project(project["id"])["options"] == {}

    clip = db.create_clip(
        {"project_id": project["id"], "title": "c", "start": 0.0, "end": 1.0}
    )
    with db.connect() as conn:
        conn.execute("UPDATE clips SET hashtags = 'zzz', meta = 'zzz' WHERE id = ?", (clip["id"],))
    stored = db.get_clip(clip["id"])
    assert stored["hashtags"] == []
    assert stored["meta"] == {}


def test_job_queries_and_updates():
    job = db.create_job(
        {"kind": "process", "project_id": "prj_x", "monitor_id": "mon_x", "step": "queued"}
    )
    assert db.get_job(job["id"])["kind"] == "process"
    assert [row["id"] for row in db.list_jobs(project_id="prj_x")] == [job["id"]]
    assert [row["id"] for row in db.list_jobs(monitor_id="mon_x")] == [job["id"]]
    assert any(row["id"] == job["id"] for row in db.list_jobs())

    updated = db.update_job(job["id"], status="running", progress=0.25)
    assert updated["status"] == "running"
    assert updated["progress"] == 0.25
    assert db.update_job("job_missing", status="running") is None


def test_moments_and_chat_events():
    monitor = db.create_monitor({"url": "demo://x", "title": "m"})
    moment = db.create_moment(
        {
            "monitor_id": monitor["id"],
            "start": 1.0,
            "end": 2.0,
            "score": 3.0,
            "signal": {"rate": 1},
            "messages": [{"t": 1.0, "text": "hi"}],
        }
    )
    assert db.list_moments(monitor["id"])[0]["id"] == moment["id"]
    db.attach_clip_to_moment(moment["id"], "clip_1")
    assert db.list_moments(monitor["id"])[0]["clip_id"] == "clip_1"

    db.add_chat_events(monitor["id"], [{"t": 0.0, "author": "a", "text": "old"}])
    db.add_chat_events(monitor["id"], [{"t": 100.0, "author": "b", "text": "new"}])
    db.add_chat_events(monitor["id"], [])  # no-op
    assert [row["text"] for row in db.list_chat_events(monitor["id"])] == ["old", "new"]
    assert [row["text"] for row in db.list_chat_events(monitor["id"], since=50)] == ["new"]

    db.prune_chat_events(monitor["id"], keep_seconds=10)
    assert [row["text"] for row in db.list_chat_events(monitor["id"])] == ["new"]

    db.update_monitor(monitor["id"], status="live", stats={"messages": 2})
    assert db.get_monitor(monitor["id"])["stats"] == {"messages": 2}
    assert any(row["id"] == monitor["id"] for row in db.list_monitors())

    db.delete("monitors", monitor["id"])
    assert db.get_monitor(monitor["id"]) is None
    assert db.list_moments(monitor["id"]) == []


# ---------------------------------------------------------------------- config


def test_env_helpers(monkeypatch):
    monkeypatch.setenv("UNIT_TEXT", "  hello  ")
    assert config._env("UNIT_TEXT") == "hello"
    monkeypatch.setenv("UNIT_TEXT", "   ")
    assert config._env("UNIT_TEXT", "fallback") == "fallback"
    monkeypatch.delenv("UNIT_TEXT", raising=False)
    assert config._env("UNIT_TEXT") is None

    monkeypatch.setenv("UNIT_INT", "7")
    assert config._env_int("UNIT_INT", 1) == 7
    monkeypatch.setenv("UNIT_INT", "bad")
    assert config._env_int("UNIT_INT", 1) == 1

    monkeypatch.delenv("UNIT_BOOL", raising=False)
    assert config._env_bool("UNIT_BOOL", True) is True
    monkeypatch.setenv("UNIT_BOOL", "YES")
    assert config._env_bool("UNIT_BOOL") is True
    monkeypatch.setenv("UNIT_BOOL", "0")
    assert config._env_bool("UNIT_BOOL", True) is False

    monkeypatch.setenv("UNIT_FLOAT", "1.25")
    assert config._env_float("UNIT_FLOAT", 0.0) == 1.25
    monkeypatch.setenv("UNIT_FLOAT", "nope")
    assert config._env_float("UNIT_FLOAT", 0.5) == 0.5


def test_settings_dirs_and_capabilities(tmp_path, monkeypatch):
    settings = dataclasses.replace(config.get_settings(), data_dir=tmp_path)
    settings.ensure_dirs()
    for directory in (tmp_path, settings.projects_dir, settings.live_dir, settings.models_dir):
        assert directory.is_dir()

    caps = config.detect_capabilities(settings)
    assert caps.ffmpeg is True
    assert caps.transcriber in {"whisper", "mock"}
    assert isinstance(caps.as_dict(), dict)

    no_llm = config.detect_capabilities(
        dataclasses.replace(
            settings, llm_provider="openai", openai_api_key=None, transcriber="mock"
        )
    )
    assert no_llm.llm_provider is None
    assert no_llm.transcriber == "mock"

    for provider, field in (
        ("openai", "openai_api_key"),
        ("anthropic", "anthropic_api_key"),
        ("gemini", "gemini_api_key"),
    ):
        configured = config.detect_capabilities(
            dataclasses.replace(settings, llm_provider=provider, **{field: "key"})
        )
        assert configured.llm_provider == provider

    assert config.detect_capabilities(
        dataclasses.replace(settings, llm_provider="ollama")
    ).llm_provider == "ollama"


def test_meme_provider_name_is_reported(monkeypatch):
    settings = dataclasses.replace(
        config.get_settings(), meme_provider="fixture", meme_api_url=None
    )
    assert config._meme_provider_name(settings) == "fixture"
    assert config._meme_provider_name(
        dataclasses.replace(settings, meme_provider="none")
    ) is None


def test_has_module():
    assert config._has_module("json") is True
    assert config._has_module("not_a_real_module_zzz") is False


# ----------------------------------------------------------------------- media


def test_run_errors(monkeypatch):
    with pytest.raises(media.MediaError, match="missing binary"):
        media.run(["definitely-not-a-real-binary-zzz"])

    with pytest.raises(media.MediaError, match="timed out"):
        media.run([sys.executable, "-c", "import time; time.sleep(3)"], timeout=1)

    with pytest.raises(media.MediaError, match="failed"):
        media.run([sys.executable, "-c", "raise SystemExit(3)"])

    ok = media.run([sys.executable, "-c", "raise SystemExit(3)"], check=False)
    assert ok.returncode == 3


def test_probe_real_file_and_bad_json(tmp_path, monkeypatch):
    source = make_video(tmp_path / "p.mp4", seconds=1, size="160x90", fps=25)
    info = media.probe(source)
    assert info.width == 160 and info.height == 90
    assert info.fps == pytest.approx(25.0, abs=0.5)
    assert info.has_audio is True
    assert info.duration == pytest.approx(1.0, abs=0.2)
    assert info.aspect == pytest.approx(160 / 90)

    monkeypatch.setattr(media, "run", lambda *a, **k: SimpleNamespace(stdout="{bad"))
    with pytest.raises(media.MediaError, match="invalid JSON"):
        media.probe(source)


def test_probe_parses_odd_metadata(tmp_path, monkeypatch):
    payload = {
        "format": {"duration": "12.5"},
        "streams": [
            {
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30000/1001",
                "codec_name": "h264",
            }
        ],
    }
    monkeypatch.setattr(
        media, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(payload))
    )
    info = media.probe(Path("x.mp4"))
    assert info.duration == 12.5
    assert info.fps == pytest.approx(29.97, abs=0.01)
    assert info.has_audio is False
    assert info.codec == "h264"

    broken = {
        "format": {},
        "streams": [{"codec_type": "video", "duration": "4", "avg_frame_rate": "bad"}],
    }
    monkeypatch.setattr(
        media, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(broken))
    )
    info = media.probe(Path("x.mp4"))
    assert info.duration == 4.0
    assert info.fps == 30.0


def test_audio_rms_and_extract(tmp_path):
    source = make_video(tmp_path / "a.mp4", seconds=2, size="160x90")
    audio = media.extract_audio(source, tmp_path / "out" / "audio.wav")
    assert audio.exists()

    rms = media.audio_rms(audio, window=0.25)
    assert rms and all(0.0 <= value <= 1.0 for _, value in rms)
    assert rms[0][0] == 0.0

    junk = tmp_path / "junk.wav"
    junk.write_text("not audio")
    assert media.audio_rms(junk) == []


def test_thumbnail(tmp_path, monkeypatch):
    source = make_video(tmp_path / "t.mp4", seconds=1, size="160x90")
    target = tmp_path / "thumb.jpg"
    assert media.thumbnail(source, 0.2, target) == target
    assert target.exists()

    monkeypatch.setattr(
        media, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="")
    )
    assert media.thumbnail(source, 0.2, tmp_path / "nope.jpg") is None


def test_parse_progress():
    assert media.parse_progress("out_time_ms=1500000") == 1.5
    assert media.parse_progress("out_time_ms=0") == 0.0
    assert media.parse_progress("frame=12") is None
    assert media.parse_progress("out_time_ms=abc") is None


def test_escape_filter_path():
    escaped = media.escape_filter_path(Path("/a b:c'd,e\\f"))
    assert r"\:" in escaped
    assert r"\'" in escaped
    assert r"\," in escaped
    assert "\\\\" in escaped


def test_hw_encoder(monkeypatch):
    media._ENCODER_CACHE.clear()
    assert media.hw_encoder("cpu") == media._CPU_ENCODER
    assert media.hw_encoder("not-a-real-encoder") == media._CPU_ENCODER
    # No GPU here, so an explicitly requested hardware encoder must degrade.
    assert media.hw_encoder("nvenc") == media._CPU_ENCODER
    assert "libx264" in media.hw_encoder("auto")
    media._ENCODER_CACHE.clear()


# --------------------------------------------------------------------- reframe


def make_plan(**overrides) -> ReframePlan:
    data = {
        "mode": "track",
        "out_w": 1080,
        "out_h": 1920,
        "source_w": 1920,
        "source_h": 1080,
        "crop_xs": [0, 0, 40, 40, 80],
        "fps": 30.0,
    }
    data.update(overrides)
    return ReframePlan(**data)


def test_sendcmd_dedupes_change_points(tmp_path):
    plan = make_plan()
    commands = plan.sendcmd()
    assert commands.splitlines() == [
        "0.0000 crop@cam x 0;",
        "0.0667 crop@cam x 40;",
        "0.1333 crop@cam x 80;",
    ]
    path = tmp_path / "sub" / "crop.cmd"
    returned = plan.with_sendcmd(path)
    assert returned.sendcmd_path == str(path)
    assert path.read_text().startswith("0.0000")
    assert make_plan(crop_xs=[]).sendcmd() == ""


def test_geometry_helpers():
    assert _clamp(5, 0, 3) == 3
    assert _clamp(-1, 0, 3) == 0
    assert _smooth([]) == []
    assert len(_smooth([0.1, 0.9], radius=1)) == 2

    assert _interpolate([], 30.0, 3) == ([0.5, 0.5, 0.5], False)
    xs, ok = _interpolate([FaceSample(0.0, 0.2, 0.5, 0.3)], 30.0, 3)
    assert ok is True and xs == [0.2, 0.2, 0.2]

    samples = [FaceSample(0.0, 0.0, 0.5, 0.2), FaceSample(1.0, 1.0, 0.5, 0.2)]
    xs, ok = _interpolate(samples, 2.0, 3)
    assert ok is True
    assert xs[0] == 0.0 and xs[-1] == pytest.approx(1.0)


def test_plan_reframe_modes(monkeypatch):
    portrait = plan_reframe(Path("v.mp4"), 0, 10, 1080, 1920)
    assert portrait.mode == "blur"
    assert portrait.note

    explicit = plan_reframe(Path("v.mp4"), 0, 10, 1920, 1080, mode="blur")
    assert explicit.mode == "blur"

    wide = plan_reframe(Path("v.mp4"), 0, 10, 320, 180, mode="auto")
    assert wide.mode == "blur"
    assert "blur bed" in wide.note

    # A crop that only trims a little is still the better answer.
    slight = plan_reframe(Path("v.mp4"), 0, 10, 800, 1000, mode="auto")
    assert slight.mode == "center"
    assert slight.note == "no faces detected; centre crop"

    monkeypatch.setattr(
        "clipbait.reframe.sample_faces",
        lambda *a, **k: [
            FaceSample(i * 0.1, 0.3 + (i % 3) * 0.2, 0.5, 0.2) for i in range(12)
        ],
    )
    tracked = plan_reframe(Path("v.mp4"), 0, 4, 1920, 1080, mode="track")
    assert tracked.mode == "track"
    assert len(tracked.crop_xs) >= 100
    assert tracked.note.startswith("tracked")

    split = plan_reframe(Path("v.mp4"), 0, 4, 1920, 1080, mode="split")
    assert split.mode == "split"
    assert split.split is not None

    monkeypatch.setattr("clipbait.reframe.sample_faces", lambda *a, **k: [])
    assert plan_reframe(Path("v.mp4"), 0, 4, 1920, 1080, mode="split").mode == "blur"
    assert plan_reframe(Path("v.mp4"), 0, 4, 1920, 1080, mode="track").mode == "blur"


def test_narrow_source_is_padded_by_geometry(monkeypatch):
    plan = plan_reframe(Path("v.mp4"), 0, 2, 400, 1000, mode="center")
    assert plan.mode == "center"
    assert plan.crop_xs == [0]


def test_split_layout_centres(monkeypatch):
    monkeypatch.setattr("clipbait.reframe.sample_faces", lambda *a, **k: [])
    assert split_layout_centres(Path("v.mp4"), 0, 1) is None


def test_probe_source(tmp_path):
    source = make_video(tmp_path / "probe.mp4", seconds=1, size="160x90")
    assert probe_source(source).width == 160


def test_tracker_without_opencv(monkeypatch, tmp_path):
    # Simulate an absent OpenCV so the fallback path is exercised even in a venv
    # that installed the vision extra; a bare `available() is False` assertion
    # only held when OpenCV happened to be missing.
    monkeypatch.setitem(sys.modules, "cv2", None)
    assert available() is False
    source = make_video(tmp_path / "faces.mp4", seconds=1, size="160x90")
    assert sample_faces(source, 0, 1) == []


def test_two_speaker_and_split_positions():
    assert two_speaker_positions([]) is None

    single = [FaceSample(0.0, 0.5, 0.5, 0.2) for _ in range(10)]
    assert two_speaker_positions(single) is None

    two = [FaceSample(i, 0.2, 0.5, 0.2) for i in range(6)] + [
        FaceSample(6 + i, 0.8, 0.5, 0.2) for i in range(6)
    ]
    left, right = two_speaker_positions(two)
    assert left == pytest.approx(0.2) and right == pytest.approx(0.8)

    assert split_positions([FaceSample(0, 0.5, 0.5, 0.2)] * 3) is None
    assert split_positions([FaceSample(0, 0.5, 0.5, 0.2)] * 10) is None
    clusters = [FaceSample(i, 0.15, 0.5, 0.2) for i in range(6)] + [
        FaceSample(6 + i, 0.85, 0.5, 0.2) for i in range(6)
    ]
    split = split_positions(clusters)
    assert split is not None
    assert split[0] < 0.3 and split[1] > 0.7


# --------------------------------------------------------------- meme providers


def test_meme_request_cache_key():
    one = mp.MemeRequest("p", 1.0, 1080, 1920)
    same = mp.MemeRequest("p", 1.0, 1080, 1920)
    other = mp.MemeRequest("q", 1.0, 1080, 1920)
    assert one.cache_key == same.cache_key
    assert one.cache_key != other.cache_key
    assert len(one.cache_key) == 20


def test_find_url_variants():
    assert mp._find_url("https://cdn/x.mp4") == "https://cdn/x.mp4"
    assert mp._find_url("not a url") is None
    assert mp._find_url({"video_url": "https://cdn/a.mp4"}) == "https://cdn/a.mp4"
    assert mp._find_url({"other": {"url": "https://cdn/b.mp4"}}) == "https://cdn/b.mp4"
    assert mp._find_url({"items": [{"x": 1}, "https://cdn/c.mp4"]}) == "https://cdn/c.mp4"
    assert mp._find_url({"x": 1}) is None
    assert mp._find_url(None) is None


def test_status_done():
    assert mp._status_done({"status": "completed"}) is True
    assert mp._status_done({"state": "ready"}) is True
    assert mp._status_done({"status": "pending"}) is False
    assert mp._status_done({}) is False
    with pytest.raises(mp.MemeError, match="generation failed"):
        mp._status_done({"status": "failed", "error": "bad prompt"})


def test_request_id():
    assert mp.ApiMemeProvider()._request_id({"request_id": "r"}) == "r"
    assert mp.ApiMemeProvider()._request_id({"id": "i"}) == "i"
    assert mp.ApiMemeProvider()._request_id({"job_id": "j"}) == "j"
    assert mp.ApiMemeProvider()._request_id({"task_id": "t"}) == "t"
    assert mp.ApiMemeProvider()._request_id({"id": 5}) is None
    assert mp.ApiMemeProvider()._request_id("nope") is None


def test_json_or_text_falls_back_to_a_url():
    provider = mp.ApiMemeProvider()

    class Response:
        text = "https://cdn/x.mp4"

        def json(self):
            raise ValueError("not json")

    assert provider._json_or_text(Response()) == "https://cdn/x.mp4"


# Importing clipbait auto-loads a developer's .env, so a real Giphy/Tenor key
# in there would silently change these tests. Pin the meme keys to their code
# defaults and let each test opt in explicitly.
_MEME_DEFAULTS = dict(
    meme_provider="auto",
    meme_api_url=None,
    meme_api_key=None,
    meme_search_url=None,
    meme_search_key=None,
    meme_search_auth="bearer",
    meme_search_header="Authorization",
    meme_search_key_param="api_key",
    meme_search_query_param="q",
    meme_search_results="results",
    meme_search_video="url",
    meme_search_limit=8,
    meme_search_rating="",
)


def settings_with(**overrides):
    return dataclasses.replace(config.get_settings(), **{**_MEME_DEFAULTS, **overrides})


def test_api_provider_requires_a_url(tmp_path):
    provider = mp.ApiMemeProvider(settings_with(meme_api_url=None, meme_api_key=None))
    assert provider.available() is False
    with pytest.raises(mp.MemeError, match="not set"):
        provider.generate(mp.MemeRequest("p", 1, 10, 10), tmp_path)


def test_api_provider_cache_hit(tmp_path):
    request = mp.MemeRequest("prompt", 1.0, 10, 10)
    cached = tmp_path / f"{request.cache_key}.mp4"
    cached.write_bytes(b"video")
    provider = mp.ApiMemeProvider(settings_with(meme_api_url="https://api.example.com/gen"))
    assert provider.generate(request, tmp_path) == cached


def test_api_provider_synchronous_response(tmp_path, monkeypatch):
    provider = mp.ApiMemeProvider(
        settings_with(meme_api_url="https://api.example.com/gen", meme_api_key="k")
    )
    request = mp.MemeRequest("prompt", 1.2, 1080, 1920)
    calls: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.update({"url": url, "headers": headers, "json": json})
        return SimpleNamespace(status_code=200, json=lambda: {"url": "https://cdn/v.mp4"}, text="")

    monkeypatch.setattr(mp.httpx, "post", fake_post)
    monkeypatch.setattr(mp, "ensure_safe_url", lambda url: url)

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, _size):
            yield b"video-bytes"

    monkeypatch.setattr(mp.httpx, "stream", lambda *a, **k: Stream())

    result = provider.generate(request, tmp_path)
    assert result is not None and result.exists()
    assert calls["headers"]["authorization"] == "Bearer k"
    assert calls["json"]["aspect_ratio"] == "9:16"
    assert calls["json"]["duration"] == 1.2


def test_api_provider_async_response(tmp_path, monkeypatch):
    provider = mp.ApiMemeProvider(settings_with(meme_api_url="https://api.example.com/gen"))
    monkeypatch.setattr(
        mp.httpx,
        "post",
        lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {"id": "job-1"}, text=""),
    )
    monkeypatch.setattr(provider, "_poll", lambda request_id: "https://cdn/v.mp4")
    monkeypatch.setattr(provider, "_download", lambda url, target: target.write_bytes(b"v"))

    result = provider.generate(mp.MemeRequest("p", 1.0, 10, 10), tmp_path)
    assert result is not None and result.read_bytes() == b"v"


def test_api_provider_errors(tmp_path, monkeypatch):
    provider = mp.ApiMemeProvider(settings_with(meme_api_url="https://api.example.com/gen"))
    monkeypatch.setattr(
        mp.httpx,
        "post",
        lambda *a, **k: SimpleNamespace(status_code=500, text="boom", json=lambda: {}),
    )
    with pytest.raises(mp.MemeError, match="500"):
        provider.generate(mp.MemeRequest("p", 1.0, 10, 10), tmp_path)

    monkeypatch.setattr(
        mp.httpx,
        "post",
        lambda *a, **k: SimpleNamespace(status_code=200, text="", json=lambda: {"x": 1}),
    )
    with pytest.raises(mp.MemeError, match="no video URL"):
        provider.generate(mp.MemeRequest("p", 1.0, 10, 10), tmp_path)


def test_api_provider_poll(monkeypatch):
    provider = mp.ApiMemeProvider(
        settings_with(
            meme_api_url="https://api.example.com/gen/", meme_api_key="k", meme_poll_seconds=10
        )
    )
    ticks = iter([0.0, 0.0, 1.0, 1.0, 2.0])
    monkeypatch.setattr(mp.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(mp.time, "sleep", lambda _s: None)

    responses = iter(
        [
            SimpleNamespace(status_code=500, json=lambda: {}),
            SimpleNamespace(
                status_code=200,
                json=lambda: {"status": "completed", "video_url": "https://cdn/v.mp4"},
            ),
        ]
    )
    monkeypatch.setattr(mp.httpx, "get", lambda *a, **k: next(responses))
    assert provider._poll("job-1") == "https://cdn/v.mp4"

    ticks = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(mp.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        mp.httpx, "get", lambda *a, **k: SimpleNamespace(status_code=500, json=lambda: {})
    )
    with pytest.raises(mp.MemeError, match="did not finish"):
        provider._poll("job-1")


def test_api_provider_download_limit(tmp_path, monkeypatch):
    provider = mp.ApiMemeProvider(settings_with(meme_api_url="https://api.example.com/gen"))
    monkeypatch.setattr(mp, "ensure_safe_url", lambda url: url)
    monkeypatch.setattr(mp, "MAX_DOWNLOAD_BYTES", 4)

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, _size):
            yield b"0123456789"

    monkeypatch.setattr(mp.httpx, "stream", lambda *a, **k: Stream())
    target = tmp_path / "out.mp4"
    with pytest.raises(mp.MemeError, match="size limit"):
        provider._download("https://cdn/v.mp4", target)
    assert not target.exists()
    assert not list(tmp_path.glob("*.part"))


def test_drawtext_safe():
    assert mp._drawtext_safe("hello: world's 'quote'") == "hello world s quote"
    assert mp._drawtext_safe("   ") == "clipbait"
    assert len(mp._drawtext_safe("a" * 100)) <= 42


def test_get_meme_provider_variants():
    assert mp.get_meme_provider(settings_with(meme_provider="none")) is None
    assert isinstance(
        mp.get_meme_provider(settings_with(meme_provider="api", meme_api_url="https://x")),
        mp.ApiMemeProvider,
    )
    assert isinstance(
        mp.get_meme_provider(settings_with(meme_provider="fixture")), mp.FixtureMemeProvider
    )
    assert isinstance(
        mp.get_meme_provider(
            settings_with(meme_provider="search", meme_search_url="https://lib/search")
        ),
        mp.HttpMemeProvider,
    )
    # auto prefers a real meme library over generation, then generation, then fixture.
    assert isinstance(
        mp.get_meme_provider(
            settings_with(
                meme_provider="auto",
                meme_search_url="https://lib/search",
                meme_api_url="https://api/gen",
            )
        ),
        mp.HttpMemeProvider,
    )
    assert isinstance(
        mp.get_meme_provider(settings_with(meme_provider="auto", meme_api_url="https://x")),
        mp.ApiMemeProvider,
    )
    assert isinstance(
        mp.get_meme_provider(settings_with(meme_provider="auto", meme_api_url=None)),
        mp.FixtureMemeProvider,
    )
    assert mp.provider_name(None) is None
    assert mp.provider_name(mp.FixtureMemeProvider()) == "fixture"
    assert mp.provider_name(mp.HttpMemeProvider()) == "search"


# ------------------------------------------------------------ meme search API


def search_settings(**overrides):
    base = dict(
        meme_search_url="https://lib.example.com/search",
        meme_search_key="k",
        meme_search_results="data",
        meme_search_video="media.mp4.url",
        meme_search_limit=3,
    )
    base.update(overrides)
    return settings_with(**base)


def test_http_provider_requires_a_url(tmp_path):
    provider = mp.HttpMemeProvider(search_settings(meme_search_url=None))
    assert provider.available() is False
    with pytest.raises(mp.MemeError, match="not set"):
        provider.generate(mp.MemeRequest("p", 1, 10, 10), tmp_path)


def test_http_provider_query_is_a_meme_search():
    provider = mp.HttpMemeProvider(search_settings())
    request = mp.MemeRequest(
        "reaction b-roll for shock, failure: and then the system totally broke", 2, 10, 10
    )
    assert provider.query_for(request) == "shock failure system totally broke"
    assert provider.query_for(mp.MemeRequest("...", 1, 10, 10)) == "reaction"


def test_http_provider_searches_and_downloads_the_first_playable(tmp_path, monkeypatch):
    provider = mp.HttpMemeProvider(search_settings())
    request = mp.MemeRequest("reaction b-roll: wow", 1.0, 10, 10)
    calls: dict = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.update({"url": url, "headers": headers, "params": params})
        return SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {
                "data": [
                    {"media": {"mp4": {"url": "https://cdn/bad.mp4"}}},
                    {"media": {"mp4": {"url": "https://cdn/good.mp4"}}},
                    {"media": {"mp4": {"url": "https://cdn/never.mp4"}}},
                ]
            },
        )

    attempts: list[str] = []

    def fake_download(url, target):
        attempts.append(url)
        if url.endswith("bad.mp4"):
            raise mp.MemeError("dead cdn")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video")

    monkeypatch.setattr(mp.httpx, "get", fake_get)
    monkeypatch.setattr(mp, "_download_stream", fake_download)

    result = provider.generate(request, tmp_path)
    assert result is not None and result.read_bytes() == b"video"
    assert attempts == ["https://cdn/bad.mp4", "https://cdn/good.mp4"]
    assert calls["headers"]["authorization"] == "Bearer k"
    assert calls["params"]["q"] == "wow"
    assert calls["params"]["limit"] == "3"


def test_http_provider_auth_styles_and_template_query():
    provider = mp.HttpMemeProvider(
        search_settings(meme_search_auth="header", meme_search_header="X-Api-Key")
    )
    assert provider._headers()["X-Api-Key"] == "k"

    provider = mp.HttpMemeProvider(search_settings(meme_search_auth="query"))
    assert provider._params("wow")["api_key"] == "k"

    provider = mp.HttpMemeProvider(
        search_settings(meme_search_auth="none", meme_search_key=None)
    )
    assert "authorization" not in provider._headers()

    provider = mp.HttpMemeProvider(
        search_settings(meme_search_url="https://lib/search?term={query}")
    )
    seen: dict = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen.update({"url": url, "params": params})
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"data": [{"media": {"mp4": {"url": "https://cdn/a.mp4"}}}]},
            text="",
        )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mp.httpx, "get", fake_get)
    try:
        found = provider._search("big wow")
    finally:
        monkeypatch.undo()
    assert found == ["https://cdn/a.mp4"]
    assert seen["url"] == "https://lib/search?term=big+wow"
    assert "q" not in seen["params"]


def test_http_provider_empty_and_bad_responses(tmp_path, monkeypatch):
    provider = mp.HttpMemeProvider(search_settings())
    monkeypatch.setattr(
        mp.httpx,
        "get",
        lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {"data": []}, text=""),
    )
    with pytest.raises(mp.MemeError, match="no results"):
        provider.generate(mp.MemeRequest("p", 1, 10, 10), tmp_path)

    monkeypatch.setattr(
        mp.httpx,
        "get",
        lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {"other": []}, text=""),
    )
    with pytest.raises(mp.MemeError, match="no result list"):
        provider.generate(mp.MemeRequest("p", 1, 10, 10), tmp_path)

    monkeypatch.setattr(
        mp.httpx,
        "get",
        lambda *a, **k: SimpleNamespace(status_code=503, json=lambda: {}, text="down"),
    )
    with pytest.raises(mp.MemeError, match="503"):
        provider.generate(mp.MemeRequest("p", 1, 10, 10), tmp_path)


def test_http_provider_cache_hit(tmp_path):
    request = mp.MemeRequest("prompt", 1.0, 10, 10)
    cached = tmp_path / f"{request.cache_key}.mp4"
    cached.write_bytes(b"video")
    provider = mp.HttpMemeProvider(search_settings())
    assert provider.generate(request, tmp_path) == cached


def test_dot_get():
    payload = {"a": {"b": [{"c": "x"}]}}
    assert mp._dot_get(payload, "a.b.0.c") == "x"
    assert mp._dot_get(payload, "a.missing.c") is None
    assert mp._dot_get(payload, "a.b.9.c") is None
    assert mp._dot_get("scalar", "a") is None


def test_fixture_provider_renders_and_caches(tmp_path):
    provider = mp.FixtureMemeProvider()
    request = mp.MemeRequest("reaction b-roll: wow", 0.4, 90, 160, fps=10)
    first = provider.generate(request, tmp_path)
    assert first is not None and first.exists()
    second = provider.generate(request, tmp_path)
    assert second == first


def test_prepare_insert_normalises_shape(tmp_path):
    source = make_video(tmp_path / "raw.mp4", seconds=1, size="160x90")
    out = meme_assets.prepare_insert(
        source, tmp_path / "prepared.mp4", width=90, height=160, duration=0.6, fps=10
    )
    info = media.probe(out)
    assert (info.width, info.height) == (90, 160)
    assert info.has_audio is False


# -------------------------------------------------------------------- net_guard


def test_ensure_safe_url_rejects_bad_input(monkeypatch):
    with pytest.raises(net_guard.UnsafeUrl, match="http"):
        net_guard.ensure_safe_url("file:///etc/passwd")
    with pytest.raises(net_guard.UnsafeUrl, match="no host"):
        net_guard.ensure_safe_url("http:///path")
    with pytest.raises(net_guard.UnsafeUrl, match="credentials"):
        net_guard.ensure_safe_url("https://user:pass@example.com/x")


def test_ensure_safe_url_private_and_public(monkeypatch):
    monkeypatch.delenv("CLIPBAIT_ALLOW_PRIVATE_URLS", raising=False)
    with pytest.raises(net_guard.UnsafeUrl, match="private"):
        net_guard.ensure_safe_url("http://127.0.0.1/x")
    with pytest.raises(net_guard.UnsafeUrl, match="private"):
        net_guard.ensure_safe_url("http://10.0.0.5/x")

    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    assert net_guard.ensure_safe_url("https://example.com/x") == "https://example.com/x"

    monkeypatch.setattr("socket.getaddrinfo", _raise_gaierror)
    with pytest.raises(net_guard.UnsafeUrl, match="cannot resolve"):
        net_guard.ensure_safe_url("https://nope.invalid/x")


def test_ensure_safe_url_allow_private_overrides(monkeypatch):
    monkeypatch.setenv("CLIPBAIT_ALLOW_PRIVATE_URLS", "true")
    assert net_guard.ensure_safe_url("http://192.168.1.1/x") == "http://192.168.1.1/x"
    assert net_guard.ensure_safe_url("http://127.0.0.1/x", allow_private=True)


def test_is_blocked_ip():
    assert net_guard._is_blocked_ip("127.0.0.1") is True
    assert net_guard._is_blocked_ip("169.254.169.254") is True
    assert net_guard._is_blocked_ip("8.8.8.8") is False


def _raise_gaierror(*_args):
    import socket

    raise socket.gaierror("nope")
