"""Source ingestion providers."""

from .downloader import (
    DownloadTracker,
    IngestResult,
    fetch_audio,
    fetch_remote,
    fetch_section,
    ingest_local,
    probe_url,
    source_kind_for,
)

__all__ = [
    "DownloadTracker",
    "IngestResult",
    "fetch_audio",
    "fetch_remote",
    "fetch_section",
    "ingest_local",
    "probe_url",
    "source_kind_for",
]