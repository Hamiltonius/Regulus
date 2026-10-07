#!/usr/bin/env python3
"""
intelligence_pass2_acceptance_cs01.py — diagnostic acceptance runner for
the LIVE Intelligence Analyst Pass #2 (see intelligence_analyst_pass2.
run_intelligence_pass2), using CS-01 as the first live acceptance case.

THIS SCRIPT MAKES ONE REAL, PAID ANTHROPIC API CALL WHEN EXECUTED (no
web_search/tool use, no retrieval, no other network call). Per the
explicit instruction for this task, this script is built but is NOT
executed as part of this implementation task. Running it requires an
explicit, separate authorization and a real ANTHROPIC_API_KEY.

WHAT THIS SCRIPT DOES NOT DO (scope control):
  - does NOT re-run corpus collection, the Corpus Analyst, or the
    Evidence Analyst -- CS-01's original Pass #1 story is read verbatim
    from the already-committed golden fixture
    tests/fixtures/corpus_analyst_acceptance_3.json, and the Evidence
    Analyst package is read verbatim from an already-produced artifact
    file (see --evidence-package below) -- NEVER regenerated here;
  - does NOT perform any new web research;
  - does NOT write to any production database (no sqlite3 import, no
    dd_pipeline import, no due_diligence_records write of any kind);
  - does NOT send email;
  - does NOT modify any production file, table, or schema, or the golden
    fixtures themselves (read-only);
  - does NOT merge or deploy anything.

WHAT IT DOES:
  1. Load CS-01's original Pass #1 candidate_story from the golden Corpus
     Analyst fixture (tests/fixtures/corpus_analyst_acceptance_3.json),
     unmodified.
  2. Load the validated Evidence Analyst package for CS-01 from a
     supplied JSON file (--evidence-package PATH). This script does NOT
     ship or commit that artifact itself -- it must be a file already
     produced by a prior, separately-authorized
     evidence_analyst_acceptance_cs01.py live run (either its diagnostic
     artifact, from which this script extracts the "outcome.raw" package,
     or a bare Evidence Analyst output dict). If no path is given, this
     script looks for the most recently modified
     evidence_acceptance_runs/evidence_analyst_acceptance_CS-01_*.json
     file; if none is found, it exits with a clear error rather than
     fabricating or substituting anything.
  3. Call intelligence_analyst_pass2.run_intelligence_pass2(story_id,
     original_story, evidence_package, api_key=...) with its REAL default
     call_analyst (no stub) -- one real Anthropic call, no tools.
  4. Write a single diagnostic JSON artifact under
     intelligence_pass2_acceptance_runs/ containing the full outcome (raw
     output, validation status/errors, failure_reason, and every
     attempt's diagnostics) plus run metadata.
  5. Print a concise run summary to stdout.

CRITICAL: per the task spec, the five known Evidence Analyst findings for
CS-01 (the unsupported inclusion of 2026-19161, the administrative
character of the OFAC action, the remaining sanctions/export controls,
the lack of explicit interagency-coordination proof, and the new-
initiative-vs-codification distinction) are an EVALUATION RUBRIC for a
human reviewing the output afterward -- they are NOT hardcoded into this
script, into intelligence_analyst_pass2.PASS2_SYSTEM_PROMPT, or into
intelligence_analyst_pass2_schema.py's validation logic anywhere. This
script passes the model nothing beyond story_id/original_story/
evidence_package.

Run (NOT executed by this task):
    ANTHROPIC_API_KEY=... python3 intelligence_pass2_acceptance_cs01.py \\
        --evidence-package evidence_acceptance_runs/evidence_analyst_acceptance_CS-01_<ts>.json
"""

import argparse
import glob
import json
import os
from datetime import datetime, timezone

import intelligence_analyst_pass2 as pass2

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixtures")
CORPUS_ANALYST_FIXTURE_PATH = os.path.join(FIXTURES_DIR, "corpus_analyst_acceptance_3.json")

DEFAULT_STORY_ID = "CS-01"
DEFAULT_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intelligence_pass2_acceptance_runs")
DEFAULT_EVIDENCE_RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence_acceptance_runs")


def load_golden_candidate_story(story_id: str, path: str = CORPUS_ANALYST_FIXTURE_PATH) -> dict:
    """Load the golden, unmodified Corpus Analyst output and return the
    one candidate_story matching story_id, verbatim -- no Corpus Analyst
    call is made here, this is a read of an already-committed fixture.
    Mirrors evidence_analyst_acceptance_cs01.load_golden_candidate_story
    exactly, reimplemented here so this script has no import dependency
    on that one."""
    with open(path, "r", encoding="utf-8") as f:
        acceptance_output = json.load(f)
    for story in acceptance_output["candidate_stories"]:
        if story["story_id"] == story_id:
            return story
    raise KeyError(
        f"story_id {story_id!r} not found in {path} -- available story_ids: "
        f"{[s.get('story_id') for s in acceptance_output['candidate_stories']]}"
    )


def _find_most_recent_evidence_artifact(story_id: str, runs_dir: str = DEFAULT_EVIDENCE_RUNS_DIR) -> str:
    pattern = os.path.join(runs_dir, f"evidence_analyst_acceptance_{story_id}_*.json")
    matches = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not matches:
        raise FileNotFoundError(
            f"No Evidence Analyst acceptance artifact found matching {pattern!r}. "
            "Pass #2 requires an already-produced, validated Evidence Analyst package for "
            f"{story_id} -- pass --evidence-package PATH explicitly, or run "
            "evidence_analyst_acceptance_cs01.py first (a separate, explicitly-authorized "
            "live run) and point this script at its diagnostic artifact."
        )
    return matches[-1]


def load_evidence_package(path: str, story_id: str) -> dict:
    """Load the validated Evidence Analyst package for story_id from a
    JSON file. Accepts either:
      - a full evidence_analyst_acceptance_cs01.py diagnostic artifact
        (this script extracts obj["outcome"]["raw"]), or
      - a bare Evidence Analyst output dict (story_id/research_status/.../
        evidence_records directly at the top level).
    Raises ValueError if neither shape is recognized, or if the package's
    own story_id (when present) disagrees with story_id -- never silently
    substitutes or repairs the artifact."""
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)

    if isinstance(obj, dict) and "outcome" in obj and isinstance(obj["outcome"], dict):
        package = obj["outcome"].get("raw")
        if package is None:
            raise ValueError(
                f"{path!r} is a diagnostic artifact but outcome.raw is null -- the Evidence "
                "Analyst run it records did not produce a valid evidence package. Pass #2 "
                "requires a VALIDATED Evidence Analyst package; this artifact does not have one."
            )
    elif isinstance(obj, dict) and "evidence_records" in obj:
        package = obj
    else:
        raise ValueError(
            f"{path!r} does not look like an Evidence Analyst diagnostic artifact or a bare "
            "Evidence Analyst output (no 'outcome.raw' and no top-level 'evidence_records')."
        )

    package_story_id = package.get("story_id") if isinstance(package, dict) else None
    if package_story_id is not None and package_story_id != story_id:
        raise ValueError(
            f"Evidence package story_id {package_story_id!r} (from {path!r}) does not match "
            f"the requested story_id {story_id!r}."
        )
    return package


def _outcome_to_diagnostic_dict(story_id: str, evidence_package_path: str, outcome, *,
                                 started_at: str, finished_at: str) -> dict:
    """Serialize a full Pass2Outcome (plus run metadata) into a JSON-safe
    diagnostic dict. Never includes an API key or any request header --
    Pass2AttemptDiagnostics.to_dict() already carries no credential
    field."""
    return {
        "run_metadata": {
            "story_id": story_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "model": pass2.PASS2_MODEL,
            "max_tokens": pass2.PASS2_MAX_TOKENS,
            "timeout_seconds": pass2.PASS2_TIMEOUT_SECONDS,
            "max_attempts": pass2.PASS2_MAX_ATTEMPTS,
            "prompt_version": pass2.PROMPT_VERSION,
            "schema_version": pass2.SCHEMA_VERSION,
            "corpus_analyst_fixture": os.path.relpath(
                CORPUS_ANALYST_FIXTURE_PATH, os.path.dirname(os.path.abspath(__file__))
            ),
            "evidence_package_source": evidence_package_path,
        },
        "outcome": {
            "is_valid": outcome.is_valid,
            "validation_status": outcome.validation_status,
            "validation_errors": outcome.validation_errors,
            "failure_reason": outcome.failure_reason,
            "raw": outcome.raw,
        },
        "attempts": [a.to_dict() for a in outcome.attempts],
    }


def run_acceptance(story_id: str = DEFAULT_STORY_ID, *, evidence_package_path: str = None,
                    output_dir: str = DEFAULT_OUTPUT_DIR, api_key: str = None) -> str:
    """Run the live Intelligence Analyst Pass #2 over CS-01's original
    story and an already-produced Evidence Analyst package, and write a
    diagnostic artifact. Returns the path written.

    Makes exactly ONE real Anthropic API call (no tools) -- this is the
    live acceptance path, not a test. Never writes to a production
    database, never emails, never touches dd_pipeline/dd_schema/
    regulus_v3, never re-invokes the Corpus Analyst or the Evidence
    Analyst, never performs new research."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is required to run the live Intelligence Analyst Pass #2 "
            "acceptance run (set the environment variable or pass api_key)."
        )

    evidence_package_path = evidence_package_path or _find_most_recent_evidence_artifact(story_id)
    original_story = load_golden_candidate_story(story_id)
    evidence_package = load_evidence_package(evidence_package_path, story_id)

    started_at = datetime.now(timezone.utc).isoformat()
    outcome = pass2.run_intelligence_pass2(story_id, original_story, evidence_package, api_key=api_key)
    finished_at = datetime.now(timezone.utc).isoformat()

    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(output_dir, f"intelligence_pass2_acceptance_{story_id}_{timestamp}.json")
    diagnostic = _outcome_to_diagnostic_dict(
        story_id, evidence_package_path, outcome, started_at=started_at, finished_at=finished_at,
    )
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(diagnostic, f, indent=2)

    _print_summary(story_id, outcome, out_path)
    return out_path


def _print_summary(story_id: str, outcome, out_path: str) -> None:
    print(f"Intelligence Analyst Pass #2 live acceptance run -- story_id={story_id}")
    print(f"  validation_status      : {outcome.validation_status}")
    print(f"  failure_reason         : {outcome.failure_reason}")
    if outcome.raw:
        print(f"  assessment_disposition : {outcome.raw.get('assessment_disposition')}")
        print(f"  confidence             : {outcome.raw.get('confidence')}")
        print(f"  editor_eligibility     : {outcome.raw.get('editor_eligibility')}")
        print(f"  material_changes       : {len(outcome.raw.get('material_changes') or [])}")
        print(f"  supported_findings     : {len(outcome.raw.get('supported_findings') or [])}")
        print(f"  weakened/rejected      : {len(outcome.raw.get('weakened_or_rejected_findings') or [])}")
        print(f"  remaining_uncertainties: {len(outcome.raw.get('remaining_uncertainties') or [])}")
    print(f"  attempts made           : {len(outcome.attempts)}")
    if outcome.validation_errors:
        print(f"  validation_errors      : {len(outcome.validation_errors)} (see diagnostic artifact)")
    print(f"  diagnostic artifact     : {out_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Live Intelligence Analyst Pass #2 acceptance runner (CS-01 by default). "
                    "Makes one real, paid Anthropic API call -- run only when explicitly authorized."
    )
    parser.add_argument("--story-id", default=DEFAULT_STORY_ID,
                         help=f"candidate_story story_id to reassess (default: {DEFAULT_STORY_ID})")
    parser.add_argument("--evidence-package", default=None,
                         help="path to an already-produced Evidence Analyst artifact/package JSON "
                             "file for this story_id (defaults to the most recent matching file "
                             f"under {DEFAULT_EVIDENCE_RUNS_DIR})")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                         help="directory to write the diagnostic JSON artifact into")
    args = parser.parse_args()
    run_acceptance(args.story_id, evidence_package_path=args.evidence_package, output_dir=args.output_dir)


if __name__ == "__main__":
    main()
