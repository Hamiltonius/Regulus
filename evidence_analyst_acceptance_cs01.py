#!/usr/bin/env python3
"""
evidence_analyst_acceptance_cs01.py — diagnostic acceptance runner for the
LIVE Evidence Analyst (see evidence_analyst.run_live_evidence_analysis),
using CS-01 from the golden Corpus Analyst fixture as the first live
acceptance case.

THIS SCRIPT MAKES REAL, PAID NETWORK CALLS WHEN EXECUTED:
  - real Federal Register API lookups + real PDF downloads for CS-01's
    five supporting documents (evidence_retrieval.build_evidence_source_
    material with no mocks);
  - one real Anthropic Messages API call (with the server-side web_search
    tool enabled) via evidence_analyst.call_anthropic_evidence_analyst.

Per the explicit instruction for this task, this script is built but is
NOT executed as part of this implementation task. Running it requires an
explicit, separate authorization and a real ANTHROPIC_API_KEY.

WHAT THIS SCRIPT DOES NOT DO (scope control):
  - does NOT write to any production database (no sqlite3 import, no
    dd_pipeline import, no due_diligence_records write of any kind);
  - does NOT send email;
  - does NOT modify any production file, table, or schema;
  - does NOT invoke the Corpus Analyst again -- CS-01 is read verbatim
    from the already-committed golden fixture
    tests/fixtures/corpus_analyst_acceptance_3.json, never regenerated;
  - does NOT modify the golden fixtures themselves (read-only);
  - does NOT merge or deploy anything.

WHAT IT DOES:
  1. Load the golden corpus fixture (tests/fixtures/corpus_acceptance_3.json)
     and reconstruct the real corpus_extractor.Corpus from it (same
     reconstruction logic as tests/_evidence_test_helpers.
     load_corpus_acceptance_3, reimplemented here rather than imported --
     this production script does not import anything from tests/).
  2. Load the golden Corpus Analyst fixture
     (tests/fixtures/corpus_analyst_acceptance_3.json) and select CS-01 by
     story_id -- unmodified, exactly as the Corpus Analyst produced it.
     Nothing about CS-01's own hypothesis, research_questions, or any
     human evaluation of it is injected into the Evidence Analyst's
     prompt or payload; the Evidence Analyst investigates CS-01's own
     research_questions cold, as a genuine evidence test.
  3. Call evidence_analyst.run_live_evidence_analysis(story, corpus,
     api_key=...) with its REAL default call_analyst/retrieve (no stubs)
     -- real retrieval, real model call.
  4. Write a single diagnostic JSON artifact under
     evidence_acceptance_runs/ containing the full outcome (raw output,
     validation status/errors, failure_reason, the complete retrieval
     bundle, and every attempt's diagnostics) plus run metadata.
  5. Print a concise run summary to stdout.

Run (NOT executed by this task):
    ANTHROPIC_API_KEY=... python3 evidence_analyst_acceptance_cs01.py
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import evidence_analyst
from corpus_extractor import Corpus, CorpusObservation

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixtures")
CORPUS_FIXTURE_PATH = os.path.join(FIXTURES_DIR, "corpus_acceptance_3.json")
CORPUS_ANALYST_FIXTURE_PATH = os.path.join(FIXTURES_DIR, "corpus_analyst_acceptance_3.json")

DEFAULT_STORY_ID = "CS-01"
DEFAULT_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence_acceptance_runs")


def load_golden_corpus(path: str = CORPUS_FIXTURE_PATH) -> Corpus:
    """Reconstruct the real corpus_extractor.Corpus from the golden,
    unmodified corpus_acceptance_3.json fixture. Every CorpusObservation
    field is read verbatim from the fixture -- nothing invented or
    defaulted. Mirrors tests/_evidence_test_helpers.load_corpus_
    acceptance_3 exactly, reimplemented here so this production script
    has no dependency on the tests/ package."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    observations = [
        CorpusObservation(
            document_number=o["document_number"],
            publication_date=o["publication_date"],
            effective_date=o["effective_date"],
            title=o["title"],
            agency=o["agency"],
            score=o["score"],
            countries=o["countries"],
            entities=o["entities"],
            eccns=o["eccns"],
            change_type=o["change_type"],
            summary=o["summary"],
            primary_source_url=o["primary_source_url"],
            tier=o["tier"],
            due_diligence_ran=o["due_diligence_ran"],
        )
        for o in raw["observations"]
    ]
    return Corpus(
        start_date=raw["reporting_period"]["start"],
        end_date=raw["reporting_period"]["end"],
        observations=observations,
    )


def load_golden_candidate_story(story_id: str, path: str = CORPUS_ANALYST_FIXTURE_PATH) -> dict:
    """Load the golden, unmodified Corpus Analyst output and return the
    one candidate_story matching story_id, verbatim -- no Corpus Analyst
    call is made here, this is a read of an already-committed fixture."""
    with open(path, "r", encoding="utf-8") as f:
        acceptance_output = json.load(f)
    for story in acceptance_output["candidate_stories"]:
        if story["story_id"] == story_id:
            return story
    raise KeyError(
        f"story_id {story_id!r} not found in {path} -- available story_ids: "
        f"{[s.get('story_id') for s in acceptance_output['candidate_stories']]}"
    )


def _outcome_to_diagnostic_dict(story_id: str, outcome, *, started_at: str, finished_at: str) -> dict:
    """Serialize a full EvidenceAnalysisOutcome (plus run metadata) into a
    JSON-safe diagnostic dict. Never includes an API key or any request
    header -- EvidenceAnalystAttemptDiagnostics.to_dict() and
    EvidenceRetrievalBundle.to_dict() already carry no credential field."""
    return {
        "run_metadata": {
            "story_id": story_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "model": evidence_analyst.EVIDENCE_ANALYST_MODEL,
            "max_tokens": evidence_analyst.EVIDENCE_ANALYST_MAX_TOKENS,
            "timeout_seconds": evidence_analyst.EVIDENCE_ANALYST_TIMEOUT_SECONDS,
            "max_attempts": evidence_analyst.EVIDENCE_ANALYST_MAX_ATTEMPTS,
            "web_search_max_uses": evidence_analyst.EVIDENCE_ANALYST_WEB_SEARCH_MAX_USES,
            "prompt_version": evidence_analyst.PROMPT_VERSION,
            "schema_version": evidence_analyst.EVIDENCE_ANALYST_SCHEMA_VERSION,
            "corpus_fixture": os.path.relpath(CORPUS_FIXTURE_PATH, os.path.dirname(os.path.abspath(__file__))),
            "corpus_analyst_fixture": os.path.relpath(
                CORPUS_ANALYST_FIXTURE_PATH, os.path.dirname(os.path.abspath(__file__))
            ),
        },
        "outcome": {
            "is_valid": outcome.is_valid,
            "validation_status": outcome.validation_status,
            "validation_errors": outcome.validation_errors,
            "failure_reason": outcome.failure_reason,
            "raw": outcome.raw,
        },
        "retrieval": outcome.retrieval.to_dict() if outcome.retrieval is not None else None,
        "attempts": [a.to_dict() for a in outcome.attempts],
    }


def run_acceptance(story_id: str = DEFAULT_STORY_ID, *, output_dir: str = DEFAULT_OUTPUT_DIR,
                    api_key: str = None) -> str:
    """Run the live Evidence Analyst over one golden candidate_story and
    write a diagnostic artifact. Returns the path written.

    Makes REAL retrieval + REAL Anthropic API calls (no stubs) -- this is
    the live acceptance path, not a test. Never writes to a production
    database, never emails, never touches dd_pipeline/dd_schema, never
    re-invokes the Corpus Analyst."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is required to run the live Evidence Analyst "
            "acceptance run (set the environment variable or pass api_key)."
        )

    corpus = load_golden_corpus()
    story = load_golden_candidate_story(story_id)

    started_at = datetime.now(timezone.utc).isoformat()
    outcome = evidence_analyst.run_live_evidence_analysis(story, corpus, api_key=api_key)
    finished_at = datetime.now(timezone.utc).isoformat()

    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(output_dir, f"evidence_analyst_acceptance_{story_id}_{timestamp}.json")
    diagnostic = _outcome_to_diagnostic_dict(story_id, outcome, started_at=started_at, finished_at=finished_at)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(diagnostic, f, indent=2)

    _print_summary(story_id, outcome, out_path)
    return out_path


def _print_summary(story_id: str, outcome, out_path: str) -> None:
    print(f"Evidence Analyst live acceptance run -- story_id={story_id}")
    print(f"  validation_status : {outcome.validation_status}")
    print(f"  failure_reason    : {outcome.failure_reason}")
    if outcome.raw:
        print(f"  research_status       : {outcome.raw.get('research_status')}")
        print(f"  hypothesis_assessment : {outcome.raw.get('hypothesis_assessment')}")
        print(f"  confidence            : {outcome.raw.get('confidence')}")
        print(f"  evidence_records      : {len(outcome.raw.get('evidence_records') or [])}")
        print(f"  contradictions        : {len(outcome.raw.get('contradictions') or [])}")
    if outcome.retrieval is not None:
        print(f"  documents retrieved   : {len(outcome.retrieval.retrieved_documents)}"
              f"/{len(outcome.retrieval.documents)}")
    print(f"  attempts made         : {len(outcome.attempts)}")
    if outcome.validation_errors:
        print(f"  validation_errors     : {len(outcome.validation_errors)} (see diagnostic artifact)")
    print(f"  diagnostic artifact   : {out_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Live Evidence Analyst acceptance runner (CS-01 by default). "
                    "Makes real, paid network calls -- run only when explicitly authorized."
    )
    parser.add_argument("--story-id", default=DEFAULT_STORY_ID,
                         help=f"candidate_story story_id to investigate (default: {DEFAULT_STORY_ID})")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                         help="directory to write the diagnostic JSON artifact into")
    args = parser.parse_args()
    run_acceptance(args.story_id, output_dir=args.output_dir)


if __name__ == "__main__":
    main()
