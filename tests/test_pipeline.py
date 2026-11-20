"""End-to-end orchestration.

One test renders a real clip through the whole chain (tiny source, short
output). Everything else drives the same branches with the renderer patched, so
the suite stays fast while still covering the real control flow.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import FixedTranscriber, make_video, scripted_transcript

from clipbait import db, media, pipeline, storage
from clipbait.ingest import IngestResult
from clipbait.schema import Highlight, ProjectOptions, Transcript, TranscriptSegment, Word

SOURCE_SECONDS = 48.0


@pytest.fixture(scope="module")
def source(tmp_path_factory) -> Path:
    return make_video(
        tmp_path_factory.mktemp("media") / "source.mp4",
        seconds=SOURCE_SECONDS,
        size="320x180",
    )


@pytest.fixture(scope="module")
def transcript():
    return scripted_transcript(SOURCE_SECONDS)


def make_project(source_path: Path | None, *, source_url: str | None = None, **options) -> dict:
    return db.create_project(
        {
            "title": "Pipeline test",
            "source_kind": "local",
            "source_url": source_url,
            "source_path": storage.relative(source_path) if source_path else None,
            "duration": SOURCE_SECONDS,
            "options": ProjectOptions(**options).model_dump(),
        }
    )


def fixed_transcriber(monkeypatch, transcript) -> FixedTranscriber:
    transcriber = FixedTranscriber(transcript)
    monkeypatch.setattr("clipbait.transcribe.get_transcriber", lambda *a, **k: transcriber)
    return transcriber


def test_process_project_renders_a_clip(source, transcript, monkeypatch):
    project = make_project(
        source,
        clips=1,
        target_seconds=15,
        meme_inserts=True,
        meme_insert_seconds=1.2,
        meme_max_inserts=1,
    )
    fixed_transcriber(monkeypatch, transcript)

    result = pipeline.process_project(project["id"])

    assert result["project_id"] == project["id"]
    assert result["clips"] == 1
    assert result["transcriber"] == "fixture"

    stored = db.get_project(project["id"])
    assert stored["status"] == "completed"
    assert stored["error"] is None

    clips = db.list_clips(project["id"])
    assert len(clips) == 1
    clip = clips[0]
    assert clip["status"] == "ready"
    rendered = storage.resolve(clip["render_path"])
    assert rendered.exists() and rendered.stat().st_size > 0
    assert storage.resolve(clip["thumb_path"]).exists()

    meta = clip["meta"]
    assert meta["insert_count"] == len(meta["inserts"])
    assert meta["layout"] in {"center", "track", "blur", "split"}
    assert clip["score"] >= 0
    # The transcript cache is written so a retry never pays for Whisper twice.
    assert storage.transcript_path(project["id"]).exists()


def test_process_project_reuses_the_cached_transcript(source, transcript, monkeypatch):
    project = make_project(source, clips=1, target_seconds=15)
    transcriber = fixed_transcriber(monkeypatch, transcript)
    renders: list[str] = []

    def fake_render(clip_id, *, transcript=None, on_progress=None):
        if on_progress:
            on_progress(0.5, "encoding")
        renders.append(clip_id)
        return {}

    monkeypatch.setattr(pipeline, "render_clip_record", fake_render)
    calls = {"n": 0}
    original = transcriber.transcribe

    def counted(audio, language="auto"):
        calls["n"] += 1
        return original(audio, language)

    transcriber.transcribe = counted  # type: ignore[method-assign]

    pipeline.process_project(project["id"])
    pipeline.process_project(project["id"])

    assert calls["n"] == 1
    assert len(renders) == 2  # one clip per run


def test_process_project_keeps_going_when_one_clip_fails(source, transcript, monkeypatch):
    project = make_project(source, clips=2, target_seconds=15)
    fixed_transcriber(monkeypatch, transcript)

    highlights = [
        Highlight(start=0.0, end=14.0, title="First", hook="First", scores={"overall": 80}),
        Highlight(start=20.0, end=34.0, title="Second", hook="Second", scores={"overall": 70}),
    ]
    monkeypatch.setattr(
        pipeline.analyze, "analyze", lambda *a, **k: (highlights, {"provider": "stub"})
    )

    attempts: list[str] = []

    def flaky(clip_id, *, transcript=None, on_progress=None):
        attempts.append(clip_id)
        if len(attempts) == 1:
            raise media.MediaError("boom")
        return {}

    monkeypatch.setattr(pipeline, "render_clip_record", flaky)

    result = pipeline.process_project(project["id"])

    assert result["clips"] == 1
    statuses = {clip["title"]: clip["status"] for clip in db.list_clips(project["id"])}
    assert statuses["First"] == "failed"
    assert statuses["Second"] != "failed"
    first = next(clip for clip in db.list_clips(project["id"]) if clip["title"] == "First")
    assert "boom" in first["meta"]["error"]


def test_process_project_fails_when_every_clip_fails(source, transcript, monkeypatch):
    project = make_project(source, clips=1, target_seconds=15)
    fixed_transcriber(monkeypatch, transcript)
    monkeypatch.setattr(
        pipeline,
        "render_clip_record",
        lambda *a, **k: (_ for _ in ()).throw(media.MediaError("no encoder")),
    )

    with pytest.raises(media.MediaError, match="every clip failed"):
        pipeline.process_project(project["id"])


def test_process_project_reports_no_moments(source, transcript, monkeypatch):
    project = make_project(source, clips=1, target_seconds=15)
    fixed_transcriber(monkeypatch, transcript)
    monkeypatch.setattr(
        pipeline.analyze, "analyze", lambda *a, **k: ([], {"provider": "stub"})
    )

    with pytest.raises(ValueError, match="No clip-worthy moments"):
        pipeline.process_project(project["id"])


def test_process_project_rejects_unknown_ids():
    with pytest.raises(ValueError, match="unknown project"):
        pipeline.process_project("proj_missing")


def test_ensure_source_reuses_an_existing_file(source, monkeypatch):
    project = make_project(source)
    called = {"ingest": False}

    def fail(*a, **k):
        called["ingest"] = True
        raise AssertionError("should not re-ingest")

    monkeypatch.setattr(pipeline, "ingest_source", fail)
    assert pipeline.ensure_source(project, None)["id"] == project["id"]
    assert called["ingest"] is False


def test_ensure_source_downloads_a_missing_source(source, monkeypatch):
    project = make_project(None, source_url="https://example.com/watch?v=1")
    monkeypatch.setattr(
        pipeline,
        "ingest_source",
        lambda proj, job_id=None, on_progress=None: (
            on_progress(0.5, "halfway") if on_progress else None
        )
        or IngestResult(
            path=source,
            title="Downloaded title",
            duration=SOURCE_SECONDS,
            source_kind="youtube",
        ),
    )

    updated = pipeline.ensure_source(project, None)

    assert updated["title"] == "Downloaded title"
    assert updated["duration"] == SOURCE_SECONDS
    assert Path(updated["source_path"]).name == source.name


def test_ensure_source_needs_a_url_or_path():
    project = make_project(None)
    with pytest.raises(media.MediaError, match="no source URL"):
        pipeline.ensure_source(project, None)


def test_ingest_source_local_and_remote(source, monkeypatch):
    local = make_project(source, source_kind="local")
    result = pipeline.ingest_source(local)
    assert result.source_kind == "local"
    assert Path(result.path).exists()
    assert Path(result.path).stat().st_size == source.stat().st_size

    remote = make_project(None, source_url="https://example.com/v")
    monkeypatch.setattr(
        "clipbait.ingest.fetch_remote",
        lambda url, project_id, on_progress=None: IngestResult(
            path=source, title=url, duration=1.0, source_kind="upload"
        ),
    )
    fetched = pipeline.ingest_source(remote)
    assert fetched.source_kind == "upload"


def test_ingest_source_without_url_is_an_error():
    project = make_project(None)
    with pytest.raises(ValueError, match="missing source"):
        pipeline.ingest_source(project)


def test_create_clip_without_memes(transcript):
    project = make_project(None, memes=False)
    highlight = Highlight(start=0.0, end=10.0, title="Hi", hook="Hi", quote="hello")
    clip = pipeline._create_clip(
        project["id"], highlight, ProjectOptions(memes=False), {"provider": "x"}, transcript
    )
    assert clip["meta"]["variants"] == []
    assert clip["title"]


def test_render_clip_record_error_branches(source, transcript):
    with pytest.raises(ValueError, match="unknown clip"):
        pipeline.render_clip_record("clip_missing")

    project = make_project(None)
    clip = db.create_clip(
        {
            "project_id": project["id"],
            "title": "No source",
            "hook": "No source",
            "start": 0.0,
            "end": 5.0,
        }
    )
    with pytest.raises(ValueError, match="project source missing"):
        pipeline.render_clip_record(clip["id"])

    project2 = make_project(source)
    monkeypatch_clip = db.create_clip(
        {
            "project_id": project2["id"],
            "title": "No transcript",
            "hook": "No transcript",
            "start": 0.0,
            "end": 5.0,
        }
    )
    with pytest.raises(ValueError, match="transcript missing"):
        pipeline.render_clip_record(monkeypatch_clip["id"])


def test_render_clip_inserts_broll_between_sentences(source):
    """A real 6s render with one gap-triggered b-roll cutaway."""
    project = make_project(source, clips=1, target_seconds=15)
    words = [
        Word(text="one", start=0.3, end=0.7),
        Word(text="two", start=0.8, end=1.2),
        Word(text="three.", start=1.3, end=1.7),
        # 2.0s of dead air -> a natural seam
        Word(text="four", start=3.7, end=4.1),
        Word(text="five", start=4.2, end=4.6),
    ]
    transcript = Transcript(
        language="en",
        duration=6.0,
        words=words,
        segments=[TranscriptSegment(text="one two three.", start=0.3, end=1.7)],
        provider="fixture",
    )
    clip = db.create_clip(
        {
            "project_id": project["id"],
            "title": "B-roll",
            "hook": "B-roll",
            "start": 0.0,
            "end": 6.0,
            "caption_preset": "default",
            "reframe_mode": "center",
            "meta": {
                "meme_inserts": True,
                "meme_insert_seconds": 1.2,
                "meme_max_inserts": 1,
            },
        }
    )

    updated = pipeline.render_clip_record(clip["id"], transcript=transcript)

    assert updated["status"] == "ready"
    assert updated["meta"]["insert_count"] == 1
    assert len(updated["meta"]["inserts"]) == 1
    assert storage.resolve(updated["meta"]["inserts"][0]["path"]).exists()
    assert storage.resolve(updated["render_path"]).exists()


def test_process_live_moment(transcript, monkeypatch, tmp_path):
    project = make_project(None, chat_overlay=True, clips=1)
    moment_dir = storage.project_dir(project["id"]) / "source"
    moment_dir.mkdir(parents=True, exist_ok=True)
    capture = make_video(moment_dir / "moment.mp4", seconds=4, size="320x180")
    db.update("projects", project["id"], {"source_path": storage.relative(capture)})

    fixed_transcriber(monkeypatch, transcript)
    captured: dict = {}

    def fake_render(clip_id, *, transcript=None, on_progress=None):
        captured["clip"] = db.get_clip(clip_id)
        if on_progress:
            on_progress(1.0, "rendered")
        return captured["clip"]

    monkeypatch.setattr(pipeline, "render_clip_record", fake_render)

    result = pipeline.process_live_moment(
        project["id"],
        {
            "monitor_id": "mon_1",
            "moment_id": "mom_1",
            "title": "Chat went wild",
            "reason": "rate spike",
            "score": 8.4,
            "messages": [{"t": 1.0, "author": "a", "text": "CLIP IT"}],
        },
    )

    assert result["clip_id"]
    clip = captured["clip"]
    assert clip["meta"]["live"] is True
    assert clip["meta"]["monitor_id"] == "mon_1"
    assert clip["meta"]["chat"][0]["text"] == "CLIP IT"
    assert clip["meta"]["analysis"] == "live-chat-spike"
    assert clip["score"] == 84
    assert db.get_project(project["id"])["status"] == "completed"


def test_process_live_moment_uses_what_the_streamer_said(transcript, monkeypatch):
    project = make_project(None, chat_overlay=True, clips=1)
    moment_dir = storage.project_dir(project["id"]) / "source"
    moment_dir.mkdir(parents=True, exist_ok=True)
    capture = make_video(moment_dir / "moment.mp4", seconds=4, size="320x180")
    db.update("projects", project["id"], {"source_path": storage.relative(capture)})

    fixed_transcriber(monkeypatch, transcript)
    captured: dict = {}

    def fake_render(clip_id, *, transcript=None, on_progress=None):
        captured["clip"] = db.get_clip(clip_id)
        return captured["clip"]

    monkeypatch.setattr(pipeline, "render_clip_record", fake_render)

    pipeline.process_live_moment(
        project["id"],
        {
            "monitor_id": "mon_2",
            "moment_id": "mom_2",
            "title": 'On stream: "NO WAY"',
            "reason": "hype language: no way; loudness spike",
            "quote": "NO WAY",
            "origin": "fused",
            "score": 7.1,
            "messages": [],
        },
    )

    clip = captured["clip"]
    assert clip["meta"]["analysis"] == "live-content"
    assert clip["meta"]["origin"] == "fused"
    assert clip["quote"] == "NO WAY"
    assert clip["scores"]["overall"] == 71


def test_process_live_moment_requires_a_capture():
    project = make_project(None)
    with pytest.raises(ValueError, match="no source"):
        pipeline.process_live_moment(project["id"], {"title": "x"})


def make_link_project(**options) -> dict:
    payload = {
        "clips": 1,
        "target_seconds": 15,
        "meme_inserts": False,
        **options,
    }
    return db.create_project(
        {
            "title": "Link project",
            "source_kind": "youtube",
            "source_url": "https://example.com/watch?v=abc",
            "source_path": None,
            "duration": SOURCE_SECONDS,
            "options": ProjectOptions(**payload).model_dump(),
        }
    )


def test_link_mode_predicate(source):
    local = make_project(source)
    assert pipeline._link_mode(local, ProjectOptions()) is False

    remote = make_link_project()
    assert pipeline._link_mode(remote, ProjectOptions()) is True
    assert pipeline._link_mode(remote, ProjectOptions(source_mode="download")) is False


def test_link_mode_reuses_the_audio_on_retry(source, monkeypatch):
    project = make_link_project()
    audio = storage.link_audio_path(project["id"])
    audio.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, audio)

    def explode(*_a, **_k):
        raise AssertionError("the audio must not be downloaded twice")

    monkeypatch.setattr("clipbait.ingest.fetch_audio", explode)
    updated = pipeline.ensure_source(project, None)
    assert updated["id"] == project["id"]
    assert updated["source_path"] is None


def test_process_project_link_mode_never_downloads_the_whole_video(
    source, transcript, monkeypatch
):
    project = make_link_project()
    fixed_transcriber(monkeypatch, transcript)
    monkeypatch.setattr(
        pipeline.analyze,
        "analyze",
        lambda *a, **k: (
            [Highlight(start=0.0, end=14.0, title="Moment", hook="Moment", scores={"overall": 80})],
            {"provider": "stub"},
        ),
    )

    audio_calls: list[str] = []
    section_calls: list[tuple[float, float]] = []

    def fake_fetch_audio(url, project_id, on_progress=None):
        audio_calls.append(url)
        target = storage.link_audio_path(project_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if on_progress:
            on_progress(1.0, "Audio ready")
        return IngestResult(
            path=target,
            title="Linked talk",
            duration=SOURCE_SECONDS,
            source_kind="youtube",
            webpage_url=url,
        )

    def fake_fetch_section(url, start, end, target, *, on_progress=None):
        section_calls.append((start, end))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if on_progress:
            on_progress(1.0, "Section ready")
        return target

    monkeypatch.setattr("clipbait.ingest.fetch_audio", fake_fetch_audio)
    monkeypatch.setattr("clipbait.ingest.fetch_section", fake_fetch_section)

    result = pipeline.process_project(project["id"])

    assert result["clips"] == 1
    stored = db.get_project(project["id"])
    assert stored["status"] == "completed"
    # Link mode: the whole-file path was never populated, and only one section
    # (the kept clip) was fetched from the remote source.
    assert stored["source_path"] is None
    assert audio_calls == ["https://example.com/watch?v=abc"]
    assert section_calls == [(0.0, 14.0)]

    clip = db.list_clips(project["id"])[0]
    assert clip["status"] == "ready"
    assert clip["meta"]["link_mode"] is True
    assert storage.resolve(clip["render_path"]).exists()


def test_link_mode_shifts_words_and_chat_to_the_clip_window(source, transcript, monkeypatch):
    project = make_link_project()
    clip = db.create_clip(
        {
            "project_id": project["id"],
            "title": "Second moment",
            "hook": "Second moment",
            "start": 10.0,
            "end": 24.0,
            "caption_preset": "default",
            "reframe_mode": "center",
            "meta": {"chat": [{"t": 12.0, "author": "a", "text": "clip it"}]},
        }
    )
    monkeypatch.setattr(
        "clipbait.ingest.fetch_section",
        lambda url, start, end, target, **k: shutil.copyfile(source, target) or target,
    )

    captured: dict = {}

    def fake_render(source_path, out_path, start, end, plan, **kwargs):
        captured.update({"start": start, "end": end, "words": kwargs.get("words"), "chat": kwargs.get("chat")})
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"video")
        return SimpleNamespace(path=out_path, duration=14.0, size_bytes=5)

    monkeypatch.setattr(pipeline, "render_clip", fake_render)

    updated = pipeline.render_clip_record(clip["id"], transcript=transcript)

    assert captured["start"] == 0.0
    assert captured["end"] == 14.0
    # Every word is rebased on the fetched window, so captions cannot drift.
    original = transcript.words
    for before, after in zip(original, captured["words"], strict=False):
        assert after.start == pytest.approx(before.start - 10.0)
    assert captured["chat"][0].t == pytest.approx(2.0)
    assert updated["meta"]["link_mode"] is True


def test_link_mode_falls_back_to_a_full_download(source, transcript, monkeypatch):
    project = make_link_project()
    clip = db.create_clip(
        {
            "project_id": project["id"],
            "title": "Fallback",
            "hook": "Fallback",
            "start": 0.0,
            "end": 6.0,
            "caption_preset": "default",
            "reframe_mode": "center",
        }
    )

    def broken_section(*_a, **_k):
        raise media.MediaError("range requests unsupported")

    full = storage.source_path(project["id"])
    full.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, full)

    def fake_ingest(project_row, job_id, on_progress=None):
        return IngestResult(
            path=full,
            title="Downloaded anyway",
            duration=SOURCE_SECONDS,
            source_kind="youtube",
            webpage_url=project_row["source_url"],
        )

    monkeypatch.setattr("clipbait.ingest.fetch_section", broken_section)
    monkeypatch.setattr(pipeline, "ingest_source", fake_ingest)

    updated = pipeline.render_clip_record(clip["id"], transcript=transcript)

    assert updated["status"] == "ready"
    assert db.get_project(project["id"])["source_path"] is not None
    # Once the full file is on disk the clip renders on the original timeline.
    assert updated["meta"]["link_mode"] is False


def test_cleanup_outputs_removes_the_project_dir(source):
    project = make_project(source)
    directory = storage.project_dir(project["id"])
    assert directory.exists()
    pipeline.cleanup_outputs(project["id"])
    assert not directory.exists()


def test_a_chat_clip_is_named_by_what_the_streamer_said(transcript, monkeypatch):
    """Chat names the reflex; the clip's own audio names the moment."""
    project = make_project(None, chat_overlay=True, clips=1)
    moment_dir = storage.project_dir(project["id"]) / "source"
    moment_dir.mkdir(parents=True, exist_ok=True)
    capture = make_video(moment_dir / "moment.mp4", seconds=4, size="320x180")
    db.update("projects", project["id"], {"source_path": storage.relative(capture)})

    fixed_transcriber(monkeypatch, transcript)
    captured: dict = {}

    def fake_render(clip_id, *, transcript=None, on_progress=None):
        captured["clip"] = db.get_clip(clip_id)
        return captured["clip"]

    monkeypatch.setattr(pipeline, "render_clip_record", fake_render)

    pipeline.process_live_moment(
        project["id"],
        {
            "monitor_id": "mon_3",
            "moment_id": "mom_3",
            "title": 'Chat: "You wash your hands"',
            "reason": "0.25/s chat velocity",
            "origin": "chat",
            "score": 3.1,
            "messages": [{"t": 1.0, "author": "a", "text": "You wash your hands"}],
        },
    )

    clip = captured["clip"]
    assert clip["meta"]["analysis"] == "live-chat-spike"
    assert clip["quote"] and "no way" in clip["quote"].lower()
    assert "wash your hands" not in (clip["hook"] or "").lower()
    assert "wash your hands" not in (clip["description"] or "").lower()


def test_grab_cuts_exactly_what_the_human_pointed_at(transcript, monkeypatch):
    """A person who saw the moment should not have to out-guess the detector."""
    project = make_project(None, source_url="https://example.com/v.mp4")
    fixed_transcriber(monkeypatch, transcript)
    captured: dict = {}

    def fake_fetch(url, start, end, target, *, on_progress=None):
        captured["window"] = (url, start, end)
        target.parent.mkdir(parents=True, exist_ok=True)
        make_video(target, seconds=4, size="320x180")
        return target

    monkeypatch.setattr("clipbait.ingest.fetch_section", fake_fetch)

    def fake_render(clip_id, *, transcript=None, on_progress=None):
        captured["clip"] = db.get_clip(clip_id)
        return captured["clip"]

    monkeypatch.setattr(pipeline, "render_clip_record", fake_render)

    clip = pipeline.grab_moment(project["id"], 754.0, 794.0, title="The thing I saw")

    assert captured["window"] == ("https://example.com/v.mp4", 754.0, 794.0)
    assert clip["meta"]["analysis"] == "manual-grab"
    assert clip["meta"]["origin"] == "manual"
    assert clip["title"] == "The thing I saw"
    assert clip["reason"] == "Hand-picked 12:34-13:14 of the stream."


def test_grab_cuts_from_a_local_file_without_a_url(tmp_path, transcript, monkeypatch):
    source = make_video(tmp_path / "full.mp4", seconds=8, size="320x180")
    project = make_project(source)
    fixed_transcriber(monkeypatch, transcript)

    def fake_render(clip_id, *, transcript=None, on_progress=None):
        return db.get_clip(clip_id)

    monkeypatch.setattr(pipeline, "render_clip_record", fake_render)

    clip = pipeline.grab_moment(project["id"], 2.0, 5.0, reason="local cut")

    target = storage.resolve(db.get_project(project["id"])["source_path"])
    assert target.name == "video.mp4"
    assert target.exists() and target.stat().st_size > 0
    info = pipeline.media.probe(target)
    assert 2.5 <= info.duration <= 3.6
    assert clip["reason"] == "local cut"


def test_process_live_moment_uses_the_stream_hashtags(transcript, monkeypatch):
    """Stream tags seed the copy so the clip is about this stream, not a genre."""
    project = make_project(None, clips=1, hashtags=["kaicenat", "gta6"])
    moment_dir = storage.project_dir(project["id"]) / "source"
    moment_dir.mkdir(parents=True, exist_ok=True)
    capture = make_video(moment_dir / "moment.mp4", seconds=4, size="320x180")
    db.update("projects", project["id"], {"source_path": storage.relative(capture)})

    fixed_transcriber(monkeypatch, transcript)
    captured: dict = {}

    def fake_render(clip_id, *, transcript=None, on_progress=None):
        captured["clip"] = db.get_clip(clip_id)
        return captured["clip"]

    monkeypatch.setattr(pipeline, "render_clip_record", fake_render)

    pipeline.process_live_moment(
        project["id"],
        {
            "monitor_id": "mon_tags",
            "moment_id": "mom_tags",
            "title": "Chat went wild",
            "reason": "rate spike",
            "score": 8.4,
            "messages": [{"t": 1.0, "author": "a", "text": "CLIP IT"}],
        },
    )

    tags = {tag.lower() for tag in captured["clip"]["hashtags"]}
    assert "#kaicenat" in tags
    assert "#gta6" in tags
