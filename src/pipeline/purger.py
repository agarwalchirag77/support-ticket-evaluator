"""Purge QC-excluded tickets from local data and clear their Zendesk QC fields.

Removes tickets matching the configured exclusion rules (duplicate / alert /
spam / side-conversation / unassigned / feature-request) from:
  1. Zendesk — clears all configured QC custom fields (only for evaluations
     that were published), so agents' Zendesk-side QC data matches
  2. SQLite DB — tickets, evaluations, metric_results rows
  3. Disk — ticket JSONs and eval JSONs across all date dirs

Order per ticket is Zendesk → DB → files: a failed Zendesk clear skips the
local deletion for that ticket, so the command stays re-runnable.

Default is DRY-RUN; deletion requires execute=True.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.clients.zendesk import ZendeskClient
from src.config import AppConfig
from src.storage.database import Database
from src.storage.file_store import FileStore
from src.utils.exclusions import exclusion_reason

logger = logging.getLogger(__name__)


@dataclass
class PurgeStats:
    scanned: int = 0
    matched: int = 0
    by_category: dict = field(default_factory=dict)
    zendesk_cleared: int = 0
    zendesk_failed: int = 0
    db_tickets_deleted: int = 0
    db_evals_deleted: int = 0
    files_deleted: int = 0
    dry_run: bool = True


class Purger:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._db = Database(config.output.database)
        self._file_store = FileStore(config)
        self._zendesk = ZendeskClient(config)
        self._exports_dir = Path(config.output.exports_dir)

    # ------------------------------------------------------------------

    def _clearable_field_ids(self) -> list[int]:
        """All configured QC write-back field IDs (header + per-metric)."""
        wb = self._config.zendesk_write_back
        cf = wb.custom_fields
        ids = [
            v for v in (
                cf.aggregate_score, cf.evaluation_date, cf.evaluator_confidence,
                cf.prompt_version, cf.frt_status, cf.ttr_status, cf.llm_provider,
            ) if v
        ]
        ids.extend(v for v in wb.metric_fields.values() if v)
        return [int(i) for i in ids]

    def _find_excluded(self, ticket_ids: Optional[list[int]] = None) -> dict[int, str]:
        """Scan all on-disk tickets; return {ticket_id: exclusion reason}."""
        excl = self._config.zendesk.exclusions
        wanted = set(ticket_ids) if ticket_ids else None
        matches: dict[int, str] = {}
        scanned = 0
        for path in self._file_store.iter_ticket_paths():
            import json
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Skipping unreadable ticket JSON %s: %s", path, exc)
                continue
            scanned += 1
            ticket = (data.get("Ticket_Metadata") or {}).get("ticket") or {}
            tid = ticket.get("id")
            if tid is None:
                continue
            tid = int(tid)
            if wanted is not None and tid not in wanted:
                continue
            reason = exclusion_reason(ticket, excl)
            if reason:
                matches[tid] = reason
        self._scanned = scanned
        return matches

    def _backup_db(self) -> Path:
        """Checkpoint WAL and copy the sqlite file aside before deleting."""
        db_path = Path(self._config.output.database)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
        finally:
            conn.close()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = db_path.with_name(f"{db_path.name}.bak-{ts}")
        shutil.copy2(db_path, backup)
        logger.info("DB backed up to %s", backup)
        return backup

    async def _clear_zendesk_fields(self, ticket_id: int, field_ids: list[int]) -> None:
        """Null out all QC custom fields on the Zendesk ticket (strict: 422 raises)."""
        payload = [{"id": fid, "value": None} for fid in field_ids]
        await self._zendesk.update_custom_fields(ticket_id, payload, strict=True)

    # ------------------------------------------------------------------

    async def purge(
        self,
        execute: bool = False,
        clear_zendesk: bool = True,
        ticket_ids: Optional[list[int]] = None,
    ) -> PurgeStats:
        stats = PurgeStats(dry_run=not execute)
        matches = self._find_excluded(ticket_ids)
        stats.scanned = getattr(self, "_scanned", 0)
        stats.matched = len(matches)
        for reason in matches.values():
            cat = reason.split(" (")[0]
            stats.by_category[cat] = stats.by_category.get(cat, 0) + 1

        # --- Report ---
        print(f"\n=== Purge excluded tickets ({'DRY RUN' if not execute else 'EXECUTE'}) ===")
        print(f"  Tickets scanned on disk: {stats.scanned}")
        print(f"  Matching exclusion rules: {stats.matched}")
        for cat, n in sorted(stats.by_category.items(), key=lambda kv: -kv[1]):
            print(f"    {cat}: {n}")
        if not matches:
            print("  Nothing to purge.")
            return stats

        # Which matched tickets have published evaluations (need Zendesk clearing)?
        published_tids = []
        eval_rows_by_tid: dict[int, list] = {}
        for tid in matches:
            rows = self._db.get_ticket_eval_rows(tid)
            eval_rows_by_tid[tid] = rows
            if any(r["published_to_zendesk"] for r in rows):
                published_tids.append(tid)
        print(f"  With published Zendesk QC fields (need clearing): {len(published_tids)}")

        if not execute:
            ids_sorted = sorted(matches)
            print(f"\n  Ticket IDs ({len(ids_sorted)}):")
            print("  " + ",".join(str(t) for t in ids_sorted))
            print("\n  Dry run — nothing deleted. Re-run with --execute to purge.")
            return stats

        # --- Execute ---
        self._backup_db()
        field_ids = self._clearable_field_ids()
        logger.info(
            "Purging %d tickets (%d need Zendesk clearing across %d field IDs)",
            stats.matched, len(published_tids), len(field_ids),
        )

        audit_rows = []
        sem = asyncio.Semaphore(3)

        async def _purge_one(tid: int) -> None:
            reason = matches[tid]
            was_published = tid in published_tids
            cleared = False
            async with sem:
                # 1. Zendesk clear first — failure skips local deletion (re-runnable)
                if was_published and clear_zendesk and field_ids:
                    try:
                        await self._clear_zendesk_fields(tid, field_ids)
                        cleared = True
                        stats.zendesk_cleared += 1
                    except Exception as exc:
                        stats.zendesk_failed += 1
                        logger.error(
                            "Zendesk clear FAILED for ticket %s (%s) — keeping local data for retry",
                            tid, exc,
                        )
                        audit_rows.append([tid, reason, 0, was_published, False, 0, "zendesk_clear_failed"])
                        return
            # 2. DB rows
            counts = self._db.delete_ticket_data(tid)
            stats.db_tickets_deleted += counts["tickets"]
            stats.db_evals_deleted += counts["evaluations"]
            # 3. Files
            deleted_paths = self._file_store.delete_ticket_artifacts(tid)
            stats.files_deleted += len(deleted_paths)
            logger.info(
                "Purged ticket %s (%s): evals=%d files=%d zendesk_cleared=%s",
                tid, reason, counts["evaluations"], len(deleted_paths), cleared,
            )
            audit_rows.append(
                [tid, reason, counts["evaluations"], was_published, cleared, len(deleted_paths), "ok"]
            )

        await asyncio.gather(*(_purge_one(tid) for tid in sorted(matches)))

        # --- Audit CSV ---
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._exports_dir.mkdir(parents=True, exist_ok=True)
        audit_path = self._exports_dir / f"purged_{ts}.csv"
        with open(audit_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ticket_id", "reason", "evals_deleted", "was_published",
                        "zendesk_cleared", "files_deleted", "status"])
            w.writerows(sorted(audit_rows))
        logger.info("Audit CSV written to %s", audit_path)

        print(f"\n  Purged: {stats.db_tickets_deleted} tickets, "
              f"{stats.db_evals_deleted} evaluations, {stats.files_deleted} files")
        print(f"  Zendesk fields cleared: {stats.zendesk_cleared} "
              f"(failed: {stats.zendesk_failed})")
        print(f"  Audit: {audit_path}")
        if stats.zendesk_failed:
            print("  WARNING: some Zendesk clears failed — those tickets were kept; re-run to retry.")
        return stats
