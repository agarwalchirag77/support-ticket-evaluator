"""Stage 3: Push evaluation results back to Zendesk custom fields."""

from __future__ import annotations

import asyncio
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.clients.zendesk import ZendeskClient
from src.config import AppConfig
from src.models.evaluation import EvaluationResult
from src.storage.factory import make_database

logger = logging.getLogger(__name__)


def _find_tagger_option(options: list[str], value: str) -> Optional[str]:
    """Find the tagger option tag that ends with _{value} (case-insensitive).

    E.g. options=['qc_metric_1','qc_metric_2','qc_metric_na'], value='4'
    → returns 'qc_metric_4'.  Returns None if no match.
    """
    suffix = f"_{str(value).lower()}"
    if value == "N/A":
        suffix = "_na"
    for opt in options:
        if opt.endswith(suffix):
            return opt
    return None


class Publisher:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._db = make_database(config)
        self._zendesk = ZendeskClient(config)
        # Cache: field_id (int) -> list of tagger option strings (empty = not a tagger)
        self._tagger_options: dict[int, list[str]] = {}

    async def _load_tagger_options(self) -> None:
        """Pre-fetch tagger options for all configured write-back fields (once per run)."""
        if self._tagger_options:
            return  # already loaded

        wb = self._config.zendesk_write_back
        cf = wb.custom_fields

        all_field_ids: list[int] = []
        for fid in [cf.aggregate_score, cf.evaluation_date, cf.evaluator_confidence,
                    cf.prompt_version, cf.frt_status, cf.ttr_status, cf.llm_provider]:
            if fid:
                all_field_ids.append(int(fid))
        for fid in wb.metric_fields.values():
            if fid:
                all_field_ids.append(int(fid))

        results = await asyncio.gather(
            *[self._zendesk.fetch_tagger_options(fid) for fid in all_field_ids],
            return_exceptions=True,
        )
        for fid, options in zip(all_field_ids, results):
            if isinstance(options, Exception):
                logger.warning("Could not fetch options for field %d: %s", fid, options)
                self._tagger_options[fid] = []
            else:
                self._tagger_options[fid] = options
                if options:
                    logger.debug("Field %d is tagger with %d options", fid, len(options))

    async def publish_all(
        self,
        results: list[EvaluationResult],
        dry_run: bool = False,
    ) -> tuple[int, int]:
        """Publish results to Zendesk. Returns (success_count, error_count).

        When dry_run=True, prints what would be published without making API calls.
        """
        wb = self._config.zendesk_write_back
        if not wb.enabled and not dry_run:
            logger.info("Zendesk write-back disabled — skipping publish")
            return 0, 0

        await self._load_tagger_options()

        if dry_run:
            logger.info("[DRY RUN] Would publish %d evaluation(s) to Zendesk", len(results))
            for r in results:
                fields = self._build_custom_fields(r)
                score = r.aggregate_score.numeric if r.aggregate_score else "?"
                logger.info(
                    "[DRY RUN] Ticket %s — score=%s band=%s fields=%d",
                    r.ticket_id, score, r.aggregate_score.band if r.aggregate_score else "?", len(fields),
                )
                for f in fields:
                    logger.info("[DRY RUN]   field_id=%s value=%s", f["id"], f["value"])
            return len(results), 0

        published = 0
        errors = 0
        sem = asyncio.Semaphore(3)

        tasks = [self._publish_one(result, sem) for result in results]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        for result, outcome in zip(results, outcomes):
            if isinstance(outcome, Exception):
                logger.error("Failed to publish ticket %s: %s", result.ticket_id, outcome)
                errors += 1
            elif outcome:
                published += 1

        # Export CSVs
        if self._config.output.export_csv:
            self._export_csv(results)

        return published, errors

    async def _publish_one(
        self, result: EvaluationResult, sem: asyncio.Semaphore
    ) -> bool:
        """Push evaluation scores to Zendesk custom fields."""
        async with sem:
            fields = self._build_custom_fields(result)
            if not fields:
                logger.debug("No configured custom field IDs for ticket %s — skipping", result.ticket_id)
                return False

            try:
                await self._zendesk.update_custom_fields(int(result.ticket_id), fields)
                now = datetime.now(timezone.utc).isoformat()
                # Update DB: mark published
                # (We look up by ticket_id and prompt_version)
                conn_rows = self._db.get_unpublished_evaluations()
                for row in conn_rows:
                    if str(row["ticket_id"]) == result.ticket_id:
                        self._db.mark_published(row["id"], now)
                        break
                return True
            except Exception as exc:
                logger.error("Zendesk update failed for ticket %s: %s", result.ticket_id, exc)
                raise

    def _build_custom_fields(self, result: EvaluationResult) -> list[dict]:
        """Build Zendesk custom_fields payload from evaluation result."""
        cf = self._config.zendesk_write_back.custom_fields
        mf = self._config.zendesk_write_back.metric_fields

        sla = result.sla_status
        frt_status = sla.first_response_time.status if sla else None
        ttr_status = sla.resolution_time.status if sla else None
        score = result.aggregate_score

        field_map = {
            cf.aggregate_score: str(round(score.numeric, 2)) if score else None,
            cf.evaluation_date: result.evaluation_date[:10] if result.evaluation_date else None,
            cf.evaluator_confidence: result.evaluator_confidence,
            cf.prompt_version: result.prompt_version,
            cf.frt_status: frt_status,
            cf.ttr_status: ttr_status,
            cf.llm_provider: result.llm_model,
        }

        fields = []
        for field_id, value in field_map.items():
            if field_id and value is not None:
                fid_int = int(field_id)
                options = self._tagger_options.get(fid_int, [])
                resolved = _find_tagger_option(options, str(value)) if options else value
                if resolved is not None:
                    fields.append({"id": fid_int, "value": resolved})

        # Per-metric fields
        for metric in result.metrics:
            field_id = mf.get(metric.metric_id)
            if field_id:
                fid_int = int(field_id)
                options = self._tagger_options.get(fid_int, [])
                if metric.rating == "N/A":
                    rating_val = _find_tagger_option(options, "N/A") if options else None
                else:
                    rating_val = _find_tagger_option(options, str(metric.rating)) if options else metric.rating
                if rating_val is not None:
                    fields.append({"id": fid_int, "value": rating_val})

        return fields

    def export_csv_for_range(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        fmt: str = "both",
    ) -> int:
        """Export evaluations for a date range to CSV without touching Zendesk.

        Args:
            from_date: Filter tickets closed on/after this date (YYYY-MM-DD).
            to_date: Filter tickets closed on/before this date (YYYY-MM-DD).
            fmt: "wide", "long", or "both".

        Returns the number of evaluations exported.
        """
        from src.models.evaluation import EvaluationResult as _ER
        rows = self._db.get_evaluations_in_range(from_date, to_date)
        results = []
        for row in rows:
            path = row["eval_json_path"]
            if path and Path(path).exists():
                try:
                    results.append(_ER.model_validate_json(Path(path).read_text()))
                except Exception as exc:
                    logger.warning("Skipping unreadable eval %s: %s", path, exc)

        if not results:
            logger.info("No evaluations found for the given range")
            return 0

        exports_dir = Path(self._config.output.exports_dir)
        exports_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"{from_date or 'all'}_to_{to_date or 'now'}"

        if fmt in ("wide", "both"):
            self._export_wide_csv(results, exports_dir / f"{prefix}_evaluations_wide.csv")
        if fmt in ("long", "both"):
            self._export_long_csv(results, exports_dir / f"{prefix}_evaluations_long.csv")

        return len(results)

    # ------------------------------------------------------------------
    # CSV export
    # ------------------------------------------------------------------

    def _export_csv(self, results: list[EvaluationResult]) -> None:
        if not results:
            return

        exports_dir = Path(self._config.output.exports_dir)
        exports_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        self._export_wide_csv(results, exports_dir / f"{date_str}_evaluations_wide.csv")
        self._export_long_csv(results, exports_dir / f"{date_str}_evaluations_long.csv")

    def _export_wide_csv(self, results: list[EvaluationResult], path: Path) -> None:
        """One row per ticket; metrics as columns in canonical METRIC_1..18 order."""
        METRIC_IDS = [f"METRIC_{i}" for i in range(1, 20)]

        fieldnames = [
            "ticket_id", "evaluation_date", "agent_name", "prompt_version",
            "llm_model", "aggregate_score", "performance_band",
            "evaluator_confidence", "frt_status", "frt_minutes",
            "ttr_status", "ttr_minutes", "flags",
        ]
        for mid in METRIC_IDS:
            fieldnames += [f"{mid}_rating", f"{mid}_label"]

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in results:
                sla = r.sla_status
                frt = sla.first_response_time if sla else None
                ttr = sla.resolution_time if sla else None

                row: dict = {
                    "ticket_id": r.ticket_id,
                    "evaluation_date": r.evaluation_date,
                    "agent_name": r.agent_name,
                    "prompt_version": r.prompt_version or "",
                    "llm_model": r.llm_model or "",
                    "aggregate_score": r.aggregate_score.numeric if r.aggregate_score else "",
                    "performance_band": r.aggregate_score.band if r.aggregate_score else "",
                    "evaluator_confidence": r.evaluator_confidence,
                    "frt_status": frt.status if frt else "",
                    "frt_minutes": frt.value_minutes if frt else "",
                    "ttr_status": ttr.status if ttr else "",
                    "ttr_minutes": ttr.value_minutes if ttr else "",
                    "flags": "; ".join(r.flags),
                }

                metric_by_id = {m.metric_id: m for m in r.metrics}
                for mid in METRIC_IDS:
                    m = metric_by_id.get(mid)
                    row[f"{mid}_rating"] = m.rating if m else ""
                    row[f"{mid}_label"] = m.rating_label if m else ""

                writer.writerow(row)

        logger.info("Exported wide CSV → %s (%d rows)", path, len(results))

    def _export_long_csv(self, results: list[EvaluationResult], path: Path) -> None:
        """One row per metric per ticket."""
        fieldnames = [
            "ticket_id", "evaluation_date", "agent_name", "prompt_version",
            "llm_model", "aggregate_score", "performance_band",
            "evaluator_confidence", "frt_status", "ttr_status",
            "metric_id", "metric_name", "rating", "rating_label",
            "evidence", "reasoning", "improvement_note",
        ]

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in results:
                sla = r.sla_status
                frt = sla.first_response_time if sla else None
                ttr = sla.resolution_time if sla else None
                base = {
                    "ticket_id": r.ticket_id,
                    "evaluation_date": r.evaluation_date,
                    "agent_name": r.agent_name,
                    "prompt_version": r.prompt_version or "",
                    "llm_model": r.llm_model or "",
                    "aggregate_score": r.aggregate_score.numeric if r.aggregate_score else "",
                    "performance_band": r.aggregate_score.band if r.aggregate_score else "",
                    "evaluator_confidence": r.evaluator_confidence,
                    "frt_status": frt.status if frt else "",
                    "ttr_status": ttr.status if ttr else "",
                }
                for m in r.metrics:
                    row = {
                        **base,
                        "metric_id": m.metric_id,
                        "metric_name": m.metric_name,
                        "rating": m.rating,
                        "rating_label": m.rating_label,
                        "evidence": m.evidence,
                        "reasoning": m.reasoning,
                        "improvement_note": m.improvement_note,
                    }
                    writer.writerow(row)

        logger.info("Exported long CSV → %s", path)
