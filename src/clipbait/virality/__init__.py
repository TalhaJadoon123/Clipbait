"""Virality playbooks: region profiles, meme formats, packaging and forecast."""

from .memes import MEMES, detect_signals, select_memes
from .packaging import best_posting_time, build_variants, forecast
from .regions import DEFAULT_REGION, REGIONS, RegionProfile, catalog, get_region, platform_limits

__all__ = [
    "MEMES",
    "REGIONS",
    "DEFAULT_REGION",
    "RegionProfile",
    "best_posting_time",
    "build_variants",
    "catalog",
    "detect_signals",
    "forecast",
    "get_region",
    "platform_limits",
    "select_memes",
]