"""Reference-style packaging for IShowSpeed Shorts.

The channel this project imitates (``@Sky_aep77``) packages almost every clip
with the same shape::

    Speed <VERB> <SUPERLATIVE/OBJECT> in GTA 6 <EMOJI>

e.g. "Speed Goes CRAZY on GTA 6 NPCs 😭💀", "Speed Was NOT Ready for GTA 6's
Ending 😭", "Speed Finds The Weirdest GTA 6 NPC PEEING 😭💀".

This module builds that shape from the clip's own transcript. It is deliberately
conservative: a title is only composed when the transcript actually contains a
recognisable action and an object or proper noun to hang it on. When nothing
matches, it returns ``""`` and the caller keeps the plain transcript title
rather than inventing a claim the footage cannot back up.
"""

from __future__ import annotations

import re

# Verb phrasings, most specific first. Each entry is (pattern, rendered verb).
# ``{obj}`` is substituted with the detected object (may already read as a noun
# phrase such as "GTA 6 NPCs").
_VERBS: tuple[tuple[str, str], ...] = (
    (r"\b(?:went|goes|going|is going)\s+(?:absolutely\s+)?crazy\b", "Goes CRAZY on"),
    (r"\bcan'?t believe\b|\bcannot believe\b", "Can't Believe"),
    (r"\bnot ready\b|\bwasn'?t ready\b", "Was NOT Ready for"),
    (r"\b(?:found|finds|finding)\b", "Finds"),
    (r"\b(?:tried|tries|trying)\s+to\b", "Tries to"),
    (r"\b(?:became|becomes|becoming)\b", "Becomes"),
    (r"\b(?:attacked|attacks)\b", "Gets Attacked by"),
    (r"\b(?:picked|picks|picking)\s+(?:him|her|it|them|up)\b", "Picks Up"),
    (r"\b(?:saved|saves|saving)\b", "Saves"),
    (r"\b(?:helped|helps|helping)\b", "Helps"),
    (r"\b(?:escaped|escapes|escaping)\b", "Escapes"),
    (r"\b(?:beat|beats|beating)\b", "Beats"),
    (r"\b(?:stole|steals|stealing)\b", "Steals"),
    (r"\b(?:ruined|ruins|ruining)\b", "Ruins"),
    (r"\b(?:backflip|backflips|backflipped)\b", "Backflips in"),
    (r"\b(?:got|gets|getting)\s+(?:the\s+)?girl\b", "Got The Girl"),
    (r"\b(?:met|meets|meeting)\b", "Meets"),
    (r"\b(?:reacts?|reacted)\s+to\b", "Reacts to"),
    (r"\b(?:showed|shows|showing)\b", "Shows"),
    (r"\b(?:got|gets|getting)\b", "Gets"),
    (r"\b(?:did|does|doing)\b", "Does"),
    (r"\b(?:is|was)\s+(?:so\s+)?(?:funny|hilarious)\b", "Is Too Funny in"),
)

# Tone -> trailing emoji, matching the reference channel's use of 😭💀 / ❤️🥹 / 😭🔥.
_TONE_EMOJI: tuple[tuple[tuple[str, ...], str], ...] = (
    (("laugh", "lmao", "haha", "funny", "hilarious", "lol", "joke"), "😂💀"),
    (("love", "thank", "sweet", "kind", "kindness", "heart", "care", "hug"), "❤️🥹"),
    (("crazy", "insane", "unbelievable", "wild", "cops", "police", "chase"), "😭🔥"),
    (("cry", "crying", "sad", "died", "dead", "shock", "scared"), "😭💀"),
)

# Words that read as the object of a Speed clip when nothing better is found.
_OBJECT_FALLBACKS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("npc", "npcs", "ped", "peds"), "GTA 6 NPCs"),
    (("police", "cop", "cops", "wanted"), "GTA 6 Police"),
    (("car", "cars", "vehicle", "truck"), "a GTA 6 Car"),
    (("mission", "heist", "ending", "final"), "GTA 6's Ending"),
    (("store", "shop", "mall", "restaurant"), "GTA 6's Weirdest Store"),
    (("girl", "woman", "girlfriend"), "The Girl in GTA 6"),
    (("dog", "cat", "animal", "alligator", "shark"), "a GTA 6 Animal"),
)

_QUOTED = re.compile(r"[\"“']([^\"”']{2,30})[\"”']")
_PROPER = re.compile(r"\b([A-Z][a-z]{2,})(?:\s+([A-Z][a-z]{2,}))?")
_STOP = {
    "the", "and", "but", "you", "that", "this", "with", "have", "what", "your",
    "for", "not", "are", "was", "his", "her", "him", "she", "they", "them",
    "from", "they're", "there", "here", "just", "like", "gonna", "okay", "yeah",
    "what's", "it's", "i'm", "don't", "can't", "speed", "gta", "game", "games",
    "play", "playing", "stream", "streamer", "chat", "lol", "lmao",
}
# Words we strip from the front of a phrase grabbed after the verb, and the
# words that end it (the object is the noun phrase right after the action).
# Articles (a/an/the) are kept so "Steals a Car" does not read as "Steals Car";
# only demonstratives and a leading preposition are dropped, because the verb
# already ends in that preposition ("Goes CRAZY on GTA 6 NPCs", not "on on").
_LEAD_SKIP = {"these", "those", "some", "another", "any"}
_PREPS = {
    "on", "in", "at", "for", "to", "by", "with", "of", "from", "into", "over",
    "under", "about", "off", "out", "up", "down",
}
_PHRASE_STOP = {
    "in", "and", "but", "that", "so", "because", "when", "then", "to", "for",
    "with", "on", "at", "is", "was", "like", "about", "into", "over", "up",
    "down", "out", "his", "her", "their", "your", "my",
}
_PRONOUNS = {
    "him", "her", "them", "me", "us", "you", "it", "this", "that", "these",
    "those", "they", "he", "she", "we", "i",
}
_SMALL_WORDS = {"in", "on", "of", "the", "a", "an", "to", "for", "and", "at", "by"}
# Trailing filler that ends a spoken clause but is never part of the object.
_TRAIL_FILLER = {
    "every", "single", "one", "just", "like", "really", "so", "too", "bro",
    "dude", "oh", "my", "god", "man", "nah", "yo", "right", "now", "here",
    "there", "again", "though", "anyway",
}
_TRAIL_STOP = _PHRASE_STOP | _PREPS | _PRONOUNS | _TRAIL_FILLER


def _phrase_after(text: str, position: int) -> str:
    """The noun phrase right after the matched verb, e.g. "his family name".

    Leading prepositions, demonstratives and pronouns are dropped so a verb that
    already carries its preposition ("Goes CRAZY **on**", "NOT Ready **for**")
    does not yield "on on These" or "for for the", and a clause that starts with
    "and they..." does not become an object.
    """
    words = re.findall(r"[A-Za-z0-9']+", text[position:])
    picked: list[str] = []
    for word in words:
        low = word.lower()
        if not picked:
            if low in _PREPS or low in _LEAD_SKIP:
                continue
            if low in _PRONOUNS:
                return ""
            # A leading possessive ("his family name") is part of the object;
            # other leading stop words are filler from the clause before it.
            if low in _PHRASE_STOP and low not in {"his", "her", "their", "your", "my"}:
                continue
        if low in _PHRASE_STOP and picked:
            break
        picked.append(word)
        if len(picked) >= 4:
            break
    while picked and picked[-1].lower() in _TRAIL_STOP:
        picked.pop()
    phrase = " ".join(picked).strip()
    # A lone short filler ("it", "oh", "so") is not an object.
    if len(phrase) < 4 or len(picked) == 1 and len(picked[0]) < 4:
        return ""
    return " ".join(
        word if word.lower() in _SMALL_WORDS else word[:1].upper() + word[1:]
        for word in phrase.split()
    )


def _tone_emoji(text: str) -> str:
    low = text.lower()
    for words, emoji in _TONE_EMOJI:
        if any(word in low for word in words):
            return emoji
    return "😭💀"


def _named_object(text: str) -> str:
    """A quoted shout or a capitalised name — the strongest object signals."""
    quoted = _QUOTED.search(text)
    if quoted:
        phrase = re.sub(r"\s+", " ", quoted.group(1)).strip()
        if 1 <= len(phrase.split()) <= 4:
            return phrase if phrase[0].isupper() else phrase.capitalize()

    for match in _PROPER.finditer(text):
        candidate = " ".join(part for part in match.groups() if part).strip()
        if candidate.lower() in _STOP or len(candidate) < 4:
            continue
        return candidate
    return ""


def _topic_object(text: str, keywords: list[str] | None) -> str:
    """A topic word mapped onto a GTA-ish noun phrase, e.g. npc -> GTA 6 NPCs."""
    pool = [str(word).lower() for word in (keywords or [])]
    low = text.lower()
    for words, phrase in _OBJECT_FALLBACKS:
        if any(word in pool or word in low for word in words):
            return phrase
    return ""


def speed_title(text: str, keywords: list[str] | None = None) -> str:
    """Compose ``Speed <VERB> <OBJECT> in GTA 6 <EMOJI>`` or return ``""``.

    ``""`` means "no confident shape"; the caller should keep the plain title.
    """
    body = re.sub(r"\s+", " ", text or "").strip()
    if len(body) < 12:
        return ""

    verb = ""
    verb_end = 0
    for pattern, rendered in _VERBS:
        match = re.search(pattern, body, flags=re.IGNORECASE)
        if match:
            verb = rendered
            verb_end = match.end()
            break
    if not verb:
        return ""

    obj = (
        _named_object(body)
        or _phrase_after(body, verb_end)
        or _topic_object(body, keywords)
    )
    if not obj:
        return ""

    # Only append "in GTA 6" when the verb does not already end in a preposition
    # and the object does not already name the game: "Speed Goes CRAZY on GTA 6
    # NPCs", not "... on GTA 6 NPCs in GTA 6".
    prepositional = verb.split()[-1].lower() in {"on", "for", "in", "to", "by", "at", "with"}
    if prepositional or "gta 6" in obj.lower():
        title = f"Speed {verb} {obj}"
    else:
        title = f"Speed {verb} {obj} in GTA 6"

    title = re.sub(r"\s+", " ", title).strip()
    return f"{title[:88]} {_tone_emoji(body)}"


# Tags the reference channel uses on every upload; combined with transcript
# keywords so the video lands in the same discovery pool.
SPEED_TAGS = ["ishowspeed", "speed", "gta6", "gta6gameplay", "gaming", "shorts"]


def speed_tags(keywords: list[str] | None) -> list[str]:
    tags: list[str] = []
    for tag in [*(keywords or []), *SPEED_TAGS, "viral"]:
        clean = re.sub(r"[^a-z0-9]", "", str(tag).lower())
        if len(clean) >= 3 and clean not in tags:
            tags.append(clean)
    return tags[:8]
