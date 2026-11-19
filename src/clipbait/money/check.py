"""Pre-post check: will this file actually be accepted by the campaign?

A render can look fine on the desktop and still be rejected: wrong aspect,
60 fps, no audio track, 12 seconds instead of 15, a banned word in the hook, or
a missing required hashtag. Every one of those is cheap to detect and expensive
to discover after uploading.

Two inputs:

* a campaign rules file (``min_seconds``, ``max_seconds``, ``required_hashtags``,
  ``required_mention``, optional ``banned_terms``), and
* the finished file plus the caption that will be pasted next to it.

The check never edits anything. It returns a report the operator (and the
export bundle) can read. Exit code is the CLI's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .. import media

# Words that get a clip rejected or demonetised on most clipping campaigns, and
# that we never want to promise in copy. Matched as substrings on the lowercase
# hook + caption, so "leaked build" also catches "leaked".
BANNED_TERMS: tuple[str, ...] = (
    "leak",
    "play early",
    "early access download",
    "free download",
    "crack",
    "piracy",
    "mod menu",
    "money glitch",
    "afk bot",
    "cheats",
)

DEFAULT_MIN_SECONDS = 7.0
DEFAULT_MAX_SECONDS = 60.0
CAPTION_LIMIT = 2200  # Instagram/TikTok both accept this much; beyond it gets cut.


@dataclass(frozen=True)
class CampaignRules:
    min_seconds: float = DEFAULT_MIN_SECONDS
    max_seconds: float = DEFAULT_MAX_SECONDS
    required_hashtags: tuple[str, ...] = ()
    required_mention: str = ""
    banned_terms: tuple[str, ...] = ()
    source: str = "defaults"
    explicit: bool = False
    # What the campaign pays per 1000 verified views. None means "unknown":
    # the queue then falls back to platform RPM medians and says so.
    rate_per_1k: float | None = None

    @property
    def from_file(self) -> bool:
        """True only when the file actually set a rule, not for a blank template."""
        return self.explicit

    @property
    def all_banned(self) -> tuple[str, ...]:
        extra = tuple(term for term in self.banned_terms if term)
        return BANNED_TERMS + extra

    def as_dict(self) -> dict:
        return {
            "min_seconds": self.min_seconds,
            "max_seconds": self.max_seconds,
            "required_hashtags": list(self.required_hashtags),
            "required_mention": self.required_mention,
            "banned_terms": list(self.banned_terms),
            "source": self.source,
            "explicit": self.explicit,
            "rate_per_1k": self.rate_per_1k,
        }


def parse_campaign(text: str, *, source: str = "inline") -> CampaignRules:
    """Read the small key: value format campaigns actually publish.

    Full-line comments are dropped first, so a template full of commented
    examples stays inert and cannot be mistaken for a real rules file.
    """
    text = "\n".join(
        line for line in (text or "").splitlines() if not line.strip().startswith("#")
    )
    rules = CampaignRules(source=source)
    explicit = False
    min_seconds = _number(text, r"min_seconds:\s*(\d+(?:\.\d+)?)")
    if min_seconds is not None:
        rules = replace(rules, min_seconds=min_seconds)
        explicit = True
    max_seconds = _number(text, r"max_seconds:\s*(\d+(?:\.\d+)?)")
    if max_seconds is not None:
        rules = replace(rules, max_seconds=max_seconds)
        explicit = True
    tags: tuple[str, ...] = ()
    match = re.search(r"required_hashtags:\s*(.+)", text, re.I)
    if match:
        tags = tuple(dict.fromkeys(re.findall(r"#\w+", match.group(1))))
        explicit = True
    mention = ""
    match = re.search(r"required_mention:\s*(@[\w.]+)", text, re.I)
    if match:
        mention = match.group(1)
        explicit = True
    banned: tuple[str, ...] = ()
    match = re.search(r"banned_terms:\s*(.+)", text, re.I)
    if match:
        banned = tuple(part.strip().lower() for part in match.group(1).split(",") if part.strip())
        explicit = True
    rate = _number(text, r"rate_per_1k:\s*\$?(\d+(?:\.\d+)?)")
    if rate is not None:
        explicit = True
    return replace(
        rules,
        required_hashtags=tags,
        required_mention=mention,
        banned_terms=banned,
        explicit=explicit,
        rate_per_1k=rate,
    )


def _number(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text, re.I)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def load_campaign(path: Path | str | None) -> CampaignRules:
    if not path:
        return default_campaign()
    target = Path(path).expanduser()
    if not target.exists():
        return default_campaign()
    return parse_campaign(target.read_text(encoding="utf-8", errors="replace"), source=str(target))


def campaigns_dir() -> Path:
    from ..config import get_settings

    return get_settings().data_dir / "campaigns"


def default_campaign() -> CampaignRules:
    """Use ``data/campaigns/*.md`` when the operator dropped one there.

    First file wins, alphabetically, so ``00-clipmoney.md`` beats a scratch note.
    """
    directory = campaigns_dir()
    try:
        candidates = sorted(p for p in directory.glob("*.md") if p.is_file())
    except OSError:
        candidates = []
    if not candidates:
        return CampaignRules()
    return load_campaign(candidates[0])


def template() -> str:
    return (
        "# Campaign rules\n"
        "# Copy this into data/campaigns/<campaign>.md, uncomment the lines the\n"
        "# brief actually states, and delete the rest. Commented lines are inert:\n"
        "# the check will say so instead of pretending it verified a campaign.\n"
        "\n"
        "# min_seconds: 15\n"
        "# max_seconds: 60\n"
        "# required_hashtags: #tag1 #tag2\n"
        "# required_mention: @handle\n"
        "# banned_terms: spoiler, early build\n"
        "# rate_per_1k: 2.00\n"
        "\n"
        "# Always checked, no config needed: 1080x1920, ~30 fps, an audio track,\n"
        "# duration sanity, piracy/mod-menu words, caption under 2200 characters.\n"
    )


def banned_in(text: str, terms: tuple[str, ...] | None = None) -> list[str]:
    low = (text or "").lower()
    return [term for term in (terms or BANNED_TERMS) if term and term in low]


@dataclass(frozen=True)
class CheckRow:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class CheckReport:
    path: str
    caption: str
    rules: CampaignRules
    rows: tuple[CheckRow, ...] = ()
    info: media.MediaInfo | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return bool(self.rows) and all(row.ok for row in self.rows)

    @property
    def failures(self) -> tuple[CheckRow, ...]:
        return tuple(row for row in self.rows if not row.ok)

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "passed": self.passed,
            "caption": self.caption,
            "rules": self.rules.as_dict(),
            "rows": [row.as_dict() for row in self.rows],
            "failures": [row.name for row in self.failures],
            "warnings": list(self.warnings),
            "media": (
                {
                    "duration": round(self.info.duration, 3),
                    "width": self.info.width,
                    "height": self.info.height,
                    "fps": self.info.fps,
                    "has_audio": self.info.has_audio,
                }
                if self.info
                else None
            ),
        }

    def lines(self) -> list[str]:
        out = [
            f"{'PASS' if row.ok else 'FAIL'}  {row.name}" + (f"  [{row.detail}]" if row.detail else "")
            for row in self.rows
        ]
        out.extend(f"NOTE  {warning}" for warning in self.warnings)
        out.append("RESULT: " + ("PASS" if self.passed else "FAIL"))
        return out


def check_clip(
    path: Path | str,
    *,
    caption: str = "",
    hook: str = "",
    rules: CampaignRules | None = None,
) -> CheckReport:
    """Inspect a finished file against the campaign. Never raises on bad media."""
    target = Path(path).expanduser()
    active = rules or default_campaign()
    copy = "\n".join(part for part in (hook, caption) if part).strip()
    rows: list[CheckRow] = []
    warnings: list[str] = []
    info: media.MediaInfo | None = None

    def add(name: str, ok: bool, detail: str = "") -> None:
        rows.append(CheckRow(name=name, ok=ok, detail=detail))

    add("file exists", target.exists(), str(target) if target.exists() else "not found")
    if target.exists():
        add("file is not empty", target.stat().st_size > 0, f"{target.stat().st_size:,} bytes")
        try:
            info = media.probe(target)
        except media.MediaError as exc:
            add("file is readable video", False, str(exc)[:160])
        else:
            add("file is readable video", True, f"{info.codec or 'video'}")
            add(
                "resolution 1080x1920",
                (info.width, info.height) == (1080, 1920),
                f"{info.width}x{info.height}",
            )
            add("fps about 30", 29.0 <= info.fps <= 31.0, f"{info.fps:.2f}")
            add("has audio", info.has_audio, "no audio track" if not info.has_audio else "")
            add(
                f"duration {active.min_seconds:g}-{active.max_seconds:g}s"
                + ("" if active.from_file else " (default)"),
                active.min_seconds <= info.duration <= active.max_seconds,
                f"{info.duration:.1f}s",
            )
    else:
        add("file is readable video", False, "cannot probe a missing file")

    hits = banned_in(copy, active.all_banned)
    add("no banned terms in caption/hook", not hits, ", ".join(hits))
    if active.required_hashtags:
        missing = [tag for tag in active.required_hashtags if tag.lower() not in copy.lower()]
        add(
            "required hashtags",
            not missing,
            ("missing " + " ".join(missing)) if missing else " ".join(active.required_hashtags),
        )
    if active.required_mention:
        add(
            "required mention",
            active.required_mention.lower() in copy.lower(),
            active.required_mention,
        )
    add("caption fits 2200 characters", len(copy) <= CAPTION_LIMIT, f"{len(copy)} chars")
    if copy and "#" not in copy:
        warnings.append("caption has no hashtags; discovery will be weaker")
    if not active.from_file:
        warnings.append(
            "no campaign file: defaults used, this is not a rules check. "
            "Paste the real brief into data/campaigns/ or pass --campaign."
        )

    return CheckReport(
        path=str(target),
        caption=copy,
        rules=active,
        rows=tuple(rows),
        info=info,
        warnings=tuple(warnings),
    )