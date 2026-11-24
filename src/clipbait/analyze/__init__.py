"""Highlight analysis orchestrator: LLM first, deterministic fallback always."""

from __future__ import annotations

import logging

from ..config import Settings, get_settings
from ..schema import ClipScores, Highlight, ProjectOptions, Transcript
from ..virality import games, hashtags
from . import packaging, prompts, ranking
from .heuristics import candidate_highlights, keywords
from .llm import LLMClient, LLMError

log = logging.getLogger("clipbait.analyze")

CHUNK_SECONDS = 840.0
CHUNK_OVERLAP = 45.0


def _transcript_slice(transcript: Transcript, start: float, end: float) -> str:
    lines: list[str] = []
    for segment in transcript.segments:
        if segment.end < start or segment.start > end:
            continue
        stamp = f"[{max(0.0, segment.start - start):.1f}-{min(end, segment.end) - start:.1f}]"
        lines.append(f"{stamp} {segment.text}")
    if not lines:
        for word in transcript.words:
            if start <= word.start <= end:
                lines.append(word.text)
        return " ".join(lines)
    return "\n".join(lines)


def _chunks(duration: float) -> list[tuple[float, float]]:
    if duration <= CHUNK_SECONDS:
        return [(0.0, duration)]
    chunks: list[tuple[float, float]] = []
    cursor = 0.0
    while cursor < duration:
        end = min(duration, cursor + CHUNK_SECONDS)
        chunks.append((cursor, end))
        if end >= duration:
            break
        cursor = end - CHUNK_OVERLAP
    return chunks


def _parse_highlights(payload: dict, transcript: Transcript, offset: float) -> list[Highlight]:
    out: list[Highlight] = []
    for item in payload.get("highlights", []) or []:
        try:
            start = max(0.0, float(item.get("start", 0)) + offset)
            end = min(transcript.duration or 1e9, float(item.get("end", 0)) + offset)
        except (TypeError, ValueError):
            continue
        if end - start < 8:
            continue
        scores_raw = item.get("scores") or {}
        scores = ClipScores(
            hook=scores_raw.get("hook", 0),
            emotion=scores_raw.get("emotion", 0),
            value=scores_raw.get("value", 0),
            share=scores_raw.get("share", 0),
            overall=scores_raw.get("overall", 0),
        )
        if scores.overall == 0:
            scores.overall = round(
                0.34 * scores.hook + 0.24 * scores.emotion + 0.22 * scores.value + 0.20 * scores.share
            )
        out.append(
            Highlight(
                start=round(start, 3),
                end=round(end, 3),
                title=(item.get("title") or "Untitled clip")[:90],
                hook=(item.get("hook") or "")[:160],
                quote=(item.get("quote") or "")[:300],
                reason=(item.get("reason") or "")[:300],
                keywords=[str(k).lower() for k in (item.get("keywords") or [])][:8],
                scores=scores,
                source="llm",
            )
        )
    return out


def llm_highlights(
    transcript: Transcript, options: ProjectOptions, llm: LLMClient
) -> list[Highlight]:
    system = prompts.build_system_prompt(options.target_seconds)
    chunks = _chunks(transcript.duration or 600.0)
    all_highlights: list[Highlight] = []
    content_type, density = "video", "medium"
    for index, (start, end) in enumerate(chunks, start=1):
        body = _transcript_slice(transcript, start, end)
        if len(body) < 200:
            continue
        prompt = prompts.build_chunk_prompt(
            body, index, len(chunks), transcript.duration, content_type, density
        )
        try:
            result = llm.complete_json(prompt, system=system)
        except LLMError as exc:
            log.warning("chunk %s/%s failed: %s", index, len(chunks), exc)
            continue
        content_type = result.data.get("content_type", content_type)
        density = result.data.get("density", density)
        all_highlights.extend(_parse_highlights(result.data, transcript, start))
    if not all_highlights:
        raise LLMError("LLM returned no usable highlights")
    return all_highlights


def _enrich(highlights: list[Highlight], transcript: Transcript) -> None:
    for highlight in highlights:
        text = transcript.text_between(highlight.start, highlight.end)
        if not highlight.hook:
            highlight.hook = ranking.snap_to_words(highlight, transcript.words).title or ""
        if not highlight.title or highlight.title == "Untitled clip":
            highlight.title = (highlight.hook or text[:60] or "Untitled clip")[:80]
        if not highlight.keywords:
            highlight.keywords = keywords(text)
        if not highlight.quote:
            highlight.quote = highlight.hook or text[:160]
        if not highlight.reason:
            highlight.reason = "Selected by the offline ranker for a strong opening and payoff."


def _hashtags(highlight: Highlight) -> list[str]:
    """Discovery tags for a clip, built from what it actually shows.

    The game named in the title or the words leads, then the ranker's/LLM's own
    keywords, then the general Shorts block as a floor.
    """
    game = games.detect(highlight.title, highlight.quote, " ".join(highlight.keywords))[0]
    tags = hashtags.make(
        text=highlight.quote or "",
        title=highlight.title,
        game=game,
        ai_tags=highlight.keywords,
        limit=8,
    )
    for filler in ("shorts", "reels", "tiktok"):
        if filler not in tags and len(tags) < 8:
            tags.append(filler)
    return tags[:8]


def _window_text(transcript: Transcript, highlight: Highlight) -> str:
    return transcript.text_between(highlight.start, highlight.end) or highlight.hook or ""


def _prefer_titled(
    raw: list[Highlight],
    fallback: list[Highlight],
    options: ProjectOptions,
    transcript: Transcript,
    meta: dict,
) -> list[Highlight]:
    """Pick the moments that can carry the reference-channel title shape.

    On a long VOD the ranker can choose between many equally good moments. When
    the project wants reference titles, prefer the ones that actually package
    as ``Speed <VERB> <OBJECT> in GTA 6`` over higher-scoring moments that
    cannot — otherwise a 200-clip batch ships half fragments. Each title is used
    once so the channel grid never shows the same phrase twice.
    """
    names = frozenset(packaging.known_names(transcript.words))
    meta["known_names"] = sorted(names)

    titled: dict[tuple[float, float], str] = {}
    pool: list[Highlight] = []
    for highlight in raw:
        title = packaging.speed_title(_window_text(transcript, highlight), highlight.keywords, names=names)
        if title:
            pool.append(highlight)
            titled[(highlight.start, highlight.end)] = title

    if len(pool) < options.clips:
        # Not enough packagable moments to fill the batch; keep the ranker's
        # own best picks and let the title step fall back to a quote.
        return fallback

    return ranking.select(
        pool,
        max_clips=options.clips,
        max_overlap=0.25,
        min_gap_seconds=3.0,
        min_score=22.0,
        group_key=lambda h: titled.get((h.start, h.end), ""),
        max_per_group=1,
    )


def analyze(
    transcript: Transcript,
    options: ProjectOptions,
    *,
    energy: list[tuple[float, float]] | None = None,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
) -> tuple[list[Highlight], dict]:
    """Return (highlights, analysis_meta). Never raises for ranking problems."""
    settings = settings or get_settings()
    llm = llm or LLMClient(settings)
    meta: dict = {"prompt_version": prompts.PROMPT_VERSION, "provider": "heuristic"}

    raw: list[Highlight] = []
    if options.llm and llm.available:
        try:
            raw = llm_highlights(transcript, options, llm)
            meta.update(provider=llm.provider, model=llm.model, chunks=len(_chunks(transcript.duration)))
        except LLMError as exc:
            log.warning("falling back to heuristic ranking: %s", exc)
            meta["llm_error"] = str(exc)

    if not raw:
        raw = candidate_highlights(
            transcript,
            target_seconds=options.target_seconds,
            max_clips=max(options.clips * 3, 12),
            energy=energy or [],
        )
        meta["provider"] = "heuristic"
    else:
        # LLM output is still subject to boundary integrity.
        raw = [
            ranking.snap_to_words(h, transcript.words, transcript.duration)
            for h in raw
            if h.end > h.start
        ]

    selected = ranking.select(
        raw,
        max_clips=options.clips,
        max_overlap=0.25,
        min_gap_seconds=3.0,
    )
    if options.title_style == "speed":
        selected = _prefer_titled(raw, selected, options, transcript, meta)
    if not selected:
        selected = ranking.select(
            candidate_highlights(
                transcript,
                target_seconds=options.target_seconds,
                max_clips=max(options.clips, 5),
                energy=energy or [],
            ),
            max_clips=options.clips,
            max_overlap=0.45,
        )
        meta["provider"] = "heuristic"

    _enrich(selected, transcript)
    meta["selected"] = len(selected)
    meta["hashtags"] = {h.title: _hashtags(h) for h in selected}
    return selected, meta


def packaging_for(highlight: Highlight, analysis_meta: dict) -> dict:
    tags = analysis_meta.get("hashtags", {}).get(highlight.title) or _hashtags(highlight)
    # The caption body is public copy: the spoken quote, not the detector's own
    # note (``reason`` explains the pick to the operator and stays on the clip).
    body = (highlight.quote or "").strip()
    if body.lower() == (highlight.reason or "").strip().lower():
        body = ""
    return {
        "title": highlight.title,
        "hook": highlight.hook,
        "description": body[:280],
        "hashtags": tags,
    }