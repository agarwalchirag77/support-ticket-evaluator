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
from src.storage.database import Database
from src.storage.file_store import FileStore
from src.storage.state import RunState
from src.utils.notifier import Notifier, RunSummary

logger = logging.getLogger(__name__)


@dataclass
class PipelineStats:
    mode: str
    fetched: int = 0
    evaluated: int = 0
    published: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "fetched": self.fetched,
            "evaluated": self.evaluated,
            "published": self.published,
            "errors": self.errors,
        }


class Orchestrator:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._db = Database(config.output.database)
        self._state = RunState(config)
        self._file_store = FileStore(config)
        self._fetcher = Fetcher(config)
        self._evaluator = Evaluator(config)
        self._publisher = Publisher(config)
        self._notifier = Notifier(config.notifications)

    async def run(self, force: bool = False) -> PipelineStats:
        """Run the full incremental pipeline: fetch → evaluate → publish."""
        stats = PipelineStats(mode="incremental")
        run_id = self._db.start_run(
            started_at=stats.started_at.isoformat(),
            mode="incremental",
            cursor_used=self._state.zendesk_cursor,
        )
        self._state.mark_run_started()

        try:
            # Stage 1: Fetch
            logger.info("=== Stage 1: Fetching tickets ===")
            ticket_data_list = await self._fetcher.fetch_all(self._state, force=force)
            stats.fetched = len(ticket_data_list)
            logger.info("Fetched %d tickets", stats.fetched)

            if not ticket_data_list:
                logger.info("No new tickets to evaluate")
                await self._finish(stats, run_id)
                return stats

            # Stage 2: Evaluate
            logger.info("=== Stage 2: Evaluating %d tickets ===", stats.fetched)
            results = await self._evaluator.evaluate_all(ticket_data_list, force=force)
            stats.evaluated = len(results)
            stats.errors += stats.fetched - stats.evaluated
            logger.info("Evaluated %d/%d tickets", stats.evaluated, stats.fetched)

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

        stats.fetched = len(ticket_data_list)
        logger.info("Re-evaluating %d tickets", stats.fetched)

        results = await self._evaluator.evaluate_all(ticket_data_list, force=True)
        stats.evaluated = len(results)
        stats.errors += stats.fetched - stats.evaluated

        published, pub_errors = await self._publisher.publish_all(results)
        stats.published = published
        stats.errors += pub_errors

        await self._finish(stats, run_id)
        return stats

    async def publish_unpublished(self) -> PipelineStats:
        """Re-publish all evaluations that failed to push to Zendesk."""
        stats = PipelineStats(mode="publish-unpublished")
        run_id = self._db.start_run(stats.started_at.isoformat(), "publish-unpublished")

        rows = self._db.get_unpublished_evaluations()
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

        published, errors = await self._publisher.publish_all(results)
        stats.evaluated = len(results)
        stats.published = published
        stats.errors = errors
        await self._finish(stats, run_id)
        return stats

    # ------------------------------------------------------------------

    async def _finish(self, stats: PipelineStats, run_id: int) -> None:
        completed = datetime.now(timezone.utc).isoformat()
        self._db.complete_run(
            run_id, completed,
            stats.fetched, stats.evaluated, stats.published, stats.errors,
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
