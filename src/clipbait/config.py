"""Runtime configuration.

Everything is environment driven so the same image can run as a laptop tool, a
self-hosted server or a worker. No secret ever gets a default.

The design rule: Clipbait must be useful with *zero* API keys. Local Whisper +
heuristic ranking is the fallback path; cloud keys only upgrade quality.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    ffmpeg: str
    ffprobe: str
    ytdlp: str
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    transcriber: str
    llm_provider: str
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    openai_model: str = "gpt-4o-mini"
    anthropic_model: str = "claude-sonnet-4-2"
    gemini_model: str = "gemini-2.5-flash"
    ollama_model: str = "qwen2.5:14b"
    # Meme video inserts. ``auto`` uses the hosted API when one is configured and
    # falls back to a locally rendered placeholder otherwise.
    meme_provider: str = "auto"
    meme_api_url: str | None = None
    meme_api_key: str | None = None
    meme_model: str = ""
    meme_poll_seconds: int = 240
    # Search a user-supplied meme *library* (Giphy/Tenor/own catalogue). The
    # query is the insert prompt; the first result whose video downloads wins.
    meme_search_url: str | None = None
    meme_search_key: str | None = None
    meme_search_auth: str = "bearer"  # bearer | header | query | none
    meme_search_header: str = "Authorization"
    meme_search_key_param: str = "api_key"
    meme_search_query_param: str = "q"
    meme_search_results: str = "results"  # dot path to the result list
    meme_search_video: str = "url"  # dot path to a video URL in each result
    meme_search_limit: int = 8
    meme_search_rating: str = ""  # e.g. "pg-13" for Giphy
    max_clips: int = 8
    target_clip_seconds: int = 55
    render_concurrency: int = 1
    hw_encoder: str = "auto"
    cors_origins: tuple[str, ...] = ("*",)
    live_buffer_seconds: int = 420
    live_segment_seconds: int = 4
    live_default_provider: str = "auto"
    public_base_url: str = "http://localhost:8000"
    # Publishing the last mile. Upload-Post reaches every connected network with
    # one request and reports the account's measured best hours. No key means a
    # dry-only run: the plan is printed, nothing leaves the machine.
    upload_post_api_key: str | None = None
    upload_post_base_url: str = "https://api.upload-post.com"
    upload_post_user: str | None = None
    # Seconds before an upload request is abandoned. A vertical clip is tens of
    # megabytes and a home uplink is often a few hundred KB/s, so the default
    # leaves room for the body to arrive before the client gives up.
    upload_post_timeout: float = 300.0
    # Embedding-free product analytics: never phones home.
    telemetry: bool = False
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def live_dir(self) -> Path:
        return self.data_dir / "live"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.projects_dir, self.live_dir, self.models_dir):
            path.mkdir(parents=True, exist_ok=True)


def _default_data_dir() -> Path:
    configured = _env("CLIPBAIT_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    # Repo-local data keeps the "git clone && run" story simple.
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() and (parent / "src").exists():
            return parent / "data"
    return Path.home() / ".clipbait"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = _default_data_dir()
    origins = tuple(
        part.strip() for part in (_env("CLIPBAIT_CORS_ORIGINS", "*") or "*").split(",") if part.strip()
    )
    return Settings(
        data_dir=data_dir,
        db_path=Path(_env("CLIPBAIT_DB_PATH", str(data_dir / "clipbait.db"))).expanduser(),
        ffmpeg=_env("CLIPBAIT_FFMPEG", shutil.which("ffmpeg") or "ffmpeg") or "ffmpeg",
        ffprobe=_env("CLIPBAIT_FFPROBE", shutil.which("ffprobe") or "ffprobe") or "ffprobe",
        ytdlp=_env("CLIPBAIT_YTDLP", shutil.which("yt-dlp") or "yt-dlp") or "yt-dlp",
        whisper_model=_env("CLIPBAIT_WHISPER_MODEL", "small") or "small",
        whisper_device=_env("CLIPBAIT_WHISPER_DEVICE", "auto") or "auto",
        whisper_compute_type=_env("CLIPBAIT_WHISPER_COMPUTE", "int8") or "int8",
        transcriber=_env("CLIPBAIT_TRANSCRIBER", "auto") or "auto",
        llm_provider=_env("CLIPBAIT_LLM_PROVIDER", "auto") or "auto",
        openai_api_key=_env("OPENAI_API_KEY"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        gemini_api_key=_env("GEMINI_API_KEY"),
        ollama_base_url=_env("OLLAMA_BASE_URL", "http://localhost:11434") or "http://localhost:11434",
        openai_model=_env("CLIPBAIT_OPENAI_MODEL", "gpt-4o-mini") or "gpt-4o-mini",
        anthropic_model=_env("CLIPBAIT_ANTHROPIC_MODEL", "claude-sonnet-4-2") or "claude-sonnet-4-2",
        gemini_model=_env("CLIPBAIT_GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash",
        ollama_model=_env("CLIPBAIT_OLLAMA_MODEL", "qwen2.5:14b") or "qwen2.5:14b",
        meme_provider=_env("CLIPBAIT_MEME_PROVIDER", "auto") or "auto",
        meme_api_url=_env("CLIPBAIT_MEME_API_URL"),
        meme_api_key=_env("CLIPBAIT_MEME_API_KEY"),
        meme_model=_env("CLIPBAIT_MEME_MODEL", "") or "",
        meme_poll_seconds=max(10, _env_int("CLIPBAIT_MEME_POLL_SECONDS", 240)),
        meme_search_url=_env("CLIPBAIT_MEME_SEARCH_URL"),
        meme_search_key=_env("CLIPBAIT_MEME_SEARCH_KEY"),
        meme_search_auth=(_env("CLIPBAIT_MEME_SEARCH_AUTH", "bearer") or "bearer").lower(),
        meme_search_header=_env("CLIPBAIT_MEME_SEARCH_HEADER", "Authorization") or "Authorization",
        meme_search_key_param=_env("CLIPBAIT_MEME_SEARCH_KEY_PARAM", "api_key") or "api_key",
        meme_search_query_param=_env("CLIPBAIT_MEME_SEARCH_QUERY_PARAM", "q") or "q",
        meme_search_results=_env("CLIPBAIT_MEME_SEARCH_RESULTS", "results") or "results",
        meme_search_video=_env("CLIPBAIT_MEME_SEARCH_VIDEO", "url") or "url",
        meme_search_limit=max(1, _env_int("CLIPBAIT_MEME_SEARCH_LIMIT", 8)),
        meme_search_rating=_env("CLIPBAIT_MEME_SEARCH_RATING", "") or "",
        max_clips=_env_int("CLIPBAIT_MAX_CLIPS", 8),
        target_clip_seconds=_env_int("CLIPBAIT_TARGET_CLIP_SECONDS", 55),
        render_concurrency=max(1, _env_int("CLIPBAIT_RENDER_CONCURRENCY", 1)),
        hw_encoder=_env("CLIPBAIT_HW_ENCODER", "auto") or "auto",
        cors_origins=origins,
        live_buffer_seconds=max(60, _env_int("CLIPBAIT_LIVE_BUFFER_SECONDS", 420)),
        live_segment_seconds=max(2, _env_int("CLIPBAIT_LIVE_SEGMENT_SECONDS", 4)),
        live_default_provider=_env("CLIPBAIT_LIVE_CHAT_PROVIDER", "auto") or "auto",
        public_base_url=_env("CLIPBAIT_PUBLIC_BASE_URL", "http://localhost:8000") or "http://localhost:8000",
        upload_post_api_key=_env("UPLOAD_POST_API_KEY"),
        upload_post_base_url=_env("CLIPBAIT_UPLOAD_POST_URL", "https://api.upload-post.com")
        or "https://api.upload-post.com",
        upload_post_user=_env("UPLOAD_POST_USER"),
        upload_post_timeout=max(30.0, _env_float("CLIPBAIT_UPLOAD_POST_TIMEOUT", 300.0)),
        telemetry=_env_bool("CLIPBAIT_TELEMETRY", False),
    )


@dataclass(frozen=True)
class Capabilities:
    ffmpeg: bool
    ffprobe: bool
    ytdlp: bool
    whisper: bool
    vision: bool
    pytchat: bool
    llm_provider: str | None
    transcriber: str
    meme_provider: str | None = None
    live_chat: str = "innertube"

    def as_dict(self) -> dict[str, object]:
        return {
            "ffmpeg": self.ffmpeg,
            "ffprobe": self.ffprobe,
            "ytdlp": self.ytdlp,
            "whisper": self.whisper,
            "vision": self.vision,
            "pytchat": self.pytchat,
            "llm_provider": self.llm_provider,
            "transcriber": self.transcriber,
            "meme_provider": self.meme_provider,
            "live_chat": self.live_chat,
        }


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def detect_capabilities(settings: Settings | None = None) -> Capabilities:
    settings = settings or get_settings()
    console = settings.transcriber

    provider: str | None = None
    preference = settings.llm_provider
    if preference != "none":
        if preference == "openai" and settings.openai_api_key:
            provider = "openai"
        elif preference == "anthropic" and settings.anthropic_api_key:
            provider = "anthropic"
        elif preference == "gemini" and settings.gemini_api_key:
            provider = "gemini"
        elif preference == "ollama":
            provider = "ollama"
        elif preference == "auto":
            if settings.openai_api_key:
                provider = "openai"
            elif settings.anthropic_api_key:
                provider = "anthropic"
            elif settings.gemini_api_key:
                provider = "gemini"
            elif _env("OLLAMA_BASE_URL"):
                provider = "ollama"

    if console == "auto":
        console = "whisper" if _has_module("faster_whisper") else "mock"

    if _has_module("pytchat"):
        live_chat = "pytchat"
    elif _env("YOUTUBE_API_KEY"):
        live_chat = "data-api"
    else:
        live_chat = "innertube"

    return Capabilities(
        ffmpeg=bool(shutil.which(settings.ffmpeg) or Path(settings.ffmpeg).exists()),
        ffprobe=bool(shutil.which(settings.ffprobe) or Path(settings.ffprobe).exists()),
        ytdlp=bool(shutil.which(settings.ytdlp) or Path(settings.ytdlp).exists()),
        whisper=_has_module("faster_whisper"),
        vision=_has_module("cv2"),
        pytchat=_has_module("pytchat"),
        llm_provider=provider,
        transcriber=console,
        meme_provider=_meme_provider_name(settings),
        live_chat=live_chat,
    )


def _meme_provider_name(settings: Settings) -> str | None:
    # Imported lazily: the memes package imports this module.
    from .memes.providers import get_meme_provider, provider_name

    return provider_name(get_meme_provider(settings))