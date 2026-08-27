"""Stage 1: Fetch tickets from Zendesk and write to file store.

Fetches new/updated closed tickets from configured groups using the Zendesk
incremental cursor API, then enriches each with full metrics + comments.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from src.clients.zendesk import ZendeskClient
from src.config import AppConfig
from src.storage.factory import make_database
from src.storage.file_store import FileStore
from src.storage.state import RunState

logger = logging.getLogger(__name__)


class Fetcher:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._zendesk = ZendeskClient(config)
        self._file_store = FileStore(config)
        self._db = make_database(config)

    async def fetch_all(
        self,
        state: RunState,
        force: bool = False,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        update_cursor: bool = True,
    ) -> list[dict]:
        """Fetch closed tickets from Zendesk.

        When from_date is provided, bypasses the cursor and fetches from that
        date forward (backfill mode). update_cursor should be False in that case
        so the incremental cursor is not overwritten.

        Returns a list of composite dicts (Ticket_Metadata, Ticket_Metrics,
        Ticket_Comments) ready for the evaluation stage.
        """
        if from_date:
            # Backfill mode: use the from_date as start time, ignore cursor
            from datetime import datetime as _dt
            dt = _dt.fromisoformat(from_date)
            start_time = int(dt.timestamp())
            cursor = None
            logger.info("Backfill mode: fetching from %s (unix=%d)", from_date, start_time)
        else:
            cursor = state.zendesk_cursor
            start_time = state.initial_fetch_unix if cursor is None else None
            logger.info(
                "Fetching tickets: %s",
                f"cursor={cursor}" if cursor else f"start_time_unix={start_time} (first run)",
            )

        # Collect all ticket stubs from the incremental API
        stubs: list[dict] = []
        async for stub in self._zendesk.fetch_tickets_since(
            cursor=cursor, start_time_unix=start_time
        ):
            stubs.append(stub)

        if not stubs:
            logger.info("No new closed tickets found in configured groups")
            # Advance cursor even if nothing new (only in incremental mode)
            if update_cursor and self._zendesk.last_cursor:
                state.update_cursor(
                    self._zendesk.last_cursor,
                    datetime.now(timezone.utc).isoformat(),
                )
            return []

        logger.info(
            "Found %d new closed ticket(s) — enriching with metrics + comments",
            len(stubs),
        )

        # Enrich in parallel (bounded concurrency)
        sem = asyncio.Semaphore(self._config.pipeline.concurrent_fetches)
        tasks = [self._enrich_ticket(stub, sem, force) for stub in stubs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        enriched: list[dict] = []
        for stub, result in zip(stubs, results):
            tid = stub["id"]
            if isinstance(result, Exception):
                logger.error("Failed to enrich ticket %s: %s", tid, result)
            else:
                enriched.append(result)

        # Apply to_date filter: drop tickets closed after the window end
        if to_date:
            before = len(enriched)
            enriched = [
                t for t in enriched
                if (
                    t.get("Ticket_Metrics", {}).get("ticket_metric", {}).get("solved_at") or
                    t.get("Ticket_Metadata", {}).get("ticket", {}).get("updated_at") or ""
                )[:10] <= to_date
            ]
            dropped = before - len(enriched)
            if dropped:
                logger.info("Filtered out %d ticket(s) closed after %s", dropped, to_date)

        # Advance the cursor after a successful batch (incremental mode only)
        if update_cursor and self._zendesk.last_cursor:
            last_updated = max(
                (t.get("Ticket_Metadata", {}).get("ticket", {}).get("updated_at", "")
                 for t in enriched),
                default=datetime.now(timezone.utc).isoformat(),
            )
            state.update_cursor(self._zendesk.last_cursor, last_updated)

        logger.info("Enriched %d/%d tickets successfully", len(enriched), len(stubs))
        return enriched

    async def _enrich_ticket(
        self, stub: dict, sem: asyncio.Semaphore, force: bool
    ) -> dict:
        """Fetch metrics + comments for a single ticket stub; save to disk + DB."""
        ticket_id = stub["id"]

        async with sem:
            # Return cached data if already on disk and not forced
            if not force:
                existing = self._file_store.load_ticket(ticket_id)
                if existing:
                    logger.debug("Ticket %s already on disk — using cached version", ticket_id)
                    return existing

            # Fetch metrics and comments concurrently
            metrics, comments = await asyncio.gather(
                self._zendesk.fetch_metrics(ticket_id),
                self._zendesk.fetch_comments(ticket_id),
            )

            raw = {
                "Ticket_Metadata": {"ticket": stub},
                "Ticket_Metrics": {"ticket_metric": metrics},
                "Ticket_Comments": {"comments": comments},
            }

            # Use ticket's closed date (solved_at from metrics, fallback to updated_at)
            # so directories are named by when the ticket closed, not when it was fetched
            solved_at = metrics.get("solved_at") or stub.get("updated_at") or ""
            closed_date = solved_at[:10] if solved_at else None

            path = self._file_store.save_ticket(ticket_id, raw, date=closed_date)
            self._db.upsert_ticket(
                ticket_id=ticket_id,
                fetched_at=datetime.now(timezone.utc).isoformat(),
                status=stub.get("status"),
                channel=stub.get("via", {}).get("channel"),
                group_id=stub.get("group_id"),
                group_name=None,
                agent_name=None,
                created_at=stub.get("created_at"),
                closed_at=stub.get("updated_at"),
                json_path=str(path),
            )
            logger.debug("Enriched and saved ticket %s", ticket_id)
            return raw
