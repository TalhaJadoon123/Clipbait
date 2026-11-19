"""Regional virality playbooks.

These are editing heuristics, not cultural truth. Each profile encodes what
performs *on average* for a market: language, humour register, caption density,
evergreen hashtags, posting windows in local time and the platforms that matter.

Everything is data, so an operator can override any field without touching
code (see ``CLIPBAIT_REGION_OVERRIDES``).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace

log = logging.getLogger("clipbait.virality.regions")


@dataclass(frozen=True)
class RegionProfile:
    code: str
    name: str
    flag: str
    languages: tuple[str, ...]
    humor: str
    emoji_density: int  # 0-3
    caption_preset: str
    hashtags: tuple[str, ...]
    cta: str
    posting_windows: tuple[str, ...]
    platform_bias: tuple[str, ...]
    slang: tuple[str, ...] = ()
    notes: str = ""
    avoid: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "flag": self.flag,
            "languages": list(self.languages),
            "humor": self.humor,
            "emoji_density": self.emoji_density,
            "caption_preset": self.caption_preset,
            "hashtags": list(self.hashtags),
            "cta": self.cta,
            "posting_windows": list(self.posting_windows),
            "platform_bias": list(self.platform_bias),
            "slang": list(self.slang),
            "notes": self.notes,
            "avoid": list(self.avoid),
        }


REGIONS: dict[str, RegionProfile] = {
    "US": RegionProfile(
        "US", "United States", "🇺🇸", ("en",), "punchy, confident, direct", 2, "hormozi",
        ("#shorts", "#viral", "#storytime", "#podcast", "#business", "#mindset"),
        "Follow for part two", ("07:00-09:00", "12:00-13:00", "18:00-22:00"),
        ("tiktok", "shorts", "reels"),
        ("no cap", "lowkey", "it's giving", "cooked", "rent free", "main character"),
        "Front-load the claim. Numbers beat adjectives. Cut every 'um'.",
        ("political endorsements", "medical claims", "financial guarantees"),
    ),
    "UK": RegionProfile(
        "UK", "United Kingdom", "🇬🇧", ("en",), "dry, self-deprecating, understated", 1, "clean",
        ("#shorts", "#uk", "#fyp", "#storytime", "#podcast", "#london"),
        "Tell me I'm wrong", ("07:30-09:30", "12:00-14:00", "19:00-22:00"),
        ("tiktok", "reels", "shorts"),
        ("innit", "taking the piss", "absolute state", "sorted", "gutted", "fair enough"),
        "Irony lands; hype does not. Keep captions smaller and let the line breathe.",
        ("royal family takes", "regional stereotypes"),
    ),
    "CA": RegionProfile(
        "CA", "Canada", "🇨🇦", ("en", "fr"), "friendly, deadpan", 1, "clean",
        ("#shorts", "#canada", "#fyp", "#storytime", "#podcast"),
        "Save this for later", ("07:00-09:00", "12:00-13:00", "19:00-22:00"),
        ("tiktok", "reels", "shorts"),
        ("no worries", "bud", "to be fair", "honestly eh"),
        "Polite framing outperforms aggression. Bilingual variants for QC audiences.",
        ("US politics",),
    ),
    "AU": RegionProfile(
        "AU", "Australia", "🇦🇺", ("en",), "larrikin, blunt, warm", 2, "hormozi",
        ("#shorts", "#australia", "#fyp", "#aussie", "#podcast"),
        "Have a go, tell me how it goes", ("06:30-08:30", "12:00-14:00", "19:00-22:00"),
        ("tiktok", "reels", "shorts"),
        ("yeah nah", "heaps", "fully cooked", "arvo", "mate"),
        "Self-mockery and swearing-lite travel. Avoid corporate tone entirely.",
        ("crocodile/outback clichés unless self-aware",),
    ),
    "IN": RegionProfile(
        "IN", "India", "🇮🇳", ("en", "hi"), "high-energy, story-first, aspirational", 3, "lime",
        ("#shorts", "#india", "#reels", "#businessideas", "#motivation", "#hindishorts"),
        "Comment 'GUIDE' and I'll send it", ("08:00-10:00", "13:00-15:00", "20:00-23:00"),
        ("shorts", "reels", "tiktok"),
        ("scene", "bhai", "jugaad", "full paisa vasool", "kya baat"),
        "Hindi-English code-mixing (Hinglish) widens reach; keep one idea per clip.",
        ("religious sensitivity", "caste references", "communal framing"),
    ),
    "PK": RegionProfile(
        "PK", "Pakistan", "🇵🇰", ("ur", "en"), "warm, rhetorical, story-first", 2, "default",
        ("#shorts", "#pakistan", "#reels", "#urdu", "#motivation"),
        "Share this with someone who needs it", ("09:00-11:00", "14:00-16:00", "21:00-23:30"),
        ("shorts", "reels", "tiktok"),
        ("yaar", "bilkul", "zabardast", "mashallah"),
        "Urdu hooks outperform English for local reach; keep honourifics correct.",
        ("religion", "politics", "India-Pakistan bait"),
    ),
    "BD": RegionProfile(
        "BD", "Bangladesh", "🇧🇩", ("bn", "en"), "warm, expressive", 2, "default",
        ("#shorts", "#bangladesh", "#reels", "#bangla", "#motivation"),
        "Tag a friend", ("09:00-11:00", "14:00-16:00", "21:00-23:00"),
        ("shorts", "reels", "tiktok"),
        ("bhai", "darun", "ki bolen"),
        "Bengali first, English subtitle second. Music-forward edits travel.",
        ("religious commentary", "political commentary"),
    ),
    "ID": RegionProfile(
        "ID", "Indonesia", "🇮🇩", ("id", "en"), "playful, communal, meme-native", 2, "lime",
        ("#shorts", "#indonesia", "#fyp", "#reels", "#viral"),
        "Komen 'MAU' biar dikirim", ("11:00-13:00", "16:00-18:00", "20:00-23:00"),
        ("tiktok", "reels", "shorts"),
        ("gas", "kuy", "receh", "auto viral", "anjir"),
        "Duet/stitch formats and reaction bait work; keep captions fast and simple.",
        ("religious sensitivity", "ethnic stereotypes"),
    ),
    "PH": RegionProfile(
        "PH", "Philippines", "🇵🇭", ("tl", "en"), "warm, dramatic, relational", 3, "hormozi",
        ("#shorts", "#philippines", "#fyp", "#reels", "#tagalog"),
        "Sino ang kailangan makakita nito?", ("11:00-13:00", "17:00-19:00", "21:00-24:00"),
        ("tiktok", "reels", "shorts"),
        ("grabe", "charot", "sana all", "lodi", "petmalu"),
        "Emotional payoff and family angles overperform; Taglish captions widen reach.",
        ("political endorsements",),
    ),
    "BR": RegionProfile(
        "BR", "Brazil", "🇧🇷", ("pt",), "loud, funny, physically expressive", 3, "lime",
        ("#shorts", "#brasil", "#fyp", "#reels", "#viral"),
        "Comenta se você faria", ("11:00-13:00", "17:00-19:00", "20:00-23:00"),
        ("tiktok", "reels", "shorts"),
        ("mano", "cara", "nossa", "que isso", "bora"),
        "Hooks in Brazilian Portuguese; keep slang current and regional-neutral.",
        ("political candidates", "regional prejudice"),
    ),
    "MX": RegionProfile(
        "MX", "Mexico", "🇲🇽", ("es",), "fast, playful, double-entendre", 3, "lime",
        ("#shorts", "#mexico", "#fyp", "#reels", "#parati"),
        "Etiqueta a quien lo necesita", ("11:00-13:00", "17:00-19:00", "21:00-23:00"),
        ("tiktok", "reels", "shorts"),
        ("güey", "neta", "no manches", "chido", "órale"),
        "Mexican Spanish over neutral Spanish for reach; humour travels across LATAM.",
        ("narco glorification", "class slurs"),
    ),
    "ES": RegionProfile(
        "ES", "Spain", "🇪🇸", ("es",), "ironic, fast, social", 2, "hormozi",
        ("#shorts", "#españa", "#reels", "#parati", "#viral"),
        "Guárdalo para luego", ("08:00-10:00", "14:00-16:00", "20:00-23:00"),
        ("tiktok", "reels", "shorts"),
        ("tío", "flipas", "en plan", "vaya", "mola"),
        "Spain-Spanish slang reads very differently to LATAM; keep separate variants.",
        ("regional politics",),
    ),
    "DE": RegionProfile(
        "DE", "Germany", "🇩🇪", ("de",), "precise, dry, proof-first", 0, "clean",
        ("#shorts", "#deutschland", "#reels", "#wissen", "#business"),
        "Speichern und später ansehen", ("07:00-09:00", "12:00-14:00", "18:00-21:00"),
        ("shorts", "reels", "tiktok"),
        ("krass", "alter", "digga", "läuft"),
        "Credentials and specificity beat hype. No emoji spam; no unverifiable claims.",
        ("Nazi-era comparisons", "unverifiable financial promises"),
    ),
    "FR": RegionProfile(
        "FR", "France", "🇫🇷", ("fr",), "witty, contrarian, understated", 1, "cine",
        ("#shorts", "#france", "#reels", "#pourtoi", "#viral"),
        "Dis-moi ce que tu en penses", ("07:30-09:30", "12:00-14:00", "19:00-22:00"),
        ("tiktok", "reels", "shorts"),
        ("franchement", "carrément", "c'est chaud", "grave"),
        "A counter-intuitive claim is the hook. Sarcasm works; over-enthusiasm doesn't.",
        ("immigration politics",),
    ),
    "NG": RegionProfile(
        "NG", "Nigeria", "🇳🇬", ("en", "pidgin"), "bold, witty, status-aware", 3, "lime",
        ("#shorts", "#nigeria", "#reels", "#naija", "#hustle"),
        "Drop a 🔥 if you agree", ("08:00-10:00", "13:00-15:00", "20:00-23:00"),
        ("tiktok", "reels", "shorts"),
        ("wahala", "abeg", "no wahala", "e choke", "soft life"),
        "Pidgin hooks widen reach; confidence and hustle framing convert.",
        ("ethnic jokes", "religious bait"),
    ),
    "ZA": RegionProfile(
        "ZA", "South Africa", "🇿🇦", ("en", "zu"), "warm, blunt, funny", 2, "default",
        ("#shorts", "#southafrica", "#reels", "#fyp", "#mzansi"),
        "Share with your people", ("07:00-09:00", "12:00-14:00", "19:00-22:00"),
        ("tiktok", "reels", "shorts"),
        ("eish", "sharp", "yoh", "sho"),
        "Code-switching is the norm; keep subtitles clean and representative.",
        ("racial stereotypes",),
    ),
    "AE": RegionProfile(
        "AE", "UAE", "🇦🇪", ("ar", "en"), "aspirational, polished", 1, "clean",
        ("#shorts", "#uae", "#dubai", "#reels", "#business"),
        "Follow for more", ("08:00-10:00", "13:00-15:00", "20:00-23:00"),
        ("reels", "shorts", "tiktok"),
        ("habibi", "wallah", "inshallah"),
        "Bilingual captions (AR/EN). Avoid alcohol and dating angles for broad reach.",
        ("religion", "politics"),
    ),
    "SA": RegionProfile(
        "SA", "Saudi Arabia", "🇸🇦", ("ar",), "respectful, aspirational", 1, "clean",
        ("#shorts", "#السعودية", "#reels", "#ريلس", "#تحفيز"),
        "شارك الفيديو", ("08:00-10:00", "13:00-15:00", "21:00-00:00"),
        ("shorts", "reels", "tiktok"),
        ("يا طويل العمر", "ما شاء الله", "الله يعطيك العافية"),
        "Arabic-first captions, RTL layout. Keep music optional and modest visuals.",
        ("religion", "politics", "dating"),
    ),
    "TR": RegionProfile(
        "TR", "Türkiye", "🇹🇷", ("tr",), "dramatic, emotional, fast", 2, "hormozi",
        ("#shorts", "#türkiye", "#reels", "#keşfet", "#viral"),
        "Kaydet ve paylaş", ("08:00-10:00", "13:00-15:00", "20:00-23:00"),
        ("tiktok", "reels", "shorts"),
        ("abi", "yani", "valla", "efsane"),
        "Strong emotional contrast works; keep music and captions tightly synced.",
        ("political figures", "Atatürk comparisons"),
    ),
    "JP": RegionProfile(
        "JP", "Japan", "🇯🇵", ("ja",), "polite, detail-obsessed, soft-spoken", 0, "clean",
        ("#shorts", "#日本", "#reels", "#豆知識", "#viral"),
        "保存して後で見てね", ("07:00-09:00", "12:00-13:00", "20:00-23:00"),
        ("shorts", "reels", "tiktok"),
        ("やばい", "すごい", "マジで", "エグい"),
        "Vertical text and small typography read as native; avoid hard-sell.",
        ("political topics", "historical disputes"),
    ),
    "KR": RegionProfile(
        "KR", "South Korea", "🇰🇷", ("ko",), "fast, aesthetic, competitive", 1, "hormozi",
        ("#shorts", "#한국", "#reels", "#정보", "#viral"),
        "저장해두세요", ("07:00-09:00", "12:00-13:00", "20:00-23:00"),
        ("tiktok", "reels", "shorts"),
        ("대박", "진짜", "헐", "레전드"),
        "Clean typography and quick cuts; honorifics must be right.",
        ("political topics",),
    ),
    "VN": RegionProfile(
        "VN", "Vietnam", "🇻🇳", ("vi",), "playful, fast, meme-native", 2, "lime",
        ("#shorts", "#vietnam", "#reels", "#xuhuong", "#viral"),
        "Lưu lại để xem sau", ("11:00-13:00", "17:00-19:00", "20:00-23:00"),
        ("tiktok", "reels", "shorts"),
        ("trời ơi", "xỉu", "hết hồn", "đỉnh"),
        "Local slang and reaction captions are the product; keep text large.",
        ("political topics",),
    ),
    "TH": RegionProfile(
        "TH", "Thailand", "🇹🇭", ("th",), "playful, absurd, gentle", 2, "lime",
        ("#shorts", "#thailand", "#reels", "#fyp", "#viral"),
        "เซฟไว้ดูทีหลัง", ("11:00-13:00", "17:00-19:00", "20:00-23:00"),
        ("tiktok", "reels", "shorts"),
        ("555", "จริงดิ", "โอ้ย", "ปัง"),
        "Comedy and gentle absurdity travel; avoid confrontation.",
        ("monarchy", "religion"),
    ),
    "NL": RegionProfile(
        "NL", "Netherlands", "🇳🇱", ("nl", "en"), "direct, dry, no-nonsense", 0, "clean",
        ("#shorts", "#nederland", "#reels", "#fyp", "#business"),
        "Bewaar voor later", ("07:00-09:00", "12:00-14:00", "18:00-21:00"),
        ("shorts", "reels", "tiktok"),
        ("gewoon", "echt", "nou", "lekker"),
        "Directness is respect. Skip the hype adjectives.",
        ("Zwarte Piet references",),
    ),
    "PL": RegionProfile(
        "PL", "Poland", "🇵🇱", ("pl",), "dry, ironic, resilient", 1, "clean",
        ("#shorts", "#polska", "#reels", "#fyp", "#biznes"),
        "Zapisz na później", ("07:00-09:00", "12:00-14:00", "19:00-22:00"),
        ("shorts", "reels", "tiktok"),
        ("serio", "masakra", "spoko", "no ba"),
        "Understatement and irony beat enthusiasm; keep claims concrete.",
        ("politics", "historical grievances"),
    ),
    "AR": RegionProfile(
        "AR", "Argentina", "🇦🇷", ("es",), "rapid-fire, ironic, theatrical", 2, "hormozi",
        ("#shorts", "#argentina", "#reels", "#parati", "#viral"),
        "Guardalo para después", ("11:00-13:00", "17:00-19:00", "21:00-00:00"),
        ("tiktok", "reels", "shorts"),
        ("che", "boludo", "postá", "zarpado"),
        "Rioplatense voseo, not neutral Spanish. Exaggeration is the format.",
        ("political figures",),
    ),
}

DEFAULT_REGION = "US"


def _apply_overrides(base: dict[str, RegionProfile]) -> dict[str, RegionProfile]:
    """Merge operator overrides from ``CLIPBAIT_REGION_OVERRIDES``.

    Shape: ``{"IN": {"cta": "Comment GUIDE", "hashtags": ["#shorts"]}}``.
    Unknown regions and unknown fields are ignored rather than failing startup:
    a bad override must not take the whole product down.
    """
    raw = os.getenv("CLIPBAIT_REGION_OVERRIDES", "").strip()
    if not raw:
        return base
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("CLIPBAIT_REGION_OVERRIDES is not valid JSON; ignoring")
        return base
    if not isinstance(payload, dict):
        log.warning("CLIPBAIT_REGION_OVERRIDES must be a JSON object; ignoring")
        return base

    fields = set(RegionProfile.__dataclass_fields__)
    merged = dict(base)
    for code, patch in payload.items():
        profile = merged.get(str(code).upper())
        if profile is None or not isinstance(patch, dict):
            continue
        updates: dict[str, object] = {}
        for key, value in patch.items():
            if key not in fields or key in {"code", "name", "flag"}:
                continue
            current = getattr(profile, key)
            if isinstance(current, tuple) and isinstance(value, list):
                updates[key] = tuple(str(item) for item in value)
            elif isinstance(current, tuple) and isinstance(value, str):
                updates[key] = tuple(part.strip() for part in value.split(",") if part.strip())
            else:
                updates[key] = value
        merged[str(code).upper()] = replace(profile, **updates)  # type: ignore[arg-type]
    return merged


REGIONS = _apply_overrides(REGIONS)


def get_region(code: str | None) -> RegionProfile:
    if not code:
        return REGIONS[DEFAULT_REGION]
    return REGIONS.get(code.upper(), REGIONS[DEFAULT_REGION])


def catalog() -> list[dict]:
    return [profile.as_dict() for profile in REGIONS.values()]


def platform_limits(platform: str) -> dict[str, int]:
    return {
        "tiktok": {"title": 100, "caption": 2200, "hashtags": 6},
        "reels": {"title": 125, "caption": 2200, "hashtags": 8},
        "shorts": {"title": 100, "caption": 5000, "hashtags": 3},
        "linkedin": {"title": 200, "caption": 3000, "hashtags": 5},
        "x": {"title": 120, "caption": 280, "hashtags": 2},
    }.get(platform, {"title": 100, "caption": 2200, "hashtags": 6})