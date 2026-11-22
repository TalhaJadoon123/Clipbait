"""CLI surface: every verb is scriptable and returns an exit code.

Heavy work (renders, servers, live threads) is stubbed at the seam so these
tests only exercise argument handling, wiring and output.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest
from helpers import make_video

from clipbait import cli, db, storage
from clipbait.config import Capabilities


def caps(**overrides) -> Capabilities:
    data = {
        "ffmpeg": True,
        "ffprobe": True,
        "ytdlp": True,
        "whisper": True,
        "vision": False,
        "pytchat": False,
        "llm_provider": None,
        "transcriber": "whisper",
        "meme_provider": "fixture",
    }
    data.update(overrides)
    return Capabilities(**data)


def process_args(source: str, **overrides) -> argparse.Namespace:
    data = {
        "source": source,
        "clips": 1,
        "target_seconds": 15,
        "preset": "default",
        "caption_position": "auto",
        "reframe": "auto",
        "facecam_box": None,
        "facecam_ratio": 0.52,
        "title_style": "plain",
        "region": "US",
        "platform": "tiktok",
        "title": None,
        "no_captions": False,
        "no_hooks": False,
        "no_memes": False,
        "no_meme_inserts": False,
        "no_llm": True,
        "json": False,
    }
    data.update(overrides)
    return argparse.Namespace(**data)


# --------------------------------------------------------------------- doctor


def test_doctor_reports_a_healthy_install(capsys):
    assert cli.cmd_doctor(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "Clipbait doctor" in out
    assert "[ok  ] ffmpeg" in out
    assert "transcriber" in out


def test_doctor_warns_about_missing_ffmpeg(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "detect_capabilities",
        lambda settings=None: caps(ffmpeg=False, ytdlp=False, transcriber="mock"),
    )
    assert cli.cmd_doctor(argparse.Namespace()) == 1
    out = capsys.readouterr().out
    assert "[miss] ffmpeg" in out
    assert "Warnings:" in out
    assert "synthetic" in out


def test_publishing_note_names_the_active_provider():
    from dataclasses import replace

    from clipbait.config import get_settings

    base = get_settings()
    postiz = replace(
        base,
        publish_provider="postiz",
        postiz_api_key="k",
        postiz_api_url="http://box:4007/api/public/v1",
    )
    assert cli._publishing_note(postiz) == "postiz @ http://box:4007/api/public/v1 (key set)"
    # A missing Postiz key reads as dry-run, not as a broken Upload-Post setup.
    assert cli._publishing_note(replace(postiz, postiz_api_key=None)).endswith(
        "(no key: dry run only)"
    )
    # The Upload-Post provider keeps its own wording.
    upload = replace(base, publish_provider="upload_post", upload_post_api_key="k")
    assert cli._publishing_note(upload) == "upload-post (key set)"


# ---------------------------------------------------------------------- serve


def test_serve_boots_uvicorn(monkeypatch, capsys):
    calls: dict = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.update(kwargs))

    code = cli.cmd_serve(
        argparse.Namespace(host="0.0.0.0", port=9000, verbose=True, reload=False)
    )

    assert code == 0
    assert calls["host"] == "0.0.0.0"
    assert calls["port"] == 9000
    assert calls["log_level"] == "debug"
    assert "http://0.0.0.0:9000" in capsys.readouterr().out


# -------------------------------------------------------------------- process


@pytest.fixture()
def render_stub(monkeypatch):
    """Replace the pipeline entry point; return the dict it was called with."""
    calls: list[dict] = []

    def fake_process(project_id, job_id=None):
        calls.append({"project_id": project_id})
        return {"clips": 1, "transcriber": "fixture", "analysis": "heuristic"}

    monkeypatch.setattr("clipbait.pipeline.process_project", fake_process)
    return calls


def test_process_local_file(monkeypatch, render_stub, tmp_path, capsys):
    source = make_video(tmp_path / "talk.mp4", seconds=1, size="160x90")

    code = cli.cmd_process(process_args(str(source)))

    assert code == 0
    assert len(render_stub) == 1
    project = db.get_project(render_stub[0]["project_id"])
    assert project["source_kind"] == "local"
    assert project["source_url"] is None
    assert storage.resolve(project["source_path"]).exists()
    assert "Project page" in capsys.readouterr().out


def test_process_url_and_json(monkeypatch, render_stub, capsys):
    code = cli.cmd_process(
        process_args("https://www.youtube.com/watch?v=abcdefghijk", json=True, title="Mine")
    )

    assert code == 0
    project = db.get_project(render_stub[0]["project_id"])
    assert project["source_kind"] == "youtube"
    assert project["source_url"].endswith("abcdefghijk")
    assert project["title"] == "Mine"
    assert "clip(s) ready" in capsys.readouterr().out


def test_process_json_output(monkeypatch, render_stub, capsys):
    cli.cmd_process(process_args("https://example.com/video.mp4", json=True))
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{") :])
    assert payload["project"] == render_stub[0]["project_id"]
    assert payload["clips"] == []


def test_process_reports_failures(monkeypatch, capsys):
    def boom(project_id, job_id=None):
        raise RuntimeError("decoder exploded")

    monkeypatch.setattr("clipbait.pipeline.process_project", boom)
    code = cli.cmd_process(process_args("https://example.com/video.mp4"))
    captured = capsys.readouterr()
    assert code == 1
    assert "decoder exploded" in captured.err


# ----------------------------------------------------------------------- live


def live_args(source: str = "demo", **overrides) -> argparse.Namespace:
    data = {
        "source": source,
        "threshold": 3.5,
        "chat_provider": "auto",
        "preset": "default",
        "region": "IN",
        "platform": "reels",
        "title": None,
        "no_clip": False,
        "no_captions": False,
        "no_chat_overlay": False,
        "no_content": False,
        "chat_only_clips": False,
        "content_threshold": 2.5,
        "max_clips": 8,
        "buffer_seconds": 420,
        "language": "auto",
    }
    data.update(overrides)
    return argparse.Namespace(**data)


def test_live_prints_spikes_and_stops_on_ctrl_c(monkeypatch, capsys):
    started: list[dict] = []
    stopped: list[str] = []

    def fake_start(monitor_id):
        started.append(db.get_monitor(monitor_id))
        db.create_moment(
            {
                "monitor_id": monitor_id,
                "start": 12.5,
                "end": 20.0,
                "score": 5.25,
                "signal": {"top_messages": ["NO WAY"]},
                "messages": [],
            }
        )
        return started[-1]

    monkeypatch.setattr("clipbait.live.start", fake_start)
    monkeypatch.setattr(
        "clipbait.live.stop", lambda monitor_id: stopped.append(monitor_id) or {}
    )

    def interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", interrupt)

    code = cli.cmd_live(live_args())

    assert code == 0
    monitor = started[0]
    assert monitor["url"] == "demo://live"
    assert monitor["options"]["chat_provider"] == "demo"
    assert monitor["options"]["spike_threshold"] == 3.5
    assert monitor["options"]["region"] == "IN"
    assert stopped == [monitor["id"]]
    out = capsys.readouterr().out
    assert "spike" in out
    assert "NO WAY" in out
    assert "Stopping" in out


def test_live_tolerates_a_runner_that_already_stopped(monkeypatch, capsys):
    monkeypatch.setattr("clipbait.live.start", lambda monitor_id: {})
    monkeypatch.setattr(
        "clipbait.live.stop",
        lambda monitor_id: (_ for _ in ()).throw(ValueError("gone")),
    )
    monkeypatch.setattr("time.sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt))
    assert cli.cmd_live(live_args("https://twitch.tv/someone")) == 0
    assert "Monitored for" in capsys.readouterr().out


# --------------------------------------------------------------- parser / main


def test_main_without_a_command_prints_help(capsys):
    assert cli.main([]) == 2
    assert "usage: clipbait" in capsys.readouterr().out


def test_main_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert "clipbait 0.1.0" in capsys.readouterr().out


def test_main_dispatches_and_handles_ctrl_c(monkeypatch):
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: 0)
    assert cli.main(["doctor"]) == 0

    monkeypatch.setattr(
        cli, "cmd_doctor", lambda args: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    assert cli.main(["doctor"]) == 130


def test_parser_defaults():
    parser = cli.build_parser()
    args = parser.parse_args(["serve"])
    assert args.host in {"127.0.0.1", os.getenv("CLIPBAIT_HOST", "127.0.0.1")}
    assert isinstance(args.port, int)
    live = parser.parse_args(["live", "demo"])
    assert live.threshold == 3.0
    process_cmd = parser.parse_args(["process", "x.mp4"])
    assert process_cmd.clips == 5
    assert process_cmd.platform == "tiktok"


def test_project_options_follow_the_flags(monkeypatch, render_stub):
    cli.cmd_process(
        process_args(
            "https://example.com/v.mp4",
            no_captions=True,
            no_hooks=True,
            no_memes=True,
        )
    )
    project = db.get_project(render_stub[0]["project_id"])
    options = project["options"]
    assert options["captions"] is False
    assert options["hook_titles"] is False
    assert options["memes"] is False
    assert options["llm"] is False


def test_meme_inserts_are_on_by_default(monkeypatch, render_stub):
    # A raw gameplay slice reads flat in Shorts; b-roll cutaways are the default.
    cli.cmd_process(process_args("https://example.com/v.mp4"))
    project = db.get_project(render_stub[0]["project_id"])
    assert project["options"]["meme_inserts"] is True


def test_meme_inserts_can_be_turned_off(monkeypatch, render_stub):
    cli.cmd_process(process_args("https://example.com/v.mp4", no_meme_inserts=True))
    project = db.get_project(render_stub[0]["project_id"])
    assert project["options"]["meme_inserts"] is False


def test_live_monitor_wants_memes_in_shorts():
    from clipbait.schema import MonitorOptions

    options = MonitorOptions()
    assert options.meme_inserts is True
    assert options.meme_max_inserts >= 1


def test_project_options_default_to_memes():
    # process, live and grab all read this default; a flat, meme-less Short
    # should be the opt-in, never the silent default.
    from clipbait.schema import ProjectOptions

    assert ProjectOptions().meme_inserts is True


def grab_args(source: str, **overrides) -> argparse.Namespace:
    data = {
        "source": source,
        "at": "0:30",
        "seconds": 20.0,
        "title": None,
        "reason": None,
        "preset": "default",
        "reframe": "auto",
        "language": "auto",
        "hashtags": None,
        "region": "US",
        "platform": "tiktok",
        "campaign": None,
        "export": False,
        "no_captions": False,
        "no_hooks": False,
        "no_meme_inserts": False,
        "json": False,
    }
    data.update(overrides)
    return argparse.Namespace(**data)


def _stub_grab(seen: list[str]):
    def fake_grab(project_id, start, end, title=None, reason=None):
        seen.append(project_id)
        return {"id": "clip_grab", "status": "ready", "title": "t", "quote": "", "render_path": None}

    return fake_grab


def test_grab_wants_memes_in_shorts(monkeypatch, tmp_path):
    # `grab` is the "clip this exact moment" verb; it must want the same b-roll
    # inserts as process/live instead of silently rendering a flat Short.
    seen: list[str] = []
    monkeypatch.setattr("clipbait.pipeline.grab_moment", _stub_grab(seen))
    source = make_video(tmp_path / "grab.mp4", seconds=1, size="160x90")

    assert cli.cmd_grab(grab_args(str(source))) == 0
    assert db.get_project(seen[0])["options"]["meme_inserts"] is True


def test_grab_can_turn_memes_off(monkeypatch, tmp_path):
    seen: list[str] = []
    monkeypatch.setattr("clipbait.pipeline.grab_moment", _stub_grab(seen))
    source = make_video(tmp_path / "grab.mp4", seconds=1, size="160x90")

    assert cli.cmd_grab(grab_args(str(source), no_meme_inserts=True)) == 0
    assert db.get_project(seen[0])["options"]["meme_inserts"] is False


def test_out_dir_is_reported(monkeypatch, render_stub, tmp_path, capsys):
    source = make_video(tmp_path / "small.mp4", seconds=1, size="160x90")
    cli.cmd_process(process_args(str(source)))
    out = capsys.readouterr().out
    assert "Files:" in out
    assert "clips" in out


def test_path_expansion(monkeypatch, render_stub, tmp_path):
    # A plain filename that exists resolves through Path.expanduser().exists()
    source = make_video(tmp_path / "tilde.mp4", seconds=1, size="160x90")
    assert Path(source).exists()
    cli.cmd_process(process_args(str(source)))
    project = db.get_project(render_stub[0]["project_id"])
    assert project["source_kind"] == "local"


def test_live_content_flags_reach_the_monitor_options(monkeypatch, tmp_path):
    """--no-content and --content-threshold must persist, not just parse."""
    from clipbait import live

    started: list[dict] = []

    class FakeLive:
        @staticmethod
        def start(monitor_id: str) -> None:
            started.append(db.get_monitor(monitor_id))

        @staticmethod
        def stop(monitor_id: str) -> None:
            pass

    # cmd_live imports the module locally, so patch the registry module.
    monkeypatch.setattr(live, "start", FakeLive.start)
    monkeypatch.setattr(live, "stop", FakeLive.stop)

    def stop_the_loop(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", stop_the_loop)
    args = argparse.Namespace(
        source="demo",
        threshold=3.0,
        chat_provider="demo",
        preset="default",
        region="US",
        platform="tiktok",
        title=None,
        no_clip=False,
        no_captions=False,
        no_chat_overlay=False,
        no_content=True,
        content_threshold=4.5,
        max_clips=8,
        buffer_seconds=420,
        language="en",
    )
    assert cli.cmd_live(args) == 0
    options = started[0]["options"]
    assert options["content_detection"] is False
    assert options["content_threshold"] == 4.5
    assert options["language"] == "en"


def _ready_clip(score: int = 80) -> dict:
    project = db.create_project(
        {
            "title": "CLI money",
            "source_kind": "local",
            "duration": 120.0,
            "options": {"region": "US", "platform": "tiktok"},
        }
    )
    clip = db.create_clip(
        {
            "project_id": project["id"],
            "title": "Queue me",
            "hook": "Queue me",
            "description": "copy",
            "hashtags": ["#shorts"],
            "start": 10.0,
            "end": 48.0,
            "score": score,
            "status": "ready",
            "meta": {"region": "US", "forecast": {"overall": score}},
        }
    )
    path = storage.project_dir(project["id"]) / "clips" / f"{clip['id']}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 2048)
    return db.update("clips", clip["id"], {"render_path": storage.relative(path)}) or clip


def test_queue_command_ranks_and_exports(capsys):
    clip = _ready_clip()
    assert cli.main(["queue"]) == 0
    out = capsys.readouterr().out
    assert "ready clip" in out
    assert "Queue me" in out

    assert cli.main(["queue", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] >= 1
    assert any(item["clip_id"] == clip["id"] for item in payload["items"])

    assert cli.main(["queue", "--export"]) == 0
    out = capsys.readouterr().out
    assert "exported" in out


def test_queue_mark_uploaded_hides_the_clip(capsys):
    clip = _ready_clip(score=75)
    assert cli.main(["queue", "--mark", clip["id"], "--url", "https://x.test/p"]) == 0
    assert "marked" in capsys.readouterr().out
    assert cli.main(["queue", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert all(item["clip_id"] != clip["id"] for item in payload["items"])
    assert cli.main(["queue", "--mark", "clip_nope"]) == 1


def test_parser_exposes_content_and_queue_flags():
    parser = cli.build_parser()
    live = parser.parse_args(["live", "demo", "--no-content", "--content-threshold", "4"])
    assert live.no_content is True
    assert live.content_threshold == 4.0
    queue = parser.parse_args(["queue", "--limit", "5", "--export", "--min-score", "70"])
    assert queue.limit == 5
    assert queue.export is True
    assert queue.min_score == 70.0


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("75", 75.0),
        ("1:15", 75.0),
        ("0:01:15", 75.0),
        ("2:03:04.5", 7384.5),
        ("-5", 0.0),
    ],
)
def test_grab_timestamp_parsing(text, seconds):
    from clipbait.cli import parse_timestamp

    assert parse_timestamp(text) == pytest.approx(seconds)


def test_grab_timestamp_rejects_nonsense():
    from clipbait.cli import parse_timestamp

    for bad in ("", "abc", "1:2:3:4"):
        with pytest.raises(ValueError):
            parse_timestamp(bad)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, []),
        ("", []),
        ("#gta6", ["gta6"]),
        ("gta6", ["gta6"]),
        ("#gta6 #kaicenat", ["gta6", "kaicenat"]),
        ("gta6,kaicenat", ["gta6", "kaicenat"]),
        ("#gta6, kai cenat", ["gta6", "kai cenat"]),
        ("#gta6 #GTA6", ["gta6"]),
    ],
)
def test_parse_hashtags(text, expected):
    from clipbait.cli import parse_hashtags

    assert parse_hashtags(text) == expected
