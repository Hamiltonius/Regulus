#!/usr/bin/env python3
"""
Offline regression tests for CS-03 diagnostic preservation: Evidence attempt
diagnostics survive into StoryCycleResult and the acceptance diagnostic JSON,
and a locally blocked budget_exhausted retry never masks the underlying failure.

No LLM/network call: requests.post raises; all stages are stubs; temp SQLite only.
Run: python3 tests/test_evidence_attempt_diagnostics.py
"""
import copy, json, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == "FAIL" else ""))
    return cond


import requests


def _no_network(*a, **k):
    raise AssertionError("tests must never make a network call")


requests.post = _no_network

import evidence_analyst as ea
import regulus_orchestrator as orch
import regulus_brief001_acceptance as acc
from _evidence_test_helpers import load_acceptance_3, get_story, load_corpus_acceptance_3
import evidence_retrieval as er

A3 = load_acceptance_3()
CORPUS = load_corpus_acceptance_3()
PERIOD = {"start": "2026-09-14", "end": "2026-10-05"}
TMP = tempfile.mkdtemp(prefix="regulus_evattempts_")
STORY = get_story(A3, "CS-03")


def corpus_stub(payload, api_key):
    raw = copy.deepcopy(A3)
    raw["candidate_stories"] = [copy.deepcopy(STORY)]
    return raw


def fake_retrieve(story, corpus, **kw):
    return er.EvidenceRetrievalBundle(story_id=story["story_id"], documents=[])


def pass2_never(*a, **k):
    raise AssertionError("Pass #2 must not run after an Evidence failure")


def editor_never(*a, **k):
    raise AssertionError("Editor must not run")


def json_decode_exc():  # exact CS-03 first attempt: pause_turn, no text -> model JSON decode error
    return ea.EvidenceAnalystJSONDecodeError(
        json.JSONDecodeError("Expecting value", "", 0), raw_text="", content_blocks=[],
        response_meta={"stop_reason": "pause_turn", "usage": {"input_tokens": 5, "output_tokens": 7}})


def api_decode_exc():
    return ea.EvidenceAnalystAPIResponseJSONDecodeError(
        json.JSONDecodeError("Expecting value", "", 0), status_code=200,
        content_type="application/json", body_length=0, body_preview="")


def cycle(name, exc_factory, budget, cb, ids_stub=None):
    def ev(story, bundle, corpus, api_key):
        raise exc_factory()
    out = orch.run_intelligence_cycle(
        name, CORPUS, PERIOD, api_key="fake", db_path=os.path.join(TMP, name + ".db"),
        call_corpus_analyst=corpus_stub, call_evidence_analyst=ev, retrieve=fake_retrieve,
        call_pass2=pass2_never, call_editor=editor_never, budget=budget, circuit_breaker=cb)
    return out


# --- 1. exact CS-03 sequence: attempt1 model_output_json_decode_error, attempt2 budget_exhausted ---
b = orch.RunBudget(max_total_llm_calls=3, max_evidence_analyst_calls=1, max_evidence_attempts_per_story=1)
cb = orch.CircuitBreaker(consecutive_failure_threshold=1)
out = cycle("cs03", json_decode_exc, b, cb)
s = out.stories[0]
cats = [a["error_category"] for a in s.evidence_attempts]
check("1a. two attempts preserved", len(s.evidence_attempts) == 2, str(cats))
check("1b. attempt cats = [model_output_json_decode_error, budget_exhausted]",
      cats == ["model_output_json_decode_error", "budget_exhausted"], str(cats))
check("1c. attempt 1 detail intact (stop_reason pause_turn, tokens)",
      s.evidence_attempts[0]["stop_reason"] == "pause_turn" and s.evidence_attempts[0]["output_tokens"] == 7)
check("1d. final category NOT masked", s.evidence_error_category == "model_output_json_decode_error",
      str(s.evidence_error_category))
check("1e. circuit breaker received underlying class",
      cb.triggered and cb.failure_class == "model_output_json_decode_error", str(cb.failure_class))
check("1f. exactly one real request made", b.summary()["evidence_analyst_calls_made"] == 1,
      str(b.summary()))

# --- 2. diagnostic JSON carries evidence_attempts ---
d = acc._outcome_to_diagnostic_dict("cs03", out, started_at="t0", finished_at="t1", db_path="x.db")
ds = json.loads(json.dumps(d))["stories"][0]
check("2a. diagnostic JSON evidence_attempts present with 2 entries", len(ds.get("evidence_attempts", [])) == 2)
check("2b. diagnostic evidence_error_category preserved",
      ds["evidence_error_category"] == "model_output_json_decode_error")

# --- 3. api_response_json_decode_error equivalent ---
if True:
    b = orch.RunBudget(max_total_llm_calls=3, max_evidence_analyst_calls=1, max_evidence_attempts_per_story=1)
    cb = orch.CircuitBreaker(consecutive_failure_threshold=1)
    s = cycle("api", api_decode_exc, b, cb).stories[0]
    check("3a. api_response_json_decode_error not masked",
          s.evidence_error_category == "api_response_json_decode_error", str(s.evidence_error_category))
    check("3b. breaker got api_response_json_decode_error", cb.failure_class == "api_response_json_decode_error")

# --- 4. helper edge cases ---
mk = lambda c: ea.EvidenceAnalystAttemptDiagnostics(attempt_number=1, request_succeeded=False, model='m', max_tokens=1, timeout_seconds=1, error_category=c)
f = orch._underlying_evidence_error_category
check("4a. all budget_exhausted stays budget_exhausted", f([mk("budget_exhausted"), mk("budget_exhausted")]) == "budget_exhausted")
check("4b. no attempts -> None", f([]) is None)
check("4c. two external failures -> last external", f([mk("transport_error"), mk("model_output_json_decode_error")]) == "model_output_json_decode_error")
check("4d. budget-first then external -> external", f([mk("budget_exhausted"), mk("transport_error")]) == "transport_error")

# --- 5. all-budget-exhausted via real cycle: budget blocks before any request ---
b = orch.RunBudget(max_total_llm_calls=3, max_evidence_analyst_calls=0, max_evidence_attempts_per_story=1)
cb = orch.CircuitBreaker(consecutive_failure_threshold=1)
s = cycle("allbudget", json_decode_exc, b, cb).stories[0]
check("5a. every attempt budget-blocked -> budget_exhausted", s.evidence_error_category == "budget_exhausted",
      str(s.evidence_error_category))
check("5b. no real request made", b.summary()["evidence_analyst_calls_made"] == 0)

# --- 6. reuse path unchanged: reused evidence carries no attempts ---
# --- 6. default / reuse path: StoryCycleResult default has no attempts ---
check("6a. StoryCycleResult default evidence_attempts == []", orch.StoryCycleResult(story_id="x", included=False).evidence_attempts == [])

failed = [r for r in results if r[1] == "FAIL"]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
if failed:
    for n, _, d_ in failed:
        print(f"  - {n}: {d_}")
    sys.exit(1)
