#!/usr/bin/env python3
"""Fetch structured QC evidence for monthly agent feedback.

Emits, per agent, the *evidence* a reviewer needs to write a Strengths /
Areas-for-Development check-in — weighted score, per-metric averages (N/A
excluded), weakest / strongest weighted metrics, flag rates, a short trend, and
specific low- and high-scored tickets with their ``ticket_summary`` and the
low metrics' ``reasoning`` / ``improvement_note``. **No prose** — the skill
writes the narrative from this JSON per METHODOLOGY.md.

Backend-agnostic: reads through ``make_database`` so it works against the local
SQLite dev DB *and* the remote Snowflake DB (the skill points it at the
read-only ``SNOWFLAKE_READER_*`` user via config/env). The weighting scheme is
the single source of truth in ``WEIGHTS`` below (mirrors deploy/seed_metric_weights.sql
and METHODOLOGY.md).

Usage:
  python skills/agent-feedback/fetch_qc_data.py --agent "Sthitapragyan Rout" --month 2026-06
  python skills/agent-feedback/fetch_qc_data.py --agent all --month 2026-06 --group L1
  python skills/agent-feedback/fetch_qc_data.py --list-agents --month 2026-06
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.config import load_config  # noqa: E402
from src.storage.factory import make_database  # noqa: E402

# --- Weighting: 12 metrics summing to 100. Metrics not listed carry weight 0 ---
# (they are recorded but excluded from the weighted score — QC scores written
# ticket handling; see METHODOLOGY.md). Single source of truth for the skill.
WEIGHTS: dict[str, float] = {
    "METRIC_1": 7.5,    # Clarifying Questions
    "METRIC_4": 10.0,   # Root Cause Analysis
    "METRIC_5": 10.0,   # Resolution Accuracy
    "METRIC_6": 10.0,   # Detailed Resolution Steps
    "METRIC_7": 10.0,   # All Concerns Addressed
    "METRIC_9": 5.0,    # Proactive Updates
    "METRIC_11": 10.0,  # Clear Communication
    "METRIC_12": 10.0,  # Empathetic & Professional Tone
    "METRIC_13": 5.0,   # Resolution Status Set Correctly
    "METRIC_15": 7.5,   # Workaround Provided
    "METRIC_17": 5.0,   # KB / Docs Referenced
    "METRIC_18": 10.0,  # Internal Notes Quality
}

# Fallback display names (the DB now carries metric_name, but keep a map for
# rows migrated before the name column existed).
METRIC_NAMES = {
    "METRIC_1": "Clarifying Questions", "METRIC_2": "Roadmap to Resolution",
    "METRIC_3": "Correct SLA Expectations", "METRIC_4": "Root Cause Analysis",
    "METRIC_5": "Resolution Accuracy", "METRIC_6": "Detailed Resolution Steps",
    "METRIC_7": "All Concerns Addressed", "METRIC_8": "Timely First Response",
    "METRIC_9": "Proactive Updates", "METRIC_10": "Resolution On Time",
    "METRIC_11": "Clear Communication", "METRIC_12": "Empathetic & Professional Tone",
    "METRIC_13": "Resolution Status Set Correctly", "METRIC_14": "Custom Attributes Filled",
    "METRIC_15": "Workaround Provided", "METRIC_16": "Escalation Judgment",
    "METRIC_17": "KB / Docs Referenced", "METRIC_18": "Internal Notes Quality",
    "METRIC_19": "QC Reopen Reason",
}

GROUP_IDS = {"L1": 44897999201817, "L2": 6338786491161}
LOW_BANDS = {"Poor", "Needs Improvement"}
GOOD_BANDS = {"Excellent", "Good"}
LOW_RATING = 2  # ratings <= this are "low"


def _num(rating) -> float | None:
    """Numeric rating or None for N/A / blank."""
    if rating is None:
        return None
    s = str(rating).strip()
    if s == "" or s.upper() == "N/A":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _rowget(row, key):
    """Row is a sqlite3.Row (SQLite) or a lowercase dict (Snowflake)."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _prev_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"


def _group_tickets(rows) -> dict:
    """Group flat (ticket × metric) rows into {ticket_id: {meta, metrics:{id:{...}}}}."""
    tickets: dict = {}
    for row in rows:
        tid = _rowget(row, "ticket_id")
        if tid is None:
            continue
        t = tickets.setdefault(tid, {
            "ticket_id": tid,
            "closed_at": _rowget(row, "closed_at"),
            "group_id": _rowget(row, "group_id"),
            "band": _rowget(row, "performance_band"),
            "aggregate_score": _rowget(row, "aggregate_score"),
            "ticket_summary": _rowget(row, "ticket_summary") or "",
            "flags": _parse_flags(_rowget(row, "flags")),
            "metrics": {},
        })
        mid = _rowget(row, "metric_id")
        if mid:
            t["metrics"][mid] = {
                "metric_id": mid,
                "metric_name": _rowget(row, "metric_name") or METRIC_NAMES.get(mid, mid),
                "rating": _rowget(row, "rating"),
                "rating_label": _rowget(row, "rating_label"),
                "reasoning": _rowget(row, "reasoning") or "",
                "improvement_note": _rowget(row, "improvement_note") or "",
                "evidence": _rowget(row, "evidence") or "",
            }
    return tickets


def _norm_flag(flag: str) -> str:
    """Collapse a flag to its family: drop trailing '(...)' detail and specific metric names.

    'SLA_TTR_BREACHED (433.0 min, threshold 120.0 min)' -> 'SLA_TTR_BREACHED'
    'rating of 1 on Resolution Accuracy'                 -> 'rating of 1 on a metric'
    """
    import re
    f = re.sub(r"\s*\(.*\)\s*$", "", str(flag)).strip()
    f = re.sub(r"^(rating of \d+) on .+$", r"\1 on a metric", f)
    return f


def _parse_flags(raw) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _weighted_ticket_score(metrics: dict) -> float | None:
    """Sum(w*rating)/Sum(w) over non-N/A weighted metrics; N/A dropped from num+den."""
    num = den = 0.0
    for mid, w in WEIGHTS.items():
        m = metrics.get(mid)
        if not m:
            continue
        val = _num(m["rating"])
        if val is None:
            continue
        num += w * val
        den += w
    return (num / den) if den else None


def analyse(rows) -> dict:
    """Aggregate flat rows into one agent-month evidence bundle."""
    tickets = _group_tickets(rows)
    n = len(tickets)

    # Per-metric averages (N/A excluded) and low counts, across all metrics present.
    metric_vals: dict[str, list[float]] = defaultdict(list)
    metric_low: dict[str, int] = defaultdict(int)
    metric_na: dict[str, int] = defaultdict(int)
    for t in tickets.values():
        for mid, m in t["metrics"].items():
            val = _num(m["rating"])
            if val is None:
                metric_na[mid] += 1
            else:
                metric_vals[mid].append(val)
                if val <= LOW_RATING:
                    metric_low[mid] += 1

    per_metric = []
    for mid in sorted(metric_vals.keys() | metric_na.keys(), key=lambda x: int(x.split("_")[1])):
        vals = metric_vals.get(mid, [])
        per_metric.append({
            "metric_id": mid,
            "metric_name": METRIC_NAMES.get(mid, mid),
            "weight": WEIGHTS.get(mid, 0.0),
            "avg": round(sum(vals) / len(vals), 3) if vals else None,
            "rated": len(vals),
            "na": metric_na.get(mid, 0),
            "low": metric_low.get(mid, 0),
        })

    # Weighted score = mean of per-ticket weighted scores.
    ticket_scores = {}
    for tid, t in tickets.items():
        ws = _weighted_ticket_score(t["metrics"])
        ticket_scores[tid] = ws
    scored = [s for s in ticket_scores.values() if s is not None]
    weighted_score = round(sum(scored) / len(scored), 3) if scored else None

    # Weakest / strongest among *weighted* metrics with enough data (>=3 rated).
    weighted_rated = [p for p in per_metric if p["weight"] > 0 and p["rated"] >= 3 and p["avg"] is not None]
    weakest = min(weighted_rated, key=lambda p: p["avg"]) if weighted_rated else None
    strongest = max(weighted_rated, key=lambda p: p["avg"]) if weighted_rated else None

    # Flag rates. Normalise flag families (strip trailing "(...)" detail like SLA minutes)
    # so e.g. all SLA_TTR_BREACHED variants aggregate into one rate.
    flag_counts: dict[str, int] = defaultdict(int)
    for t in tickets.values():
        for f in {_norm_flag(x) for x in t["flags"]}:
            flag_counts[f] += 1
    flags_pct = {f: round(100 * c / n, 1) for f, c in sorted(flag_counts.items(), key=lambda kv: -kv[1])} if n else {}

    # Low tickets: band in LOW_BANDS OR any weighted metric <= LOW_RATING.
    low_tickets, best_tickets = [], []
    for tid, t in tickets.items():
        low_metrics = [
            {"metric_name": m["metric_name"], "metric_id": mid, "rating": m["rating"],
             "reasoning": m["reasoning"], "improvement_note": m["improvement_note"]}
            for mid, m in t["metrics"].items()
            if WEIGHTS.get(mid, 0) > 0 and (_num(m["rating"]) is not None and _num(m["rating"]) <= LOW_RATING)
        ]
        is_low = (t["band"] in LOW_BANDS) or bool(low_metrics)
        entry = {
            "ticket_id": tid,
            "weighted_score": round(ticket_scores[tid], 3) if ticket_scores[tid] is not None else None,
            "band": t["band"],
            "summary": t["ticket_summary"],
            "flags": sorted(set(t["flags"])),
        }
        if is_low:
            entry["low_metrics"] = sorted(low_metrics, key=lambda x: _num(x["rating"]) or 99)
            low_tickets.append(entry)
        if t["band"] in GOOD_BANDS and not low_metrics:
            best_tickets.append(entry)

    low_tickets.sort(key=lambda e: (e["weighted_score"] if e["weighted_score"] is not None else 99))
    best_tickets.sort(key=lambda e: (e["weighted_score"] if e["weighted_score"] is not None else 0), reverse=True)

    return {
        "n_tickets": n,
        "weighted_score": weighted_score,
        "per_metric": per_metric,
        "weakest": {"metric_id": weakest["metric_id"], "metric_name": weakest["metric_name"],
                    "avg": weakest["avg"]} if weakest else None,
        "strongest": {"metric_id": strongest["metric_id"], "metric_name": strongest["metric_name"],
                      "avg": strongest["avg"]} if strongest else None,
        "flags_pct": flags_pct,
        "low_tickets": low_tickets[:12],
        "best_tickets": best_tickets[:5],
    }


def ticket_detail(rows) -> dict:
    """Full evidence for a single ticket — every metric with its rating + narrative.

    Answers per-ticket questions: why it scored as it did, what specifically fell
    short, and what to improve. Returns {} if the ticket has no latest evaluation.
    """
    tickets = _group_tickets(rows)
    if not tickets:
        return {}
    tid, t = next(iter(tickets.items()))

    metrics = []
    for mid, m in sorted(t["metrics"].items(), key=lambda kv: int(kv[0].split("_")[1])):
        val = _num(m["rating"])
        metrics.append({
            "metric_id": mid,
            "metric_name": m["metric_name"],
            "weight": WEIGHTS.get(mid, 0.0),
            "scored": WEIGHTS.get(mid, 0.0) > 0,  # contributes to the weighted score
            "rating": m["rating"],
            "rating_label": m["rating_label"],
            "is_low": val is not None and val <= LOW_RATING,
            "reasoning": m["reasoning"],
            "evidence": m["evidence"],
            "improvement_note": m["improvement_note"],
        })

    scored = [m for m in metrics if m["scored"]]
    lowlights = sorted(
        [m for m in scored if _num(m["rating"]) is not None and _num(m["rating"]) <= LOW_RATING],
        key=lambda m: _num(m["rating"]))
    strengths = sorted(
        [m for m in scored if _num(m["rating"]) == 4],
        key=lambda m: -m["weight"])
    # "What to improve": any scored metric rated below 4 that carries an improvement note.
    improvements = [
        {"metric_name": m["metric_name"], "rating": m["rating"], "improvement_note": m["improvement_note"]}
        for m in sorted(scored, key=lambda m: (_num(m["rating"]) if _num(m["rating"]) is not None else 99))
        if (_num(m["rating"]) is not None and _num(m["rating"]) < 4 and m["improvement_note"])
    ]

    gid = t["group_id"]
    return {
        "ticket_id": tid,
        "agent_name": _first_agent(rows),
        "close_month": (t["closed_at"] or "")[:7],
        "closed_at": t["closed_at"],
        "group": next((k for k, v in GROUP_IDS.items() if v == gid), str(gid)),
        "band": t["band"],
        "aggregate_score": t["aggregate_score"],
        "weighted_score": round(_weighted_ticket_score(t["metrics"]), 3)
                          if _weighted_ticket_score(t["metrics"]) is not None else None,
        "summary": t["ticket_summary"],
        "flags": sorted(set(t["flags"])),
        "metrics": metrics,
        "lowlights": lowlights,      # scored metrics rated <= 2, worst first
        "strengths": strengths,      # scored metrics rated 4, by weight
        "improvements": improvements,  # scored metrics < 4 with a concrete improvement note
    }


def _first_agent(rows):
    for row in rows:
        a = _rowget(row, "agent_name")
        if a:
            return a
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", help="Exact agent name, or 'all' for every agent in the month.")
    ap.add_argument("--month", help="Close month, YYYY-MM.")
    ap.add_argument("--group", choices=["L1", "L2"], help="Restrict to L1 (Chat) or L2 (Escalation).")
    ap.add_argument("--ticket", type=int,
                    help="Drill into a single ticket: full per-metric ratings + reasoning + "
                         "improvement notes (for 'why was this rated low' / 'what to improve' questions).")
    ap.add_argument("--trend", type=int, default=2, help="How many prior months of weighted score to include.")
    ap.add_argument("--list-agents", action="store_true",
                    help="List distinct agent names (+ ticket counts) for the month and exit.")
    ap.add_argument("--config", default="config/config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    db = make_database(cfg)
    group_id = GROUP_IDS.get(args.group) if args.group else None

    if args.ticket is not None:
        rows = db.get_feedback_rows(ticket_id=args.ticket)
        detail = ticket_detail(rows)
        if not detail:
            print(json.dumps({"ticket_id": args.ticket, "error": "no latest evaluation found "
                              "(ticket may be QC-excluded, not yet evaluated, or purged)"}, indent=2))
            return 0
        print(json.dumps(detail, indent=2, default=str))
        return 0

    if args.list_agents:
        agents = [dict(a) if not isinstance(a, dict) else a for a in db.list_agents(month=args.month)]
        for a in agents:
            gid = a.get("group_id")
            grp = next((k for k, v in GROUP_IDS.items() if v == gid), str(gid))
            print(f"{a.get('n'):>5}  [{grp}]  {a.get('agent_name')}")
        return 0

    if not args.month:
        ap.error("--month YYYY-MM is required (unless --list-agents).")

    # Which agents?
    if args.agent and args.agent.lower() != "all":
        agent_names = [args.agent]
    else:
        rows = db.list_agents(month=args.month)
        agent_names = []
        for a in rows:
            a = dict(a) if not isinstance(a, dict) else a
            if group_id is not None and a.get("group_id") != group_id:
                continue
            agent_names.append(a.get("agent_name"))

    results = []
    for name in agent_names:
        rows = db.get_feedback_rows(agent_name=name, month=args.month, group_id=group_id)
        bundle = analyse(rows)
        bundle["agent_name"] = name
        bundle["month"] = args.month
        bundle["group"] = args.group

        # Trend: prior months' weighted scores.
        trend = []
        m = args.month
        for _ in range(max(0, args.trend)):
            m = _prev_month(m)
            prev_rows = db.get_feedback_rows(agent_name=name, month=m, group_id=group_id)
            prev = analyse(prev_rows)
            trend.append({"month": m, "weighted_score": prev["weighted_score"], "n_tickets": prev["n_tickets"]})
        bundle["trend"] = list(reversed(trend))
        results.append(bundle)

    out = results[0] if len(results) == 1 else {"month": args.month, "group": args.group, "agents": results}
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
