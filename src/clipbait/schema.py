"""Domain models shared by the pipeline, API and CLI.

These are intentionally small and framework-free so the core engine can be
imported as a library without FastAPI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ReframeMode = Literal["auto", "center", "blur", "split"]
CaptionPreset = Literal["default", "hormozi", "pill", "lime", "oneword", "cine", "clean"]
SourceKind = Literal["youtube", "twitch", "kick", "upload", "local", "live"]
JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class Word(BaseModel):
    text: str
    start: float
    end: float
    prob: float | None = None


class TranscriptSegment(BaseModel):
    text: str
    start: float
    end: float


class Transcript(BaseModel):
    language: str = "en"
    duration: float = 0.0
    words: list[Word] = Field(default_factory=list)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    provider: str = "unknown"
    speaker_turns: list[dict] | None = None

    def text_between(self, start: float, end: float) -> str:
        return " ".join(
            w.text for w in self.words if w.end >= start and w.start <= end
        ).strip()


class ClipScores(BaseModel):
    hook: int = 0
    emotion: int = 0
    value: int = 0
    share: int = 0
    overall: int = 0

    @field_validator("hook", "emotion", "value", "share", "overall", mode="before")
    @classmethod
    def clamp(cls, value: object) -> int:
        try:
            number = int(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            number = 0
        return max(0, min(100, number))


class Highlight(BaseModel):
    start: float
    end: float
    title: str
    hook: str = ""
    reason: str = ""
    quote: str = ""
    keywords: list[str] = Field(default_factory=list)
    scores: ClipScores = Field(default_factory=ClipScores)
    source: str = "heuristic"

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class ProjectOptions(BaseModel):
    clips: int = Field(default=5, ge=1, le=20)
    target_seconds: int = Field(default=55, ge=15, le=180)
    language: str = "auto"
    caption_preset: CaptionPreset = "default"
    reframe_mode: ReframeMode = "auto"
    captions: bool = True
    hook_titles: bool = True
    chat_overlay: bool = False
    aspect: str = "9:16"
    llm: bool = True
    region: str = "US"
    platform: Literal["tiktok", "reels", "shorts", "linkedin", "x"] = "tiktok"
    memes: bool = True
    variants: int = Field(default=4, ge=1, le=8)
    allow_medium_risk: bool = False
    # ``auto`` streams a URL source: only the audio is downloaded for
    # transcription and each kept clip is fetched as a short section. ``download``
    # forces the old "fetch the whole file first" behaviour.
    source_mode: Literal["auto", "download"] = "auto"
    # Meme *video* inserts (b-roll cutaways between spoken sentences). Off by
    # default: it changes the footage, not just the copy.
    meme_inserts: bool = False
    meme_insert_seconds: float = Field(default=2.0, ge=0.8, le=5.0)
    meme_max_inserts: int = Field(default=2, ge=1, le=6)


class Project(BaseModel):
    id: str
    title: str
    source_kind: SourceKind
    source_url: str | None = None
    source_path: str | None = None
    duration: float | None = None
    status: str = "queued"
    language: str | None = None
    options: ProjectOptions = Field(default_factory=ProjectOptions)
    error: str | None = None
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)


class Clip(BaseModel):
    id: str
    project_id: str
    title: str
    hook: str = ""
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)
    start: float
    end: float
    score: int = 0
    scores: ClipScores = Field(default_factory=ClipScores)
    reason: str = ""
    quote: str = ""
    status: str = "draft"  # draft | rendering | ready | failed
    render_path: str | None = None
    thumb_path: str | None = None
    caption_preset: CaptionPreset = "default"
    reframe_mode: ReframeMode = "auto"
    captions: bool = True
    hook_titles: bool = True
    meta: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)


class Job(BaseModel):
    id: str
    project_id: str | None = None
    monitor_id: str | None = None
    kind: str
    status: JobStatus = "queued"
    progress: float = 0.0
    step: str = "queued"
    message: str = ""
    error: str | None = None
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)


class ChatMessage(BaseModel):
    t: float
    author: str = ""
    text: str
    kind: str = "text"  # text | superchat | membership | emoji


class SpikeSignal(BaseModel):
    """Why a moment was cut, in numbers.

    Chat and on-stream content share this shape so the UI can render one
    explanation card. A fused moment fills both halves: the chat block says how
    the audience reacted, the content block says what the streamer actually did.
    """

    rate: float = 0.0
    rate_z: float = 0.0
    unique_z: float = 0.0
    hype: float = 0.0
    repeats: float = 0.0
    sentiment: float = 0.0
    score: float = 0.0
    top_messages: list[str] = Field(default_factory=list)
    # On-stream content signals (speech / audio / action).
    speech: float = 0.0
    loudness: float = 0.0
    action: float = 0.0
    quote: str = ""
    reasons: list[str] = Field(default_factory=list)
    origin: str = "chat"  # chat | speech | audio | action | fused


class LiveMoment(BaseModel):
    id: str
    monitor_id: str
    start: float
    end: float
    score: float
    signal: SpikeSignal = Field(default_factory=SpikeSignal)
    messages: list[ChatMessage] = Field(default_factory=list)
    clip_id: str | None = None
    created_at: str = Field(default_factory=utcnow)


class MonitorOptions(BaseModel):
    chat_provider: str = "auto"
    spike_threshold: float = Field(default=3.0, ge=0.5, le=20)
    auto_clip: bool = True
    captions: bool = True
    hook_titles: bool = True
    chat_overlay: bool = True
    caption_preset: CaptionPreset = "default"
    reframe_mode: ReframeMode = "auto"
    buffer_seconds: int = Field(default=180, ge=60, le=900)
    pre_roll: int = Field(default=25, ge=5, le=120)
    post_roll: int = Field(default=20, ge=5, le=120)
    cooldown: int = Field(default=45, ge=10, le=600)
    max_clips: int = Field(default=8, ge=1, le=50)
    region: str = "US"
    platform: Literal["tiktok", "reels", "shorts", "linkedin", "x"] = "tiktok"
    # Watch the stream itself, not only chat: transcribe the rolling buffer,
    # measure audio spikes and scene motion, and clip when the streamer says or
    # does something worth cutting. This is what catches a viral line before
    # chat has time to react.
    content_detection: bool = True
    content_threshold: float = Field(default=2.5, ge=0.5, le=12)
    content_interval: int = Field(default=18, ge=8, le=120)
    speech_window: int = Field(default=32, ge=12, le=120)


class LiveMonitor(BaseModel):
    id: str
    url: str
    title: str = ""
    status: str = "idle"  # idle | live | stopped | failed
    chat_provider: str = "auto"
    options: MonitorOptions = Field(default_factory=MonitorOptions)
    started_at: str | None = None
    stopped_at: str | None = None
    error: str | None = None
    stats: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)


# ---------------------------------------------------------------- requests


class CreateProjectRequest(BaseModel):
    url: str | None = None
    source_kind: SourceKind | None = None
    title: str | None = None
    options: ProjectOptions = Field(default_factory=ProjectOptions)

    @field_validator("url")
    @classmethod
    def url_must_be_http(cls, value: str | None) -> str | None:
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("url must be http(s)")
        return value


class CreateMonitorRequest(BaseModel):
    url: str
    title: str | None = None
    options: MonitorOptions = Field(default_factory=MonitorOptions)

    @field_validator("url")
    @classmethod
    def url_must_be_http(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("url must be http(s)")
        return value


class ClipPatch(BaseModel):
    title: str | None = None
    hook: str | None = None
    start: float | None = None
    end: float | None = None
    caption_preset: CaptionPreset | None = None
    reframe_mode: ReframeMode | None = None
    captions: bool | None = None
    hook_titles: bool | None = None


class RenderRequest(BaseModel):
    caption_preset: CaptionPreset | None = None
    reframe_mode: ReframeMode | None = None
    captions: bool | None = None
    hook_titles: bool | None = None


class MarkUploadedRequest(BaseModel):
    url: str | None = None
    platform: str | None = None