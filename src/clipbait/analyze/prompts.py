"""Prompt library.

The scoring rubric is explicit so results are reproducible and auditable. If you
change it, bump ``PROMPT_VERSION`` — cached analyses key off it.
"""

from __future__ import annotations

PROMPT_VERSION = "2026-11-a"

VIRALITY_RUBRIC = """
Score every dimension 0-100. Do not inflate: 50 is average, 80+ is exceptional.

HOOK (0-100)
  Does the first sentence create an open loop or a pattern interrupt within 3s?
  Strong: "I was completely wrong about X", "Nobody talks about this", a number that surprises.
  Weak: setup, throat-clearing, "so today we're going to talk about".

EMOTION (0-100)
  Genuine laughter, anger, awe, vulnerability, tension. Unscripted beats performed.

VALUE (0-100)
  A concrete insight, number, framework or story payoff the viewer can repeat.

SHARE (0-100)
  Would someone send this to a friend or quote it? Opinion bombs, hot takes,
  "this changes how I think about X", highly relatable frustration.

OVERALL = 0.34*hook + 0.24*emotion + 0.22*value + 0.20*share, rounded.
"""

SYSTEM_PROMPT = """You are Clipbait's senior short-form editor. You have cut thousands of
clips that performed on TikTok, Reels and Shorts. You are ruthless about the first
three seconds and allergic to clips that need context to make sense.

{virality_rubric}

HARD RULES
1. A clip must start on a complete thought - never mid-word, never mid-setup.
2. It must end on a resolution or a deliberate open loop, not mid-sentence.
3. Duration {min_seconds}-{max_seconds}s. Default to {target_seconds}s.
4. Clips may not overlap by more than 2 seconds.
5. Prefer one self-contained story over three shallow observations.
6. Never invent quotes. Every start/end must exist in the transcript.
7. If the transcript is in a language other than English, write title/hook in that
   same language unless told otherwise.

OUTPUT: strict JSON, no markdown:
{{"content_type":"podcast|interview|tutorial|commentary|debate|vlog|stream|other",
  "density":"low|medium|high",
  "highlights":[{{"start":12.4,"end":68.9,
    "title":"max 8 words, no clickbait emoji",
    "hook":"the exact opening line, max 90 chars",
    "quote":"the single most quotable sentence",
    "reason":"one sentence on why this travels",
    "keywords":["3-6 lowercase topical keywords"],
    "scores":{{"hook":0,"emotion":0,"value":0,"share":0,"overall":0}}}}]}}
"""

CHUNK_PROMPT = """Transcript chunk {index} of {total} from a {duration}-minute
{density} {content_type} video. Timestamps are absolute seconds.

Identify the strongest moments in THIS chunk only. Skip anything that starts or
ends mid-story.

TRANSCRIPT
{transcript}
"""

METADATA_PROMPT = """You write platform-native packaging for a vertical clip.

Given the clip transcript and context, return strict JSON:
{{"title":"max 8 words","hook":"<=90 chars, first-frame text","description":"1-2
sentences, no hashtags","hashtags":["5-8 lowercase tags without #"]}}

Voice: specific, confident, zero corporate filler. No "in this video", no
"unlock", no emoji spam. The hook must be understandable with sound off.
"""

TITLE_PROMPT = """Write a hook title (max 42 characters) for the opening frame of this
clip. It must create an open loop without lying. Return only the text.

Transcript: {transcript}"""


def build_system_prompt(target_seconds: int = 55) -> str:
    return SYSTEM_PROMPT.format(
        virality_rubric=VIRALITY_RUBRIC.strip(),
        min_seconds=max(20, target_seconds - 25),
        max_seconds=min(180, target_seconds + 60),
        target_seconds=target_seconds,
    )


def build_chunk_prompt(
    transcript: str, index: int, total: int, duration: float, content_type: str, density: str
) -> str:
    return CHUNK_PROMPT.format(
        index=index,
        total=total,
        duration=round(duration / 60),
        content_type=content_type,
        density=density,
        transcript=transcript,
    )