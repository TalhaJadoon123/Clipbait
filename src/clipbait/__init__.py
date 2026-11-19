"""Package marker for Clipbait."""

from __future__ import annotations

# Load `.env` once, before any submodule reads the environment. Existing
# environment variables win, so CI and containers behave predictably.
try:  # pragma: no cover - trivial import guard
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

__version__ = "0.1.0"
__all__ = ["__version__"]