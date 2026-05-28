"""Delay utilities: sleep + user-agent rotation."""

from __future__ import annotations

import random
import time
from typing import Iterable

DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Mobile/15E148 Safari/604.1",
]


def random_user_agent(user_agents: Iterable[str] | None = None) -> str:
    """Return a random user-agent string."""
    agents = list(user_agents) if user_agents is not None else DEFAULT_USER_AGENTS
    if not agents:
        raise ValueError("user_agents cannot be empty")
    return random.choice(agents)


def build_headers(base_headers: dict[str, str] | None = None) -> dict[str, str]:
    """Build request headers with randomized User-Agent."""
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
        "User-Agent": random_user_agent(),
    }
    if base_headers:
        headers.update(base_headers)
    return headers


def human_sleep(min_seconds: float = 2.0, max_seconds: float = 4.0) -> float:
    """Sleep for a random interval and return the duration."""
    if min_seconds < 0 or max_seconds < 0:
        raise ValueError("Sleep interval cannot be negative")
    if min_seconds > max_seconds:
        raise ValueError("min_seconds cannot be greater than max_seconds")

    duration = random.uniform(min_seconds, max_seconds)
    time.sleep(duration)
    return duration
