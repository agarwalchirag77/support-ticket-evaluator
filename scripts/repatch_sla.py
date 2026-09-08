#!/usr/bin/env python3
"""Recompute FRT/TTR SLA (+ breach flags, aggregate score/band) on existing evaluations — no LLM.

Reapplies the current SLA config via src/utils/sla.py:patch_sla_and_ratings to the latest
evaluations, using each ticket's stored data. Use it to roll a config SLA change (e.g. the new
severity-based L2 thresholds) onto historical rows without re-running the model.

Only METRIC_8 (FRT), METRIC_10 (TTR) and METRIC_19 (reopen) ratings, the SLA breach flags, and
the aggregate score/band can change — the feedback skill's weighted QC score is unaffected
(those metrics are weight-0). By default only EMAIL tickets are touched (chat SLA is unchanged).

    python scripts/repatch_sla.py                     # dry-run, all email latest evals
    python scripts/repatch_sla.py --from 2026-08-01   # limit by ticket close date
    python scripts/repatch_sla.py --execute           # write the changes (DB + eval blob)
    python scripts/repatch_sla.py --execute --all-channels

Run where the ticket JSON blobs live (the VM), against whatever backend config.yaml selects.
Default is dry-run; nothing is written without --execute.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.models.evaluation import EvaluationResult  # noqa: E402
from src.models.ticket import RawTicket  # noqa: E402
from src.storage.factory import make_database  # noqa: E402
from src.storage.file_store import FileStore  # noqa: E402
from src.utils.sla import patch_sla_and_ratings  # noqa: E402

_CHAT = {"chat", "native_messaging", "chat_transcript"}


def _rowget(row, key):
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _snapshot(result: EvaluationResult):
    """The SLA-visible fields, to detect whether a re-patch changed anything."""
    sla = result.sla_status
    frt = sla.first_response_time if sla else None
    ttr = sla.resolution_time if sla else None
    return {
        "frt_status": frt.status if frt else None,
        "ttr_status": ttr.status if ttr else None,
        "ttr_threshold": ttr.threshold_minutes if ttr else None,
        "band": result.aggregate_score.band if result.aggregate_score else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD", help="Only tickets closed on/after this date.")
    ap.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD", help="Only tickets closed on/before this date.")
    ap.add_argument("--all-channels", action="store_true", help="Also re-patch chat tickets (default: email only).")
    ap.add_argument("--execute", action="store_true", help="Write changes (default: dry-run).")
    ap.add_argument("--limit", type=int, help="Cap number of evaluations processed (for testing).")
    args = ap.parse_args()

    cfg = load_config(args.config)
    db = make_database(cfg)
    fs = FileStore(cfg)

    rows = db.get_latest_evaluations(from_date=args.from_date, to_date=args.to_date)
    total = len(rows)
    processed = changed = skipped_chat = missing = errors = 0
    examples = []

    for row in rows:
        if args.limit and processed >= args.limit:
            break
        channel = (_rowget(row, "channel") or "").lower()
        if channel in _CHAT and not args.all_channels:
            skipped_chat += 1
            continue

        eval_id = _rowget(row, "evaluation_id")
        ticket_id = _rowget(row, "ticket_id")
        eval_path = _rowget(row, "eval_json_path")

        try:
            result = EvaluationResult.model_validate_json(Path(eval_path).read_text())
        except Exception:
            missing += 1
            continue
        ticket_data = fs.load_ticket(int(ticket_id))
        if not ticket_data:
            missing += 1
            continue

        try:
            raw = RawTicket(**ticket_data)
            before = _snapshot(result)
            patch_sla_and_ratings(result, raw.get_metrics(), raw.get_ticket(), cfg.evaluation)
            after = _snapshot(result)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            if len(examples) < 3:
                examples.append((ticket_id, f"ERROR: {type(exc).__name__}: {exc}", ""))
            continue

        processed += 1
        if before != after:
            changed += 1
            if len(examples) < 12:
                examples.append((ticket_id, before, after))
            if args.execute:
                db.update_sla_result(eval_id, result)
                try:
                    Path(eval_path).write_text(result.model_dump_json(indent=2))
                except Exception:
                    pass  # DB is source of truth for reporting; blob rewrite is best-effort

    print(f"backend: {(cfg.storage.backend or 'sqlite')}  | mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print(f"latest evaluations:        {total}")
    print(f"  processed ({'email+chat' if args.all_channels else 'email'}):  {processed}")
    print(f"  changed (FRT/TTR/band):  {changed}")
    print(f"  skipped (chat):          {skipped_chat}")
    print(f"  missing blob/ticket:     {missing}")
    print(f"  errors:                  {errors}")
    if examples:
        print("\n  sample changes (ticket → before / after):")
        for tid, b, a in examples:
            print(f"    #{tid}: {b} -> {a}")
    if not args.execute and changed:
        print("\n  Dry run — re-run with --execute to write these changes (DB + eval blobs).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
