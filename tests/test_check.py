"""Pre-post check: campaign rules, banned copy, and real file properties."""

from __future__ import annotations

import pytest
from helpers import make_video

from clipbait.money.check import (
    BANNED_TERMS,
    CampaignRules,
    banned_in,
    check_clip,
    parse_campaign,
    template,
)


def test_campaign_file_is_parsed_not_guessed(tmp_path) -> None:
    path = tmp_path / "rules.md"
    path.write_text(
        "min_seconds: 12\nmax_seconds: 45\n"
        "required_hashtags: #clipmoney #gta6\nrequired_mention: @clipmoney\n"
        "banned_terms: spoiler, early build\n",
        encoding="utf-8",
    )
    text = path.read_text()
    rules = parse_campaign(text, source=str(path))

    assert rules.min_seconds == 12
    assert rules.max_seconds == 45
    assert rules.required_hashtags == ("#clipmoney", "#gta6")
    assert rules.required_mention == "@clipmoney"
    assert "spoiler" in rules.all_banned
    assert rules.from_file


def test_defaults_are_documented_not_invented() -> None:
    rules = CampaignRules()
    assert rules.min_seconds == 7
    assert rules.max_seconds == 60
    assert rules.required_hashtags == ()
    assert not rules.from_file
    assert "min_seconds" in template()


def test_blank_template_does_not_pretend_to_be_a_campaign() -> None:
    rules = parse_campaign(template(), source="data/campaigns/campaign.md")
    assert rules.source.endswith("campaign.md")
    assert not rules.from_file  # every line is commented out
    report = check_clip("/tmp/does-not-exist.mp4", rules=rules)
    assert any("not a rules check" in warning for warning in report.warnings)


def test_banned_terms_cover_the_usual_demonetisers() -> None:
    hits = banned_in("FREE DOWNLOAD of the leaked build, mod menu included")
    assert {"free download", "leak", "mod menu"} <= set(hits)
    assert "leak" in BANNED_TERMS and banned_in("a normal caption") == []


def test_pass_on_a_real_vertical_clip(tmp_path) -> None:
    video = make_video(tmp_path / "good.mp4", seconds=3, size="1080x1920", fps=30)
    report = check_clip(
        video,
        caption="Kai lost it\n\n#shorts #gta6",
        rules=CampaignRules(min_seconds=1, max_seconds=60, required_hashtags=("#gta6",)),
    )

    assert report.passed, report.lines()
    assert report.info is not None and (report.info.width, report.info.height) == (1080, 1920)
    assert report.as_dict()["passed"] is True


def test_failures_name_the_problem(tmp_path) -> None:
    video = make_video(tmp_path / "wide.mp4", seconds=2, size="480x270", fps=25)
    report = check_clip(
        video,
        caption="crack the game, free download",
        rules=CampaignRules(
            min_seconds=10,
            max_seconds=60,
            required_hashtags=("#gta6",),
            required_mention="@clipmoney",
        ),
    )

    assert not report.passed
    failed = {row.name for row in report.failures}
    assert "resolution 1080x1920" in failed
    assert "fps about 30" in failed
    assert "no banned terms in caption/hook" in failed
    assert "required hashtags" in failed
    assert "required mention" in failed
    assert any(name.startswith("duration 10-60s") for name in failed)
    assert report.as_dict()["failures"]


def test_missing_files_report_instead_of_crashing(tmp_path) -> None:
    report = check_clip(tmp_path / "nope.mp4", caption="anything")
    assert not report.passed
    assert "file exists" in {row.name for row in report.failures}


def test_unreadable_video_does_not_raise(tmp_path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    report = check_clip(broken, caption="#shorts")
    assert not report.passed
    assert "file is readable video" in {row.name for row in report.failures}


@pytest.mark.parametrize("bad", ["#shorts", ""])
def test_caption_length_rule_reads_the_copy(tmp_path, bad: str) -> None:
    report = check_clip(tmp_path / "missing.mp4", caption=bad)
    row = next(row for row in report.rows if row.name.startswith("caption fits"))
    assert row.ok