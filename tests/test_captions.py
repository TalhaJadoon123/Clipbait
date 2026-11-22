"""ASS caption generation.

Pins the two things that broke in practice: the style line must format (the
``-1 if bold`` regression) and one Dialogue event per word must actually be
emitted, because that is what makes karaoke highlighting flicker-free.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clipbait.captions.ass import (
    ass_color,
    ass_time,
    build_captions,
    build_chat_overlay,
    build_hook,
    escape_text,
    write_ass,
)
from clipbait.schema import ChatMessage, Word

WORDS = [
    Word(text="Nobody", start=10.0, end=10.4),
    Word(text="talks", start=10.45, end=10.8),
    Word(text="about", start=10.85, end=11.1),
    Word(text="this", start=11.15, end=11.4),
]


def test_time_and_colour_helpers() -> None:
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(3661.25) == "1:01:01.25"
    assert ass_color("#C6FF3D") == "&H003DFFC6&"
    assert ass_color("nope") == "&H00FFFFFF&"
    assert escape_text("{delete}\\me\nnow") == "(delete)\\\\me now"


@pytest.mark.parametrize("preset", ["default", "hormozi", "pill", "lime", "oneword", "cine", "clean"])
def test_every_preset_renders_a_valid_header(preset: str) -> None:
    document = build_captions(WORDS, 10.0, 12.0, preset)
    assert "[Script Info]" in document
    assert "Style: Caption," in document
    assert "PlayResX: 1080" in document
    assert "PlayResY: 1920" in document


def test_one_dialogue_event_per_word() -> None:
    document = build_captions(WORDS, 10.0, 12.0, "default")
    events = [line for line in document.splitlines() if line.startswith("Dialogue:")]
    assert len(events) == len(WORDS)
    assert all(line.startswith("Dialogue: 0,") for line in events)


def test_timestamps_are_relative_to_clip_start() -> None:
    document = build_captions(WORDS, 10.0, 12.0, "default")
    first = [line for line in document.splitlines() if line.startswith("Dialogue:")][0]
    assert first.split(",")[1] == "0:00:00.00"


def test_active_word_is_highlighted_and_future_words_dimmed() -> None:
    document = build_captions(WORDS, 10.0, 12.0, "default")
    first = [line for line in document.splitlines() if line.startswith("Dialogue:")][0]
    assert "\\c&H003DFFC6&" in first  # lime highlight on the spoken word
    assert "NOBODY" in first.upper()
    assert "TALKS" in first.upper()


def test_hook_wraps_long_titles() -> None:
    short = build_hook("Short hook")
    assert short.startswith("Dialogue: 1,")
    assert "\\N" not in short

    long = build_hook("This hook is definitely longer than twenty six characters")
    assert long.count("\\N") == 1


def test_chat_overlay_is_bounded_and_timed() -> None:
    messages = [
        ChatMessage(t=20.0 + index, author=f"user{index}", text=f"message {index}")
        for index in range(20)
    ]
    overlay = build_chat_overlay(messages, 19.0, 40.0, max_messages=5)
    lines = [line for line in overlay.splitlines() if line.startswith("Dialogue: 2,")]
    assert len(lines) <= 5
    assert "message 0" in lines[0]


def test_chat_overlay_drops_what_was_said_before_the_clip() -> None:
    """A clamped pre-roll stacks stale lines on frame one; drop them instead."""
    messages = [
        ChatMessage(t=1.0, author="early", text="nothing to do with the clip"),
        ChatMessage(t=30.5, author="fan", text="NO WAY"),
    ]
    overlay = build_chat_overlay(messages, 30.0, 45.0)
    assert "nothing to do with the clip" not in overlay
    assert "NO WAY" in overlay
    assert "Dialogue: 2,0:00:00.50" in overlay


def test_chat_overlay_lines_never_overlap() -> None:
    """Three reactions at the same instant must not print on top of each other."""
    messages = [
        ChatMessage(t=5.0, author="a", text="first"),
        ChatMessage(t=5.0, author="b", text="second"),
        ChatMessage(t=5.0, author="c", text="third"),
    ]
    overlay = build_chat_overlay(messages, 0.0, 30.0, hold=2.0)
    spans = []
    for line in overlay.splitlines():
        if not line.startswith("Dialogue: 2,"):
            continue
        _, start, end, *_ = line.split(",", 4)
        spans.append((start, end))
    assert len(spans) == 3
    for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
        assert start >= end


def test_chat_overlay_hides_handles_by_default() -> None:
    overlay = build_chat_overlay([ChatMessage(t=1.0, author="loudmouth", text="WW")], 0.0, 20.0)
    assert "loudmouth" not in overlay
    assert "WW" in overlay


def test_write_ass_combines_captions_hook_and_chat(tmp_path: Path) -> None:
    target = tmp_path / "clip.ass"
    write_ass(
        target,
        WORDS,
        10.0,
        12.0,
        "hormozi",
        hook="Nobody talks about this",
        chat=[ChatMessage(t=10.5, author="fan", text="CLIP IT")],
    )
    text = target.read_text(encoding="utf-8")
    assert text.count("Dialogue: 0,") == len(WORDS)
    assert "Dialogue: 1," in text
    assert "Dialogue: 2," in text
    assert "CLIP IT" in text


def test_empty_word_list_still_writes_a_header(tmp_path: Path) -> None:
    target = write_ass(tmp_path / "empty.ass", [], 0.0, 5.0)
    text = target.read_text(encoding="utf-8")
    assert "[Script Info]" in text
    assert "Dialogue:" not in text

def test_speed_preset_cycles_emphasis_colours() -> None:
    from clipbait.captions.ass import build_captions
    from clipbait.schema import Word

    words = [
        Word(text=text, start=index * 0.5, end=index * 0.5 + 0.45, prob=1.0)
        for index, text in enumerate("he just flew into vice city".split())
    ]
    doc = build_captions(words, 0.0, 4.0, "speed")
    # Centre alignment (5) and at least two distinct palette colours in play.
    assert ",5," in doc
    colors = {line.split(r"\c")[1][:9] for line in doc.splitlines() if r"\c&H" in line}
    assert len(colors) >= 2


def test_noise_tokens_never_reach_burned_captions() -> None:
    # A transcript cached before the ingestion filter landed still has YouTube's
    # "[ __ ]"/"[Music]"/"♪" stage cues; build_captions must drop them so they
    # are never drawn on screen.
    from clipbait.text import is_caption_noise

    for noisy in ("[\u00a0__\u00a0]", "[Music]", "[laughter]", "\u266a", "...", "__"):
        assert is_caption_noise(noisy)
    for spoken in ("real", "GTA", "don't", "100%"):
        assert not is_caption_noise(spoken)

    words = [
        Word(text="[\u00a0__\u00a0]", start=0.0, end=0.4),
        Word(text="real", start=0.4, end=0.9),
        Word(text="\u266a", start=0.9, end=1.3),
    ]
    doc = build_captions(words, 0.0, 2.0, "speed")
    assert "__" not in doc
    assert "\u266a" not in doc
    assert "real" in doc.lower()
    assert len([line for line in doc.splitlines() if line.startswith("Dialogue:")]) == 1
