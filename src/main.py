#!/usr/bin/env python3
"""Ticket Evaluation Tool — CLI entry point.

Usage:
  python src/main.py run                                   # Full incremental pipeline
  python src/main.py run --fetch-only                      # Fetch tickets only
  python src/main.py run --force                           # Force re-run (ignore skip cache)
  python src/main.py run --from 2025-01-01                 # Backfill from date (no cursor advance)
  python src/main.py run --from 2025-01-01 --to 2025-03-31 # Backfill date window

  python src/main.py re-evaluate --from 2025-01-01 --to 2025-06-01
  python src/main.py re-evaluate --tickets 67207,67258,64557
  python src/main.py re-evaluate --all

  python src/main.py publish --unpublished                 # Re-push failed write-backs
  python src/main.py publish --unpublished --from 2025-03-01 --to 2025-03-31
  python src/main.py publish --unpublished --dry-run       # Preview without API calls

  python src/main.py purge-excluded                        # Dry-run: report QC-excluded tickets
  python src/main.py purge-excluded --execute              # Purge them + clear Zendesk QC fields
  python src/main.py purge-excluded --execute --skip-zendesk
  python src/main.py purge-excluded --tickets 123,456 --execute

  python src/main.py status                                # Pipeline health summary
  python src/main.py audit [--from DATE] [--to DATE]       # Find unevaluated/unpublished tickets
  python src/main.py export [--from DATE] [--to DATE] [--format wide|long|both]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure project root is in sys.path when run as a script
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config import load_config
from src.pipeline.orchestrator import Orchestrator
from src.utils.logger import setup_logging


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ticket Evaluation Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config", default="config/config.yaml",
        help="Path to config.yaml (default: config/config.yaml)"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # --- run ---
    run_p = sub.add_parser("run", help="Run the evaluation pipeline")
    run_p.add_argument("--fetch-only", action="store_true", help="Only fetch tickets, do not evaluate")
    run_p.add_argument("--force", action="store_true", help="Force re-evaluation of already-evaluated tickets")
    run_p.add_argument("--from", dest="from_date", metavar="DATE",
                       help="Backfill: fetch tickets closed on/after DATE (YYYY-MM-DD). Bypasses cursor.")
    run_p.add_argument("--to", dest="to_date", metavar="DATE",
                       help="Backfill upper bound (YYYY-MM-DD). Used with --from.")

    # --- re-evaluate ---
    re_p = sub.add_parser("re-evaluate", help="Re-evaluate tickets with current prompt")
    group = re_p.add_mutually_exclusive_group(required=True)
    group.add_argument("--from", dest="from_date", metavar="DATE", help="Start date (YYYY-MM-DD)")
    group.add_argument("--tickets", metavar="IDS", help="Comma-separated ticket IDs")
    group.add_argument("--all", action="store_true", help="Re-evaluate all tickets on disk")
    re_p.add_argument("--to", dest="to_date", metavar="DATE", help="End date (YYYY-MM-DD), used with --from")
    re_p.add_argument("--force-fetch", action="store_true", help="Re-fetch ticket data from Zendesk")

    # --- publish ---
    pub_p = sub.add_parser("publish", help="Publish evaluation results to Zendesk")
    pub_p.add_argument("--unpublished", action="store_true", help="Re-push all unpublished results")
    pub_p.add_argument("--from", dest="from_date", metavar="DATE",
                       help="Filter by ticket closed date on/after DATE (YYYY-MM-DD)")
    pub_p.add_argument("--to", dest="to_date", metavar="DATE",
                       help="Filter by ticket closed date on/before DATE (YYYY-MM-DD)")
    pub_p.add_argument("--dry-run", action="store_true",
                       help="Preview what would be published without making Zendesk API calls")

    # --- purge-excluded ---
    purge_p = sub.add_parser(
        "purge-excluded",
        help="Remove QC-excluded tickets (duplicate/alert/spam/etc.) from local data "
             "and clear their Zendesk QC fields. DRY-RUN by default.",
    )
    purge_p.add_argument("--execute", action="store_true",
                         help="Actually delete (default is dry-run report only)")
    purge_p.add_argument("--skip-zendesk", action="store_true",
                         help="Do not clear QC custom fields on Zendesk")
    purge_p.add_argument("--tickets", metavar="IDS",
                         help="Comma-separated ticket IDs to restrict the purge to "
                              "(must still match an exclusion rule)")

    # --- status ---
    sub.add_parser("status", help="Show pipeline health summary")

    # --- audit ---
    audit_p = sub.add_parser("audit", help="Find tickets that are unevaluated or unpublished")
    audit_p.add_argument("--from", dest="from_date", metavar="DATE",
                         help="Filter by ticket closed date on/after DATE (YYYY-MM-DD)")
    audit_p.add_argument("--to", dest="to_date", metavar="DATE",
                         help="Filter by ticket closed date on/before DATE (YYYY-MM-DD)")

    # --- export ---
    export_p = sub.add_parser("export", help="Export evaluations to CSV without publishing")
    export_p.add_argument("--from", dest="from_date", metavar="DATE",
                          help="Filter by ticket closed date on/after DATE (YYYY-MM-DD)")
    export_p.add_argument("--to", dest="to_date", metavar="DATE",
                          help="Filter by ticket closed date on/before DATE (YYYY-MM-DD)")
    export_p.add_argument("--format", dest="fmt", choices=["wide", "long", "both"], default="both",
                          help="CSV format: wide (one row/ticket), long (one row/metric), or both (default)")

    return parser.parse_args()


async def _main(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(config.logging)

    import logging
    logger = logging.getLogger(__name__)
    logger.info("Ticket Evaluator starting — command: %s", args.command)

    orchestrator = Orchestrator(config)

    try:
        if args.command == "run":
            if args.fetch_only:
                stats = await orchestrator.run_fetch_only()
            else:
                stats = await orchestrator.run(
                    force=args.force,
                    from_date=getattr(args, "from_date", None),
                    to_date=getattr(args, "to_date", None),
                )
            mode_label = "Backfill" if getattr(args, "from_date", None) else "Done"
            print(f"\n{mode_label}. fetched={stats.fetched} evaluated={stats.evaluated} "
                  f"published={stats.published} excluded={stats.excluded} errors={stats.errors}")
            return 1 if stats.errors > 0 else 0

        elif args.command == "re-evaluate":
            ticket_ids = None
            from_date = None
            to_date = None
            if args.tickets:
                ticket_ids = [int(t.strip()) for t in args.tickets.split(",")]
            elif args.all:
                pass  # no filter
            else:
                from_date = args.from_date
                to_date = getattr(args, "to_date", None)

            stats = await orchestrator.re_evaluate(
                from_date=from_date,
                to_date=to_date,
                ticket_ids=ticket_ids,
            )
            print(f"\nRe-evaluation done. evaluated={stats.evaluated} "
                  f"published={stats.published} excluded={stats.excluded} errors={stats.errors}")
            return 1 if stats.errors > 0 else 0

        elif args.command == "publish":
            if args.unpublished:
                stats = await orchestrator.publish_unpublished(
                    from_date=getattr(args, "from_date", None),
                    to_date=getattr(args, "to_date", None),
                    dry_run=args.dry_run,
                )
                prefix = "[DRY RUN] " if args.dry_run else ""
                print(f"\n{prefix}Publish done. published={stats.published} errors={stats.errors}")
                return 1 if stats.errors > 0 else 0

        elif args.command == "purge-excluded":
            ticket_ids = None
            if args.tickets:
                ticket_ids = [int(t.strip()) for t in args.tickets.split(",")]
            purge_stats = await orchestrator.purge_excluded(
                execute=args.execute,
                clear_zendesk=not args.skip_zendesk,
                ticket_ids=ticket_ids,
            )
            return 1 if purge_stats.zendesk_failed > 0 else 0

        elif args.command == "status":
            info = orchestrator.get_status()
            print("\n=== Ticket Evaluator Status ===")
            print(f"  Last run:            {info.get('last_run_at') or 'never'}")
            print(f"  Last successful run: {info.get('last_successful_run_at') or 'never'}")
            print(f"  Last ticket closed:  {info.get('last_ticket_updated_at') or 'unknown'}")
            print(f"  Cursor set:          {'yes' if info.get('cursor_set') else 'no (will use initial_fetch_from)'}")
            print(f"\n  Tickets in DB:       {info.get('tickets_total', 0)}")
            print(f"  Evaluations (latest):{info.get('evals_total', 0)}")
            print(f"  Unpublished:         {info.get('unpublished', 0)}")
            last_stats = info.get("last_run_stats") or {}
            if last_stats:
                print(f"\n  Last run stats: mode={last_stats.get('mode')} "
                      f"fetched={last_stats.get('fetched')} evaluated={last_stats.get('evaluated')} "
                      f"published={last_stats.get('published')} errors={last_stats.get('errors')}")
            runs = info.get("recent_runs", [])
            if runs:
                print(f"\n  Recent runs (last {len(runs)}):")
                for r in runs:
                    completed = r.get("completed_at", "in-progress")[:19] if r.get("completed_at") else "in-progress"
                    print(f"    [{r.get('id')}] {r.get('started_at', '')[:19]}  mode={r.get('mode')}  "
                          f"fetched={r.get('fetched')} eval={r.get('evaluated')} "
                          f"pub={r.get('published')} err={r.get('errors')}  completed={completed}")
            return 0

        elif args.command == "audit":
            from_date = getattr(args, "from_date", None)
            to_date = getattr(args, "to_date", None)
            rows = orchestrator.get_audit(from_date=from_date, to_date=to_date)
            if not rows:
                print("No tickets found for the given criteria.")
                return 0

            unevaluated = [r for r in rows if not r["evaluated"]]
            unpublished = [r for r in rows if r["evaluated"] and not r["published"]]

            print(f"\n=== Audit ({len(rows)} tickets total) ===")
            print(f"  Unevaluated: {len(unevaluated)}")
            print(f"  Evaluated but unpublished: {len(unpublished)}")
            print(f"  Complete (evaluated + published): {len(rows) - len(unevaluated) - len(unpublished)}")

            if unevaluated:
                print(f"\nUnevaluated tickets ({len(unevaluated)}):")
                print(f"  {'ticket_id':<12} {'closed_at':<22} {'channel':<10} agent")
                for r in unevaluated:
                    print(f"  {r['ticket_id']:<12} {(r['closed_at'] or '')[:19]:<22} "
                          f"{(r['channel'] or ''):<10} {r['agent_name'] or ''}")

            if unpublished:
                print(f"\nUnpublished evaluations ({len(unpublished)}):")
                print(f"  {'ticket_id':<12} {'closed_at':<22} {'score':<8} band")
                for r in unpublished:
                    score = f"{r['aggregate_score']:.2f}" if r['aggregate_score'] is not None else "?"
                    print(f"  {r['ticket_id']:<12} {(r['closed_at'] or '')[:19]:<22} "
                          f"{score:<8} {r['performance_band'] or ''}")

            return 0

        elif args.command == "export":
            from_date = getattr(args, "from_date", None)
            to_date = getattr(args, "to_date", None)
            count = orchestrator.export_csv(
                from_date=from_date,
                to_date=to_date,
                fmt=args.fmt,
            )
            print(f"\nExported {count} evaluation(s) to {config.output.exports_dir}/")
            return 0

    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 130
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("Fatal error: %s", exc)
        print(f"\nFatal error: {exc}", file=sys.stderr)
        return 1

    return 0


def main() -> None:
    args = _parse_args()
    exit_code = asyncio.run(_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
