"""Optional proxy handler utilities."""

from __future__ import annotations

import os


def get_proxy_url(explicit_proxy: str | None = None) -> str | None:
    """Resolve proxy URL from explicit value or environment variables."""
    if explicit_proxy:
        return explicit_proxy

    for key in ("SCRAPER_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        value = os.getenv(key)
        if value:
            return value
    return None


def build_requests_proxies(proxy_url: str | None = None) -> dict[str, str] | None:
    """Build requests-compatible proxy mapping."""
    resolved = get_proxy_url(proxy_url)
    if not resolved:
        return None
    return {"http": resolved, "https": resolved}


def build_playwright_proxy(proxy_url: str | None = None) -> dict[str, str] | None:
    """Build Playwright proxy object."""
    resolved = get_proxy_url(proxy_url)
    if not resolved:
        return None
    return {"server": resolved}
