"""Retry helpers with exponential backoff and Retry-After header support."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Callable, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryError(Exception):
    """Raised when all retry attempts are exhausted."""


async def with_retry(
    coro_fn: Callable,
    *,
    max_attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: bool = True,
    retryable_status: tuple[int, ...] = (429, 500, 502, 503, 504),
) -> T:
    """Execute an async callable with exponential backoff.

    The callable should be a no-arg async function (use functools.partial
    or a lambda to bind arguments).

    Respects the ``Retry-After`` header on 429 responses.
    Raises ``RetryError`` after all attempts are exhausted.
    """
    last_exc: Exception = RuntimeError("No attempts made")

    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_fn()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in retryable_status:
                raise

            retry_after = _parse_retry_after(exc.response)
            if retry_after:
                delay = retry_after
            else:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                if jitter:
                    delay += random.uniform(0, delay * 0.2)

            logger.warning(
                "HTTP %s on attempt %d/%d — retrying in %.1fs",
                status, attempt, max_attempts, delay,
            )
            last_exc = exc
            if attempt < max_attempts:
                await asyncio.sleep(delay)

        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            if jitter:
                delay += random.uniform(0, delay * 0.2)
            logger.warning(
                "Network error on attempt %d/%d (%s) — retrying in %.1fs",
                attempt, max_attempts, type(exc).__name__, delay,
            )
            last_exc = exc
            if attempt < max_attempts:
                await asyncio.sleep(delay)

    raise RetryError(f"All {max_attempts} attempts failed") from last_exc


def _parse_retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    return None
