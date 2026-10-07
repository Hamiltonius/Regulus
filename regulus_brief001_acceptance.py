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
import corpus_analyst
import evidence_analyst
import intelligence_analyst_pass2 as pass2
import intelligence_editor as editor
import intelligence_store as store
import regulus_orchestrator as orch

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixtures")
CORPUS_FIXTURE_PATH = os.path.join(FIXTURES_DIR, "corpus_acceptance_3.json")

DEFAULT_RUN_ID = "brief001-acceptance-1"
DEFAULT_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brief_acceptance_runs")

# ---------------------------------------------------------------------------
# CONSERVATIVE ACCEPTANCE BUDGET DEFAULTS -- added per the reliability/cost-
# control patch (see regulus_orchestrator.RunBudget / CircuitBreaker
# docstrings for exact semantics). These are deliberately TIGHT, not
# generous: by default this script cannot complete a full, fully-healthy
# Acceptance #3 run (11 candidate stories) in one invocation -- it will
# hit max_total_llm_calls=4 after roughly one story's worth of calls and
# fail closed (run_budget_exhausted), persisting whatever it got and
# stopping. This is intentional: these are ACCEPTANCE defaults for
# controlled, incremental, human-supervised runs after the live incident
# this patch responds to (an uncontrolled run silently burned ~$5 before
# it was manually aborted) -- not a throughput setting and not a global
# production limit (regulus_orchestrator.run_intelligence_cycle's own
# default remains unlimited; nothing here changes that). A full run is
# expected to require either several resumed invocations against the
# SAME --db-path (each one picking up where the last left off via the
# resume/reuse mechanism) or an explicit, wider budget passed via CLI
# flags. Nothing here silently raises the ceiling.
# ---------------------------------------------------------------------------
DEFAULT_MAX_TOTAL_LLM_CALLS = 4
DEFAULT_MAX_EVIDENCE_ANALYST_CALLS = 1
DEFAULT_MAX_EVIDENCE_ATTEMPTS_PER_STORY = 1
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 2


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
                "evidence_error_category": s.evidence_error_category,
                "evidence_reused": s.evidence_reused,
                "pass2_is_valid": s.pass2_is_valid,
                "pass2_failure_reason": s.pass2_failure_reason,
                "pass2_editor_eligibility": s.pass2_editor_eligibility,
                "pass2_reused": s.pass2_reused,
                "exception": s.exception,
            }
            for s in outcome.stories
        ],
        "editor_inputs_count": outcome.editor_inputs_count,
        "brief_id": outcome.brief_id,
        "brief_skipped_reason": outcome.brief_skipped_reason,
        "budget_exhausted_reason": outcome.budget_exhausted_reason,
        "budget_summary": outcome.budget_summary,
        "circuit_breaker": outcome.circuit_breaker,
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
    print(f"  budget_exhausted_reason  : {outcome.budget_exhausted_reason}")
    if outcome.budget_summary is not None:
        print(f"  budget_summary           : {outcome.budget_summary}")
    if outcome.circuit_breaker is not None:
        print(f"  circuit_breaker          : {outcome.circuit_breaker}")
    if outcome.brief_outcome is not None:
        print(f"  brief_id                 : {outcome.brief_id}")
        print(f"  brief validation_status  : {outcome.brief_outcome.validation_status}")
        print(f"  brief failure_reason     : {outcome.brief_outcome.failure_reason}")
    print(f"  diagnostic artifact      : {out_path}")


def _print_budget_plan(budget: orch.RunBudget, circuit_breaker: orch.CircuitBreaker, *, db_path: str) -> None:
    """Print the configured call budget BEFORE any execution (live or
    dry-run) -- required so a human watching stdout can abort before any
    request is made if the configured budget looks wrong."""
    print("Regulus Intelligence Brief #001 acceptance -- configured run budget (enforced BEFORE "
          "each request is initiated, not after):")
    print(f"  max_total_llm_calls              : {budget.max_total_llm_calls}")
    print(f"  max_evidence_analyst_calls        : {budget.max_evidence_analyst_calls}")
    print(f"  max_evidence_attempts_per_story    : {budget.max_evidence_attempts_per_story}")
    print(f"  circuit_breaker_consecutive_threshold: {circuit_breaker.consecutive_failure_threshold}")
    print(f"  intelligence_store_db_path        : {db_path}")


def _dry_run_report(run_id: str, *, db_path: str, budget: orch.RunBudget,
                     circuit_breaker: orch.CircuitBreaker) -> dict:
    """Report what a real run WOULD do, making ZERO API requests.

    Loads the corpus (a local, read-only fixture load -- not a model
    call) and checks intelligence_store for any already-persisted,
    reusable state for this exact run_id at this exact db_path (only
    present if --db-path points at a db file from a PRIOR run of this
    same run_id -- the default fresh-timestamped db_path never has any).

    If no persisted Corpus Analyst output exists for this run_id, the
    candidate stories are genuinely unknown without a live call -- this
    function does not guess or fabricate them; it reports that plainly
    along with the worst-case call volume bound by the configured
    EVIDENCE_ANALYST/PASS2/EDITOR_MAX_ATTEMPTS constants and the budget.
    """
    print(f"\n[DRY RUN] run_id={run_id!r} -- no API request will be made.")
    _print_budget_plan(budget, circuit_breaker, db_path=db_path)

    corpus, _ = load_golden_corpus()  # local fixture load only -- no API call

    conn = None
    persisted_corpus_artifacts = []
    if os.path.exists(db_path):
        conn = store.get_connection(db_path)
        persisted_corpus_artifacts = store.list_story_artifacts(run_id, stage="corpus_analyst", conn=conn)

    report: dict = {
        "run_id": run_id,
        "db_path": db_path,
        "db_path_exists": os.path.exists(db_path),
        "budget": budget.summary(),
        "circuit_breaker_consecutive_threshold": circuit_breaker.consecutive_failure_threshold,
        "corpus_analyst": None,
        "stories": [],
        "would_call_editor": None,
        "max_possible_calls_worst_case": None,
        "max_possible_calls_budget_ceiling": budget.max_total_llm_calls,
    }

    if not persisted_corpus_artifacts:
        print("  corpus_analyst   : no persisted output found for this run_id at this db_path -- "
              "candidate stories are unknown without a live call.")
        report["corpus_analyst"] = {
            "resolved": False,
            "note": "no persisted artifact -- a real run would call Corpus Analyst Pass #1 "
                    f"(up to {corpus_analyst.CORPUS_ANALYST_MAX_ATTEMPTS} attempts) before anything "
                    "else is knowable",
        }
        # Worst case with zero prior state: Corpus Analyst's own attempts,
        # plus nothing else is computable yet (candidate_stories unknown).
        report["max_possible_calls_worst_case"] = corpus_analyst.CORPUS_ANALYST_MAX_ATTEMPTS
        print(f"  max possible calls (worst case, corpus unresolved): "
              f"{report['max_possible_calls_worst_case']} (Corpus Analyst attempts only -- "
              f"nothing further is knowable before that call)")
        if conn is not None:
            conn.close()
        return report

    # A persisted corpus_analyst artifact exists -- use its candidate
    # stories (exactly as the real run would via intelligence_store) to
    # report per-story reuse eligibility, with zero model calls.
    candidate_stories = [rec.payload for rec in persisted_corpus_artifacts if rec.is_valid]
    report["corpus_analyst"] = {"resolved": True, "candidate_story_count": len(candidate_stories)}
    print(f"  corpus_analyst   : persisted, valid output found ({len(candidate_stories)} candidate stories)")

    worst_case = 0
    any_would_call_editor_input = False
    for candidate_story in candidate_stories:
        story_id = candidate_story.get("story_id", "UNKNOWN")
        evidence_fp = store.compute_fingerprint(candidate_story)
        reusable_evidence = store.find_reusable_story_artifact(
            run_id, story_id, "evidence_analyst", evidence_fp, conn=conn,
        )
        story_report = {"story_id": story_id}
        if reusable_evidence is not None:
            story_report["evidence_analyst"] = "would_reuse"
            evidence_payload = reusable_evidence.payload
        else:
            story_report["evidence_analyst"] = "would_call"
            evidence_payload = None
            worst_case += evidence_analyst.EVIDENCE_ANALYST_MAX_ATTEMPTS

        if evidence_payload is not None:
            pass2_fp = store.compute_fingerprint(candidate_story, evidence_payload)
            reusable_pass2 = store.find_reusable_story_artifact(run_id, story_id, "pass2", pass2_fp, conn=conn)
            if reusable_pass2 is not None:
                story_report["pass2"] = "would_reuse"
                any_would_call_editor_input = True
            else:
                story_report["pass2"] = "would_call"
                worst_case += pass2.PASS2_MAX_ATTEMPTS
                any_would_call_editor_input = True
        else:
            story_report["pass2"] = "unknown_pending_evidence_call"
            worst_case += pass2.PASS2_MAX_ATTEMPTS  # conservative: assume it would run too

        report["stories"].append(story_report)
        print(f"    - {story_id}: evidence_analyst={story_report['evidence_analyst']}, "
              f"pass2={story_report['pass2']}")

    if any_would_call_editor_input:
        worst_case += editor.EDITOR_MAX_ATTEMPTS
        report["would_call_editor"] = True
    else:
        report["would_call_editor"] = False

    report["max_possible_calls_worst_case"] = worst_case
    print(f"  max possible calls (worst case, all own-retries exhausted): {worst_case}")
    if budget.max_total_llm_calls is not None:
        print(f"  budget ceiling (max_total_llm_calls)                      : {budget.max_total_llm_calls}")

    if conn is not None:
        conn.close()
    return report


def run_acceptance(run_id: str = DEFAULT_RUN_ID, *,
                    reporting_period: dict = None,
                    output_dir: str = DEFAULT_OUTPUT_DIR,
                    api_key: str = None,
                    db_path: str = None,
                    budget: "orch.RunBudget" = None,
                    circuit_breaker: "orch.CircuitBreaker" = None,
                    dry_run: bool = False) -> str:
    """Run one full, LIVE Regulus intelligence cycle over the golden
    Acceptance #3 corpus, persist it to a fresh timestamped SQLite file
    under output_dir (or to `db_path`, when given, enabling resume across
    runs of the SAME run_id), write a diagnostic JSON artifact, and
    return the artifact's path.

    `budget` and `circuit_breaker` default to the conservative acceptance
    defaults (DEFAULT_* constants above) when not supplied -- NOT to
    unlimited -- so invoking this function (or the CLI) with no extra
    flags always runs under a bounded budget; raising any limit requires
    an explicit, separate argument.

    If `dry_run` is True, makes ZERO API requests: prints and returns the
    dry-run report from _dry_run_report() and does not call
    run_intelligence_cycle at all.

    Makes MULTIPLE real Anthropic API calls when NOT in dry-run mode (see
    module docstring) -- this is the live acceptance path, not a test.
    Never writes to bis_watcher.db / regulus_v3.DB_PATH, never sends
    email, never re-runs corpus extraction from the production alerts
    table.
    """
    budget = budget if budget is not None else orch.RunBudget(
        max_total_llm_calls=DEFAULT_MAX_TOTAL_LLM_CALLS,
        max_evidence_analyst_calls=DEFAULT_MAX_EVIDENCE_ANALYST_CALLS,
        max_evidence_attempts_per_story=DEFAULT_MAX_EVIDENCE_ATTEMPTS_PER_STORY,
    )
    circuit_breaker = circuit_breaker if circuit_breaker is not None else orch.CircuitBreaker(
        consecutive_failure_threshold=DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
    )

    os.makedirs(output_dir, exist_ok=True)
    if db_path is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        db_path = os.path.join(output_dir, f"regulus_intelligence_acceptance_{run_id}_{timestamp}.db")

    if dry_run:
        return _dry_run_report(run_id, db_path=db_path, budget=budget, circuit_breaker=circuit_breaker)

    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is required to run the live Regulus Intelligence Brief #001 "
            "acceptance run (set the environment variable or pass api_key). This check runs "
            "BEFORE any network call -- nothing is contacted if this raises."
        )

    corpus, fixture_reporting_period = load_golden_corpus()
    reporting_period = reporting_period or fixture_reporting_period

    _print_budget_plan(budget, circuit_breaker, db_path=db_path)

    started_at = datetime.now(timezone.utc).isoformat()
    # Every call_* parameter is left at its default (None) -- each stage
    # uses its OWN real live caller. No stub, no hardcoded expectation.
    outcome = orch.run_intelligence_cycle(
        run_id, corpus, reporting_period, api_key=api_key, db_path=db_path,
        budget=budget, circuit_breaker=circuit_breaker,
    )
    finished_at = datetime.now(timezone.utc).isoformat()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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
    parser.add_argument("--db-path", default=None,
                         help="use this SQLite file instead of a fresh timestamped one -- required to "
                              "actually resume/reuse a PRIOR run of the same --run-id; the default "
                              "(omitted) always starts a brand-new db file, so resume has nothing to "
                              "reuse unless this points at the previous run's db file")
    parser.add_argument("--max-total-llm-calls", type=int, default=DEFAULT_MAX_TOTAL_LLM_CALLS,
                         help=f"run-level ceiling on total LLM calls initiated, across every stage "
                              f"(default: {DEFAULT_MAX_TOTAL_LLM_CALLS}, the conservative acceptance "
                              f"budget -- pass a higher value explicitly to allow more)")
    parser.add_argument("--max-evidence-analyst-calls", type=int, default=DEFAULT_MAX_EVIDENCE_ANALYST_CALLS,
                         help=f"ceiling on Evidence Analyst calls across the whole run "
                              f"(default: {DEFAULT_MAX_EVIDENCE_ANALYST_CALLS})")
    parser.add_argument("--max-evidence-attempts-per-story", type=int,
                         default=DEFAULT_MAX_EVIDENCE_ATTEMPTS_PER_STORY,
                         help=f"ceiling on Evidence Analyst call attempts for any ONE story "
                              f"(default: {DEFAULT_MAX_EVIDENCE_ATTEMPTS_PER_STORY})")
    parser.add_argument("--circuit-breaker-threshold", type=int, default=DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
                         help=f"number of consecutive, identical-failure-class Evidence Analyst "
                              f"failures across different stories that stops all further Evidence "
                              f"Analyst calls for the run (default: {DEFAULT_CIRCUIT_BREAKER_THRESHOLD})")
    parser.add_argument("--dry-run", action="store_true",
                         help="report what a real run would call or reuse, and the maximum possible "
                              "call volume under the configured budget -- makes ZERO API requests and "
                              "does not execute the cycle")
    args = parser.parse_args()

    reporting_period = None
    if args.reporting_period_start or args.reporting_period_end:
        _, fixture_reporting_period = load_golden_corpus()
        reporting_period = {
            "start": args.reporting_period_start or fixture_reporting_period["start"],
            "end": args.reporting_period_end or fixture_reporting_period["end"],
        }

    budget = orch.RunBudget(
        max_total_llm_calls=args.max_total_llm_calls,
        max_evidence_analyst_calls=args.max_evidence_analyst_calls,
        max_evidence_attempts_per_story=args.max_evidence_attempts_per_story,
    )
    circuit_breaker = orch.CircuitBreaker(consecutive_failure_threshold=args.circuit_breaker_threshold)

    run_acceptance(
        args.run_id, reporting_period=reporting_period, output_dir=args.output_dir,
        db_path=args.db_path, budget=budget, circuit_breaker=circuit_breaker, dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
