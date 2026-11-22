"""The reference-channel title shape, built only from what the clip says."""

from clipbait.analyze.packaging import speed_tags, speed_title


def test_finds_object_composes_reference_title() -> None:
    title = speed_title("Bro I just found his family name in the GTA 6 menu, look at this")
    assert title.startswith("Speed Finds ")
    assert " in GTA 6" in title


def test_crazy_on_npcs_keeps_the_preposition() -> None:
    title = speed_title("he goes crazy on these GTA 6 NPCs every single time dude")
    assert title.startswith("Speed Goes CRAZY on ")
    assert " in GTA 6" not in title


def test_no_recognisable_action_falls_back_to_empty() -> None:
    # Nothing here is an action we can honestly package, so we refuse and the
    # caller keeps the plain transcript title.
    assert speed_title("alright chat lets see what happens next here") == ""


def test_tone_picks_a_matching_emoji() -> None:
    funny = speed_title("he finds the funniest thing and everyone laughs so hard")
    assert "😂" in funny or "💀" in funny


def test_speed_tags_include_the_channel_discovery_block() -> None:
    tags = speed_tags(["npc", "bathroom"])
    for expected in ("ishowspeed", "speed", "gta6", "shorts"):
        assert expected in tags
    assert "npc" in tags


def test_verb_preposition_is_not_repeated() -> None:
    title = speed_title("he goes crazy on these GTA 6 NPCs every single time dude")
    assert " on on " not in title
    assert " on On " not in title
    assert "for for" not in speed_title("he is not ready for the ending of this mission")


def test_clause_filler_never_becomes_the_object() -> None:
    # "and they was acting weird" must not turn into "Meets and They".
    assert speed_title("bro i just met and they was acting weird the whole time") == ""
    # A trailing clause word is trimmed off the object.
    title = speed_title("he goes crazy on these GTA 6 NPCs every single time dude")
    assert not title.rstrip("😭🔥💀😂 ").endswith("Every")


def test_possessive_object_is_kept() -> None:
    assert speed_title("Bro I just found his family name in the GTA 6 menu") == (
        "Speed Finds His Family Name in GTA 6 😭💀"
    )
