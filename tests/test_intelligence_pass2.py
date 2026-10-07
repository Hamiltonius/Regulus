#!/usr/bin/env python3
"""
Tests for intelligence_analyst_pass2.py — the Intelligence Analyst's
second pass (evidence reassessment), live-call implementation
(run_intelligence_pass2 / Pass2Outcome).

No REAL Anthropic call is made anywhere in this file. call_analyst is
always an injected stub; requests.post is monkeypatched to raise if it is
ever actually invoked, as an additional, stronger guarantee than relying
on every test remembering to pass a stub.

Run: python3 tests/test_intelligence_pass2.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == "FAIL" else ""))
    return condition


import intelligence_analyst_pass2 as p2
from _evidence_test_helpers import load_acceptance_3, get_story

ACCEPTANCE_3 = load_acceptance_3()
CS01_STORY = get_story(ACCEPTANCE_3, "CS-01")
CS01_ALT_HYPOTHESES = CS01_STORY["alternative_hypotheses"]

EVIDENCE_PACKAGE = {
    "story_id": "CS-01",
    "research_status": "partial",
    "evidence_records": [
        {"evidence_id": "EV-1", "document_number": "2026-18918"},
        {"evidence_id": "EV-2", "document_number": "2026-19161"},
    ],
}

# ===========================================================================
# Guard against any REAL network call for the whole file.
# ===========================================================================
import requests as _requests_module

_real_requests_post = _requests_module.post
_network_call_attempted = {"flag": False}


def _guard_requests_post(*args, **kwargs):
    _network_call_attempted["flag"] = True
    raise AssertionError(
        "test_intelligence_pass2.py attempted a REAL network POST -- "
        "every test must supply its own call_analyst stub"
    )


_requests_module.post = _guard_requests_post


def _valid_pass2_output(story=CS01_STORY, alt_hypotheses=None):
    alt_hypotheses = story["alternative_hypotheses"] if alt_hypotheses is None else alt_hypotheses
    alt_assessments = [
        {
            "hypothesis": h,
            "disposition": "unresolved" if i == 0 else "weakened",
            "explanation": "Synthetic explanation.",
            "evidence_ids": ["EV-1"] if i == 0 else [],
        }
        for i, h in enumerate(alt_hypotheses)
    ]
    return {
        "story_id": "CS-01",
        "assessment_disposition": "confirmed_with_modification",
        "original_hypothesis": story["preliminary_hypothesis"],
        "revised_hypothesis": "Synthetic revised hypothesis text.",
        "material_changes": [
            {
                "original_claim": "Synthetic original claim.",
                "disposition": "modified",
                "revised_claim": "Synthetic revised claim.",
                "reason": "Synthetic reason citing evidence.",
                "evidence_ids": ["EV-1", "EV-2"],
            }
        ],
        "supported_findings": ["Synthetic supported finding."],
        "weakened_or_rejected_findings": ["Synthetic weakened finding."],
        "remaining_uncertainties": ["Synthetic remaining uncertainty."],
        "alternative_hypotheses_assessment": alt_assessments,
        "intelligence_assessment": "Synthetic overall intelligence assessment.",
        "confidence": "medium",
        "editor_eligibility": "eligible_with_caveats",
        "editor_caveats": ["Synthetic caveat."],
    }


def _tuple_stub(story_id, original_story, evidence_package, api_key):
    raw = _valid_pass2_output(original_story)
    raw_text = json.dumps(raw)
    response_meta = {
        "stop_reason": "end_turn", "stop_sequence": None, "model": p2.PASS2_MODEL,
        "usage": {"input_tokens": 800, "output_tokens": 400},
    }
    return raw, raw_text, response_meta


# ===========================================================================
# A. Successful mocked live call
# ===========================================================================
outcome = p2.run_intelligence_pass2(
    "CS-01", CS01_STORY, EVIDENCE_PACKAGE, api_key="fake-key-not-real", call_analyst=_tuple_stub,
)
check("A1. successful mocked call produces a VALID outcome", outcome.is_valid, str(outcome.validation_errors))
check("A2. exactly one attempt was recorded", len(outcome.attempts) == 1)
check("A3. the recorded attempt's request_succeeded is True", outcome.attempts[0].request_succeeded)
check("A4. the recorded attempt captures stop_reason/usage from response_meta",
      outcome.attempts[0].stop_reason == "end_turn" and outcome.attempts[0].output_tokens == 400)

# ===========================================================================
# B. Malformed JSON — both attempts fail, diagnostics preserve raw_text
# ===========================================================================


def _malformed_json_stub(story_id, original_story, evidence_package, api_key):
    try:
        json.loads("{not valid json")
    except json.JSONDecodeError as e:
        raise p2.Pass2JSONDecodeError(
            e, raw_text="{not valid json",
            content_blocks=[{"type": "text", "text": "{not valid json"}],
            response_meta={
                "stop_reason": "end_turn", "stop_sequence": None, "model": p2.PASS2_MODEL,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        ) from e


outcome = p2.run_intelligence_pass2(
    "CS-01", CS01_STORY, EVIDENCE_PACKAGE, api_key="fake-key-not-real", call_analyst=_malformed_json_stub,
)
check("B1. malformed JSON on every attempt is reported via failure_reason, not raised",
      outcome.failure_reason == "pass2_call_failed")
check("B2. a malformed-JSON outcome is not valid", not outcome.is_valid)
check("B3. both configured attempts were made", len(outcome.attempts) == p2.PASS2_MAX_ATTEMPTS)
check("B4. the attempt diagnostics preserve the raw (unparseable) text",
      outcome.attempts[0].raw_text == "{not valid json" and outcome.attempts[0].parse_succeeded is False)

# ===========================================================================
# C. Timeout / transport failure (no response body at all)
# ===========================================================================


def _timeout_stub(story_id, original_story, evidence_package, api_key):
    raise TimeoutError("simulated Pass #2 request timeout")


outcome = p2.run_intelligence_pass2(
    "CS-01", CS01_STORY, EVIDENCE_PACKAGE, api_key="fake-key-not-real", call_analyst=_timeout_stub,
)
check("C1. a timeout/transport failure is reported via failure_reason, not raised",
      outcome.failure_reason == "pass2_call_failed")
check("C2. the attempt diagnostics record transport_error (no response body existed)",
      outcome.attempts[0].transport_error is not None
      and "simulated Pass #2 request timeout" in outcome.attempts[0].transport_error)
check("C3. request_succeeded is False for a transport-level failure",
      outcome.attempts[0].request_succeeded is False)

# ===========================================================================
# D. Bounded retry: attempt 1 fails transiently, attempt 2 succeeds
# ===========================================================================
_retry_calls = {"n": 0}


def _retry_then_succeed_stub(story_id, original_story, evidence_package, api_key):
    _retry_calls["n"] += 1
    if _retry_calls["n"] == 1:
        raise ConnectionError("simulated transient connection failure")
    return _tuple_stub(story_id, original_story, evidence_package, api_key)


outcome = p2.run_intelligence_pass2(
    "CS-01", CS01_STORY, EVIDENCE_PACKAGE, api_key="fake-key-not-real", call_analyst=_retry_then_succeed_stub,
)
check("D1. a transient failure followed by success yields a VALID outcome",
      outcome.is_valid, str(outcome.validation_errors))
check("D2. exactly two attempts were recorded (one retry)", len(outcome.attempts) == 2)
check("D3. the retry count never exceeds PASS2_MAX_ATTEMPTS", len(outcome.attempts) <= p2.PASS2_MAX_ATTEMPTS)

# ===========================================================================
# E. Invalid evidence IDs are caught end-to-end (schema wired correctly)
# ===========================================================================


def _invented_evidence_id_stub(story_id, original_story, evidence_package, api_key):
    raw = _valid_pass2_output(original_story)
    raw["material_changes"][0]["evidence_ids"] = ["EV-INVENTED"]
    return raw


outcome = p2.run_intelligence_pass2(
    "CS-01", CS01_STORY, EVIDENCE_PACKAGE, api_key="fake-key-not-real", call_analyst=_invented_evidence_id_stub,
)
check("E1. an invented evidence_id fails end-to-end validation", not outcome.is_valid)
check("E2. the error names the invented evidence_id",
      any("EV-INVENTED" in e for e in outcome.validation_errors), str(outcome.validation_errors))

# ===========================================================================
# F. story_id mismatch across inputs raises BEFORE any model call
# ===========================================================================
_mismatch_call_attempted = {"flag": False}


def _should_never_be_called(story_id, original_story, evidence_package, api_key):
    _mismatch_call_attempted["flag"] = True
    return _tuple_stub(story_id, original_story, evidence_package, api_key)


raised = False
try:
    p2.run_intelligence_pass2(
        "CS-01", {"story_id": "CS-99"}, EVIDENCE_PACKAGE,
        api_key="fake-key-not-real", call_analyst=_should_never_be_called,
    )
except ValueError:
    raised = True
check("F1. story_id mismatch (original_story) raises ValueError", raised)
check("F2. call_analyst is never invoked when inputs mismatch",
      not _mismatch_call_attempted["flag"])

raised = False
try:
    p2.run_intelligence_pass2(
        "CS-01", CS01_STORY, {"story_id": "CS-99", "evidence_records": []},
        api_key="fake-key-not-real", call_analyst=_should_never_be_called,
    )
except ValueError:
    raised = True
check("F3. story_id mismatch (evidence_package) raises ValueError", raised)
check("F4. call_analyst is still never invoked after the second mismatch check",
      not _mismatch_call_attempted["flag"])

# ===========================================================================
# G. editor_eligibility enum end-to-end
# ===========================================================================


def _not_eligible_stub(story_id, original_story, evidence_package, api_key):
    raw = _valid_pass2_output(original_story)
    raw["editor_eligibility"] = "not_eligible"
    raw["assessment_disposition"] = "contradicted"
    return raw


outcome = p2.run_intelligence_pass2(
    "CS-01", CS01_STORY, EVIDENCE_PACKAGE, api_key="fake-key-not-real", call_analyst=_not_eligible_stub,
)
check("G1. editor_eligibility='not_eligible' with assessment_disposition='contradicted' validates "
      "(disposition/eligibility are independent fields)", outcome.is_valid, str(outcome.validation_errors))


def _bad_eligibility_stub(story_id, original_story, evidence_package, api_key):
    raw = _valid_pass2_output(original_story)
    raw["editor_eligibility"] = "bogus"
    return raw


outcome = p2.run_intelligence_pass2(
    "CS-01", CS01_STORY, EVIDENCE_PACKAGE, api_key="fake-key-not-real", call_analyst=_bad_eligibility_stub,
)
check("G2. an invalid editor_eligibility value fails end-to-end", not outcome.is_valid)

# ===========================================================================
# H. No tool / web_search configuration anywhere in the live call -- spy on
# the ACTUAL request body call_anthropic_pass2 would send (not source text,
# which may legitimately mention "tools"/"web_search" in comments/docstrings
# explaining their deliberate absence).
# ===========================================================================
_captured_request = {}


def _spy_post(url, headers=None, json=None, timeout=None):
    _captured_request["url"] = url
    _captured_request["json"] = json
    _captured_request["timeout"] = timeout

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "content": [{"type": "text", "text": '{"story_id": "CS-01"}'}],
                "stop_reason": "end_turn", "stop_sequence": None, "model": p2.PASS2_MODEL,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
    return _FakeResp()


_requests_module.post = _spy_post
try:
    p2.call_anthropic_pass2("CS-01", CS01_STORY, EVIDENCE_PACKAGE, "fake-key-not-real")
finally:
    _requests_module.post = _guard_requests_post

check("H1. call_anthropic_pass2's actual request body never includes a 'tools' key",
      "tools" not in (_captured_request.get("json") or {}))
check("H2. the actual request body's top-level keys are exactly model/max_tokens/system/messages "
      "(no tools, no extra keys)",
      set((_captured_request.get("json") or {}).keys()) == {"model", "max_tokens", "system", "messages"})

# ===========================================================================
# I. Isolation: no coupling to any other Regulus module
# ===========================================================================
check("I1. intelligence_analyst_pass2.py does not import dd_pipeline/dd_schema/regulus_v3",
      not hasattr(p2, "dd_pipeline") and not hasattr(p2, "dd_schema") and not hasattr(p2, "regulus_v3"))
check("I2. intelligence_analyst_pass2.py does not import corpus_analyst/corpus_analyst_schema",
      not hasattr(p2, "corpus_analyst") and not hasattr(p2, "corpus_analyst_schema"))
check("I3. intelligence_analyst_pass2.py does not import evidence_analyst/evidence_analyst_schema/"
      "evidence_retrieval",
      not hasattr(p2, "evidence_analyst") and not hasattr(p2, "evidence_analyst_schema")
      and not hasattr(p2, "evidence_retrieval"))

# ===========================================================================
# J. No credentials persisted
# ===========================================================================
_SECRET_API_KEY = "sk-ant-TOTALLY-SECRET-TEST-KEY-should-never-appear-anywhere"
outcome = p2.run_intelligence_pass2(
    "CS-01", CS01_STORY, EVIDENCE_PACKAGE, api_key=_SECRET_API_KEY, call_analyst=_tuple_stub,
)
_serialized = json.dumps(outcome.raw) + json.dumps([a.to_dict() for a in outcome.attempts])
check("J1. the configured api_key never appears anywhere in the outcome or its diagnostics",
      _SECRET_API_KEY not in _serialized)
check("J2. Pass2AttemptDiagnostics.to_dict() has no api_key/credential field at all",
      "api_key" not in outcome.attempts[0].to_dict() and "x-api-key" not in outcome.attempts[0].to_dict())

# ===========================================================================
# K. Confirm no real network call was EVER attempted anywhere above
# ===========================================================================
check("K1. no real network POST was attempted anywhere in this test file",
      not _network_call_attempted["flag"])
_requests_module.post = _real_requests_post

# ===========================================================================
# Summary
# ===========================================================================
failed = [r for r in results if r[1] == "FAIL"]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
if failed:
    print("FAILURES:")
    for name, status, detail in failed:
        print(f"  - {name}: {detail}")
    sys.exit(1)
sys.exit(0)
