"""What a clip is worth before you post it.

Nobody can promise views, so this module refuses to pretend. It converts the
existing virality forecast into a *range* of expected views with a lognormal
spread, multiplies by a published-style RPM for the target platform, and adjusts
for the advertiser value of the region. Every number is labelled as an estimate
and the assumptions travel with it, so the queue can rank clips without lying to
the operator.

Two rules keep it honest:

* the spread is wide (a short can do 10x or 0.2x the median) and the UI shows
  the low end first,
* RPMs are per-platform medians, not a promise. LinkedIn pays multiples of
  TikTok because B2B inventory does; that is a fact about ad markets, not a
  growth hack.

Pure math, no I/O: trivially testable and identical everywhere it is called.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist

# RPM = revenue per 1000 views, USD, for creator payouts (not advertiser CPM).
# ``(low, mid, high)``. Shorts pays least because the Shorts fund is thin;
# LinkedIn pays most because a single B2B impression is worth far more.
PLATFORM_RPM: dict[str, tuple[float, float, float]] = {
    "tiktok": (0.20, 0.45, 0.90),
    "reels": (0.25, 0.55, 1.10),
    "shorts": (0.06, 0.16, 0.40),
    "x": (0.10, 0.30, 0.70),
    "linkedin": (1.40, 3.20, 6.00),
}

# View distribution per platform: median views for an average clip, lognormal
# sigma, and the duration the platform rewards. Sigma matters: TikTok is
# fat-tailed (a hit explodes), LinkedIn is follower-bound (narrow). That is why
# the best destination changes with the forecast score instead of being a
# constant: a likely hit is worth more on TikTok's tail, a mid clip on
# LinkedIn's RPM.
PLATFORM_PROFILE: dict[str, dict[str, float]] = {
    "tiktok": {"median": 1200, "sigma": 1.15, "sweet": 34, "spread": 150},
    "reels": {"median": 900, "sigma": 1.10, "sweet": 36, "spread": 150},
    "shorts": {"median": 700, "sigma": 1.10, "sweet": 42, "spread": 160},
    "x": {"median": 400, "sigma": 1.05, "sweet": 28, "spread": 140},
    "linkedin": {"median": 220, "sigma": 0.95, "sweet": 75, "spread": 260},
}
DEFAULT_PLATFORM_PROFILE = {"median": 700, "sigma": 1.10, "sweet": 36, "spread": 150}

# Advertiser value of an impression by market, relative to the US. This is the
# dominant term in real creator payouts: the same million views is worth many
# times more in California than in a low-CPM market.
REGION_RPM_FACTOR: dict[str, float] = {
    "US": 1.00, "CA": 0.92, "UK": 0.88, "AU": 0.95, "NZ": 0.90, "IE": 0.88,
    "CH": 1.20, "NO": 1.05, "DK": 1.00, "SE": 0.90, "DE": 0.78, "NL": 0.82,
    "AT": 0.85, "BE": 0.80, "FI": 0.85, "FR": 0.72, "IT": 0.62, "ES": 0.60,
    "PT": 0.50, "PL": 0.48, "CZ": 0.50, "GR": 0.45, "TR": 0.28,
    "AE": 0.90, "SA": 0.58, "IL": 0.82, "QA": 0.85, "KW": 0.75,
    "JP": 0.70, "KR": 0.66, "SG": 1.10, "HK": 0.80, "TW": 0.62, "MY": 0.40,
    "TH": 0.30, "VN": 0.25, "ID": 0.24, "PH": 0.28, "IN": 0.24, "PK": 0.22,
    "BD": 0.20, "LK": 0.20, "NP": 0.18,
    "BR": 0.32, "MX": 0.40, "AR": 0.26, "CL": 0.42, "CO": 0.30, "PE": 0.30,
    "ZA": 0.34, "NG": 0.20, "KE": 0.22, "EG": 0.20, "MA": 0.28,
    "RU": 0.30, "UA": 0.20,
}
DEFAULT_REGION_FACTOR = 0.55

# Reference spread used for the reported low/high range (the platform sigma
# scales it). 1.0 means the 80th percentile is ~3.6x the median.
VIEW_SIGMA = 1.0


@dataclass(frozen=True)
class PayoutEstimate:
    """One platform's expected return for one clip."""

    platform: str
    region: str
    expected_views: int
    views_low: int
    views_high: int
    rpm: float
    region_factor: float
    expected_usd: float
    usd_low: float
    usd_high: float
    confidence: str = "low"
    assumptions: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "region": self.region,
            "expected_views": self.expected_views,
            "views_low": self.views_low,
            "views_high": self.views_high,
            "rpm": round(self.rpm, 3),
            "region_factor": round(self.region_factor, 3),
            "expected_usd": round(self.expected_usd, 2),
            "usd_low": round(self.usd_low, 2),
            "usd_high": round(self.usd_high, 2),
            "confidence": self.confidence,
            "assumptions": list(self.assumptions),
        }


@dataclass(frozen=True)
class PayoutReport:
    region: str
    score: float
    duration: float
    estimates: tuple[PayoutEstimate, ...] = ()
    best: PayoutEstimate | None = None
    rationale: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "region": self.region,
            "score": round(self.score, 1),
            "duration": round(self.duration, 1),
            "best": self.best.as_dict() if self.best else None,
            "estimates": [estimate.as_dict() for estimate in self.estimates],
            "rationale": list(self.rationale),
            "disclaimer": (
                "Directional estimate from forecast score and public RPM medians, "
                "not a guarantee of views or revenue."
            ),
        }


def region_factor(region: str | None) -> float:
    return REGION_RPM_FACTOR.get((region or "").upper(), DEFAULT_REGION_FACTOR)


def _rpm(platform: str) -> tuple[float, float, float]:
    return PLATFORM_RPM.get(platform, PLATFORM_RPM["tiktok"])


def _profile(platform: str) -> dict[str, float]:
    return PLATFORM_PROFILE.get(platform, DEFAULT_PLATFORM_PROFILE)


def _retention_factor(duration: float, platform: str = "tiktok") -> float:
    """Completion beats raw length: every platform has a sweet spot."""
    duration = max(1.0, float(duration or 1.0))
    profile = _profile(platform)
    penalty = abs(duration - profile["sweet"]) / profile["spread"]
    return max(0.55, min(1.0, 1.0 - penalty))


def expected_views(score: float, platform: str, duration: float = 40.0) -> tuple[int, int, int]:
    """``(low, expected, high)`` views for a forecast score on one platform.

    The score is mapped to a percentile of the platform's view distribution
    (25 is poor, 50 is average, 75 is good, 90+ is a hit), then read off a
    lognormal. The return is the mean with the 20th/80th percentile around it,
    which is why the low end is not simply "half".
    """
    profile = _profile(platform)
    sigma = float(profile["sigma"])
    median = float(profile["median"]) * math.exp(NormalDist().inv_cdf(_percentile(score)) * sigma)
    mean = median * math.exp((sigma**2) / 2)
    factor = _retention_factor(duration, platform)

    low = median * math.exp(-0.85 * VIEW_SIGMA) * factor
    high = median * math.exp(1.28 * VIEW_SIGMA) * factor
    return (
        int(max(0, round(low))),
        int(max(0, round(mean * factor))),
        int(max(0, round(high))),
    )


def _percentile(score: float) -> float:
    # Keep extremes away from 0/1: inv_cdf(0) is -inf and a 100 is not certain.
    return max(0.02, min(0.985, (float(score) - 22.0) / 78.0))


def estimate_platform(
    score: float,
    platform: str,
    region: str = "US",
    *,
    duration: float = 40.0,
) -> PayoutEstimate:
    score = max(0.0, min(100.0, float(score or 0.0)))
    low_views, views, high_views = expected_views(score, platform, duration)
    rpm_low, rpm_mid, rpm_high = _rpm(platform)
    factor = region_factor(region)

    expected_usd = views / 1000.0 * rpm_mid * factor
    usd_low = low_views / 1000.0 * rpm_low * factor
    usd_high = high_views / 1000.0 * rpm_high * factor

    confidence = "low"
    if score >= 70 and 22 <= duration <= 65:
        confidence = "medium"

    assumptions = (
        f"{platform} pays about ${rpm_mid:.2f} per 1000 views",
        f"{region or 'US'} impressions are worth {factor:.0%} of a US impression",
        f"forecast {score:.0f}/100 maps to the {_percentile(score) * 100:.0f}th percentile of that platform",
        f"{duration:.0f}s clip keeps {_retention_factor(duration, platform):.0%} of its reach",
    )
    return PayoutEstimate(
        platform=platform,
        region=(region or "US").upper(),
        expected_views=views,
        views_low=low_views,
        views_high=high_views,
        rpm=rpm_mid,
        region_factor=factor,
        expected_usd=round(expected_usd, 2),
        usd_low=round(usd_low, 2),
        usd_high=round(usd_high, 2),
        confidence=confidence,
        assumptions=assumptions,
    )


def estimate(
    score: float,
    region: str = "US",
    *,
    duration: float = 40.0,
    platforms: tuple[str, ...] | list[str] | None = None,
) -> PayoutReport:
    """Estimate every platform and pick the one that pays best."""
    chosen = tuple(platforms or PLATFORM_RPM.keys())
    estimates = tuple(
        sorted(
            (estimate_platform(score, platform, region, duration=duration) for platform in chosen),
            key=lambda item: item.expected_usd,
            reverse=True,
        )
    )
    best = estimates[0] if estimates else None
    rationale: list[str] = []
    if best:
        rationale.append(
            f"{best.platform} pays the most at about ${best.expected_usd:.2f} expected "
            f"(${best.usd_low:.2f}-${best.usd_high:.2f})"
        )
        if best.platform == "linkedin":
            rationale.append(
                "LinkedIn wins on RPM at this score; TikTok only overtakes on a likely hit"
            )
        elif best.platform == "tiktok":
            rationale.append(
                "TikTok's fat tail pays off at this score; LinkedIn would earn more per view "
                "but reaches far fewer people"
            )
        if region_factor(region) < 0.5:
            rationale.append(
                f"{region} has low advertiser value; reach there is worth volume, not RPM"
            )
    return PayoutReport(
        region=(region or "US").upper(),
        score=float(score or 0.0),
        duration=float(duration or 0.0),
        estimates=estimates,
        best=best,
        rationale=tuple(rationale),
    )


def best_destination(report: PayoutReport) -> str:
    return report.best.platform if report.best else "tiktok"