#!/usr/bin/env python3
"""Self-test for the agent-feedback skill — locks in scoring + schema behaviour.

Runs the skill's own fetch/analysis functions against the configured QC DB and
asserts the ground truth we validated by hand (Sthitapragyan Rout, June 2026).
Catches regressions in the weighting, the N/A rule, the narrative columns, or the
DB read layer. Standalone (no pytest); prints PASS/FAIL per check and exits
non-zero on any failure.

    python skills/agent-feedback/selftest.py
    python skills/agent-feedback/selftest.py --config config/config.yaml

Intended for the local SQLite dev DB (the reference data), but it runs against
whatever backend `config.yaml` points at.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules

from qc_reader import make_reader  # noqa: E402
import fetch_qc_data as fq  # noqa: E402  (same dir)

AGENT = "Sthitapragyan Rout"
MONTH = "2026-06"
TICKET = 72247

_checks: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    _checks.append((bool(cond), label))
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", help="Path to a local SQLite QC DB (default: env / repo data/evaluations.db).")
    args = ap.parse_args()

    db = make_reader(args.sqlite)
    print(f"Self-test against {AGENT} / {MONTH} (ticket #{TICKET})\n")

    # --- Agent-month bundle ---
    b = fq.agent_bundle(db, AGENT, MONTH, group_id=None, trend_n=0)
    ws = b["weighted_score"]
    check(b["n_tickets"] >= 10, "agent has a real caseload", f"n_tickets={b['n_tickets']}")
    check(ws is not None and 3.6 <= ws <= 4.0, "weighted score in expected band", f"weighted={ws}")
    weakest = (b["weakest"] or {}).get("metric_name")
    check(weakest == "Root Cause Analysis", "weakest metric is RCA", f"weakest={weakest}")
    check(b["strongest"] is not None, "a strongest metric is identified",
          f"strongest={(b['strongest'] or {}).get('metric_name')}")

    # Ticket 72247 present among low tickets with a low, noted Resolution Accuracy.
    lt = {t["ticket_id"]: t for t in b["low_tickets"]}
    check(TICKET in lt, f"ticket #{TICKET} surfaced in low_tickets")
    if TICKET in lt:
        ra = [m for m in lt[TICKET].get("low_metrics", []) if m["metric_name"] == "Resolution Accuracy"]
        check(bool(ra), "Resolution Accuracy flagged low on #%d" % TICKET)
        check(bool(ra and ra[0].get("improvement_note")), "low metric carries an improvement note",
              (ra[0]["improvement_note"][:50] + "…") if ra else "")

    # --- Single-ticket drill-down ---
    d = fq.ticket_detail(db.get_feedback_rows(ticket_id=TICKET))
    check(bool(d), f"ticket_detail returns data for #{TICKET}")
    if d:
        check(len(d.get("metrics", [])) == 19, "all 19 metrics present", f"n={len(d.get('metrics', []))}")
        check(len(d.get("lowlights", [])) >= 1, "at least one lowlight")
        check(len(d.get("improvements", [])) >= 1, "at least one improvement note")
        check(d.get("agent_name") == AGENT, "ticket attributed to the right agent", str(d.get("agent_name")))

    # --- Weighting sanity ---
    check(round(sum(fq.WEIGHTS.values())) == 100, "weights sum to 100",
          f"sum={sum(fq.WEIGHTS.values())}")

    passed = sum(1 for ok, _ in _checks if ok)
    total = len(_checks)
    print(f"\n{passed}/{total} checks passed.")
    if passed != total:
        print("SELF-TEST FAILED")
        return 1
    print("SELF-TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
