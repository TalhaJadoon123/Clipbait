"""ASS subtitle generation.

One Dialogue event per spoken word, with the active word restyled inline. That
is the same approach CapCut/Submagic use and it avoids flicker: the line never
reflows, only the emphasis moves. Technique adapted from the MIT-licensed
openshorts project; timing, presets and hook/chat overlays are Clipbait's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..schema import ChatMessage, Word
from .presets import POSITIONS, CaptionStyle, get_preset

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{size},{primary},{secondary},{outline},{back},{bold_value},0,0,0,100,100,{spacing},0,{border_style},{outline_w},{shadow},{alignment},60,60,{margin_v},1
Style: Hook,{hook_font},{hook_size},{hook_primary},{hook_primary},{hook_outline},{hook_back},-1,0,0,0,100,100,0,0,1,4,2,8,70,70,150,1
Style: Chat,{chat_font},{chat_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H99000000,0,0,0,0,100,100,0,0,3,2,0,8,40,40,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


@dataclass(frozen=True)
class CaptionGroup:
    words: list[Word]
    start: float
    end: float


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis == 100:
        whole += 1
        centis = 0
    return f"{hours}:{minutes:02d}:{whole:02d}.{centis:02d}"


def ass_color(hex_color: str, fallback: str = "FFFFFF") -> str:
    value = (hex_color or "").lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6 or re.search(r"[^0-9a-fA-F]", value):
        value = fallback
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"&H00{blue}{green}{red}&"


def _dim(color: str, opacity: float) -> str:
    """Simulate lower opacity by blending toward black (no alpha stacking bugs)."""
    value = (color or "#FFFFFF").lstrip("#")
    if len(value) != 6:
        value = "FFFFFF"
    factor = max(0.0, min(1.0, opacity))
    parts = [int(value[i : i + 2], 16) for i in (0, 2, 4)]
    blended = "".join(f"{int(part * factor):02X}" for part in parts)
    return f"#{blended}"


def escape_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", " ")
        .strip()
    )


def _apply_case(text: str, uppercase: bool) -> str:
    return text.upper() if uppercase else text


def build_groups(
    words: list[Word],
    start: float,
    end: float,
    style: CaptionStyle,
) -> list[CaptionGroup]:
    """Group words into caption blocks on punctuation, silence and budget."""
    usable = [w for w in words if w.end > start and w.start < end]
    if not usable:
        return []
    groups: list[CaptionGroup] = []
    current: list[Word] = []
    group_start = usable[0].start

    def flush() -> None:
        nonlocal current
        if current:
            groups.append(
                CaptionGroup(words=list(current), start=current[0].start, end=current[-1].end)
            )
            current = []

    for word in usable:
        if current:
            gap = word.start - current[-1].end
            projected = word.end - group_start
            chars = len(" ".join(w.text for w in current)) + 1 + len(word.text)
            if (
                gap > 0.75
                or projected > style.max_duration
                or len(current) >= style.max_words
                or chars > style.max_chars
                or re.search(r"[.!?]$", current[-1].text)
            ):
                flush()
                group_start = word.start
        current.append(word)
    flush()
    return groups


def _line_for_group(
    group: CaptionGroup,
    active_index: int,
    style: CaptionStyle,
    *,
    highlight: str,
    base: str,
    dim: str,
    reveal: bool,
) -> str:
    parts: list[str] = []
    for index, word in enumerate(group.words):
        text = escape_text(_apply_case(word.text, style.uppercase))
        if not text:
            continue
        if index == active_index:
            pop = r"\fscx112\fscy112\t(0,90,\fscx100\fscy100)" if style.effect == "pop" else ""
            glow = r"\blur6\3c" + highlight if style.effect == "glow" else ""
            parts.append(
                f"{{\\c{highlight}{pop}{glow}\\fscx112\\fscy112}}"
                f"{text}"
                r"{\fscx100\fscy100\blur0}"
            )
        elif index > active_index:
            if reveal:
                parts.append(r"{\alpha&HFF&}" + text)
            else:
                parts.append(f"{{\\c{dim}}}{text}")
        else:
            parts.append(f"{{\\c{base}}}{text}")
    return " ".join(parts)


def build_captions(
    words: list[Word],
    start: float,
    end: float,
    preset: str = "default",
    *,
    highlight_override: str | None = None,
    position: str | None = None,
) -> str:
    """Render a complete .ass document for one clip."""
    style = get_preset(preset)
    if highlight_override:
        style = CaptionStyle(**{**style.__dict__, "highlight": highlight_override})
    alignment = POSITIONS.get((position or style.position), 2)
    border_style = 3 if style.box_color else 1
    outline_color = ass_color(style.box_color or style.outline)
    back_color = "&H99000000" if style.box_color else "&H00000000"

    header = ASS_HEADER.format(
        font=style.font,
        size=style.size,
        primary=ass_color(style.color),
        secondary=ass_color(style.highlight),
        outline=outline_color,
        back=back_color,
        bold_value=-1 if style.bold else 0,
        spacing=style.letter_spacing,
        border_style=border_style,
        outline_w=style.outline_width,
        shadow=style.shadow,
        alignment=alignment,
        margin_v=style.margin_v,
        hook_font="Montserrat" if style.font != "Anton" else "Anton",
        hook_size=min(88, max(60, style.size + 6)),
        hook_primary=ass_color("#FFFFFF"),
        hook_outline=ass_color("#000000"),
        hook_back="&HB4000000",
        chat_font="Montserrat",
        chat_size=30,
    )

    base = ass_color(_dim(style.color, style.base_opacity))
    highlight = ass_color(style.highlight)
    dim = ass_color(_dim(style.color, max(0.25, style.base_opacity - 0.2)))
    events: list[str] = []

    groups = build_groups(words, start, end, style)
    for group in groups:
        for index, word in enumerate(group.words):
            event_start = max(0.0, word.start - start)
            if index + 1 < len(group.words):
                event_end = max(event_start + 0.05, group.words[index + 1].start - start)
            else:
                event_end = max(event_start + 0.12, min(group.end, end) - start + 0.10)
            line = _line_for_group(
                group,
                index,
                style,
                highlight=highlight,
                base=base,
                dim=dim,
                reveal=style.reveal,
            )
            if not line:
                continue
            effect = r"{\fad(60,40)}" if style.effect != "pop" else ""
            events.append(
                f"Dialogue: 0,{ass_time(event_start)},{ass_time(event_end)},Caption,,0,0,0,,{effect}{line}"
            )
    return header + "\n".join(events) + ("\n" if events else "")


def build_hook(title: str, duration: float = 3.6) -> str:
    """Opening-frame headline, burned for the first seconds only."""
    text = escape_text(title)
    if not text:
        return ""
    if len(text) > 26:
        words = text.split()
        half = max(1, len(words) // 2)
        text = " ".join(words[:half]) + r"\N" + " ".join(words[half:])
    event = (
        f"Dialogue: 1,{ass_time(0.15)},{ass_time(max(1.2, duration))},Hook,,0,0,0,,"
        r"{\fad(140,260)\fscx104\fscy104\t(0,220,\fscx100\fscy100)}" + text
    )
    return event


def build_chat_overlay(messages: list[ChatMessage], start: float, end: float, max_messages: int = 7) -> str:
    """Burn the chat reaction into the top third: proof the crowd went wild."""
    if not messages:
        return ""
    events: list[str] = []
    span = max(1.0, end - start)
    usable = messages[:max_messages]
    for message in usable:
        offset = max(0.0, message.t - start)
        if offset > span - 1.0:
            continue
        shown = escape_text(message.text)[:72]
        author = escape_text(message.author)[:18]
        if not shown:
            continue
        text = f"{{\\c&H00D4FFFF&}}{author}: {{\\c&H00FFFFFF&}}{shown}"
        events.append(
            f"Dialogue: 2,{ass_time(offset)},{ass_time(min(span, offset + 2.6))},Chat,,0,0,0,,"
            r"{\fad(120,180)}" + text
        )
    return "\n".join(events)


def write_ass(
    path,
    words: list[Word],
    start: float,
    end: float,
    preset: str = "default",
    *,
    hook: str | None = None,
    chat: list[ChatMessage] | None = None,
    position: str | None = None,
):
    document = build_captions(words, start, end, preset, position=position)
    extras: list[str] = []
    if hook:
        extras.append(build_hook(hook))
    if chat:
        extras.append(build_chat_overlay(chat, start, end))
    if extras:
        document = document.rstrip("\n") + "\n" + "\n".join(e for e in extras if e) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    return path