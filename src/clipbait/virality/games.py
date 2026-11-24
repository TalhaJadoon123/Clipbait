"""Which game a stream is playing, and the tags its posts should carry.

Ported from PeakCut's ``studio/games.py`` (MIT, (c) 2026 PeakCut contributors)
into Clipbait's typed idiom. A gaming stream says which game it is: in its
title, in the category a platform shows, in the channel name. Knowing the game
sharpens the copy and puts the tags people actually search for (``#GTA6``) on a
post instead of only ``#gaming``.

The game is read from text only. It is a hint for the tags and the prompt, never
a filter: a stream whose title says GTA 6 but which is really a chat segment
still gets clips, they just carry GTA tags.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Game:
    """One game, the spellings that name it and the tags its posts should carry."""

    name: str
    aliases: tuple[str, ...]
    tags: tuple[str, ...]


GAMES: tuple[Game, ...] = (
    Game(
        "Grand Theft Auto VI",
        ("grand theft auto vi", "grand theft auto 6", "gta vi", "gta 6", "gta6"),
        ("GTA6", "GTA6Game", "ViceCity", "RockstarGames", "GTA6Launch"),
    ),
    Game(
        "Grand Theft Auto V",
        (
            "grand theft auto v",
            "grand theft auto 5",
            "gta online",
            "gta v",
            "gta 5",
            "gta5",
            "gtav",
        ),
        ("GTA5", "GTAOnline", "RockstarGames", "GTA5RP"),
    ),
    Game(
        "Grand Theft Auto IV", ("grand theft auto iv", "gta 4", "gta4"), ("GTA4", "RockstarGames")
    ),
    Game("Fortnite", ("fortnite",), ("Fortnite", "FortniteOG", "BattleRoyale")),
    Game("Minecraft", ("minecraft",), ("Minecraft", "MinecraftGame")),
    Game("Valorant", ("valorant",), ("Valorant", "ValRadiant")),
    Game(
        "League of Legends",
        ("league of legends", "league of legend"),
        ("LeagueOfLegends", "LoL", "RiotGames"),
    ),
    Game(
        "Counter-Strike",
        ("counter-strike", "counter strike", "cs2", "csgo"),
        ("CS2", "CounterStrike", "Esports"),
    ),
    Game("Elden Ring", ("elden ring",), ("EldenRing", "SoulsLike", "FromSoftware")),
    Game("Palworld", ("palworld",), ("Palworld", "Survival")),
    Game("Call of Duty", ("call of duty", "warzone"), ("CallOfDuty", "Warzone", "COD")),
    Game(
        "Helldivers 2",
        ("helldivers 2", "helldivers2"),
        ("Helldivers2", "StarshipTroopers", "CoOp"),
    ),
    Game("Cyberpunk 2077", ("cyberpunk 2077", "cyberpunk"), ("Cyberpunk2077", "NightCity")),
    Game("Starfield", ("starfield",), ("Starfield", "Bethesda")),
    Game("Black Myth Wukong", ("black myth", "wukong"), ("BlackMythWukong", "ActionRPG")),
    Game("Marvel Rivals", ("marvel rivals",), ("MarvelRivals", "Heroes")),
    Game("Overwatch 2", ("overwatch 2", "overwatch"), ("Overwatch2", "OW2", "Blizzard")),
    Game("ARK", ("ark: survival", "ark survival"), ("ARK", "Survival")),
    Game("Dead by Daylight", ("dead by daylight",), ("DeadByDaylight", "Asymmetric")),
    Game("Rocket League", ("rocket league",), ("RocketLeague", "CarSports")),
    Game("Apex Legends", ("apex legends",), ("ApexLegends", "EApex")),
    Game("PUBG", ("pubg", "playerunknown"), ("PUBG", "BattleRoyale")),
    Game(
        "Grand Theft Auto: San Andreas",
        ("grand theft auto san andreas", "gta sa"),
        ("GTA", "SanAndreas"),
    ),
)

# For a stream whose game nobody can name.
GENERIC: tuple[str, ...] = ("Gaming", "Gameplay", "GamingClips", "StreamHighlights")

# Longest alias first, so "gta 6" is read before "gta".
_ALIASES: tuple[tuple[str, str, tuple[str, ...]], ...] = tuple(
    sorted(
        ((alias, game.name, game.tags) for game in GAMES for alias in game.aliases),
        key=lambda item: -len(item[0]),
    )
)

_WORD = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    return _WORD.sub(" ", str(text or "").lower()).strip()


def detect(*texts: str) -> tuple[str | None, list[str]]:
    """The game named in the text, and the tags that belong on its posts.

    Returns ``(None, [])`` when no known game is named.
    """
    haystack = _norm(" ".join(str(text or "") for text in texts))
    if not haystack:
        return None, []
    for alias, name, tags in _ALIASES:
        if re.search(r"(?:^| )" + re.escape(_norm(alias)) + r"(?: |$)", haystack):
            return name, list(tags)
    return None, []


def name_of(game: str | None) -> str:
    """The game's proper name, given a name, an alias or one of its tags."""
    if not game:
        return ""
    normalized = _norm(game)
    for entry in GAMES:
        if (
            normalized == _norm(entry.name)
            or normalized in {_norm(a) for a in entry.aliases}
            or normalized in {_norm(t) for t in entry.tags}
        ):
            return entry.name
    return str(game)[:40]


def tags_of(game: str | None) -> list[str]:
    """The tags for a game, whether it is named in full, in short, or by one of its tags."""
    if not game:
        return []
    normalized = _norm(game)
    for entry in GAMES:
        if (
            normalized == _norm(entry.name)
            or normalized in {_norm(a) for a in entry.aliases}
            or normalized in {_norm(t) for t in entry.tags}
        ):
            return list(entry.tags)
    cleaned = re.sub(r"\W", "", str(game))[:30]
    return [cleaned] if cleaned else []
