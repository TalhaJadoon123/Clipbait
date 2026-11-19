"""Source ingestion providers."""

from .downloader import IngestResult, fetch_remote, ingest_local, probe_url, source_kind_for

__all__ = ["IngestResult", "fetch_remote", "ingest_local", "probe_url", "source_kind_for"]