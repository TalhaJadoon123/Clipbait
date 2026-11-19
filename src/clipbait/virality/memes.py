"""Meme formats, matched to content signals and region.

Meme literacy is a distribution advantage: the same 40-second story is a
different product under "POV" than under "The way he said it". Each format
declares the emotional signal it needs, the regions where it is native, and a
risk level so an operator can keep the brand safe.

Formats are evergreen structures, not stolen assets. No copyrighted media is
shipped or generated here - this is hook scaffolding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .regions import RegionProfile


@dataclass(frozen=True)
class MemeFormat:
    id: str
    name: str
    pattern: str  # {subject} / {claim} placeholders
    signals: tuple[str, ...]
    regions: tuple[str, ...]  # empty = global
    platforms: tuple[str, ...]
    risk: str = "low"  # low | medium
    example: str = ""
    note: str = ""


MEMES: tuple[MemeFormat, ...] = (
    MemeFormat(
        "pov", "POV", "POV: {claim}",
        ("story", "relatable", "confession"), (), ("tiktok", "reels", "shorts"),
        example="POV: you finally fired the client who paid late",
        note="Works when the viewer can picture themselves in the frame.",
    ),
    MemeFormat(
        "nobody", "Nobody:", "Nobody:\\n{subject}",
        ("absurd", "contrarian", "confession"), ("US", "UK", "CA", "AU", "DE", "NL"),
        ("tiktok", "reels", "shorts"),
        example="Nobody:\\nMe explaining a 4-hour workflow at 2am",
    ),
    MemeFormat(
        "tell_me", "Tell me without telling me",
        "Tell me you {subject} without telling me you {subject}",
        ("relatable", "confession", "humor"), ("US", "UK", "CA", "AU", "IN"),
        ("tiktok", "reels"),
        example="Tell me you're a founder without telling me you're a founder",
    ),
    MemeFormat(
        "wait_for_it", "Wait for it", "Wait for it… {claim}",
        ("story", "reveal", "surprise"), (), ("tiktok", "reels", "shorts"),
        example="Wait for it… the client doubled the budget",
        note="Only use when the payoff actually lands inside the clip.",
    ),
    MemeFormat(
        "the_way", "The way", "The way {subject} …",
        ("story", "emotion", "reaction"), ("US", "UK", "AU", "CA", "PH"),
        ("tiktok", "reels"),
        example="The way he paused before answering says everything",
    ),
    MemeFormat(
        "me_when", "Me when", "Me when {claim}",
        ("relatable", "humor", "frustration"), (), ("tiktok", "reels", "shorts"),
        example="Me when the export finishes on the first try",
    ),
    MemeFormat(
        "expectation", "Expectation vs reality",
        "Expectation: {claim}\\nReality: {twist}",
        ("contrast", "humor", "revelation"), (), ("tiktok", "reels", "shorts"),
        example="Expectation: passive income\\nReality: 3am support tickets",
    ),
    MemeFormat(
        "cooked", "Cooked", "{subject} is cooked",
        ("failure", "conflict", "opinion"), ("US", "UK", "AU", "CA"),
        ("tiktok", "reels"),
        example="The 9-to-5 is cooked and he explains why in 30 seconds",
        risk="medium",
    ),
    MemeFormat(
        "rent_free", "Rent free", "This lives in my head rent free: {claim}",
        ("revelation", "obsession", "praise"), ("US", "UK", "CA", "AU"),
        ("tiktok", "reels"),
        example="This framework lives in my head rent free",
    ),
    MemeFormat(
        "main_character", "Main character",
        "Main character energy: {claim}",
        ("story", "confidence", "humor"), ("US", "UK", "BR", "PH", "MX"),
        ("tiktok", "reels"),
        example="Main character energy: quitting on a Tuesday",
    ),
    MemeFormat(
        "no_way", "No way this is real",
        "No way this is real: {claim}",
        ("surprise", "revelation", "number"), (), ("tiktok", "reels", "shorts"),
        example="No way this is real: 300 videos from one recording",
    ),
    MemeFormat(
        "delulu", "Delulu is the solulu", "{subject}: delulu is the solulu",
        ("confidence", "humor", "motivation"), ("US", "UK", "PH", "AU", "IN"),
        ("tiktok", "reels"),
        example="Founder math: delulu is the solulu",
        note="Skews young; skip for B2B/enterprise cuts.",
    ),
    MemeFormat(
        "sana_all", "Sana all", "Sana all: {claim}",
        ("envy", "praise", "relatable"), ("PH",), ("tiktok", "reels"),
        example="Sana all: client paid on time",
    ),
    MemeFormat(
        "grabe", "Grabe naman", "Grabe naman: {claim}",
        ("surprise", "emotion"), ("PH",), ("tiktok", "reels", "shorts"),
        example="Grabe naman: 1M views in 6 hours",
    ),
    MemeFormat(
        "eish", "Eish", "Eish… {claim}",
        ("frustration", "relatable", "humor"), ("ZA",), ("tiktok", "reels"),
        example="Eish… load shedding during the launch",
    ),
    MemeFormat(
        "wahala", "No wahala", "{claim}. No wahala.",
        ("hustle", "resilience", "humor"), ("NG",), ("tiktok", "reels"),
        example="Client ghosted. No wahala, next one.",
    ),
    MemeFormat(
        "jugaad", "Jugaad", "Jugaad: {claim}",
        ("hack", "value", "resourcefulness"), ("IN", "PK", "BD"),
        ("shorts", "reels", "tiktok"),
        example="Jugaad: the ₹0 setup that beat a ₹50k one",
    ),
    MemeFormat(
        "scene", "Scene hai", "Scene hai: {claim}",
        ("story", "drama", "relatable"), ("IN",), ("shorts", "reels", "tiktok"),
        example="Scene hai: the intern out-earned the manager",
        risk="medium",
    ),
    MemeFormat(
        "neta", "Neta mode", "Neta mode: {claim}",
        ("opinion", "confidence"), ("MX", "AR", "ES"), ("tiktok", "reels"),
        example="Neta mode: I'm not arguing, I'm explaining",
        risk="medium",
    ),
    MemeFormat(
        "ojeito", "Jeito brasileiro", "Jeito brasileiro: {claim}",
        ("hack", "humor", "resourcefulness"), ("BR",), ("tiktok", "reels"),
        example="Jeito brasileiro: resolvido com fita crepe",
    ),
    MemeFormat(
        "mashallah", "Mashallah", "Mashallah: {claim}",
        ("praise", "awe", "wholesome"), ("PK", "BD", "AE", "SA", "TR", "ID"),
        ("reels", "tiktok", "shorts"),
        example="Mashallah: the kid's first sale",
    ),
    MemeFormat(
        "krasno", "Просто красота", "Просто красота: {claim}",
        ("praise", "awe"), ("PL",), ("reels", "shorts"),
        example="Просто красота: the chart went vertical",
    ),
    MemeFormat(
        "yabai", "Yabai", "やばい: {claim}",
        ("surprise", "awe"), ("JP",), ("shorts", "reels", "tiktok"),
        example="やばい: 3日で100万再生",
    ),
    MemeFormat(
        "daebak", "Daebak", "대박: {claim}",
        ("surprise", "praise"), ("KR",), ("shorts", "reels", "tiktok"),
        example="대박: 이걸 무료로?",
    ),
    MemeFormat(
        "xieu", "Xỉu", "Xỉu: {claim}",
        ("surprise", "humor"), ("VN",), ("tiktok", "reels", "shorts"),
        example="Xỉu: giá này mà vẫn lời",
    ),
    MemeFormat(
        "thai555", "555", "{claim} 555",
        ("humor", "absurd"), ("TH",), ("tiktok", "reels", "shorts"),
        example="ทำได้ทุกวัน 555",
    ),
)

SIGNAL_RULES = {
    "story": r"\b(i|we|he|she|they) (was|were|had|did|went|said|told|built|lost|made)\b",
    "confession": r"\b(honestly|truth is|admit|confession|i never told)\b",
    "relatable": r"\b(you know when|every time|we all|most people|adulting)\b",
    "surprise": r"\b(wait|what|no way|insane|crazy|shocked|unbelievable)\b",
    "revelation": r"\b(the secret|turns out|actually|nobody knows|here'?s why)\b",
    "number": r"\b\d+([.,]\d+)?\s?(%|percent|x|k\b|million|thousand)\b",
    "humor": r"\b(lol|haha|laugh|joke|funny|hilarious)\b",
    "frustration": r"\b(hate|annoying|tired|sick of|fed up|worst)\b",
    "failure": r"\b(failed|lost|quit|fired|rejected|wrong|broke)\b",
    "conflict": r"\b(disagree|argument|fight|pushback|versus|vs)\b",
    "opinion": r"\b(i think|in my opinion|overrated|underrated|hot take)\b",
    "confidence": r"\b(i will|i'm going to|watch me|guaranteed)\b",
    "hack": r"\b(hack|trick|shortcut|cheat code|tip)\b",
    "value": r"\b(framework|system|method|steps?|how to|template)\b",
    "praise": r"\b(amazing|incredible|best|genius|beautiful|goat)\b",
    "wholesome": r"\b(kind|helped|thank you|grateful|community)\b",
    "envy": r"\b(wish|jealous|lucky|meanwhile|meanwhile me)\b",
    "hustle": r"\b(grind|hustle|client|sales|revenue|money)\b",
    "absurd": r"\b(what is happening|makes no sense|absurd|chaos)\b",
    "awe": r"\b(insane|mind blown|crazy|unreal)\b",
    "story_peak": r"\b(and then|suddenly|in the end|the result)\b",
    "contrast": r"\b(but|however|except|instead|until)\b",
    "motivation": r"\b(keep going|start now|don't quit|discipline)\b",
}


def detect_signals(text: str) -> list[str]:
    lowered = text.lower()
    found = [name for name, pattern in SIGNAL_RULES.items() if re.search(pattern, lowered)]
    # Story is only a signal when a narrative arc is present, not on any pronoun.
    if "story" in found and not re.search(r"\b(then|after|before|finally|ended)\b", lowered):
        found.remove("story")
    return found


def select_memes(
    text: str,
    region: RegionProfile,
    platform: str = "tiktok",
    *,
    limit: int = 4,
    allow_medium_risk: bool = False,
) -> list[dict]:
    signals = set(detect_signals(text))
    ranked: list[tuple[float, MemeFormat]] = []
    for meme in MEMES:
        if meme.risk == "medium" and not allow_medium_risk:
            continue
        if meme.regions and region.code not in meme.regions:
            continue
        if meme.platforms and platform not in meme.platforms:
            continue
        overlap = len(signals.intersection(meme.signals))
        if overlap == 0:
            continue
        regional_bonus = 1.2 if region.code in meme.regions else 0.0
        score = overlap * 2.0 + regional_bonus
        ranked.append((score, meme))
    ranked.sort(key=lambda item: (-item[0], item[1].name))
    out: list[dict] = []
    for score, meme in ranked[:limit]:
        out.append(
            {
                "id": meme.id,
                "name": meme.name,
                "pattern": meme.pattern,
                "example": meme.example,
                "risk": meme.risk,
                "match": round(min(1.0, score / 6.0), 2),
                "note": meme.note,
            }
        )
    return out


def apply_pattern(meme: dict, subject: str, claim: str, twist: str = "") -> str:
    pattern = meme.get("pattern", "{claim}")
    # Patterns carry "\\n" so the raw format reads well; hooks are single-line.
    pattern = pattern.replace("\\n", " ")
    text = (
        pattern.replace("{subject}", subject)
        .replace("{claim}", claim)
        .replace("{twist}", twist or claim)
    )
    return re.sub(r"\s+", " ", text).strip()