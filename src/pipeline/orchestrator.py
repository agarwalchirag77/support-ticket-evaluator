"""Pipeline orchestrator: coordinates Fetch → Evaluate → Publish stages."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.config import AppConfig
from src.pipeline.evaluator import Evaluator
from src.pipeline.fetcher import Fetcher
from src.pipeline.publisher import Publisher
from src.storage.factory import make_database
from src.storage.file_store import FileStore
from src.storage.state import RunState
from src.utils.exclusions import exclusion_reason
from src.utils.notifier import Notifier, RunSummary

logger = logging.getLogger(__name__)


@dataclass
class PipelineStats:
    mode: str
    fetched: int = 0
    evaluated: int = 0
    published: int = 0
    errors: int = 0
    excluded: int = 0  # tickets skipped by QC exclusion rules (not errors)
    error_details: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "fetched": self.fetched,
            "evaluated": self.evaluated,
            "published": self.published,
            "errors": self.errors,
            "excluded": self.excluded,
        }


class Orchestrator:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._db = make_database(config)
        self._state = RunState(config, self._db)
        self._file_store = FileStore(config)
        self._fetcher = Fetcher(config)
        self._evaluator = Evaluator(config)
        self._publisher = Publisher(config)
        self._notifier = Notifier(config.notifications)
        self._run_window_from: Optional[str] = None

    async def run(
        self,
        force: bool = False,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> PipelineStats:
        """Run the full pipeline.

        Without dates: incremental fetch using cursor (normal daily run).
        With from_date: backfill mode — fetches from that date, does NOT advance cursor.
        """
        is_backfill = bool(from_date)
        mode = f"backfill:{from_date}" if is_backfill else "incremental"
        # Window "from" for the run log: the from_date for a backfill, else where the last
        # run left off (prior last_ticket_updated_at, or the initial fetch date on first run).
        self._run_window_from = from_date if is_backfill else (
            self._state._data.get("last_ticket_updated_at") or self._config.state.initial_fetch_from)
        stats = PipelineStats(mode=mode)
        run_id = self._db.start_run(
            started_at=stats.started_at.isoformat(),
            mode=mode,
            cursor_used=None if is_backfill else self._state.zendesk_cursor,
        )
        if not is_backfill:
            self._state.mark_run_started()

        try:
            # Stage 1: Fetch
            logger.info("=== Stage 1: Fetching tickets ===")
            ticket_data_list = await self._fetcher.fetch_all(
                self._state,
                force=force,
                from_date=from_date,
                to_date=to_date,
                update_cursor=not is_backfill,
            )
            # Defense-in-depth: fetch already filters exclusions, but this
            # also covers tickets fetched before the rules existed.
            ticket_data_list = self._filter_excluded(ticket_data_list, stats)
            stats.fetched = len(ticket_data_list)
            logger.info("Fetched %d tickets (%d excluded)", stats.fetched, stats.excluded)

            if not ticket_data_list:
                logger.info("No new tickets to evaluate")
                await self._finish(stats, run_id)
                return stats

            # Stage 2: Evaluate
            logger.info("=== Stage 2: Evaluating %d tickets ===", stats.fetched)
            results, skipped = await self._evaluator.evaluate_all(ticket_data_list, force=force)
            stats.evaluated = len(results)
            # fetched = freshly-evaluated + already-done (skipped) + failures.
            stats.errors += stats.fetched - stats.evaluated - skipped
            logger.info("Evaluated %d/%d tickets (%d already done, skipped)",
                        stats.evaluated, stats.fetched, skipped)

            # Stage 3: Publish
            logger.info("=== Stage 3: Publishing %d results ===", stats.evaluated)
            published, pub_errors = await self._publisher.publish_all(results)
            stats.published = published
            stats.errors += pub_errors
            logger.info("Published %d/%d results", published, stats.evaluated)

            await self._finish(stats, run_id)
            return stats

        except Exception as exc:
            logger.exception("Pipeline encountered a fatal error: %s", exc)
            stats.errors += 1
            stats.error_details.append(str(exc))
            await self._notify_failure(stats, str(exc))
            await self._finish(stats, run_id)
            raise

    async def run_fetch_only(self) -> PipelineStats:
        """Fetch tickets only, no evaluation or publish."""
        stats = PipelineStats(mode="fetch-only")
        run_id = self._db.start_run(
            stats.started_at.isoformat(), "fetch-only", self._state.zendesk_cursor
        )
        self._state.mark_run_started()
        try:
            ticket_data_list = await self._fetcher.fetch_all(self._state)
            stats.fetched = len(ticket_data_list)
            logger.info("Fetched %d tickets (fetch-only mode)", stats.fetched)
            await self._finish(stats, run_id)
        except Exception as exc:
            logger.exception("Fetch-only failed: %s", exc)
            stats.errors += 1
            await self._notify_failure(stats, str(exc))
            await self._finish(stats, run_id)
            raise
        return stats

    def _filter_excluded(
        self, ticket_data_list: list[dict], stats: PipelineStats
    ) -> list[dict]:
        """Drop tickets matching QC exclusion rules; count them in stats.excluded.

        Excluded tickets are NOT errors — they are intentionally out of QC scope
        (duplicates, alerts, spam, side-conversation reviews, unassigned/bot).
        """
        excl = self._config.zendesk.exclusions
        kept: list[dict] = []
        for ticket_data in ticket_data_list:
            ticket = (ticket_data.get("Ticket_Metadata") or {}).get("ticket") or {}
            reason = exclusion_reason(ticket, excl)
            if reason:
                logger.info(
                    "Ticket %s excluded from QC (%s) — skipping evaluation",
                    ticket.get("id"), reason,
                )
                stats.excluded += 1
            else:
                kept.append(ticket_data)
        return kept

    async def re_evaluate(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        ticket_ids: Optional[list[int]] = None,
        force_fetch: bool = False,
    ) -> PipelineStats:
        """Re-evaluate tickets using current prompt version.

        Uses existing ticket JSONs from disk; no Zendesk re-fetch unless force_fetch=True.
        Marks old evaluations as is_latest=0 before inserting new ones.
        """
        stats = PipelineStats(mode="re-evaluate")
        run_id = self._db.start_run(stats.started_at.isoformat(), "re-evaluate")

        # Collect ticket paths to re-evaluate
        if ticket_ids:
            # Load specific tickets
            ticket_data_list = []
            for tid in ticket_ids:
                data = self._file_store.load_ticket(tid)
                if data:
                    ticket_data_list.append(data)
                else:
                    logger.warning("Ticket %s not found on disk — skipping", tid)
                    stats.errors += 1
        else:
            # Load from date range
            ticket_data_list = []
            for path in self._file_store.iter_ticket_paths(from_date, to_date):
                import json
                ticket_data_list.append(json.loads(path.read_text()))

        # Skip tickets matching QC exclusion rules (duplicate/alert/spam/etc.)
        ticket_data_list = self._filter_excluded(ticket_data_list, stats)
        stats.fetched = len(ticket_data_list)
        logger.info("Re-evaluating %d tickets (%d excluded)", stats.fetched, stats.excluded)

        results, skipped = await self._evaluator.evaluate_all(ticket_data_list, force=True)
        stats.evaluated = len(results)
        stats.errors += stats.fetched - stats.evaluated - skipped

        published, pub_errors = await self._publisher.publish_all(results)
        stats.published = published
        stats.errors += pub_errors

        await self._finish(stats, run_id)
        return stats

    async def purge_excluded(
        self,
        execute: bool = False,
        clear_zendesk: bool = True,
        ticket_ids: Optional[list[int]] = None,
    ):
        """Purge QC-excluded tickets from local data (+ clear Zendesk QC fields)."""
        from src.pipeline.purger import Purger
        return await Purger(self._config).purge(
            execute=execute, clear_zendesk=clear_zendesk, ticket_ids=ticket_ids
        )

    async def publish_unpublished(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        dry_run: bool = False,
    ) -> PipelineStats:
        """Re-publish evaluations that failed to push to Zendesk.

        from_date/to_date filter by ticket closed_at.
        dry_run=True logs what would be published without making API calls.
        """
        stats = PipelineStats(mode="publish-unpublished")
        run_id = self._db.start_run(stats.started_at.isoformat(), "publish-unpublished")

        rows = self._db.get_unpublished_evaluations(from_date=from_date, to_date=to_date)
        results = []
        for row in rows:
            eval_path = row["eval_json_path"]
            if eval_path and Path(eval_path).exists():
                import json
                try:
                    result = __import__("src.models.evaluation", fromlist=["EvaluationResult"]).EvaluationResult.model_validate_json(
                        Path(eval_path).read_text()
                    )
                    results.append(result)
                except Exception as exc:
                    logger.error("Could not load eval %s: %s", eval_path, exc)

        published, errors = await self._publisher.publish_all(results, dry_run=dry_run)
        stats.evaluated = len(results)
        stats.published = published
        stats.errors = errors
        await self._finish(stats, run_id)
        return stats

    def get_status(self) -> dict:
        """Return pipeline health summary for the status command."""
        import json as _json
        stats = self._db.get_summary_stats()
        state_data = self._state._data
        return {
            "last_run_at": state_data.get("last_run_at"),
            "last_successful_run_at": state_data.get("last_successful_run_at"),
            "last_ticket_updated_at": state_data.get("last_ticket_updated_at"),
            "cursor_set": bool(state_data.get("zendesk_cursor")),
            "last_run_stats": state_data.get("last_run_stats", {}),
            **stats,
        }

    def get_audit(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> list:
        """Return per-ticket audit rows for the audit command."""
        return self._db.get_audit_data(from_date=from_date, to_date=to_date)

    def export_csv(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        fmt: str = "both",
    ) -> int:
        """Export evaluations to CSV for the export command."""
        return self._publisher.export_csv_for_range(from_date=from_date, to_date=to_date, fmt=fmt)

    # ------------------------------------------------------------------

    async def _finish(self, stats: PipelineStats, run_id: int) -> None:
        import socket
        completed = datetime.now(timezone.utc).isoformat()
        # Run window: cursor position at start → latest ticket timestamp after fetch
        # (records "what date to what date" the run covered).
        window_from = getattr(self, "_run_window_from", None)
        window_to = self._state._data.get("last_ticket_updated_at")
        self._db.complete_run(
            run_id, completed,
            stats.fetched, stats.evaluated, stats.published, stats.errors,
            excluded=stats.excluded,
            window_from=window_from,
            window_to=window_to,
            error_details=("; ".join(stats.error_details)[:4000] or None) if stats.error_details else None,
            host=socket.gethostname(),
        )
        self._state.mark_run_complete(stats.to_dict())
        summary = RunSummary(
            mode=stats.mode,
            fetched=stats.fetched,
            evaluated=stats.evaluated,
            published=stats.published,
            errors=stats.errors,
            error_details=stats.error_details,
        )
        summary.started_at = stats.started_at
        await self._notifier.send_summary(summary)
        logger.info("Run complete: %s", stats.to_dict())

    async def _notify_failure(self, stats: PipelineStats, message: str) -> None:
        await self._notifier.send_fatal(f"[{stats.mode}] {message}")
