"""Anthropic Claude async client for ticket evaluation."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Optional

import anthropic

from src.config import LLMProviderConfig, LLMRateLimit
from src.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

def _backoff(attempt: int, base: float = 2.0, cap: float = 60.0) -> float:
    import random
    delay = min(base * (2 ** (attempt - 1)), cap)
    return delay + random.uniform(0, delay * 0.1)


_JSON_RETRY_SUFFIX = (
    "\n\nIMPORTANT: Your previous response contained invalid JSON. "
    "Return ONLY a valid JSON object — no markdown, no explanation, no code fences. "
    "Start your response with { and end with }."
)


class ClaudeClient:
    def __init__(self, provider_cfg: LLMProviderConfig, rate_limit: LLMRateLimit) -> None:
        self._cfg = provider_cfg
        self._client = anthropic.AsyncAnthropic(api_key=provider_cfg.api_key)
        self._limiter = RateLimiter(rate_limit.requests_per_minute)

    async def evaluate(
        self,
        system_prompt: str,
        ticket_json: str,
        max_retries: int = 3,
    ) -> dict:
        """Send ticket to Claude for evaluation; return parsed JSON result."""
        user_message = (
            "Please evaluate the following support ticket according to the instructions above.\n\n"
            f"TICKET DATA:\n```json\n{ticket_json}\n```"
        )

        for attempt in range(1, max_retries + 1):
            suffix = _JSON_RETRY_SUFFIX if attempt > 1 else ""
            await self._limiter.acquire()
            try:
                response = await self._client.messages.create(
                    model=self._cfg.model,
                    max_tokens=self._cfg.max_tokens,
                    temperature=self._cfg.temperature,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_message + suffix}],
                )
                text = response.content[0].text.strip()
                return self._parse_json(text)

            except anthropic.RateLimitError as exc:
                wait = _backoff(attempt, base=5.0)
                logger.warning("Claude rate limit (attempt %d/%d) — sleeping %.1fs", attempt, max_retries, wait)
                if attempt == max_retries:
                    raise
                await asyncio.sleep(wait)

            except anthropic.BadRequestError as exc:
                if "too many tokens" in str(exc).lower() or "context_length" in str(exc).lower():
                    raise _TokenLimitError(str(exc)) from exc
                raise

            except ValueError as exc:
                # JSON parse error
                if attempt == max_retries:
                    raise
                logger.warning("JSON parse error on attempt %d: %s", attempt, exc)
                await asyncio.sleep(1.0)

        raise RuntimeError("Claude evaluation failed after all retries")

    @staticmethod
    def _parse_json(text: str) -> dict:
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if lines[-1] == "```" else "\n".join(lines[1:])
        text = text.strip()
        if not text.startswith("{"):
            # Try to find JSON object in the text
            start = text.find("{")
            if start != -1:
                text = text[start:]
        return json.loads(text)


class _TokenLimitError(Exception):
    pass
