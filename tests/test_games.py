"""Game detection: the tags a stream's posts should carry, read from its text."""

from clipbait.virality.games import GAMES, detect, name_of, tags_of


def test_detects_gta_6_from_every_spelling() -> None:
    for text in ("GTA 6", "gta6", "Grand Theft Auto VI", "gta vi"):
        name, tags = detect(text)
        assert name == "Grand Theft Auto VI", text
        assert tags[0] == "GTA6"


def test_longest_alias_wins_so_gta_5_is_not_gta_6() -> None:
    assert detect("grand theft auto 5 online")[0] == "Grand Theft Auto V"
    assert detect("gta 5 roleplay")[0] == "Grand Theft Auto V"


def test_unknown_text_names_no_game() -> None:
    assert detect("just chatting with the boys") == (None, [])
    assert detect("", None)[0] is None  # type: ignore[arg-type]


def test_detect_reads_several_texts_together() -> None:
    # The game may be in the title while the words say nothing about it.
    name, tags = detect("Speed plays the new update", "fortnite battle royale", "")
    assert name == "Fortnite"
    assert "Fortnite" in tags


def test_name_of_resolves_a_name_alias_or_tag() -> None:
    assert name_of("gta 6") == "Grand Theft Auto VI"
    assert name_of("GTA6Game") == "Grand Theft Auto VI"
    assert name_of("Grand Theft Auto VI") == "Grand Theft Auto VI"
    assert name_of("Some Unknown Game") == "Some Unknown Game"
    assert name_of(None) == ""


def test_tags_of_resolves_and_falls_back_to_a_cleaned_name() -> None:
    assert tags_of("gta 6")[:1] == ["GTA6"]
    assert tags_of("vicecity")[:1] == ["GTA6"]
    assert tags_of("My Neat Game!") == ["MyNeatGame"]
    assert tags_of("") == []


def test_every_game_has_at_least_one_alias_and_tag() -> None:
    for game in GAMES:
        assert game.name
        assert game.aliases
        assert game.tags
