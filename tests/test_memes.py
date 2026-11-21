"""Meme video inserts: planner, render graph, providers and asset prep.

The planner is pure and fully covered here. The provider/asset tests touch
ffmpeg; they are skipped when the binaries are missing rather than failing.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from clipbait import media
from clipbait.config import get_settings
from clipbait.memes import (
    ApiMemeProvider,
    FixtureMemeProvider,
    InsertConfig,
    MemeRequest,
    get_meme_provider,
    plan_inserts,
    prepare_insert,
)
from clipbait.reframe import ReframePlan
from clipbait.render.ffmpeg import _graph
from clipbait.schema import Word

HAS_FFMPEG = bool(shutil.which("ffmpeg"))


def words(spec: list[tuple[str, float, float] | Word]) -> list[Word]:
    return [
        item if isinstance(item, Word) else Word(text=item[0], start=item[1], end=item[2])
        for item in spec
    ]


def dense_speech(start: float, end: float, step: float = 0.4) -> list[Word]:
    """Continuous speech with no gaps at all: 'gap'/'signal' free filler."""
    out: list[Word] = []
    at = start
    index = 0
    while at < end:
        out.append(Word(text=f"filler{index}", start=at, end=at + step))
        at += step
        index += 1
    return out


# --------------------------------------------------------------------- planner


def test_no_insert_when_speech_is_continuous() -> None:
    # Words butt up against each other: nothing to cut away to.
    assert plan_inserts(dense_speech(0, 30), 0, 30) == []


def test_gap_between_sentences_is_a_candidate() -> None:
    spec = [
        ("so", 0.5, 0.9),
        ("here", 0.9, 1.3),
        ("we", 1.3, 1.6),
        ("go", 1.6, 2.0),
        # 2.0s of dead air
        ("and", 4.0, 4.3),
        ("then", 4.3, 4.7),
        ("done", 4.7, 5.1),
    ]
    inserts = plan_inserts(words(spec), 0, 20)
    assert len(inserts) == 1
    assert inserts[0].source == "gap"
    assert inserts[0].start == pytest.approx(2.0, abs=0.01)


def test_strong_signal_is_a_candidate_without_a_gap() -> None:
    spec = [
        ("wait", 2.0, 2.4),
        ("what", 2.4, 2.7),
        ("no", 2.7, 2.9),
        ("way", 2.9, 3.2),
        ("insane", 3.2, 3.6),
    ]
    # Dense filler elsewhere so the only trigger is the signal.
    spec = dense_speech(0, 2.0) + spec + dense_speech(3.6, 12.0, step=0.35)
    inserts = plan_inserts(words(spec), 0, 12)
    assert len(inserts) == 1
    assert inserts[0].source == "signal"
    assert "surprise" in inserts[0].signals or "awe" in inserts[0].signals
    # The insert lands on the signal word ("wait" at 2.0), not up to a window
    # earlier on the filler that preceded it.
    assert inserts[0].start == pytest.approx(2.0, abs=0.01)


def test_inserts_respect_min_spacing_and_max() -> None:
    # Three wide gaps, but max_inserts caps the result and spacing keeps them
    # apart.
    spec: list[tuple[str, float, float]] = []
    for i in range(3):
        base = i * 20.0
        spec += [(f"a{i}", base + 0.5, base + 1.0), (f"b{i}", base + 4.0, base + 4.5)]
    inserts = plan_inserts(words(spec), 0, 60)
    assert len(inserts) <= 2
    if len(inserts) == 2:
        assert inserts[1].start - inserts[0].start >= 8.0


def test_inserts_never_touch_the_clip_edges() -> None:
    spec = [
        ("a", 0.0, 0.4),
        # gap hugging the head
        ("b", 2.4, 2.8),
        ("c", 2.8, 3.2),
        # long tail silence
        ("d", 5.0, 5.4),
    ]
    inserts = plan_inserts(words(spec), 0, 20)
    for insert in inserts:
        assert insert.start >= 1.5
        assert insert.end <= 20 - 1.0 + 0.001


def test_max_inserts_one_is_honoured() -> None:
    spec: list[tuple[str, float, float]] = []
    for i in range(4):
        base = i * 15.0
        spec += [(f"x{i}", base, base + 0.5), (f"y{i}", base + 5.0, base + 5.5)]
    inserts = plan_inserts(words(spec), 0, 60, InsertConfig(max_inserts=1))
    assert len(inserts) == 1


def test_planner_is_deterministic() -> None:
    spec = [
        ("wait", 2.0, 2.4),
        ("what", 2.4, 2.7),
        ("no", 2.7, 3.0),
        ("way", 3.0, 3.4),
    ]
    spec = dense_speech(0, 2.0) + spec + dense_speech(3.4, 12.0, step=0.35)
    first = [insert.as_dict() for insert in plan_inserts(words(spec), 0, 12)]
    second = [insert.as_dict() for insert in plan_inserts(words(spec), 0, 12)]
    assert first == second


def test_too_few_words_is_no_insert() -> None:
    assert plan_inserts(words([("hi", 0.0, 0.3)]), 0, 10) == []


def test_insert_duration_respects_tail() -> None:
    # A gap right at the tail edge must not produce an over-long insert.
    spec = [("a", 0.5, 1.0), ("b", 1.0, 1.4), ("c", 5.0, 5.4), ("d", 5.4, 5.8)]
    inserts = plan_inserts(words(spec), 0, 8)
    for insert in inserts:
        assert insert.start + insert.duration <= 8.0 + 0.001


# ---------------------------------------------------------------- render graph


def _plan() -> ReframePlan:
    return ReframePlan(
        mode="center",
        out_w=1080,
        out_h=1920,
        source_w=1920,
        source_h=1080,
        crop_xs=[420],
        fps=30.0,
    )


def test_graph_without_inserts_is_unchanged() -> None:
    graph, has_audio = _graph(_plan(), None, fps=30.0, has_audio=True)
    assert "overlay" not in graph
    assert graph.endswith("[0:a]asetpts=PTS-STARTPTS,aresample=48000[a]")
    assert has_audio is True


def test_graph_overlays_insert_on_the_clip_timeline() -> None:
    graph, _ = _graph(
        _plan(), Path("/tmp/x.ass"), fps=30.0, has_audio=True, inserts=[(5.0, 7.0)]
    )
    assert "[1:v]setpts=PTS-STARTPTS+5.000/TB" in graph
    assert "overlay=(W-w)/2:(H-h)/2:enable='between(t,5.000,7.000)'" in graph
    # The insert is composited before the caption pass, so captions stay above
    # it and the spoken audio underneath is untouched.
    assert graph.index("overlay=") < graph.index("ass=f=")
    assert "[0:a]asetpts=PTS-STARTPTS,aresample=48000[a]" in graph


def test_graph_chains_multiple_inserts() -> None:
    graph, _ = _graph(
        _plan(), None, fps=30.0, has_audio=False, inserts=[(3.0, 5.0), (12.0, 14.0)]
    )
    assert "[1:v]setpts=PTS-STARTPTS+3.000/TB" in graph
    assert "[2:v]setpts=PTS-STARTPTS+12.000/TB" in graph
    assert "enable='between(t,3.000,5.000)'" in graph
    assert "enable='between(t,12.000,14.000)'" in graph


def test_graph_stopmotion_effect_runs_before_the_caption_pass() -> None:
    graph, _ = _graph(
        _plan(), Path("/tmp/x.ass"), fps=30.0, has_audio=True, effect="stopmotion"
    )
    # Dropped to ~12 updates/s then resampled back to the clip rate.
    assert "fps=12,fps=30.0" in graph
    # Captions are burned after the effect, so the text never judders with it.
    assert graph.index("fps=12") < graph.index("ass=f=")


def test_graph_punch_effect_crops_in() -> None:
    graph, _ = _graph(_plan(), None, fps=30.0, has_audio=True, effect="punch")
    assert "scale=1166:2074" in graph  # 1080x1920 * 1.08, rounded to even
    assert "crop=1080:1920" in graph


def test_graph_mixes_a_music_bed_under_the_speech() -> None:
    graph, has_audio = _graph(
        _plan(),
        None,
        fps=30.0,
        has_audio=True,
        duration=10.0,
        music_index=1,
        music_volume=0.2,
    )
    assert "[1:a]atrim=0:10.000" in graph
    assert "volume=0.200" in graph
    assert "amix=inputs=2" in graph
    assert "normalize=0" in graph  # speech stays at full level
    assert has_audio is True


def test_graph_plays_an_insert_sound_effect_at_its_window() -> None:
    graph, _ = _graph(
        _plan(),
        None,
        fps=30.0,
        has_audio=True,
        duration=10.0,
        sfx=[(1, 4.0, 6.0, 0.5)],
    )
    assert "adelay=4000|4000" in graph  # starts when the insert opens
    assert "volume=0.500" in graph
    assert "amix=inputs=2" in graph


def test_graph_with_only_music_needs_no_source_audio() -> None:
    graph, has_audio = _graph(
        _plan(), None, fps=30.0, has_audio=False, duration=5.0, music_index=1
    )
    # A silent source still gets a sound: the music bed is the one audio input
    # and is named ``[a]`` directly, so ``-map [a]`` keeps working.
    assert has_audio is True
    assert graph.endswith("[1:a]atrim=0:5.000,asetpts=PTS-STARTPTS,aresample=48000,volume=0.160[a]")


# ------------------------------------------------------------------- providers


def test_auto_provider_is_fixture_without_an_api_url() -> None:
    # Importing clipbait auto-loads a developer's .env, so pin the settings to a
    # clean "nothing configured" state instead of trusting the ambient env.
    from dataclasses import replace

    settings = replace(
        get_settings(), meme_api_url=None, meme_search_url=None, meme_provider="auto"
    )
    assert not settings.meme_api_url
    provider = get_meme_provider(settings)
    assert isinstance(provider, FixtureMemeProvider)


def test_api_provider_reports_name_and_availability() -> None:
    provider = ApiMemeProvider()
    assert provider.name == "api"
    # No URL configured in tests -> not available, and generate() explains why.
    assert provider.available() is False


def test_search_provider_prefers_the_first_resolvable_video_path(monkeypatch) -> None:
    # Giphy offers small fixed_height and a bigger original/hd; the ordered
    # paths must pick the best that a result actually has, per result.
    from dataclasses import replace

    from clipbait.memes import HttpMemeProvider

    settings = replace(
        get_settings(),
        meme_search_url="https://api.giphy.com/v1/gifs/search",
        meme_search_auth="query",
        meme_search_key="k",
        meme_search_results="data",
        meme_search_video="images.hd.mp4,images.original.mp4",
    )
    provider = HttpMemeProvider(settings)

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "data": [
                    {
                        "images": {
                            "hd": {"mp4": "https://cdn/hd1.mp4"},
                            "original": {"mp4": "https://cdn/o1.mp4"},
                        }
                    },
                    {"images": {"original": {"mp4": "https://cdn/o2.mp4"}}},
                ]
            }

    monkeypatch.setattr("clipbait.memes.providers.httpx.get", lambda *a, **k: _Resp())
    # Item 1 has hd -> take hd; item 2 has only original -> fall back to original.
    assert provider._search("shock") == ["https://cdn/hd1.mp4", "https://cdn/o2.mp4"]
    # An unset path list still means a single "url" path.
    assert HttpMemeProvider(replace(settings, meme_search_video=""))._video_paths() == ["url"]


def test_request_cache_key_is_stable_and_specific() -> None:
    base = MemeRequest(prompt="p", duration=2.0, width=1080, height=1920)
    same = MemeRequest(prompt="p", duration=2.0, width=1080, height=1920)
    other = MemeRequest(prompt="q", duration=2.0, width=1080, height=1920)
    assert base.cache_key == same.cache_key
    assert base.cache_key != other.cache_key


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_fixture_provider_and_prepare_insert_shape(tmp_path: Path) -> None:
    provider = FixtureMemeProvider()
    request = MemeRequest(prompt="reaction b-roll: wait what", duration=0.8, width=540, height=960)
    raw = provider.generate(request, tmp_path)
    assert raw is not None and raw.exists()

    prepared = prepare_insert(
        raw, tmp_path / "insert.mp4", width=540, height=960, duration=0.8, fps=30
    )
    info = media.probe(prepared)
    assert (info.width, info.height) == (540, 960)
    assert info.duration == pytest.approx(0.8, abs=0.15)
    # Silent: the clip's own audio must run underneath the insert.
    assert info.has_audio is False


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_fixture_provider_is_cached(tmp_path: Path) -> None:
    provider = FixtureMemeProvider()
    request = MemeRequest(prompt="cached", duration=0.6, width=320, height=568)
    first = provider.generate(request, tmp_path)
    mtime = first.stat().st_mtime_ns
    second = provider.generate(request, tmp_path)
    assert second == first
    assert second.stat().st_mtime_ns == mtime
