"""Hashtag building: three to five bare tags picked from what a clip shows."""

from clipbait.virality.hashtags import (
    channel_tag,
    keywords,
    make,
    numbered,
    prefixed,
    tag,
)


def test_tag_keeps_words_only() -> None:
    assert tag("Growth") == "growth"
    assert tag("AI Agents") == "aiagents"
    assert tag("ab") == ""  # too short
    assert tag("2024") == ""  # a bare number is not a tag
    assert tag("the") == ""  # a stop word
    assert tag("") == ""


def test_hinglish_and_urdu_stopwords_are_dropped() -> None:
    for word in ("yaar", "bhai", "kya", "hai", "acha"):
        assert tag(word) == ""


def test_channel_tag_strips_platform_noise() -> None:
    assert channel_tag("Kai Cenat Live") == "kaicenat"
    assert channel_tag("IShowSpeed") == "ishowspeed"
    assert channel_tag("Twitch Gaming Clips") == ""  # everything is noise


def test_numbered_reads_a_name_with_a_number() -> None:
    assert numbered("GTA 6 launch trailer") == ["gta6"]
    assert numbered("Apex 2 is coming") == ["apex2"]
    assert numbered("no numbers here") == []


def test_keywords_keep_what_repeats_or_names_itself() -> None:
    assert "shot" in keywords("he hit the shot the shot from across the map")
    assert keywords("today Speed went absolutely wild") == ["speed"]
    assert keywords("a b c d e") == []


def test_make_blends_game_channel_words_and_ai_tags() -> None:
    tags = make(
        text="he found the npc in the bathroom",
        title="GTA 6 NPC moment",
        uploader="IShowSpeed",
        game="gta 6",
        ai_tags=["npc", "bathroom"],
    )
    assert tags[0] == "gta6"  # the game leads
    assert "ishowspeed" in tags
    assert "npc" in tags
    assert len(tags) <= 5


def test_make_falls_back_to_filler_when_nothing_is_known() -> None:
    assert make() == ["shorts", "clips", "viral"]
    assert make(limit=2, ai_tags=["alpha", "bravo", "charlie"]) == ["alpha", "bravo"]


def test_prefixed_adds_the_hash() -> None:
    assert prefixed(["gta6", "shorts"]) == ["#gta6", "#shorts"]
    assert prefixed([]) == []
