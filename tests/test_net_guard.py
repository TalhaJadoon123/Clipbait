"""SSRF guard: user-supplied URLs must not reach private networks."""

from __future__ import annotations

import pytest

from clipbait.net_guard import UnsafeUrl, ensure_safe_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost:8000/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://0.0.0.0/",
    ],
)
def test_private_and_metadata_addresses_are_blocked(url: str) -> None:
    with pytest.raises(UnsafeUrl):
        ensure_safe_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x"])
def test_unsupported_schemes_are_blocked(url: str) -> None:
    with pytest.raises(UnsafeUrl):
        ensure_safe_url(url)


def test_opt_in_allows_private_urls_for_offline_deployments() -> None:
    assert ensure_safe_url("http://127.0.0.1/video.mp4", allow_private=True)


def test_demo_scheme_is_rejected_by_the_guard_but_handled_upstream() -> None:
    with pytest.raises(UnsafeUrl):
        ensure_safe_url("demo://live")