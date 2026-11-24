"""Hashtags for a clip without asking an AI: free, instant, the same every time.

Ported from PeakCut's ``studio/hashtags.py`` (MIT, (c) 2026 PeakCut
contributors) into Clipbait's idiom. Three to five tags are picked from what is
really there:

  - the game's main tag (``gta6``), when the game is known
  - the channel the clip is from (``kaicenat``)
  - the AI's or ranker's own tags for the clip, when they are sensible
  - the words said most in the clip, and a name with a number in the title
    ("GTA 6" becomes ``gta6``)
  - what kind of moment it is (``funny``, ``fails``, ``clutch``)
  - ``shorts`` and ``clips``, only if too few others were found

Tags come back bare (no leading ``#``), matching how Clipbait stores and trims
hashtags; :func:`prefixed` adds the ``#`` for a description or caption.

The English and romanised Urdu/Hindi stopword list keeps filler out of the tags
on Hinglish/Urdu stream chat as well as English.
"""

from __future__ import annotations

import re

from . import games

MOST, LEAST = 5, 3

STOP = set(
    """
about above after again against all also always among and another any anyone anything are around
because been before being between both but came can cannot come could did does doing done dont down
each else even ever every everyone everything find first for from gave get gets getting give going
gone gonna good got great guys had has have having here him his how into its just keep kind know last
left let like little look looking made make makes many may maybe mean more most much must myself need
never next nobody not nothing now off okay once one only onto other our out over own people put really
right said same say says see she should since some someone something still such sure take than that the
their them then there these they thing things think this those though through time too took try under
until upon use very want wanna was way we well went were what when where whether which while who whole
why will with without won would yeah yes you your
hai kya yaar nahi nahin bhai toh aur mein tha kar karo kiya ho hain bhi koi abhi main mera tera apna
isko usko jab tab phir woh wo ye yeh hum tum aap kuch sab bohot bahut matlab accha acha theek lekin par
kyun kaise
actual actually literally basically honestly probably definitely obviously seriously totally bro dude
man chat stream video
fuck fucking fucked shit damn bitch bitches ass dick pussy sex nigga nigger niggas porn rape
""".split()
)

CATEGORY_TAGS = {
    "funny": "funny",
    "fail": "fails",
    "clutch": "clutch",
    "rage": "rage",
    "shock": "shocking",
    "take": "hottake",
    "story": "storytime",
}
FILLER = ("shorts", "clips", "viral")
CHANNEL_NOISE = {
    "live",
    "official",
    "tv",
    "gaming",
    "clips",
    "channel",
    "highlights",
    "youtube",
    "twitch",
    "kick",
    "tiktok",
    "stream",
}
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9']{2,19}")
_NUMBERED = re.compile(r"\b([A-Za-z]{2,12})\s+(\d{1,2})\b")


def tag(value: object) -> str:
    """One bare tag: letters and digits, lower case, never a bare number or a stop word."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).lower()[:30]
    if len(cleaned) < 3 or cleaned.isdigit() or cleaned in STOP:
        return ""
    return cleaned


def prefixed(tags: list[str]) -> list[str]:
    """The same tags with a leading ``#``, for a description or caption."""
    return ["#" + tag_value for tag_value in tags if tag_value]


def channel_tag(uploader: str) -> str:
    """The channel as one tag: "Kai Cenat Live" becomes ``kaicenat``."""
    words = [
        word
        for word in re.findall(r"[A-Za-z0-9]+", str(uploader or ""))
        if word.lower() not in CHANNEL_NOISE
    ]
    cleaned = tag("".join(words))
    return cleaned if 3 <= len(cleaned) <= 25 else ""


def numbered(title: str) -> list[str]:
    """Names with a number in the title ("GTA 6", "Fortnite 2") as one tag each."""
    return [
        tag(prefix + suffix)
        for prefix, suffix in _NUMBERED.findall(str(title or ""))
        if prefix.lower() not in STOP
    ]


def keywords(text: str, title: str = "", count: int = 2) -> list[str]:
    """The words that matter most in what was said.

    Said more than once, or a name (capitalised in the middle of a sentence), and
    more so when the title has them too.
    """
    raw = str(text or "").split()
    in_title = {word.lower() for word in _WORD.findall(str(title or ""))}
    score: dict[str, int] = {}
    for index, token in enumerate(raw):
        match = _WORD.search(token)
        if not match:
            continue
        word = match.group(0).strip("'").lower()
        if word in STOP or len(word) < 4 or not tag(word):
            continue
        named = token[:1].isupper() and index > 0 and raw[index - 1][-1:] not in ".?!"
        score[word] = score.get(word, 0) + 1 + (2 if named else 0) + (2 if word in in_title else 0)
    best = sorted(
        (word for word in score if score[word] >= 2),
        key=lambda word: (-score[word], -len(word), word),
    )
    return best[:count]


def make(
    text: str = "",
    title: str = "",
    uploader: str = "",
    game: str | None = None,
    category: str = "other",
    ai_tags: object = (),
    *,
    limit: int = MOST,
) -> list[str]:
    """Three to five bare tags for one clip, most relevant first."""
    out: list[str] = []

    def add(value: object) -> None:
        cleaned = tag(value)
        if cleaned and cleaned not in out and len(out) < limit:
            out.append(cleaned)

    for game_tag in games.tags_of(game)[
        :1
    ]:  # the game's main tag; look-alikes would only use up room
        add(game_tag)
    add(channel_tag(uploader))
    for value in list(ai_tags or ())[:2]:
        add(value)
    for word in keywords(text, title):
        add(word)
    for value in numbered(title)[:1]:
        add(value)
    add(CATEGORY_TAGS.get(str(category or "").lower(), ""))
    for filler in FILLER:
        if len(out) < LEAST:
            add(filler)
    return out
