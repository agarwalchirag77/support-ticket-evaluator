#!/usr/bin/env python3
"""Ticket Evaluation Tool — CLI entry point.

Usage:
  python src/main.py run                          # Full daily pipeline
  python src/main.py run --fetch-only             # Fetch tickets only
  python src/main.py run --force                  # Force re-run (ignore skip cache)
  python src/main.py re-evaluate --from 2025-01-01 --to 2025-06-01
  python src/main.py re-evaluate --tickets 67207,67258,64557
  python src/main.py re-evaluate --all
  python src/main.py publish --unpublished        # Re-push failed write-backs
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
                stats = await orchestrator.run(force=args.force)
            print(f"\nDone. fetched={stats.fetched} evaluated={stats.evaluated} "
                  f"published={stats.published} errors={stats.errors}")
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
                  f"published={stats.published} errors={stats.errors}")
            return 1 if stats.errors > 0 else 0

        elif args.command == "publish":
            if args.unpublished:
                stats = await orchestrator.publish_unpublished()
                print(f"\nPublish done. published={stats.published} errors={stats.errors}")
                return 1 if stats.errors > 0 else 0

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
