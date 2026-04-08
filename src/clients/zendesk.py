"""Async Zendesk API client with rate limiting and retry logic."""

from __future__ import annotations

import base64
import logging
from typing import AsyncIterator, Optional

import httpx

from src.config import AppConfig
from src.utils.rate_limiter import RateLimiter
from src.utils.retry import with_retry

logger = logging.getLogger(__name__)


class ZendeskClient:
    """Async Zendesk REST API client.

    Provides:
    - Incremental ticket cursor export (for fetching new/updated tickets)
    - Ticket metrics fetch
    - Ticket comments fetch
    - Ticket custom field update (write-back)
    """

    def __init__(self, config: AppConfig) -> None:
        zd = config.zendesk
        self._base_url = f"https://{zd.subdomain}.zendesk.com/api/v2"
        credentials = f"{zd.email}/token:{zd.api_token}"
        token = base64.b64encode(credentials.encode()).decode()
        self._headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # Two separate rate limiters: incremental export is strictly rate-limited
        self._regular_limiter = RateLimiter(zd.rate_limit.regular_requests_per_minute)
        self._export_limiter = RateLimiter(zd.rate_limit.export_requests_per_minute)
        self._group_ids = set(str(g.id) for g in zd.groups)
        self._ticket_status = zd.ticket_status

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, timeout=30.0)

    # ------------------------------------------------------------------
    # Incremental ticket export
    # ------------------------------------------------------------------

    async def fetch_tickets_since(
        self,
        cursor: Optional[str] = None,
        start_time_unix: Optional[int] = None,
    ) -> AsyncIterator[dict]:
        """Yield closed tickets from configured groups since the given cursor/time.

        Uses Zendesk's cursor-based Incremental Tickets Export API.
        Filters client-side by group_id and status.

        Args:
            cursor: Zendesk after_cursor from previous run. Takes precedence.
            start_time_unix: Unix timestamp for initial fetch (first run only).
        """
        if cursor:
            url = f"{self._base_url}/incremental/tickets/cursor.json"
            params: dict = {"cursor": cursor}
            logger.info(f"Using cursor: {cursor[:20]}...")
        elif start_time_unix:
            url = f"{self._base_url}/incremental/tickets/cursor.json"
            params = {"start_time": start_time_unix}
            logger.info(f"Using start_time: {start_time_unix}")
        else:
            raise ValueError("Either cursor or start_time_unix must be provided")

        async with self._client() as client:
            iteration = 0
            while url:
                iteration += 1
                logger.info(f"Pagination iteration {iteration}: params={params}")
                await self._export_limiter.acquire()
                
                async def _fetch(u=url, p=params.copy()):
                    # Build request to log the actual URL being sent
                    import copy
                    params_copy = copy.deepcopy(p)
                    resp = await client.get(u, params=params_copy)
                    if resp.status_code >= 400:
                        logger.error(
                            "Zendesk cursor API returned %d: %s\nRequest: GET %s with params=%s",
                            resp.status_code, resp.text[:500], u, params_copy
                        )
                    resp.raise_for_status()
                    return resp.json()

                data = await with_retry(
                    _fetch,
                    max_attempts=4,
                    base_delay=2.0,
                )

                tickets = data.get("tickets", [])
                for ticket in tickets:
                    # Filter by group and status
                    group_id = str(ticket.get("group_id", ""))
                    status = ticket.get("status", "")
                    if group_id not in self._group_ids:
                        continue
                    if status != self._ticket_status:
                        continue
                    yield ticket

                # Update cursor for next page
                after_cursor = data.get("after_cursor")
                after_url = data.get("after_url")
                end_of_stream = data.get("end_of_stream", False)

                logger.info(f"Response: after_cursor={after_cursor}, after_url={after_url[:60] if after_url else None}..., end_of_stream={end_of_stream}")

                if end_of_stream or not after_cursor:
                    # Save the final cursor so next run resumes from here
                    if after_cursor:
                        self._last_cursor = after_cursor
                    break

                # Next page: use after_cursor
                url = f"{self._base_url}/incremental/tickets/cursor.json"  # Reset to base URL
                params = {"cursor": after_cursor}  # Use cursor parameter
                self._last_cursor = after_cursor

    @property
    def last_cursor(self) -> Optional[str]:
        return getattr(self, "_last_cursor", None)

    # ------------------------------------------------------------------
    # Ticket metrics
    # ------------------------------------------------------------------

    async def fetch_metrics(self, ticket_id: int) -> dict:
        """Fetch full ticket_metric object including assigned_at, solved_at, etc."""
        url = f"{self._base_url}/tickets/{ticket_id}/metrics"
        async with self._client() as client:
            await self._regular_limiter.acquire()

            async def _fetch():
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()

            data = await with_retry(_fetch, max_attempts=4, base_delay=1.0)
        return data.get("ticket_metric", {})

    # ------------------------------------------------------------------
    # Ticket comments
    # ------------------------------------------------------------------

    async def fetch_comments(self, ticket_id: int) -> list[dict]:
        """Fetch all comments for a ticket."""
        url = f"{self._base_url}/tickets/{ticket_id}/comments"
        async with self._client() as client:
            await self._regular_limiter.acquire()

            async def _fetch():
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()

            data = await with_retry(_fetch, max_attempts=4, base_delay=1.0)
        return data.get("comments", [])

    # ------------------------------------------------------------------
    # Write-back
    # ------------------------------------------------------------------

    async def update_custom_fields(
        self, ticket_id: int, custom_fields: list[dict]
    ) -> None:
        """Update ticket custom fields.

        Args:
            ticket_id: Zendesk ticket ID.
            custom_fields: List of {"id": field_id, "value": value} dicts.
        """
        if not custom_fields:
            return

        url = f"{self._base_url}/tickets/{ticket_id}.json"
        payload = {"ticket": {"custom_fields": custom_fields}}

        async with self._client() as client:
            await self._regular_limiter.acquire()

            async def _put():
                resp = await client.put(url, json=payload)
                if resp.status_code == 422:
                    logger.error(
                        "Zendesk rejected field update for ticket %s: %s",
                        ticket_id, resp.text[:200],
                    )
                    return  # Don't retry validation errors
                resp.raise_for_status()

            await with_retry(_put, max_attempts=3, base_delay=1.0)

        logger.debug("Updated %d custom field(s) on ticket %s", len(custom_fields), ticket_id)
