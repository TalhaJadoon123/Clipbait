"""Region playbooks, meme matching, packaging and the virality forecast."""

from __future__ import annotations

from clipbait.schema import ClipScores, Highlight
from clipbait.virality import build_variants, forecast, get_region
from clipbait.virality.memes import MEMES, apply_pattern, detect_signals, select_memes
from clipbait.virality.packaging import best_posting_time, platform_limits
from clipbait.virality.regions import REGIONS, catalog

TEXT = "I failed and lost everything, but the secret finally worked. It was insane and unreal."


def highlight(**overrides) -> Highlight:
    data = {
        "start": 0.0,
        "end": 42.0,
        "title": "I failed and lost everything, but the secret finally worked",
        "hook": "I failed and lost everything",
        "reason": "It was insane and unreal.",
        "keywords": ["business", "mindset", "actually", "right"],
        "scores": ClipScores(hook=88, emotion=76, value=70, share=81, overall=82),
    }
    data.update(overrides)
    return Highlight(**data)


def test_region_catalog_is_complete_and_stable() -> None:
    regions = catalog()
    assert len(regions) >= 20
    codes = {region["code"] for region in regions}
    assert {"US", "IN", "BR", "PK", "NG", "DE"} <= codes
    for region in regions:
        assert region["name"] and region["flag"]
        assert region["posting_windows"], region["code"]
        assert region["platform_bias"], region["code"]
        assert region["hashtags"], region["code"]


def test_unknown_region_falls_back_to_default() -> None:
    assert get_region("ZZ").code == REGIONS["US"].code
    assert get_region(None).code == REGIONS["US"].code
    assert get_region("in").code == "IN"


def test_platform_limits_are_sane() -> None:
    for platform in ("tiktok", "reels", "shorts", "linkedin", "x"):
        limits = platform_limits(platform)
        assert 0 < limits["title"] <= 300
        assert limits["hashtags"] >= 1


def test_variants_are_unique_and_platform_sized() -> None:
    for platform in ("tiktok", "reels", "shorts", "x"):
        variants = build_variants(highlight(), TEXT, get_region("IN"), platform, limit=5)
        assert variants
        assert len(variants) <= 5
        # Titles may repeat when the hook differs (organic vs CTA), but the
        # (title, hook) pair must always be unique.
        assert len({(v["title"], v["hook"]) for v in variants}) == len(variants)
        for variant in variants:
            assert variant["title"]
            assert len(variant["title"]) <= min(platform_limits(platform)["title"], 76)
            assert variant["hashtags"]
            assert all(tag.startswith("#") for tag in variant["hashtags"])
            assert variant["region"] == "IN"
            assert variant["platform"] == platform


def test_hashtags_filter_weak_keywords() -> None:
    variants = build_variants(highlight(), TEXT, get_region("US"), "tiktok", limit=3)
    tags = {tag.lstrip("#").lower() for tag in variants[0]["hashtags"]}
    for junk in ("actually", "right", "thing"):
        assert junk not in tags


def test_organic_variant_always_exists() -> None:
    variants = build_variants(highlight(), "plain sentence", get_region("US"), "tiktok", limit=1)
    assert variants[0]["id"] == "organic"
    assert variants[0]["kind"] == "organic"


def test_meme_variant_is_selected_for_signal_rich_text() -> None:
    variants = build_variants(highlight(), TEXT, get_region("US"), "tiktok", limit=6)
    assert any(variant["kind"] == "meme" for variant in variants)
    assert any(variant["meme"] for variant in variants)


def test_medium_risk_memes_are_opt_in() -> None:
    conservative = build_variants(highlight(), TEXT, get_region("US"), "tiktok", limit=8)
    permissive = build_variants(
        highlight(), TEXT, get_region("US"), "tiktok", limit=8, allow_medium_risk=True
    )
    assert not any(
        (variant.get("meme") or {}).get("risk") == "medium" for variant in conservative
    )
    assert len(permissive) >= len(conservative)


def test_twist_memes_need_a_reason() -> None:
    without_reason = build_variants(
        highlight(reason=""), "Expectation: this is the claim", get_region("US"), "tiktok", limit=8
    )
    assert all("expectation" != variant["id"] for variant in without_reason)


def test_apply_pattern_collapses_literal_newlines() -> None:
    shaped = apply_pattern({"pattern": "Nobody:\\n{subject}"}, subject="me", claim="x")
    assert "\\n" not in shaped
    assert shaped == "Nobody: me"


def test_detect_signals_reads_the_emotional_register() -> None:
    signals = set(detect_signals(TEXT))
    assert {"failure", "revelation", "contrast", "awe"} <= signals
    # "story" needs a narrative arc, not just a pronoun.
    assert "story" not in set(detect_signals("I was there"))


def test_select_memes_respects_region_and_platform() -> None:
    philippines = get_region("PH")
    memes = select_memes("Sana all, this is amazing and incredible", philippines, "tiktok", limit=10)
    ids = {meme["id"] for meme in memes}
    assert "sana_all" in ids
    for meme in memes:
        meta = next(item for item in MEMES if item.id == meme["id"])
        assert not meta.regions or "PH" in meta.regions


def test_forecast_stays_inside_bounds() -> None:
    result = forecast(
        highlight().scores,
        TEXT,
        get_region("BR"),
        "tiktok",
        duration=42.0,
        meme={"match": 0.8, "risk": "low"},
    )
    assert 0 <= result["overall"] <= 100
    assert 0 <= result["retention"] <= 100
    assert 0 <= result["regional_fit"] <= 100
    assert 0 <= result["meme_fit"] <= 100
    assert 0 <= result["trend_fit"] <= 100
    assert result["signals"]
    assert result["best_time"] == best_posting_time(get_region("BR"))
    assert "not a guarantee" in result["disclaimer"]


def test_forecast_penalises_overlong_clips() -> None:
    short = forecast(highlight().scores, TEXT, get_region("US"), "tiktok", duration=40.0)
    long = forecast(highlight().scores, TEXT, get_region("US"), "tiktok", duration=150.0)
    assert short["retention"] > long["retention"]
    assert short["overall"] >= long["overall"]


def test_forecast_explains_itself() -> None:
    result = forecast(highlight().scores, TEXT, get_region("US"), "tiktok", duration=42.0)
    assert result["reasons"]
    assert any("hook" in reason.lower() for reason in result["reasons"])


def test_region_overrides_are_patched_without_mutating_the_base(monkeypatch) -> None:
    from clipbait.virality import regions as module

    monkeypatch.setenv(
        "CLIPBAIT_REGION_OVERRIDES",
        '{"IN": {"cta": "Comment GUIDE", "hashtags": ["#a", "#b"]}, "ZZ": {"cta": "x"}}',
    )
    merged = module._apply_overrides(module.REGIONS)

    assert merged["IN"].cta == "Comment GUIDE"
    assert merged["IN"].hashtags == ("#a", "#b")
    assert "ZZ" not in merged, "unknown regions are ignored, not invented"
    assert module.REGIONS["IN"].cta != "Comment GUIDE", "the base catalog is immutable"


def test_broken_region_overrides_are_ignored(monkeypatch) -> None:
    from clipbait.virality import regions as module

    monkeypatch.setenv("CLIPBAIT_REGION_OVERRIDES", "{not json")
    assert module._apply_overrides(module.REGIONS) == module.REGIONS