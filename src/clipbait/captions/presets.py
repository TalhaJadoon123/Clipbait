"""Caption presets.

Numbers are in output pixels (1080x1920). Each preset is a complete look, not a
pile of toggles: that is what makes one-click output feel designed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CaptionStyle:
    font: str = "Anton"
    size: int = 64
    color: str = "#FFFFFF"
    highlight: str = "#C6FF3D"
    outline: str = "#000000"
    outline_width: float = 3.0
    shadow: float = 0.0
    box_color: str | None = None
    box_opacity: float = 0.0
    uppercase: bool = True
    bold: bool = True
    max_chars: int = 18
    max_duration: float = 2.2
    max_words: int = 5
    position: str = "bottom"
    margin_v: int = 260
    effect: str = "none"  # none | pop | glow | box | highlight
    reveal: bool = False
    base_opacity: float = 0.72
    letter_spacing: float = 0.0
    # Optional emphasis colours, cycled per caption line. The reference Speed
    # edits alternate hot pink / yellow / cyan / lime on the active words.
    palette: tuple[str, ...] = ()


PRESETS: dict[str, CaptionStyle] = {
    "default": CaptionStyle(),
    "hormozi": CaptionStyle(
        size=72, highlight="#FFD400", max_chars=16, max_duration=1.9, max_words=4,
        effect="pop", uppercase=True, margin_v=300,
    ),
    "pill": CaptionStyle(
        size=62, highlight="#FFFFFF", color="#FFFFFF", box_color="#7C3AED",
        box_opacity=0.92, outline_width=0.0, max_chars=15, max_words=4,
        effect="highlight", uppercase=True, margin_v=300,
    ),
    "lime": CaptionStyle(
        size=62, highlight="#0A0A0A", color="#0A0A0A", box_color="#C6FF3D",
        box_opacity=1.0, outline_width=0.0, max_chars=15, max_words=4,
        effect="highlight", uppercase=True, margin_v=300,
    ),
    "oneword": CaptionStyle(
        size=104, highlight="#C6FF3D", max_chars=1, max_words=1, max_duration=1.1,
        effect="pop", uppercase=True, margin_v=520, outline_width=4.0,
    ),
    # One word, dead centre, huge. The "middle of the screen, one word at a
    # time" look: the eye never leaves the action and each word lands like a
    # punch. Uppercase Anton, thick outline, lime active word.
    "bigword": CaptionStyle(
        size=124, highlight="#C6FF3D", max_chars=1, max_words=1, max_duration=1.0,
        effect="pop", uppercase=True, position="middle", margin_v=0,
        outline_width=5.0, base_opacity=0.9,
    ),
    # The reference Speed-edit look: short 3-4 word phrases dead centre, heavy
    # black outline, and the active word cycling through hot pink / yellow /
    # cyan / lime. Designed for the facecam split layout.
    "speed": CaptionStyle(
        size=86, color="#FFFFFF", highlight="#FFD400", outline_width=5.5,
        max_chars=24, max_words=4, max_duration=2.0,
        position="middle", margin_v=0, uppercase=True, bold=True,
        effect="pop", base_opacity=1.0,
        palette=("#FF3EA5", "#FFD400", "#25E6FF", "#5CFF5C"),
    ),
    "cine": CaptionStyle(
        font="Montserrat", size=54, color="#F5F1E8", highlight="#F5F1E8",
        uppercase=False, outline_width=0.0, shadow=3.0, max_chars=26, max_words=7,
        max_duration=2.8, effect="none", margin_v=240, base_opacity=0.86,
        letter_spacing=1.2,
    ),
    "clean": CaptionStyle(
        font="Montserrat", size=52, color="#FFFFFF", highlight="#FFFFFF",
        uppercase=False, outline_width=0.0, shadow=2.0, max_chars=24, max_words=7,
        max_duration=2.6, effect="none", margin_v=240, base_opacity=0.88,
    ),
}

POSITIONS = {
    "bottom": 2,
    "middle": 5,
    "top": 8,
}


def get_preset(name: str) -> CaptionStyle:
    return PRESETS.get(name, PRESETS["default"])


def preset_catalog() -> list[dict]:
    return [
        {
            "id": key,
            "font": style.font,
            "size": style.size,
            "highlight": style.highlight,
            "uppercase": style.uppercase,
            "effect": style.effect,
            "description": _DESCRIPTIONS.get(key, ""),
        }
        for key, style in PRESETS.items()
    ]


_DESCRIPTIONS = {
    "default": "White caps, acid-lime active word. The all-rounder.",
    "hormozi": "Big, fast, word-by-word punch with a pop.",
    "pill": "Violet pill behind the spoken word.",
    "lime": "Black text on a lime pill. Maximum scroll-stop.",
    "oneword": "One word at a time, huge. For high-energy cuts.",
    "bigword": "One word, dead centre, huge. The scroll-stop look for Shorts.",
    "speed": "Speed-edit look: centred phrases, rotating pink/yellow/cyan/lime emphasis.",
    "cine": "Editorial serif-free look, sentence case, soft shadow.",
    "clean": "No outline, no shouting. Documentaries and B2B.",
}