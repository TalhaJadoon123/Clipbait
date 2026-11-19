"""Virality playbooks: region profiles, meme formats, packaging and forecast."""

from .memes import MEMES, detect_signals, select_memes
from .packaging import best_posting_time, build_variants, forecast
from .payout import (
    PLATFORM_RPM,
    PayoutEstimate,
    PayoutReport,
    best_destination,
    estimate,
    estimate_platform,
    expected_views,
    region_factor,
)
from .regions import DEFAULT_REGION, REGIONS, RegionProfile, catalog, get_region, platform_limits

__all__ = [
    "PLATFORM_RPM",
    "PayoutEstimate",
    "PayoutReport",
    "best_destination",
    "estimate",
    "estimate_platform",
    "expected_views",
    "region_factor",
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