#!/usr/bin/env python3
"""
Agent Performance Report

Reads the latest (or specified) wide CSV export, joins with the tickets DB
for group/channel metadata, and produces two CSVs:

  data/exports/YYYY-MM-DD_agent_performance_summary.csv
      One row per agent: aggregate score stats, SLA compliance, band distribution.

  data/exports/YYYY-MM-DD_agent_metric_averages.csv
      One row per agent, one column-group per metric (avg, N/A%, count).

Usage:
  python scripts/agent_performance_report.py
  python scripts/agent_performance_report.py --from 2026-04-01 --to 2026-04-30
  python scripts/agent_performance_report.py --csv data/exports/custom_wide.csv
"""

import argparse
import csv
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Optional

# ---------------------------------------------------------------------------
# Metric registry
# ---------------------------------------------------------------------------

METRICS = [f"METRIC_{i}" for i in range(1, 20)]

METRIC_NAMES = {
    "METRIC_1":  "Clarifying Questions",
    "METRIC_2":  "Roadmap to Resolution",
    "METRIC_3":  "Correct SLA Expectations",
    "METRIC_4":  "Root Cause Analysis",
    "METRIC_5":  "Resolution Accuracy",
    "METRIC_6":  "Detailed Resolution Steps",
    "METRIC_7":  "All Concerns Addressed",
    "METRIC_8":  "Timely First Response",
    "METRIC_9":  "Proactive Updates",
    "METRIC_10": "Resolution On Time",
    "METRIC_11": "Clear Communication",
    "METRIC_12": "Empathetic & Professional Tone",
    "METRIC_13": "Resolution Status Set Correctly",
    "METRIC_14": "Custom Attributes Filled",
    "METRIC_15": "Workaround Provided",
    "METRIC_16": "Escalation Judgment",
    "METRIC_17": "KB / Docs Referenced",
    "METRIC_18": "Internal Notes Quality",
    "METRIC_19": "QC Reopen Reason",
}

_SLA_FLAG_RE = re.compile(r"(SLA_(?:FRT|TTR)_(?:BREACHED|MET))")

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def find_latest_wide_csv(exports_dir: Path) -> Path:
    candidates = sorted(exports_dir.glob("*_evaluations_wide.csv"), reverse=True)
    if not candidates:
        sys.exit(f"No wide CSV found in {exports_dir}")
    return candidates[0]


def load_group_map(project_root: Path) -> dict:
    """Return {group_id_str: group_name} from config, or empty dict on failure."""
    try:
        sys.path.insert(0, str(project_root))
        from src.config import load_config
        cfg = load_config(project_root / "config/config.yaml")
        return {g.id: g.name for g in cfg.zendesk.groups}
    except Exception as exc:
        print(f"Warning: could not load config ({exc}). Group IDs used as names.", file=sys.stderr)
        return {}


def load_ticket_meta(db_path: Path) -> dict:
    """Return {ticket_id: {'group_id': str, 'channel': str}} from the tickets table."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT ticket_id, group_id, channel FROM tickets").fetchall()
    conn.close()
    return {
        row[0]: {
            "group_id": str(row[1]) if row[1] is not None else "",
            "channel": row[2] or "",
        }
        for row in rows
    }


def load_wide_csv(
    csv_path: Path,
    from_date: Optional[date],
    to_date: Optional[date],
) -> list:
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if from_date or to_date:
                raw = row.get("evaluation_date", "")
                try:
                    eval_date = datetime.fromisoformat(raw).date()
                except (ValueError, TypeError):
                    eval_date = None
                if eval_date:
                    if from_date and eval_date < from_date:
                        continue
                    if to_date and eval_date > to_date:
                        continue
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_flags(flags_str: str) -> list:
    """Split semicolon-separated flags and normalise SLA ones (strip metric details)."""
    if not flags_str or not flags_str.strip():
        return []
    result = []
    for part in (p.strip() for p in flags_str.split(";")):
        if not part:
            continue
        m = _SLA_FLAG_RE.match(part)
        result.append(m.group(1) if m else part)
    return result


def pct(num: int, denom: int) -> str:
    return f"{100 * num / denom:.1f}%" if denom else ""


def fmt(v) -> str:
    return f"{v:.2f}" if v is not None else ""


def mode_value(lst: list) -> str:
    return Counter(lst).most_common(1)[0][0] if lst else ""


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _empty_agent() -> dict:
    return {
        "count": 0,
        "scores": [],
        "bands": [],
        "frt": [],
        "ttr": [],
        "flags": [],
        "confidences": [],
        "channels": [],
        "group_ids": [],
        "metric_ratings": {m: [] for m in METRICS},
        "metric_na": {m: 0 for m in METRICS},
        "metric_total": {m: 0 for m in METRICS},
    }


def aggregate(rows: list, ticket_meta: dict) -> dict:
    agents = defaultdict(_empty_agent)

    for row in rows:
        agent = row.get("agent_name", "").strip() or "(unknown)"
        d = agents[agent]
        d["count"] += 1

        # Ticket metadata from DB
        try:
            tid = int(row["ticket_id"])
        except (ValueError, TypeError):
            tid = None
        if tid is not None:
            meta = ticket_meta.get(tid, {})
            if meta.get("channel"):
                d["channels"].append(meta["channel"])
            if meta.get("group_id"):
                d["group_ids"].append(meta["group_id"])

        try:
            d["scores"].append(float(row["aggregate_score"]))
        except (ValueError, TypeError):
            pass

        if row.get("performance_band"):
            d["bands"].append(row["performance_band"])
        if row.get("frt_status"):
            d["frt"].append(row["frt_status"])
        if row.get("ttr_status"):
            d["ttr"].append(row["ttr_status"])
        if row.get("evaluator_confidence"):
            d["confidences"].append(row["evaluator_confidence"])

        d["flags"].extend(parse_flags(row.get("flags", "")))

        # Metrics
        for m in METRICS:
            rating_str = row.get(f"{m}_rating", "").strip()
            if not rating_str:
                continue
            d["metric_total"][m] += 1
            if rating_str.upper() == "N/A":
                d["metric_na"][m] += 1
            else:
                try:
                    d["metric_ratings"][m].append(float(rating_str))
                except ValueError:
                    pass

    return agents


# ---------------------------------------------------------------------------
# Summary CSV
# ---------------------------------------------------------------------------

SUMMARY_FIELDS = [
    "agent_name", "group_name", "group_id", "channel", "ticket_count",
    "avg_score", "score_stddev",
    "pct_excellent", "pct_good", "pct_needs_improvement", "pct_poor",
    "frt_applicable", "frt_met", "frt_compliance_pct",
    "ttr_applicable", "ttr_met", "ttr_compliance_pct",
    "pct_high_confidence", "top_flag",
]


def build_summary_rows(agents: dict, group_map: dict) -> list:
    rows = []
    for agent_name, d in agents.items():
        scores = d["scores"]
        bands = Counter(d["bands"])
        total_bands = sum(bands.values())

        frt = Counter(d["frt"])
        frt_applicable = frt.get("MET", 0) + frt.get("BREACHED", 0)

        ttr = Counter(d["ttr"])
        ttr_applicable = ttr.get("MET", 0) + ttr.get("BREACHED", 0)

        flag_counts = Counter(d["flags"])
        top_flag = flag_counts.most_common(1)[0][0] if flag_counts else ""

        group_id = mode_value(d["group_ids"])
        group_name = group_map.get(group_id, group_id)
        channel = mode_value(d["channels"])

        conf = Counter(d["confidences"])

        rows.append({
            "agent_name": agent_name,
            "group_name": group_name,
            "group_id": group_id,
            "channel": channel,
            "ticket_count": d["count"],
            "avg_score": fmt(mean(scores) if scores else None),
            "score_stddev": fmt(stdev(scores) if len(scores) > 1 else None),
            "pct_excellent": pct(bands.get("Excellent", 0), total_bands),
            "pct_good": pct(bands.get("Good", 0), total_bands),
            "pct_needs_improvement": pct(bands.get("Needs Improvement", 0), total_bands),
            "pct_poor": pct(bands.get("Poor", 0), total_bands),
            "frt_applicable": frt_applicable,
            "frt_met": frt.get("MET", 0),
            "frt_compliance_pct": pct(frt.get("MET", 0), frt_applicable),
            "ttr_applicable": ttr_applicable,
            "ttr_met": ttr.get("MET", 0),
            "ttr_compliance_pct": pct(ttr.get("MET", 0), ttr_applicable),
            "pct_high_confidence": pct(conf.get("HIGH", 0), sum(conf.values())) if conf else "",
            "top_flag": top_flag,
        })

    rows.sort(key=lambda r: (r["group_name"], -float(r["avg_score"]) if r["avg_score"] else 0))
    return rows


def write_summary_csv(rows: list, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Metric averages CSV
# ---------------------------------------------------------------------------

def build_metric_rows(agents: dict, group_map: dict) -> tuple:
    """Return (data_rows, header_row, internal_keys)."""
    # Build ordered column spec: agent cols, then per-metric triplet
    display_header = ["agent_name", "group_name", "ticket_count"]
    internal_keys = ["agent_name", "group_name", "ticket_count"]
    for m in METRICS:
        n = m.split("_")[1]
        name = METRIC_NAMES[m]
        display_header += [
            f"M{n} {name} - avg /4",
            f"M{n} {name} - N/A%",
            f"M{n} {name} - rated count",
        ]
        internal_keys += [f"{m}_avg", f"{m}_na_pct", f"{m}_rated_count"]

    rows = []
    for agent_name, d in sorted(agents.items()):
        group_id = mode_value(d["group_ids"])
        row = {
            "agent_name": agent_name,
            "group_name": group_map.get(group_id, group_id),
            "ticket_count": d["count"],
        }
        for m in METRICS:
            ratings = d["metric_ratings"][m]
            total = d["metric_total"][m]
            na = d["metric_na"][m]
            row[f"{m}_avg"] = fmt(mean(ratings) if ratings else None)
            row[f"{m}_na_pct"] = pct(na, total)
            row[f"{m}_rated_count"] = len(ratings)
        rows.append(row)

    return rows, display_header, internal_keys


def write_metric_csv(rows: list, display_header: list, internal_keys: list, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(display_header)
        for row in rows:
            writer.writerow([row.get(k, "") for k in internal_keys])


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

def print_table(summary_rows: list) -> None:
    col_w = [32, 22, 7, 9, 9, 7, 7]
    headers = ["Agent", "Group", "Tickets", "Avg Score", "Excellent", "FRT%", "TTR%"]
    sep = "  ".join("-" * w for w in col_w)
    fmt_row = lambda vals: "  ".join(str(v).ljust(w) if i < 2 else str(v).rjust(w)
                                     for i, (v, w) in enumerate(zip(vals, col_w)))
    print()
    print(fmt_row(headers))
    print(sep)
    for r in summary_rows:
        print(fmt_row([
            r["agent_name"][:31],
            r["group_name"][:21],
            r["ticket_count"],
            r["avg_score"],
            r["pct_excellent"],
            r["frt_compliance_pct"],
            r["ttr_compliance_pct"],
        ]))
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD", help="Evaluation start date (inclusive)")
    p.add_argument("--to",   dest="to_date",   metavar="YYYY-MM-DD", help="Evaluation end date (inclusive)")
    p.add_argument("--csv",  metavar="PATH", help="Explicit wide CSV path (default: latest in exports dir)")
    return p.parse_args()


def main():
    args = parse_args()
    project_root = Path(__file__).resolve().parent.parent

    from_date = date.fromisoformat(args.from_date) if args.from_date else None
    to_date   = date.fromisoformat(args.to_date)   if args.to_date   else None

    exports_dir = project_root / "data/exports"
    db_path     = project_root / "data/evaluations.db"
    csv_path    = Path(args.csv).resolve() if args.csv else find_latest_wide_csv(exports_dir)

    print(f"Source      : {csv_path.name}")
    if from_date or to_date:
        print(f"Date range  : {from_date or 'start'} → {to_date or 'end'}")

    group_map   = load_group_map(project_root)
    ticket_meta = load_ticket_meta(db_path)
    rows        = load_wide_csv(csv_path, from_date, to_date)

    if not rows:
        sys.exit("No rows matched. Check your date range.")

    print(f"Tickets     : {len(rows)}")

    agents = aggregate(rows, ticket_meta)
    print(f"Agents      : {len(agents)}")

    # Build output paths
    today = date.today().isoformat()
    summary_path = exports_dir / f"{today}_agent_performance_summary.csv"
    metric_path  = exports_dir / f"{today}_agent_metric_averages.csv"

    # Summary
    summary_rows = build_summary_rows(agents, group_map)
    write_summary_csv(summary_rows, summary_path)

    # Metric averages
    metric_rows, display_header, internal_keys = build_metric_rows(agents, group_map)
    write_metric_csv(metric_rows, display_header, internal_keys, metric_path)

    # Console table
    print_table(summary_rows)
    print(f"Summary CSV → {summary_path.relative_to(project_root)}")
    print(f"Metric CSV  → {metric_path.relative_to(project_root)}")


if __name__ == "__main__":
    main()
