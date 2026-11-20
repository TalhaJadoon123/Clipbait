"""Platform + region packaging and a transparent virality forecast.

Deterministic by design: the same clip yields the same variants, so A/B tests
are comparable. An LLM can rewrite a variant later; it cannot silently change
the scoring model.
"""

from __future__ import annotations

import re

from ..schema import ClipScores, Highlight
from .memes import apply_pattern, detect_signals, select_memes
from .regions import RegionProfile, get_region, platform_limits

EMOJI_SETS = {
    0: [],
    1: ["👀"],
    2: ["🔥", "👀"],
    3: ["🔥", "😳", "💀", "👀"],
}

TREND_KEYWORDS = (
    "ai", "agents", "automation", "claude", "gpt", "gemini", "cursor", "n8n",
    "faceless", "passive income", "creator economy", "shorts", "reels", "tiktok",
)


def _trim(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(".,:;!?")
    return cut + "…" if cut else text[:limit]


def _title_case_hook(text: str, limit: int) -> str:
    trimmed = _trim(text, limit)
    return trimmed[:1].upper() + trimmed[1:] if trimmed else trimmed


def _hashtags(region: RegionProfile, keywords: list[str], platform: str) -> list[str]:
    limit = platform_limits(platform)["hashtags"]
    tags: list[str] = []
    junk = {"right", "first", "thing", "things", "part", "people", "video", "make", "made",
            "going", "really", "actually", "every", "always", "never", "about", "there", "here"}
    # Specific first: "#kaicenat #gta6" tells a viewer (and the platform) what
    # this is. The region's evergreen tags are a fallback, not the headline.
    for tag in [*[k.replace(" ", "") for k in keywords], *region.hashtags]:
        cleaned = tag.strip().lstrip("#")
        if not cleaned or cleaned.lower() in junk or len(cleaned) < 4:
            continue
        if not cleaned.startswith("#"):
            cleaned = "#" + cleaned
        if cleaned.lower() not in {t.lower() for t in tags}:
            tags.append(cleaned)
    return tags[:limit]


def _emoji(region: RegionProfile, index: int) -> str:
    options = EMOJI_SETS.get(region.emoji_density, [])
    if not options or index >= len(options):
        return ""
    return options[index]


def build_variants(
    highlight: Highlight,
    text: str,
    region: RegionProfile | None = None,
    platform: str = "tiktok",
    *,
    limit: int = 4,
    allow_medium_risk: bool = False,
) -> list[dict]:
    """Return ranked packaging variants: organic, meme-led and regional."""
    region = region or get_region(None)
    limits = platform_limits(platform)
    title_limit = min(limits["title"], 76)
    signals = detect_signals(text)
    memes = select_memes(text, region, platform, limit=max(2, limit - 1), allow_medium_risk=allow_medium_risk)
    base_claim = _title_case_hook(highlight.hook or highlight.title, title_limit)
    subject = _trim(highlight.title, 42)

    variants: list[dict] = []

    organic_hook = _title_case_hook(highlight.hook or base_claim, 90)
    variants.append(
        {
            "id": "organic",
            "kind": "organic",
            "region": region.code,
            "platform": platform,
            "title": _title_case_hook(base_claim, title_limit),
            "hook": organic_hook,
            "description": _trim(highlight.reason or text, limits["caption"]),
            "hashtags": _hashtags(region, highlight.keywords, platform),
            "cta": region.cta,
            "meme": None,
            "rationale": "Literal packaging: the line itself is the hook.",
        }
    )

    for index, meme in enumerate(memes):
        if "{twist}" in (meme.get("pattern") or "") and not highlight.reason:
            continue
        shaped = apply_pattern(
            meme,
            subject=subject,
            claim=base_claim,
            twist=_trim(highlight.reason, 60),
        )
        emoji = _emoji(region, index)
        hook_text = _trim(f"{shaped} {emoji}".strip(), 95)
        variants.append(
            {
                "id": meme["id"],
                "kind": "meme",
                "region": region.code,
                "platform": platform,
                "title": _title_case_hook(shaped, title_limit),
                "hook": hook_text,
                "description": _trim(f"{highlight.reason} {region.cta}.", limits["caption"]),
                "hashtags": _hashtags(region, highlight.keywords, platform),
                "cta": region.cta,
                "meme": meme,
                "rationale": f"{meme['name']} matches {', '.join(signals[:3]) or 'the tone'} in this market.",
            }
        )

    # A region-native CTA variant is often the difference between a scroll and a save.
    variants.append(
        {
            "id": "cta",
            "kind": "cta",
            "region": region.code,
            "platform": platform,
            "title": _title_case_hook(base_claim, title_limit),
            "hook": _trim(f"{base_claim} — {region.cta}", 95),
            "description": _trim(f"{highlight.reason} {region.cta}.", limits["caption"]),
            "hashtags": _hashtags(region, highlight.keywords, platform),
            "cta": region.cta,
            "meme": None,
            "rationale": f"Local CTA for {region.name} drives saves and follows.",
        }
    )

    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for variant in variants:
        key = (variant["title"].lower(), variant["hook"].lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(variant)
    return unique[:limit]


def best_posting_time(region: RegionProfile) -> str:
    return region.posting_windows[0] if region.posting_windows else "18:00-21:00"


def forecast(
    scores: ClipScores,
    text: str,
    region: RegionProfile | None,
    platform: str,
    *,
    duration: float,
    meme: dict | None = None,
) -> dict:
    """Explainable 0-100 forecast. Never presented as a guarantee."""
    region = region or get_region(None)
    signals = set(detect_signals(text))
    lower = text.lower()

    retention = 100 - min(45, abs(duration - 38) * 1.1)
    length_penalty = 0 if 22 <= duration <= 62 else max(0, min(25, abs(duration - 42) * 0.5))

    regional_fit = 55
    if region.slang and any(term in lower for term in region.slang):
        regional_fit += 18
    if region.emoji_density >= 2 and re.search(r"[\U0001F300-\U0001FAFF]", text):
        regional_fit += 8
    if platform in region.platform_bias:
        regional_fit += 10

    meme_fit = 0
    if meme:
        meme_fit = round(meme.get("match", 0.0) * 100)
        if meme.get("risk") == "medium":
            meme_fit -= 10

    trend_hits = sum(1 for keyword in TREND_KEYWORDS if keyword in lower)
    trend_fit = min(100, 35 + trend_hits * 18)

    shareable = scores.share
    overall = round(
        0.30 * scores.hook
        + 0.20 * scores.emotion
        + 0.15 * scores.value
        + 0.15 * shareable
        + 0.08 * retention
        + 0.05 * regional_fit
        + 0.04 * meme_fit
        + 0.03 * trend_fit
        - length_penalty
    )
    overall = max(0, min(100, overall))

    reasons: list[str] = []
    if scores.hook >= 70:
        reasons.append("Strong three-second hook")
    if duration > 75:
        reasons.append("Long for Shorts; consider cutting the setup")
    if retention < 60:
        reasons.append("Duration sits outside the sweet spot")
    if meme_fit >= 60:
        reasons.append("Meme format matches the emotional signal")
    if trend_hits:
        reasons.append(f"Trend overlap: {trend_hits} keyword(s)")
    if platform not in region.platform_bias:
        reasons.append(f"{platform} is secondary in {region.name}")

    return {
        "overall": overall,
        "retention": round(retention),
        "regional_fit": min(100, regional_fit),
        "meme_fit": max(0, meme_fit),
        "trend_fit": trend_fit,
        "signals": sorted(signals),
        "reasons": reasons,
        "best_time": best_posting_time(region),
        "disclaimer": "Directional forecast from transcript + metadata, not a guarantee.",
    }


def region_catalog() -> list[dict]:
    from .regions import catalog

    return catalog()