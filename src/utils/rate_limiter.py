"""Async token-bucket rate limiter."""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class RateLimiter:
    """Simple async rate limiter using a token-bucket algorithm.

    Usage:
        limiter = RateLimiter(requests_per_minute=400)
        async with limiter:
            await make_request()
    """

    def __init__(self, requests_per_minute: int) -> None:
        self._rpm = requests_per_minute
        self._interval = 60.0 / max(requests_per_minute, 1)
        self._lock = asyncio.Lock()
        self._last_call: float = 0.0

    async def __aenter__(self) -> "RateLimiter":
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self._interval:
                wait = self._interval - elapsed
                logger.debug("Rate limiter sleeping %.3fs", wait)
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
        return self

    async def __aexit__(self, *_) -> None:
        pass

    async def acquire(self) -> None:
        async with self:
            pass
