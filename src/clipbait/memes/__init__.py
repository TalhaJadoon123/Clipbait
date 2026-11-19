"""Meme *video* inserts: b-roll cutaways between spoken sentences.

Not to be confused with :mod:`clipbait.virality.memes`, which builds hook copy.
Here the subject is footage. An insert replaces the picture for a moment while
the original audio keeps playing, so captions stay locked to what is said.
"""

from .assets import prepare_insert
from .plan import Insert, InsertConfig, plan_inserts
from .providers import (
    ApiMemeProvider,
    FixtureMemeProvider,
    MemeError,
    MemeProvider,
    MemeRequest,
    cache_dir,
    get_meme_provider,
    provider_name,
)

__all__ = [
    "ApiMemeProvider",
    "FixtureMemeProvider",
    "Insert",
    "InsertConfig",
    "MemeError",
    "MemeProvider",
    "MemeRequest",
    "cache_dir",
    "get_meme_provider",
    "plan_inserts",
    "prepare_insert",
    "provider_name",
]
