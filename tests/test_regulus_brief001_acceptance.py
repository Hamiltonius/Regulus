#!/usr/bin/env python3
"""
Tests for regulus_brief001_acceptance.py's reliability/cost-control
surface added in the budget/resume/circuit-breaker patch:
  - --dry-run makes ZERO network calls and reports the configured budget,
    resumable/reusable state (when any is persisted), and a worst-case
    call-volume bound.
  - run_acceptance() defaults to the CONSERVATIVE acceptance budget (not
    unlimited) when budget/circuit_breaker are not explicitly supplied.
  - the budget plan is printed before any execution.

requests.post is monkeypatched to raise if ever actually invoked, as an
additional, stronger guarantee beyond "dry_run never calls
run_intelligence_cycle".

Run: python3 tests/test_regulus_brief001_acceptance.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == "FAIL" else ""))
    return condition


import requests as _requests_module

_real_requests_post = _requests_module.post
_network_call_attempted = {"flag": False}


def _guard_requests_post(*args, **kwargs):
    _network_call_attempted["flag"] = True
    raise AssertionError(
        "test_regulus_brief001_acceptance.py attempted a REAL network POST -- "
        "dry-run (and this test file) must never make one"
    )


_requests_module.post = _guard_requests_post

import regulus_brief001_acceptance as acc
import regulus_orchestrator as orch
import intelligence_store as store

_TMPDIR = tempfile.mkdtemp(prefix="regulus_acceptance_test_")


def _fresh_dir(name):
    path = os.path.join(_TMPDIR, name)
    os.makedirs(path, exist_ok=True)
    return path


# ===========================================================================
# A. --dry-run with no prior persisted state: makes zero calls, reports the
# configured budget, and reports the Corpus-Analyst-only worst case since
# candidate stories are genuinely unknown without a live call.
# ===========================================================================
out_dir = _fresh_dir("a")
report = acc.run_acceptance("dryrun-a", output_dir=out_dir, dry_run=True)
check("A1. dry_run returns a dict report (not a file path, since nothing was executed)",
      isinstance(report, dict))
check("A2. report includes the configured budget summary",
      report["budget"]["max_total_llm_calls"] == acc.DEFAULT_MAX_TOTAL_LLM_CALLS
      and report["budget"]["max_evidence_analyst_calls"] == acc.DEFAULT_MAX_EVIDENCE_ANALYST_CALLS
      and report["budget"]["max_evidence_attempts_per_story"] == acc.DEFAULT_MAX_EVIDENCE_ATTEMPTS_PER_STORY)
check("A3. with no persisted corpus_analyst artifact, corpus_analyst.resolved is False",
      report["corpus_analyst"]["resolved"] is False)
check("A4. worst-case call volume with corpus unresolved is exactly CORPUS_ANALYST_MAX_ATTEMPTS",
      report["max_possible_calls_worst_case"] == acc.corpus_analyst.CORPUS_ANALYST_MAX_ATTEMPTS)
check("A5. no story-level entries are reported (nothing is knowable yet)",
      report["stories"] == [])
check("A6. a db file is NOT created by a dry run with no prior state "
      "(dry-run never calls store.get_connection unless a db file already exists)",
      not os.path.exists(report["db_path"]))

# ===========================================================================
# B. --dry-run with persisted, reusable prior state (simulating a resumed
# run): reports per-story reuse eligibility with zero calls, and a tighter
# worst-case bound than a cold start.
# ===========================================================================
out_dir = _fresh_dir("b")
db_path = os.path.join(out_dir, "resume.db")
acc3_path = os.path.join(acc.FIXTURES_DIR, "corpus_analyst_acceptance_3.json")
with open(acc3_path, "r", encoding="utf-8") as f:
    acc3 = json.load(f)
story0 = acc3["candidate_stories"][0]
story0_id = story0["story_id"]

conn = store.get_connection(db_path)
store.save_story_artifact("dryrun-b", story0_id, "corpus_analyst", story0, is_valid=True, conn=conn)
fp = store.compute_fingerprint(story0)
store.save_story_artifact("dryrun-b", story0_id, "evidence_analyst", {"story_id": story0_id, "synthetic": True},
                           is_valid=True, input_fingerprint=fp, conn=conn)
conn.close()

report = acc.run_acceptance("dryrun-b", output_dir=out_dir, db_path=db_path, dry_run=True)
check("B1. corpus_analyst.resolved is True once a persisted artifact exists",
      report["corpus_analyst"]["resolved"] is True
      and report["corpus_analyst"]["candidate_story_count"] == 1)
story_reports = {s["story_id"]: s for s in report["stories"]}
check("B2. the story with a valid, fingerprint-matching persisted Evidence artifact reports "
      "evidence_analyst='would_reuse' (no call needed)",
      story_reports[story0_id]["evidence_analyst"] == "would_reuse")
check("B3. that same story's Pass #2 (never persisted) reports pass2='would_call'",
      story_reports[story0_id]["pass2"] == "would_call")
check("B4. worst-case call volume accounts for only Pass #2 + Editor attempts "
      "(Evidence Analyst is reused, so excluded from the worst case)",
      report["max_possible_calls_worst_case"] == acc.pass2.PASS2_MAX_ATTEMPTS + acc.editor.EDITOR_MAX_ATTEMPTS)
check("B5. would_call_editor is True (at least one story has evidence)",
      report["would_call_editor"] is True)

# ===========================================================================
# C. run_acceptance() defaults to the CONSERVATIVE acceptance budget (never
# unlimited) when budget/circuit_breaker are not supplied -- verified via
# the dry-run report, which reflects exactly what a live run would use.
# ===========================================================================
out_dir = _fresh_dir("c")
report = acc.run_acceptance("dryrun-c", output_dir=out_dir, dry_run=True)
check("C1. omitting budget/circuit_breaker entirely still yields the conservative defaults, "
      "never an unbounded (None) run budget",
      report["budget"]["max_total_llm_calls"] is not None
      and report["budget"]["max_evidence_analyst_calls"] is not None
      and report["budget"]["max_evidence_attempts_per_story"] is not None)
check("C2. the circuit breaker threshold defaults to DEFAULT_CIRCUIT_BREAKER_THRESHOLD",
      report["circuit_breaker_consecutive_threshold"] == acc.DEFAULT_CIRCUIT_BREAKER_THRESHOLD)

# A caller CAN explicitly raise the budget above the conservative default --
# proving the default is a default, not a hard ceiling baked into the code.
out_dir = _fresh_dir("c2")
custom_budget = orch.RunBudget(max_total_llm_calls=999, max_evidence_analyst_calls=999,
                                max_evidence_attempts_per_story=999)
report = acc.run_acceptance("dryrun-c2", output_dir=out_dir, dry_run=True, budget=custom_budget)
check("C3. an explicitly-supplied budget overrides the conservative default",
      report["budget"]["max_total_llm_calls"] == 999)

# ===========================================================================
# D. ANTHROPIC_API_KEY is never required for a dry run (it makes no call) --
# the live path still requires it (unchanged pre-existing behavior), but
# dry-run must work with no key configured at all.
# ===========================================================================
_had_key = os.environ.pop("ANTHROPIC_API_KEY", None)
try:
    out_dir = _fresh_dir("d")
    report = acc.run_acceptance("dryrun-d", output_dir=out_dir, dry_run=True, api_key=None)
    check("D1. dry-run succeeds with no ANTHROPIC_API_KEY configured at all", isinstance(report, dict))
finally:
    if _had_key is not None:
        os.environ["ANTHROPIC_API_KEY"] = _had_key

# ===========================================================================
# E. Isolation / no real network call anywhere in this file
# ===========================================================================
check("E1. no real network POST was attempted anywhere in this test file",
      not _network_call_attempted["flag"])
_requests_module.post = _real_requests_post

# ===========================================================================
# Summary
# ===========================================================================
shutil.rmtree(_TMPDIR, ignore_errors=True)

failed = [r for r in results if r[1] == "FAIL"]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
if failed:
    print("FAILURES:")
    for name, status, detail in failed:
        print(f"  - {name}: {detail}")
    sys.exit(1)
sys.exit(0)
