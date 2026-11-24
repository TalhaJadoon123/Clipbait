"""Hindi and Urdu spelled in Roman letters, so a burned caption is always drawable.

The caption fonts are Latin-only, so a Devanagari or Urdu transcript would reach
the screen as empty boxes. These are the romanisation rules and their wiring into
the ASS renderer.
"""

from __future__ import annotations

from clipbait.captions.ass import build_captions, build_chat_overlay, build_hook
from clipbait.captions.roman import caption_words, drawable, needs_roman, to_roman
from clipbait.schema import ChatMessage, Word

SAMPLES = [
    "नमस्ते दोस्त",
    "यार क्या है",
    "یار کیا ہے",
    "привет мир",
    "γεια σου",
    "안녕하세요",
]


def test_hindi_is_spelled_the_way_people_type_it() -> None:
    assert to_roman("नमस्ते दोस्त") == "namaste dost"
    assert to_roman("चूना जो उसको") == "chuna jo usko"
    assert to_roman("यार क्या है") == "yaar kya hai"


def test_hindi_common_words_beat_the_letter_rules() -> None:
    # "main"/"nahi" are in the word list because the rules would not guess them.
    assert to_roman("मैं नहीं जानता").startswith("main nahi ")


def test_hinglish_sentence_keeps_its_english() -> None:
    assert to_roman("यार GTA 6 में आज क्या है") == "yaar GTA 6 mein aaj kya hai"


def test_urdu_is_spelled_from_its_letters() -> None:
    assert to_roman("یار کیا ہے") == "yaar kya hai"


def test_latin_text_is_left_exactly_as_it_is() -> None:
    text = "hello world, this is GTA 6!"
    assert needs_roman(text) is False
    assert to_roman(text) == text


def test_needs_roman_spots_every_script_the_fonts_lack() -> None:
    for text in SAMPLES:
        assert needs_roman(text), text
    assert needs_roman("مرحبا")  # Arabic
    assert not needs_roman("café")  # an accented Latin letter is drawable
    assert not needs_roman("")


def test_every_sample_becomes_drawable() -> None:
    for text in SAMPLES:
        assert not drawable(text) or text.isascii(), text
        assert drawable(to_roman(text)), text


def test_drawable_matches_the_latin_caption_fonts() -> None:
    assert drawable("Speed goes CRAZY 😭")
    assert drawable("café")
    assert not drawable("नमस्ते")
    assert not drawable("یار")


def test_nothing_is_mutated_and_timing_is_preserved() -> None:
    words = [
        Word(text="यार", start=1.0, end=1.4, prob=0.9),
        Word(text="hello", start=1.5, end=2.0),
    ]
    out = caption_words(words)
    assert [word.text for word in out] == ["yaar", "hello"]
    assert [(word.start, word.end) for word in out] == [(1.0, 1.4), (1.5, 2.0)]
    assert out[1] is words[1]  # a drawable word is passed through, not copied
    assert words[0].text == "यार"  # the transcript itself is untouched


def test_captions_burn_roman_letters_for_a_hindi_transcript() -> None:
    words = [
        Word(text=text, start=index * 0.5, end=index * 0.5 + 0.45)
        for index, text in enumerate(["यार", "क्या", "है"])
    ]
    document = build_captions(words, 0.0, 2.0, "speed")
    assert "yaar" in document.lower()
    assert "kya" in document.lower()
    assert "यार" not in document


def test_hook_is_romanized() -> None:
    event = build_hook("नमस्ते दोस्त")
    assert "namaste dost" in event.lower()
    assert "नमस्ते" not in event


def test_chat_overlay_is_romanized() -> None:
    messages = [ChatMessage(t=0.5, author="یار", text="क्या बात है")]
    lines = build_chat_overlay(messages, 0.0, 5.0, show_authors=True)
    assert "kya baat hai" in lines.lower()
    assert "yaar:" in lines.lower()
    assert "क्या" not in lines
