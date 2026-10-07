#!/usr/bin/env python3
"""
backfill_knowledge_layer.py — offline, idempotent, zero-API-cost CLI that
runs knowledge_projection.project_run() against an EXISTING intelligence
store database for one or more run_ids already persisted there.

This script makes NO network/API calls, reads story_artifacts/briefs rows
that are already on disk, and writes only to the additive knowledge-layer
tables (propositions / knowledge_links) defined in
intelligence_store.py. Safe to run repeatedly against the same db_path/
run_id -- see knowledge_projection.py's module docstring for the
idempotency guarantee (deterministic fingerprint primary keys +
INSERT OR REPLACE).

Usage:
    python3 backfill_knowledge_layer.py --db-path PATH --run-id RUN_ID [--run-id RUN_ID ...]
    python3 backfill_knowledge_layer.py --db-path PATH --all-runs
"""

import argparse
import json
import sqlite3
import sys

import intelligence_store as store
import knowledge_projection as kp


def _all_run_ids(db_path: str) -> list:
    conn = store.get_connection(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT run_id FROM story_artifacts ORDER BY run_id").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True,
                         help="Path to an EXISTING intelligence store database (never created by this script).")
    parser.add_argument("--run-id", action="append", default=[],
                         help="A run_id to backfill. May be repeated. Mutually exclusive with --all-runs.")
    parser.add_argument("--all-runs", action="store_true",
                         help="Backfill every run_id present in story_artifacts at --db-path.")
    args = parser.parse_args()

    if not args.run_id and not args.all_runs:
        parser.error("supply at least one --run-id, or --all-runs")
    if args.run_id and args.all_runs:
        parser.error("--run-id and --all-runs are mutually exclusive")

    try:
        sqlite3.connect(f"file:{args.db_path}?mode=rw", uri=True).close()
    except sqlite3.OperationalError:
        print(f"ERROR: {args.db_path!r} does not exist -- this script never creates a new store.",
              file=sys.stderr)
        return 2

    run_ids = args.run_id if args.run_id else _all_run_ids(args.db_path)
    if not run_ids:
        print(f"No run_id found in story_artifacts at {args.db_path!r}. Nothing to backfill.")
        return 0

    summaries = []
    for run_id in run_ids:
        summary = kp.project_run(run_id, db_path=args.db_path)
        summaries.append(summary)
        print(f"run_id={run_id!r}: stories={summary['story_ids']} "
              f"propositions_written={summary['total_propositions_written']} "
              f"links_written={summary['total_links_written']} "
              f"(brief_links_written={summary['brief_links_written']})")
        for per_story in summary["per_story"]:
            print(f"    story_id={per_story['story_id']!r} stages_seen={per_story['stages_seen']} "
                  f"propositions={per_story['propositions_written']} by_role={per_story['by_role']} links={per_story['links_written']}")

    print(json.dumps(summaries, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
