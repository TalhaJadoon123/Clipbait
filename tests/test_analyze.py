"""Highlight analysis: the LLM contract and the zero-key fallback.

No network: ``httpx.post`` is replaced everywhere, so the provider request
shapes and retry loop run for real without a key.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest
from helpers import scripted_transcript

from clipbait.analyze import (
    _chunks,
    _enrich,
    _hashtags,
    _parse_highlights,
    _transcript_slice,
    analyze,
    heuristics,
    llm_highlights,
    packaging_for,
    prompts,
    ranking,
)
from clipbait.analyze.heuristics import (
    _energy_in,
    _hook_line,
    _merge_short,
    _text_scores,
    _title_from,
    candidate_highlights,
    keywords,
    split_sentences,
)
from clipbait.analyze.llm import LLMClient, LLMError, LLMResult, _extract_json
from clipbait.config import get_settings
from clipbait.schema import (
    ClipScores,
    Highlight,
    ProjectOptions,
    Transcript,
    TranscriptSegment,
    Word,
)

# ------------------------------------------------------------------- json parsing


def test_extract_json_plain_fenced_and_embedded():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert _extract_json("here you go: {\"a\": 3} thanks") == {"a": 3}
    with pytest.raises(LLMError):
        _extract_json("```\nno json here```")


def test_extract_json_rejects_non_json():
    with pytest.raises(LLMError, match="non-JSON"):
        _extract_json("definitely not json")


# ------------------------------------------------------------------- llm client


def settings_with(**overrides):
    return dataclasses.replace(get_settings(), **overrides)


def client(provider: str, **overrides) -> LLMClient:
    field = {
        "openai": "openai_api_key",
        "anthropic": "anthropic_api_key",
        "gemini": "gemini_api_key",
    }.get(provider)
    values = {"llm_provider": provider}
    if field:
        values[field] = "test-key"
    values.update(overrides)
    return LLMClient(settings_with(**values))


def test_model_for_every_provider():
    assert client("openai").model == get_settings().openai_model
    assert client("anthropic").model == get_settings().anthropic_model
    assert client("gemini").model == get_settings().gemini_model
    assert client("ollama").model == get_settings().ollama_model
    none = LLMClient(settings_with(llm_provider="none"))
    assert none.provider is None
    assert none.model == "none"
    assert none.available is False


def test_complete_json_without_a_provider_raises():
    with pytest.raises(LLMError, match="no LLM provider"):
        LLMClient(settings_with(llm_provider="none")).complete_json("hi")


def fake_response(status=200, payload=None, text=""):
    return SimpleNamespace(
        status_code=status,
        text=text,
        json=lambda: payload if payload is not None else {},
    )


def test_openai_success(monkeypatch):
    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update({"url": url, "headers": headers, "json": json})
        return fake_response(
            payload={"choices": [{"message": {"content": '{"result": "ok"}'}}]}
        )

    monkeypatch.setattr("clipbait.analyze.llm.httpx.post", fake_post)
    result = client("openai").complete_json("prompt", system="be nice", temperature=0.1)
    assert isinstance(result, LLMResult)
    assert result.data == {"result": "ok"}
    assert result.provider == "openai"
    assert captured["json"]["messages"][0]["role"] == "system"
    assert captured["json"]["response_format"]["type"] == "json_object"
    assert captured["json"]["temperature"] == 0.1


def test_openai_error_status(monkeypatch):
    monkeypatch.setattr(
        "clipbait.analyze.llm.httpx.post",
        lambda *a, **k: fake_response(status=429, text="rate limited"),
    )
    monkeypatch.setattr("clipbait.analyze.llm.time.sleep", lambda *_: None)
    with pytest.raises(LLMError, match="429"):
        client("openai").complete_json("hi")


def test_anthropic_success_and_error(monkeypatch):
    monkeypatch.setattr(
        "clipbait.analyze.llm.httpx.post",
        lambda *a, **k: fake_response(
            payload={
                "content": [
                    {"type": "text", "text": "prefix"},
                    {"type": "thinking", "text": "ignore"},
                    {"type": "text", "text": '{"a": 1}'},
                ]
            }
        ),
    )
    assert client("anthropic").complete_json("hi", system="s").data == {"a": 1}

    monkeypatch.setattr(
        "clipbait.analyze.llm.httpx.post",
        lambda *a, **k: fake_response(status=500, text="boom"),
    )
    monkeypatch.setattr("clipbait.analyze.llm.time.sleep", lambda *_: None)
    with pytest.raises(LLMError, match="anthropic 500"):
        client("anthropic").complete_json("hi")


def test_gemini_success_and_error(monkeypatch):
    monkeypatch.setattr(
        "clipbait.analyze.llm.httpx.post",
        lambda *a, **k: fake_response(
            payload={"candidates": [{"content": {"parts": [{"text": '{"b": 2}'}]}}]}
        ),
    )
    assert client("gemini").complete_json("hi", system="s").data == {"b": 2}

    monkeypatch.setattr(
        "clipbait.analyze.llm.httpx.post",
        lambda *a, **k: fake_response(status=400, text="bad"),
    )
    monkeypatch.setattr("clipbait.analyze.llm.time.sleep", lambda *_: None)
    with pytest.raises(LLMError, match="gemini 400"):
        client("gemini").complete_json("hi")


def test_ollama_success_and_error(monkeypatch):
    captured: dict = {}

    def fake_post(url, json=None, **kwargs):
        captured.update({"url": url, "json": json})
        return fake_response(payload={"message": {"content": '{"c": 3}'}})

    monkeypatch.setattr("clipbait.analyze.llm.httpx.post", fake_post)
    assert client("ollama").complete_json("hi", system="s").data == {"c": 3}
    assert captured["url"].endswith("/api/chat")
    assert captured["json"]["format"] == "json"

    monkeypatch.setattr(
        "clipbait.analyze.llm.httpx.post",
        lambda *a, **k: fake_response(status=503, text="down"),
    )
    monkeypatch.setattr("clipbait.analyze.llm.time.sleep", lambda *_: None)
    with pytest.raises(LLMError, match="ollama 503"):
        client("ollama").complete_json("hi")


def test_complete_json_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky(*_a, **_k):
        calls["n"] += 1
        if calls["n"] < 3:
            return fake_response(status=500, text="nope")
        return fake_response(payload={"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr("clipbait.analyze.llm.httpx.post", flaky)
    monkeypatch.setattr("clipbait.analyze.llm.time.sleep", lambda *_: None)
    assert client("openai").complete_json("hi").data == {}
    assert calls["n"] == 3


def test_complete_json_exhausts_retries(monkeypatch):
    monkeypatch.setattr(
        "clipbait.analyze.llm.httpx.post",
        lambda *a, **k: fake_response(status=500, text="nope"),
    )
    monkeypatch.setattr("clipbait.analyze.llm.time.sleep", lambda *_: None)
    with pytest.raises(LLMError):
        client("openai").complete_json("hi")


# ------------------------------------------------------------------- transcript


def simple_transcript(duration: float = 60.0) -> Transcript:
    words = [
        Word(text=f"w{i}", start=float(i), end=float(i) + 0.5) for i in range(int(duration))
    ]
    segments = [
        TranscriptSegment(text=f"segment {i}", start=float(i * 10), end=float(i * 10 + 8))
        for i in range(int(duration // 10))
    ]
    return Transcript(language="en", duration=duration, words=words, segments=segments)


def test_chunks_short_and_long():
    assert _chunks(60.0) == [(0.0, 60.0)]
    assert _chunks(0.0) == [(0.0, 0.0)]
    chunks = _chunks(2000.0)
    assert len(chunks) >= 3
    assert chunks[0][0] == 0.0
    assert chunks[-1][1] == 2000.0
    # Each chunk overlaps the previous by CHUNK_OVERLAP seconds.
    assert chunks[1][0] == pytest.approx(chunks[0][1] - 45.0)


def test_transcript_slice_uses_segments_then_words():
    transcript = simple_transcript(30)
    sliced = _transcript_slice(transcript, 0.0, 25.0)
    assert "[0.0-8.0] segment 0" in sliced
    assert "[20.0-25.0] segment 2" in sliced

    words_only = Transcript(
        duration=5.0,
        words=[Word(text="hi", start=1.0, end=1.5), Word(text="there", start=2.0, end=2.5)],
    )
    assert _transcript_slice(words_only, 0.0, 3.0) == "hi there"


def test_parse_highlights_filters_and_scores():
    transcript = simple_transcript(120)
    payload = {
        "highlights": [
            {"start": 0, "end": 5, "title": "too short"},
            {"start": "bad", "end": 30},
            {
                "start": 10,
                "end": 40,
                "title": "Real clip",
                "hook": "h",
                "quote": "q",
                "reason": "r",
                "keywords": ["One", "TWO"],
                "scores": {"hook": 80, "emotion": 70, "value": 60, "share": 50},
            },
            {"start": 50, "end": 80},
        ]
    }
    parsed = _parse_highlights(payload, transcript, offset=0.0)
    assert len(parsed) == 2
    first = parsed[0]
    assert first.title == "Real clip"
    assert first.keywords == ["one", "two"]
    assert first.scores.overall > 0
    assert first.source == "llm"
    assert parsed[1].title == "Untitled clip"


def test_parse_highlights_applies_offset_and_clamps():
    transcript = simple_transcript(60)
    parsed = _parse_highlights(
        {"highlights": [{"start": 10, "end": 40, "scores": {"overall": 70}}]},
        transcript,
        offset=30,
    )
    assert parsed[0].start == 40.0
    assert parsed[0].end == 60.0


# ------------------------------------------------------------------- llm_highlights


class FakeLLM:
    def __init__(self, results=None, error=None) -> None:
        self.provider = "fake"
        self.model = "fake-1"
        self._results = list(results or [])
        self._error = error
        self.calls = 0

    @property
    def available(self) -> bool:
        return True

    def complete_json(self, prompt, system=None, temperature=0.3):
        self.calls += 1
        if self._error:
            raise self._error
        return LLMResult(data=self._results.pop(0), provider=self.provider, model=self.model)


def test_llm_highlights_skips_thin_chunks():
    llm = FakeLLM(results=[{"highlights": []}])
    transcript = Transcript(duration=60.0, segments=[TranscriptSegment(text="tiny", start=0, end=1)])
    with pytest.raises(LLMError, match="no usable highlights"):
        llm_highlights(transcript, ProjectOptions(), llm)
    assert llm.calls == 0


def test_llm_highlights_parses_a_chunk():
    transcript = simple_transcript(120)
    llm = FakeLLM(
        results=[
            {
                "content_type": "podcast",
                "density": "high",
                "highlights": [{"start": 10, "end": 50, "title": "Good"}],
            }
        ]
    )
    highlights = llm_highlights(transcript, ProjectOptions(), llm)
    assert len(highlights) == 1
    assert highlights[0].title == "Good"


def test_llm_highlights_survives_a_chunk_error():
    transcript = simple_transcript(900)

    class Sometimes(FakeLLM):
        def complete_json(self, prompt, system=None, temperature=0.3):
            self.calls += 1
            if self.calls == 1:
                raise LLMError("chunk failed")
            return LLMResult(
                data={"highlights": [{"start": 0, "end": 40, "title": "Later"}]},
                provider="fake",
                model="fake-1",
            )

    highlights = llm_highlights(transcript, ProjectOptions(), Sometimes())
    assert [h.title for h in highlights] == ["Later"]


# ------------------------------------------------------------------------ analyze


def llm_payload(**overrides) -> dict:
    payload = {
        "content_type": "podcast",
        "density": "medium",
        "highlights": [
            {
                "start": 5,
                "end": 40,
                "title": "First",
                "hook": "You need this",
                "quote": "quote",
                "reason": "reason",
                "keywords": ["growth"],
                "scores": {"hook": 90, "emotion": 80, "value": 70, "share": 60},
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_analyze_uses_the_llm_when_available(monkeypatch):
    transcript = scripted_transcript(60)
    fake = FakeLLM(results=[llm_payload()])
    highlights, meta = analyze(transcript, ProjectOptions(clips=1), llm=fake)

    assert highlights
    assert meta["provider"] == "fake"
    assert meta["model"] == "fake-1"
    assert meta["selected"] == len(highlights)
    assert meta["hashtags"][highlights[0].title]
    assert highlights[0].source == "llm"


def test_analyze_falls_back_after_an_llm_error():
    transcript = scripted_transcript(90)
    fake = FakeLLM(error=LLMError("quota"))
    highlights, meta = analyze(
        transcript, ProjectOptions(clips=2, target_seconds=30), llm=fake
    )
    assert meta["provider"] == "heuristic"
    assert meta["llm_error"]
    assert highlights


def test_analyze_skips_the_llm_when_disabled():
    transcript = scripted_transcript(90)
    fake = FakeLLM(results=[llm_payload()])
    _, meta = analyze(
        transcript, ProjectOptions(clips=2, target_seconds=30, llm=False), llm=fake
    )
    assert fake.calls == 0
    assert meta["provider"] == "heuristic"


def test_analyze_second_pass_when_selection_is_empty(monkeypatch):
    transcript = scripted_transcript(90)
    fake = FakeLLM(results=[llm_payload()])
    real_select = ranking.select
    calls = {"n": 0}

    def picky(candidates, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return []
        return real_select(candidates, **kwargs)

    monkeypatch.setattr(ranking, "select", picky)
    highlights, meta = analyze(
        transcript, ProjectOptions(clips=2, target_seconds=30), llm=fake
    )
    assert calls["n"] == 2
    assert meta["provider"] == "heuristic"
    assert highlights


def test_analyze_with_no_candidates_returns_nothing():
    transcript = Transcript(duration=5.0)
    highlights, meta = analyze(transcript, ProjectOptions(clips=1), llm=FakeLLM())
    assert highlights == []
    assert meta["selected"] == 0


# ------------------------------------------------------------------------ enrich


def test_enrich_fills_missing_fields():
    transcript = scripted_transcript(60)
    highlight = Highlight(start=0.5, end=20.0, title="Untitled clip", hook="Real hook")
    _enrich([highlight], transcript)
    assert highlight.hook == "Real hook"
    assert highlight.title == "Real hook"
    assert highlight.keywords
    assert highlight.quote
    assert highlight.reason


def test_enrich_keeps_existing_values():
    transcript = scripted_transcript(60)
    highlight = Highlight(
        start=0.5,
        end=20.0,
        title="Mine",
        hook="my hook",
        keywords=["a"],
        quote="q",
        reason="because",
    )
    _enrich([highlight], transcript)
    assert highlight.title == "Mine"
    assert highlight.hook == "my hook"
    assert highlight.keywords == ["a"]


def test_hashtags_and_packaging():
    highlight = Highlight(
        start=0,
        end=20,
        title="Clip",
        hook="Hook",
        quote="the line itself",
        reason="Reason",
        keywords=["growth", "ai", "ab"],
    )
    tags = _hashtags(highlight)
    assert tags[0] == "growth"
    assert "ab" not in tags
    assert "shorts" in tags
    assert len(tags) <= 8

    packaged = packaging_for(highlight, {"hashtags": {"Clip": ["#custom"]}})
    assert packaged["hashtags"] == ["#custom"]
    # The caption body is the spoken line, never the operator-facing reason.
    assert packaged["description"] == "the line itself"
    assert "Reason" not in packaged["description"]
    fallback = packaging_for(highlight, {})
    assert "shorts" in fallback["hashtags"]


# ---------------------------------------------------------------------- prompts


def test_prompts_render_the_contract():
    system = prompts.build_system_prompt(55)
    assert "HOOK (0-100)" in system
    assert "30-115s" in system or "min_seconds" not in system

    chunk = prompts.build_chunk_prompt("body", 1, 3, 600.0, "podcast", "high")
    assert "chunk 1 of 3" in chunk
    assert "body" in chunk


# -------------------------------------------------------------------- heuristics


def test_split_sentences_uses_segments_and_merges():
    transcript = Transcript(
        segments=[
            TranscriptSegment(text="One.", start=0.0, end=3.0),
            TranscriptSegment(text="  ", start=3.0, end=3.4),
            TranscriptSegment(text="Tiny.", start=3.4, end=3.8),
        ]
    )
    sentences = split_sentences(transcript)
    assert len(sentences) == 1
    assert sentences[0].text.startswith("One.")
    assert "Tiny." in sentences[0].text


def test_split_sentences_falls_back_to_words():
    transcript = Transcript(
        words=[
            Word(text="Hello", start=0.0, end=0.7),
            Word(text="there.", start=0.7, end=1.6),
            Word(text="Second", start=1.7, end=2.3),
            Word(text="sentence!", start=2.3, end=3.1),
        ]
    )
    sentences = split_sentences(transcript)
    assert [s.text for s in sentences] == ["Hello there.", "Second sentence!"]


def test_split_sentences_without_data():
    assert split_sentences(Transcript()) == []
    assert _merge_short([]) == []


def test_text_scores_rewards_signal_words():
    strong = _text_scores(
        "Nobody talks about this insane secret. I was completely wrong and lost 99% of my money!",
        energy=1.0,
    )
    weak = _text_scores("And then we talked about the weather for a while.", energy=0.0)
    assert strong.overall > weak.overall
    assert strong.hook >= weak.hook
    assert 0 <= strong.overall <= 100


def test_text_scores_penalises_filler():
    clean = _text_scores("The system works because the process is simple.")
    filler = _text_scores("Um, you know, like, I mean, basically the system works.")
    assert filler.overall <= clean.overall


def test_energy_in_variants():
    assert _energy_in([], 0, 10) == 0.5
    assert _energy_in([(100.0, 0.9)], 0, 10) == 0.5
    loud = _energy_in([(1.0, 0.9), (2.0, 0.8)], 0, 10)
    assert 0.5 < loud <= 1.0


def test_hook_line_and_title():
    assert _hook_line("Short line. Second.") == "Short line."
    long = _hook_line("word " * 40, limit=30)
    assert long.endswith("…")
    assert len(long) <= 30

    assert _title_from("so this is the important thing about growth") == (
        "This is the important thing about growth"
    )
    assert _title_from("one two three four five six seven eight nine ten") == (
        "One two three four five six seven eight"
    )
    assert _title_from("") == "Untitled clip"


def test_keywords_rank_by_frequency_without_stopwords():
    tags = keywords("growth growth system system system the and of growth")
    assert tags[0] == "growth"
    assert "growth" in tags
    assert "the" not in tags
    assert "and" not in tags


def test_candidate_highlights_from_a_real_transcript():
    transcript = scripted_transcript(120)
    candidates = candidate_highlights(
        transcript, target_seconds=30, max_clips=4, energy=[(float(t), 0.8) for t in range(120)]
    )
    assert candidates
    for candidate in candidates:
        assert candidate.duration >= 18.0
        assert candidate.source == "heuristic"
        assert candidate.scores.overall >= 0


def test_candidate_highlights_handles_empty_and_short():
    assert candidate_highlights(Transcript()) == []
    short = Transcript(
        segments=[TranscriptSegment(text="Too short to clip.", start=0.0, end=2.0)]
    )
    assert candidate_highlights(short) == []


def test_candidate_highlights_skips_low_token_windows():
    transcript = Transcript(
        segments=[TranscriptSegment(text="word " * 5, start=0.0, end=40.0)]
    )
    assert candidate_highlights(transcript, target_seconds=15) == []


def test_heuristics_module_constants_are_used():
    scores: ClipScores = heuristics._text_scores("haha lol this is hilarious")
    assert scores.emotion > 18
    assert json.dumps(scores.model_dump())  # serialisable, no numpy leakage
