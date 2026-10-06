#!/usr/bin/env python3
"""
regulus_brief001_acceptance.py — the live, end-to-end acceptance runner
for REGULUS INTELLIGENCE BRIEF #001: the first full-cycle live run of
Corpus Analyst Pass #1 -> Evidence Analyst -> Intelligence Analyst
Pass #2 -> Intelligence Editor, over the golden Acceptance #3 corpus.

THIS SCRIPT MAKES MULTIPLE REAL, PAID ANTHROPIC API CALLS WHEN EXECUTED:
one Corpus Analyst call, one Evidence Analyst call per candidate story
(bounded web_search, max_uses=10 each), one Intelligence Analyst Pass #2
call per eligible story (no tools), and one Intelligence Editor call (no
tools). Per the explicit instruction for this task, this script is built
but is NOT executed as part of this implementation task. Running it
requires an explicit, separate authorization and a real ANTHROPIC_API_KEY.

WHAT THIS SCRIPT DOES NOT DO (scope control):
  - does NOT re-run corpus EXTRACTION from the production alerts table --
    the corpus is loaded verbatim from the already-committed golden
    fixture tests/fixtures/corpus_acceptance_3.json (the same 87-
    observation corpus every other acceptance run in this project has
    used), never from regulus_v3/dd_pipeline;
  - does NOT write to the production DD database -- regulus_orchestrator.
    py / intelligence_store.py never import regulus_v3 or open
    bis_watcher.db; this script's own persistence goes to a dedicated,
    timestamped SQLite file under brief_acceptance_runs/ (see
    INTELLIGENCE_DB_PATH override below), never the default
    regulus_intelligence.db a casual orchestrator run would use, and
    never bis_watcher.db;
  - does NOT send email;
  - does NOT modify any production file, table, schema, or the golden
    fixture itself (read-only);
  - does NOT merge or deploy anything;
  - does NOT hardcode any story's expected conclusion -- every stage uses
    its REAL default live caller (no stub), exactly as a casual
    orchestrator run would.

WHAT IT DOES:
  1. Load the real, unmodified Acceptance #3 corpus (87 observations) from
     tests/fixtures/corpus_acceptance_3.json.
  2. Call regulus_orchestrator.run_intelligence_cycle(run_id, corpus,
     reporting_period, api_key=...) with every call_* parameter left at
     its default (None) -- so every stage uses its own REAL live caller:
     corpus_analyst.call_anthropic_corpus_analyst,
     evidence_analyst.call_anthropic_evidence_analyst (per story),
     intelligence_analyst_pass2.call_anthropic_pass2 (per eligible
     story), intelligence_editor.call_anthropic_editor.
  3. Persist every stage's outcome via intelligence_store.py to a fresh,
     timestamped SQLite file under brief_acceptance_runs/ (never the
     default regulus_intelligence.db, never bis_watcher.db).
  4. Write a single diagnostic JSON summary (run metadata, per-story
     outcomes, the compiled Brief if produced) under the same directory.
  5. Print a concise run summary to stdout.

Run (NOT executed by this task):
    ANTHROPIC_API_KEY=... python3 regulus_brief001_acceptance.py \\
        --run-id brief001-acceptance-1 \\
        --reporting-period-start 2026-09-14 --reporting-period-end 2026-10-05
"""

import argparse
import json
import os
from datetime import datetime, timezone

from corpus_extractor import Corpus, CorpusObservation
import regulus_orchestrator as orch

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixtures")
CORPUS_FIXTURE_PATH = os.path.join(FIXTURES_DIR, "corpus_acceptance_3.json")

DEFAULT_RUN_ID = "brief001-acceptance-1"
DEFAULT_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brief_acceptance_runs")


def load_golden_corpus(path: str = CORPUS_FIXTURE_PATH) -> Corpus:
    """Load and reconstruct the REAL corpus_extractor.Corpus used for
    Acceptance #3 from tests/fixtures/corpus_acceptance_3.json. No
    extraction call is made here -- this is a read-only load of an
    already-committed fixture. Mirrors tests/_evidence_test_helpers.
    load_corpus_acceptance_3() exactly, reimplemented here so this script
    has no import dependency on the tests/ package."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    observations = [
        CorpusObservation(
            document_number=o["document_number"], publication_date=o["publication_date"],
            effective_date=o["effective_date"], title=o["title"], agency=o["agency"],
            score=o["score"], countries=o["countries"], entities=o["entities"], eccns=o["eccns"],
            change_type=o["change_type"], summary=o["summary"], primary_source_url=o["primary_source_url"],
            tier=o["tier"], due_diligence_ran=o["due_diligence_ran"],
        )
        for o in raw["observations"]
    ]
    return Corpus(
        start_date=raw["reporting_period"]["start"], end_date=raw["reporting_period"]["end"],
        observations=observations,
    ), raw["reporting_period"]


def _outcome_to_diagnostic_dict(run_id: str, outcome: orch.OrchestrationOutcome, *,
                                 started_at: str, finished_at: str, db_path: str) -> dict:
    """Serialize a full OrchestrationOutcome (plus run metadata) into a
    JSON-safe diagnostic dict. Never includes an API key or any request
    header -- every nested *AttemptDiagnostics.to_dict() already carries
    no credential field, and this function adds none."""
    return {
        "run_metadata": {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "corpus_fixture": os.path.relpath(CORPUS_FIXTURE_PATH, os.path.dirname(os.path.abspath(__file__))),
            "intelligence_store_db_path": db_path,
        },
        "corpus_analysis_is_valid": outcome.corpus_analysis_is_valid,
        "corpus_analysis_failure_reason": outcome.corpus_analysis_failure_reason,
        "stories": [
            {
                "story_id": s.story_id,
                "included": s.included,
                "exclusion_reason": s.exclusion_reason,
                "evidence_is_valid": s.evidence_is_valid,
                "evidence_failure_reason": s.evidence_failure_reason,
                "pass2_is_valid": s.pass2_is_valid,
                "pass2_failure_reason": s.pass2_failure_reason,
                "pass2_editor_eligibility": s.pass2_editor_eligibility,
                "exception": s.exception,
            }
            for s in outcome.stories
        ],
        "editor_inputs_count": outcome.editor_inputs_count,
        "brief_id": outcome.brief_id,
        "brief_skipped_reason": outcome.brief_skipped_reason,
        "brief_outcome": (
            {
                "is_valid": outcome.brief_outcome.is_valid,
                "validation_status": outcome.brief_outcome.validation_status,
                "validation_errors": outcome.brief_outcome.validation_errors,
                "failure_reason": outcome.brief_outcome.failure_reason,
                "raw": outcome.brief_outcome.raw,
                "attempts": [a.to_dict() for a in outcome.brief_outcome.attempts],
            }
            if outcome.brief_outcome is not None else None
        ),
    }


def _print_summary(outcome: orch.OrchestrationOutcome, out_path: str) -> None:
    print(f"Regulus Intelligence Brief #001 live acceptance run -- run_id={outcome.run_id}")
    print(f"  corpus_analysis_is_valid : {outcome.corpus_analysis_is_valid}")
    print(f"  corpus_analysis_failure_reason: {outcome.corpus_analysis_failure_reason}")
    print(f"  stories processed        : {len(outcome.stories)}")
    print(f"  stories included         : {outcome.included_story_ids}")
    print(f"  stories excluded         : {outcome.excluded_story_ids}")
    for s in outcome.stories:
        if not s.included:
            print(f"    - {s.story_id} excluded: {s.exclusion_reason}")
    print(f"  editor_inputs_count      : {outcome.editor_inputs_count}")
    print(f"  brief_skipped_reason     : {outcome.brief_skipped_reason}")
    if outcome.brief_outcome is not None:
        print(f"  brief_id                 : {outcome.brief_id}")
        print(f"  brief validation_status  : {outcome.brief_outcome.validation_status}")
        print(f"  brief failure_reason     : {outcome.brief_outcome.failure_reason}")
    print(f"  diagnostic artifact      : {out_path}")


def run_acceptance(run_id: str = DEFAULT_RUN_ID, *,
                    reporting_period: dict = None,
                    output_dir: str = DEFAULT_OUTPUT_DIR,
                    api_key: str = None) -> str:
    """Run one full, LIVE Regulus intelligence cycle over the golden
    Acceptance #3 corpus, persist it to a fresh timestamped SQLite file
    under output_dir, write a diagnostic JSON artifact, and return the
    artifact's path.

    Makes MULTIPLE real Anthropic API calls (see module docstring) --
    this is the live acceptance path, not a test. Never writes to
    bis_watcher.db / regulus_v3.DB_PATH, never sends email, never re-runs
    corpus extraction from the production alerts table.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is required to run the live Regulus Intelligence Brief #001 "
            "acceptance run (set the environment variable or pass api_key). This check runs "
            "BEFORE any network call -- nothing is contacted if this raises."
        )

    corpus, fixture_reporting_period = load_golden_corpus()
    reporting_period = reporting_period or fixture_reporting_period

    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    db_path = os.path.join(output_dir, f"regulus_intelligence_acceptance_{run_id}_{timestamp}.db")

    started_at = datetime.now(timezone.utc).isoformat()
    # Every call_* parameter is left at its default (None) -- each stage
    # uses its OWN real live caller. No stub, no hardcoded expectation.
    outcome = orch.run_intelligence_cycle(run_id, corpus, reporting_period, api_key=api_key, db_path=db_path)
    finished_at = datetime.now(timezone.utc).isoformat()

    out_path = os.path.join(output_dir, f"brief001_acceptance_{run_id}_{timestamp}.json")
    diagnostic = _outcome_to_diagnostic_dict(
        run_id, outcome, started_at=started_at, finished_at=finished_at, db_path=db_path,
    )
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(diagnostic, f, indent=2)

    _print_summary(outcome, out_path)
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="Live, end-to-end REGULUS INTELLIGENCE BRIEF #001 acceptance runner. "
                    "Makes multiple real, paid Anthropic API calls -- run only when explicitly authorized."
    )
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID, help=f"run_id (default: {DEFAULT_RUN_ID})")
    parser.add_argument("--reporting-period-start", default=None,
                         help="override the corpus fixture's own reporting_period.start")
    parser.add_argument("--reporting-period-end", default=None,
                         help="override the corpus fixture's own reporting_period.end")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                         help="directory to write the SQLite store and diagnostic JSON artifact into")
    args = parser.parse_args()

    reporting_period = None
    if args.reporting_period_start or args.reporting_period_end:
        _, fixture_reporting_period = load_golden_corpus()
        reporting_period = {
            "start": args.reporting_period_start or fixture_reporting_period["start"],
            "end": args.reporting_period_end or fixture_reporting_period["end"],
        }

    run_acceptance(args.run_id, reporting_period=reporting_period, output_dir=args.output_dir)


if __name__ == "__main__":
    main()
