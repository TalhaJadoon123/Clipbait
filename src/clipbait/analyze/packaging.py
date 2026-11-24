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
from collections import Counter

from ..virality import games, hashtags

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

# Only real quotation marks count. Auto-captions use the ASCII apostrophe for
# contractions ("don't", "shouldn't"), so treating it as a quote turns two
# unrelated contractions into a bogus "shouted" object ("T stop. so, shouldn").
_QUOTED = re.compile(r"[\"“”]([^\"“”]{2,30})[\"“”]")
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

# Common words a caption track still capitalises — at the start of a cue, or
# because the ASR re-segments a sentence. They look exactly like a proper noun
# ("Speed Gets Then"), so they are barred from being objects *and* from the
# name gazetteer. Possessives ("his", "her") are deliberately absent so a real
# object such as "His Family Name" survives.
_COMMON_CAPS = frozenset(
    """
    what this these those they them that then than there here where when why how
    who which some any every just like really very much many well come came give
    take thank thanks look should could would might maybe real stand hour figure
    enough now again later remember forget want need everything nothing right back
    front whole fine okay yeah
    hold hair away something today sir man done going gonna wanna gotta
    somewhere info helped looking guys wait lead check most type doubt doubts
    worry really scared
    """.split()
)

_LOWER_WORD = re.compile(r"[a-z]{3,}")
_CAP_WORD = re.compile(r"[A-Z][a-z]{3,}")
_SENTENCE_END = ".!?"


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


def known_names(words: object) -> list[str]:
    """Names a Speed clip can safely hang a title on.

    A one-word title built from a transcript is only as honest as the
    transcript's spelling. Auto-captions capitalise the start of every cue, so
    a bare capital is no evidence of a name, and ASR stitches split words into
    fragments ("T stop", "S we"). The one reliable signal is a word that
    recurs, sits *mid-sentence*, and is not also a common English word — so the
    caller computes that set once and passes it to :func:`speed_title`.
    """
    capitalised: Counter[str] = Counter()
    lowercased: Counter[str] = Counter()
    previous = ""
    for word in words:  # duck-typed: anything with a ``.text`` attribute
        token = str(getattr(word, "text", word)).strip()
        if _LOWER_WORD.fullmatch(token):
            lowercased[token] += 1
        if _CAP_WORD.fullmatch(token) and previous and previous[-1] not in _SENTENCE_END:
            capitalised[token] += 1
        previous = token
    return sorted(
        token
        for token, count in capitalised.items()
        if count >= 3
        and token.lower() not in _COMMON_CAPS
        and lowercased[token.lower()] < count
    )


def _named_object(text: str, names: frozenset[str] | None = None) -> str:
    """A quoted shout or a recurring proper name.

    A quoted shout is trusted as-is. A bare capitalised word is only trusted
    when ``names`` (see :func:`known_names`) vouches for it, because on
    auto-captions a lone capital is usually just a sentence start.
    """
    quoted = _QUOTED.search(text)
    if quoted:
        phrase = re.sub(r"\s+", " ", quoted.group(1)).strip().strip(" .,!?;:\"'")
        # Inside auto-captions a quoted line is a whole spoken sentence, not an
        # object; only a short phrase with no internal punctuation can be one.
        if 1 <= len(phrase.split()) <= 3 and not any(ch in phrase for ch in ",;.!?"):
            return phrase if phrase[0].isupper() else phrase.capitalize()

    if not names:
        return ""
    for match in _PROPER.finditer(text):
        first = match.group(1)
        if first not in names:
            continue
        second = match.group(2)
        if second and second in names:
            return f"{first} {second}"
        return first
    return ""


# A phrase cannot *start* with a conjunction, determiner or bare verb left over
# from the clause before it ("Or You're Dead", "Not Mean", "Go Kind"). Kept
# separate from _COMMON_CAPS because these are only bad in the leading slot.
_OBJECT_LEAD_BLOCK = frozenset(
    """
    or and but so nor yet if because when then that not no go all just like some
    any every this these those there here
    do does did done make makes made say says said tell tells told get gets got
    give gives put keep let hold see are is am be been was were has have had can
    will would could should
    """.split()
)


def _clean_object(phrase: str) -> str:
    """Reject a grabbed phrase that cannot read as a noun object."""
    if not phrase:
        return ""
    words = phrase.split()
    if len(words) > 3:
        return ""
    for word in words:
        low = word.lower().strip("'")
        # An apostrophe in the object is almost always an ASR contraction the
        # grabber walked into ("All be while i", "S we"), not a noun.
        if len(word) < 2 or low in _PRONOUNS or "'" in word:
            return ""
        # A conjunction, determiner or bare verb anywhere in the phrase means
        # the grabber walked across a clause boundary ("Squab Wide All",
        # "Work Can Do", "Scare Change").
        if low in _OBJECT_LEAD_BLOCK:
            return ""
    if words[0].lower() in _COMMON_CAPS or words[0].lower() in _OBJECT_LEAD_BLOCK:
        return ""
    if words[-1].lower() in _COMMON_CAPS:
        return ""
    return phrase


def _topic_object(text: str, keywords: list[str] | None) -> str:
    """A topic word mapped onto a GTA-ish noun phrase, e.g. npc -> GTA 6 NPCs."""
    pool = [str(word).lower() for word in (keywords or [])]
    low = text.lower()
    for words, phrase in _OBJECT_FALLBACKS:
        if any(word in pool or word in low for word in words):
            return phrase
    return ""


def speed_title(
    text: str,
    keywords: list[str] | None = None,
    *,
    names: frozenset[str] | None = None,
) -> str:
    """Compose ``Speed <VERB> <OBJECT> in GTA 6 <EMOJI>`` or return ``""``.

    ``""`` means "no confident shape"; the caller should keep the plain title
    rather than inventing a claim the footage cannot back up. ``names`` is the
    clip's gazetteer (see :func:`known_names`); without it a bare capitalised
    word is not trusted as a name.
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
        _named_object(body, names)
        or _clean_object(_phrase_after(body, verb_end))
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


_QUOTE_WORD = re.compile(r"[A-Za-z][A-Za-z']*")
_QUOTE_HYPE = ("crazy", "what", "why", "how", "oh", "no", "bro", "look", "chat")


def quote_title(text: str, limit: int = 70) -> str:
    """A substantive line Speed actually said — the honest fallback.

    Used only when the reference shape cannot be built, so a clip never ships a
    one-word fragment ("Why", "Mean") as its title. Picks the fullest sentence
    in the window, not the first, then trims it to a title length.
    """
    parts = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text or ""))
    pool = [p.strip(" -") for p in parts if len(_QUOTE_WORD.findall(p)) >= 4]
    if not pool:
        return ""

    def score(sentence: str) -> float:
        count = len(_QUOTE_WORD.findall(sentence))
        points = 4.0 if 5 <= count <= 14 else (2.0 if count <= 20 else 0.0)
        low = sentence.lower()
        points += sum(1.5 for cue in _QUOTE_HYPE if cue in low)
        points += min(3, low.count("!"))
        if low.startswith(("and ", "but ", "so ", "because ")):
            points -= 2
        return points

    pool.sort(key=score, reverse=True)
    chosen = re.sub(r"^(so|okay|ok|and|but|well|yeah|oh)[, ]+", "", pool[0], flags=re.IGNORECASE)
    chosen = chosen.rstrip(" .!?,")
    if len(chosen) > limit:
        chosen = chosen[:limit].rsplit(" ", 1)[0].rstrip(" ,")
    if not chosen:
        return ""
    chosen = chosen[:1].upper() + chosen[1:]
    return f"{chosen} {_tone_emoji(chosen)}"


# Tags the reference channel uses on every upload; combined with what the clip
# actually shows (the game, the channel, the words said) so the video lands in
# the same discovery pool.
SPEED_TAGS = ["ishowspeed", "speed", "gta6", "gta6gameplay", "gaming", "shorts"]


def speed_tags(
    keywords: list[str] | None,
    *,
    text: str = "",
    title: str = "",
    uploader: str = "",
    game: str | None = None,
    limit: int = 8,
) -> list[str]:
    """Tags for a reference-style Speed clip.

    Blends what the clip really shows — the game named in the title or the words,
    the channel it is from, the words said most — with the discovery block the
    reference channel carries on every upload. Falls back to the transcript
    keywords and the fixed block when nothing else is known.
    """
    detected = game or games.detect(title, text, " ".join(keywords or []))[0]
    tags: list[str] = []
    candidates = [
        *hashtags.make(
            text=text,
            title=title,
            uploader=uploader,
            game=detected,
            ai_tags=keywords or (),
            limit=limit,
        ),
        *(keywords or []),
        *SPEED_TAGS,
        "viral",
    ]
    for candidate in candidates:
        clean = re.sub(r"[^a-z0-9]", "", str(candidate).lower())
        if len(clean) >= 3 and clean not in tags:
            tags.append(clean)
    return tags[:limit]
