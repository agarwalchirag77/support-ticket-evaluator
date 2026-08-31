#!/usr/bin/env python3
"""Backfill the feedback-narrative columns for evaluations already in the DB.

The narrative half of each evaluation — ``ticket_summary`` and per-metric
``metric_name`` / ``evidence`` / ``reasoning`` / ``improvement_note`` — was added
to the schema after many evaluations were already stored (their DB columns are
NULL). That text still lives in the on-disk eval JSON blobs (``eval_json_path``).
This script reads those blobs and ``UPDATE``s the columns in place.

Backend-agnostic (uses ``make_database``): run it against the local SQLite dev DB,
or the Snowflake VM DB. Idempotent and re-runnable — pass ``--only-missing`` (default)
to touch only rows whose ``ticket_summary`` is still empty.

    python scripts/backfill_eval_text.py                 # latest evals, missing only
    python scripts/backfill_eval_text.py --all           # every latest eval (re-sync)

For a one-time SQLite -> Snowflake move, prefer running the migration
(``migrate_sqlite_to_snowflake.py``), which enriches from the same blobs in a single
bulk pass; use this script for the local DB or to patch rows the migration missed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.models.evaluation import EvaluationResult  # noqa: E402
from src.storage.factory import make_database  # noqa: E402


def extract_eval_text(eval_json_path: str | None) -> tuple[str | None, str | None, dict[str, dict]]:
    """Parse an eval JSON blob → (agent_name, ticket_summary, {metric_id: {name/evidence/reasoning/improvement_note}}).

    Returns (None, None, {}) when the path is missing/unreadable/unparseable, so callers
    can skip that row without failing the whole backfill.
    """
    if not eval_json_path:
        return None, None, {}
    p = Path(eval_json_path)
    if not p.exists():
        return None, None, {}
    try:
        res = EvaluationResult.model_validate_json(p.read_text())
    except Exception:
        return None, None, {}
    per_metric = {
        m.metric_id: {
            "metric_name": m.metric_name,
            "evidence": m.evidence,
            "reasoning": m.reasoning,
            "improvement_note": m.improvement_note,
        }
        for m in res.metrics
    }
    return res.agent_name, res.ticket_summary, per_metric


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--all", action="store_true",
                    help="Re-sync every latest evaluation (default: only rows with an empty ticket_summary).")
    args = ap.parse_args()

    cfg = load_config(args.config)
    db = make_database(cfg)
    backend = (cfg.storage.backend or "sqlite").lower()

    # Pull the (eval_id, eval_json_path, agent_name, ticket_summary) list for the latest evaluations.
    if backend == "snowflake":
        rows = db._query(  # noqa: SLF001 — internal read helper, fine for a maintenance script
            "SELECT id, eval_json_path, agent_name, ticket_summary FROM evaluations WHERE is_latest=1")
        rows = [(r["id"], r["eval_json_path"], r.get("agent_name"), r.get("ticket_summary")) for r in rows]
    else:
        conn = db._connect()  # noqa: SLF001
        try:
            rows = conn.execute(
                "SELECT id, eval_json_path, agent_name, ticket_summary FROM evaluations WHERE is_latest=1"
            ).fetchall()
            rows = [(r[0], r[1], r[2], r[3]) for r in rows]
        finally:
            conn.close()

    total = len(rows)
    updated_evals = updated_metrics = skipped = missing_blob = 0

    for eval_id, path, current_agent, current_summary in rows:
        already = (current_summary not in (None, "")) and (current_agent not in (None, ""))
        if not args.all and already:
            skipped += 1
            continue
        agent, summary, per_metric = extract_eval_text(path)
        if agent is None and summary is None and not per_metric:
            missing_blob += 1
            continue

        if backend == "snowflake":
            db._execute(  # noqa: SLF001
                "UPDATE evaluations SET agent_name=?, ticket_summary=? WHERE id=?",
                (agent or "", summary or "", eval_id))
            for mid, f in per_metric.items():
                c = db._execute(  # noqa: SLF001
                    """UPDATE metric_results
                       SET metric_name=?, evidence=?, reasoning=?, improvement_note=?
                       WHERE evaluation_id=? AND metric_id=?""",
                    (f["metric_name"], f["evidence"], f["reasoning"], f["improvement_note"], eval_id, mid))
                updated_metrics += c.rowcount or 0
        else:
            with db._cursor() as cur:  # noqa: SLF001
                cur.execute("UPDATE evaluations SET agent_name=?, ticket_summary=? WHERE id=?",
                            (agent or "", summary or "", eval_id))
                for mid, f in per_metric.items():
                    cur.execute(
                        """UPDATE metric_results
                           SET metric_name=?, evidence=?, reasoning=?, improvement_note=?
                           WHERE evaluation_id=? AND metric_id=?""",
                        (f["metric_name"], f["evidence"], f["reasoning"], f["improvement_note"], eval_id, mid))
                    updated_metrics += cur.rowcount or 0
        updated_evals += 1

    print(f"Backend: {backend}")
    print(f"Latest evaluations scanned: {total}")
    print(f"  evaluations updated:       {updated_evals}")
    print(f"  metric rows updated:       {updated_metrics}")
    print(f"  skipped (already filled):  {skipped}")
    print(f"  missing/unreadable blob:   {missing_blob}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
