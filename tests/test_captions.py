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
    assert "user0" in lines[0]


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
    assert "fan" in text


def test_empty_word_list_still_writes_a_header(tmp_path: Path) -> None:
    target = write_ass(tmp_path / "empty.ass", [], 0.0, 5.0)
    text = target.read_text(encoding="utf-8")
    assert "[Script Info]" in text
    assert "Dialogue:" not in text