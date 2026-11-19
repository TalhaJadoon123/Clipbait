"""Source ingestion providers."""

from .downloader import (
    DownloadTracker,
    IngestResult,
    fetch_remote,
    ingest_local,
    probe_url,
    source_kind_for,
)

__all__ = [
    "DownloadTracker",
    "IngestResult",
    "fetch_remote",
    "ingest_local",
    "probe_url",
    "source_kind_for",
]