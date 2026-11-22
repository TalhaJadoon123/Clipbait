"""Small, dependency-free text helpers shared by ingestion and rendering."""

from __future__ import annotations

import re

# YouTube auto-captions mark unintelligible speech as "[ __ ]" and wrap
# non-speech in brackets ("[Music]", "[Applause]"); music notes ("♪") and bare
# punctuation ride along too. None of it should be analysed, quoted or burned
# into a caption, so it is dropped at both the transcript and render stages.
_BRACKETED = re.compile(r"^[\[\(\u300c\u300e<].*[\]\)\u300d\u300f>]$", re.DOTALL)


def is_caption_noise(text: str) -> bool:
    """True for tokens a viewer should never read.

    Covers bracketed stage directions, music symbols and tokens with no
    letters or digits at all (``"♪"``, ``"__"``, ``"..."``).
    """
    token = text.strip()
    if not token:
        return True
    if _BRACKETED.match(token):
        return True
    return not any(character.isalnum() for character in token)
