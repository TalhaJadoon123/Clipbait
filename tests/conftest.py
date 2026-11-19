"""Shared test setup: isolate every run in a throwaway data directory."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Must happen before any clipbait import: Settings is cached on first read.
_DATA_DIR = Path(tempfile.mkdtemp(prefix="clipbait-tests-"))
os.environ.setdefault("CLIPBAIT_DATA_DIR", str(_DATA_DIR))
os.environ.setdefault("CLIPBAIT_LLM_PROVIDER", "none")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("GEMINI_API_KEY", "")

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    """Create the SQLite schema once per run.

    The API creates it on startup; tests that touch the database directly need
    it too, and it must exist before the first insert.
    """
    from clipbait import db

    db.init_db()