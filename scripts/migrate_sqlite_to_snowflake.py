#!/usr/bin/env python3
"""One-time migration: copy the local SQLite DB + cursor into Snowflake.

Run ONCE on the VM after setting storage.backend=snowflake and the SNOWFLAKE_* env vars:

    python scripts/migrate_sqlite_to_snowflake.py --sqlite data/evaluations.db --state data/state.json

It:
  1. Ensures the Snowflake schema/sequences exist (via SnowflakeDatabase).
  2. TRUNCATEs the target tables (idempotent re-runs) unless --no-truncate.
  3. Bulk-loads tickets, evaluations, metric_results, runs with write_pandas.
     - evaluations/runs keep their SQLite ids (FKs stay valid); metric_results.id is dropped
       (AUTOINCREMENT reassigns — nothing references it).
  4. Recreates evaluations_id_seq / runs_id_seq to start above the max migrated id.
  5. Seeds pipeline_state from state.json so the incremental cursor continues seamlessly.
  6. Prints source-vs-target row counts to verify.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
from snowflake.connector.pandas_tools import write_pandas  # noqa: E402

from src.config import load_config  # noqa: E402
from src.storage.snowflake_database import SnowflakeDatabase  # noqa: E402

TABLES = ["tickets", "evaluations", "metric_results", "runs"]


def _read_table(sconn: sqlite3.Connection, name: str) -> pd.DataFrame:
    df = pd.read_sql_query(f"SELECT * FROM {name}", sconn)
    if name == "metric_results" and "id" in df.columns:
        df = df.drop(columns=["id"])  # let Snowflake AUTOINCREMENT assign
    df.columns = [c.upper() for c in df.columns]  # match unquoted (UPPER) Snowflake columns
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", default="data/evaluations.db")
    ap.add_argument("--state", default="data/state.json")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--no-truncate", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if (cfg.storage.backend or "").lower() != "snowflake":
        print("ERROR: config storage.backend must be 'snowflake' to migrate.", file=sys.stderr)
        return 2

    db = SnowflakeDatabase(cfg)          # creates tables + sequences
    conn = db._connection()
    sconn = sqlite3.connect(args.sqlite)

    src_counts, tgt_counts = {}, {}
    for t in TABLES:
        src_counts[t] = sconn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        if not args.no_truncate:
            conn.cursor().execute(f"TRUNCATE TABLE IF EXISTS {t}")
        df = _read_table(sconn, t)
        if len(df):
            ok, nchunks, nrows, _ = write_pandas(conn, df, t.upper(), quote_identifiers=True)
            print(f"  {t}: loaded {nrows} rows (ok={ok})")
        tgt_counts[t] = conn.cursor().execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]

    # Reset sequences above the max migrated id so new inserts don't collide.
    for seq, tbl in [("evaluations_id_seq", "evaluations"), ("runs_id_seq", "runs")]:
        maxid = conn.cursor().execute(f"SELECT COALESCE(MAX(id),0) FROM {tbl}").fetchone()[0]
        conn.cursor().execute(f"CREATE OR REPLACE SEQUENCE {seq} START = {int(maxid)+1} INCREMENT = 1")
        print(f"  {seq} reset to start at {int(maxid)+1}")

    # Seed cursor/state from state.json so incremental continues.
    statep = Path(args.state)
    if statep.exists():
        state = json.loads(statep.read_text())
        for key in ("zendesk_cursor", "last_run_at", "last_successful_run_at", "last_ticket_updated_at"):
            if state.get(key) is not None:
                db.set_state(key, str(state[key]))
        if state.get("last_run_stats"):
            db.set_state("last_run_stats", json.dumps(state["last_run_stats"]))
        print(f"  pipeline_state seeded (cursor={'set' if state.get('zendesk_cursor') else 'none'})")
    else:
        print(f"  WARNING: {args.state} not found — cursor NOT seeded (first VM run would re-fetch from initial_fetch_from).")

    print("\n=== row counts (SQLite -> Snowflake) ===")
    for t in TABLES:
        flag = "OK" if (t == "metric_results" or src_counts[t] == tgt_counts[t]) else "MISMATCH"
        print(f"  {t:16s} {src_counts[t]:>8} -> {tgt_counts[t]:>8}  {flag}")
    sconn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
