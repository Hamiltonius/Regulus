#!/usr/bin/env python3
"""
Tests for intelligence_editor.py — the Intelligence Editor (Role #4),
live-call implementation (run_intelligence_editor / EditorOutcome).

No REAL Anthropic call is made anywhere in this file. call_analyst is
always an injected stub; requests.post is monkeypatched to raise if it is
ever actually invoked, as an additional, stronger guarantee than relying
on every test remembering to pass a stub.

Run: python3 tests/test_intelligence_editor.py
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


import intelligence_editor as ed
from _evidence_test_helpers import load_acceptance_3, get_story

ACCEPTANCE_3 = load_acceptance_3()
CS01_STORY = get_story(ACCEPTANCE_3, "CS-01")

# ===========================================================================
# Guard against any REAL network call for the whole file.
# ===========================================================================
import requests as _requests_module

_real_requests_post = _requests_module.post
_network_call_attempted = {"flag": False}


def _guard_requests_post(*args, **kwargs):
    _network_call_attempted["flag"] = True
    raise AssertionError(
        "test_intelligence_editor.py attempted a REAL network POST -- "
        "every test must supply its own call_analyst stub"
    )


_requests_module.post = _guard_requests_post


def _pass2_output(story_id, disposition="confirmed_with_modification",
                   removed_claim=None, contradicted_hyp=None, eligibility="eligible"):
    material_changes = []
    if removed_claim:
        material_changes.append({
            "original_claim": removed_claim,
            "disposition": "removed",
            "revised_claim": "",
            "reason": "Synthetic reason the claim was removed.",
            "evidence_ids": [],
        })
    alt_assessment = []
    if contradicted_hyp:
        alt_assessment.append({
            "hypothesis": contradicted_hyp,
            "disposition": "contradicted",
            "explanation": "Synthetic explanation.",
            "evidence_ids": [],
        })
    return {
        "story_id": story_id,
        "assessment_disposition": disposition,
        "original_hypothesis": "Synthetic original hypothesis.",
        "revised_hypothesis": "Synthetic revised hypothesis.",
        "material_changes": material_changes,
        "supported_findings": ["Synthetic supported finding."],
        "weakened_or_rejected_findings": [],
        "remaining_uncertainties": [],
        "alternative_hypotheses_assessment": alt_assessment,
        "intelligence_assessment": "Synthetic intelligence assessment.",
        "confidence": "medium",
        "editor_eligibility": eligibility,
        "editor_caveats": [],
    }


REMOVED_CLAIM = "The withdrawn claim that should never resurface."
CONTRADICTED_HYP = "The contradicted alternative hypothesis that should never resurface."

EDITOR_INPUTS = [
    {
        "story_id": "CS-01",
        "original_story": CS01_STORY,
        "pass2_output": _pass2_output("CS-01", removed_claim=REMOVED_CLAIM, contradicted_hyp=CONTRADICTED_HYP),
        "evidence_ids": ["EV-1", "EV-2"],
    },
    {
        "story_id": "CS-02",
        "original_story": {"story_id": "CS-02", "title": "Second story", "candidate_materiality": "low"},
        "pass2_output": _pass2_output("CS-02"),
        "evidence_ids": ["EV-3"],
    },
]

REPORTING_PERIOD = {"start": "2026-07-01", "end": "2026-09-30"}


def _valid_brief():
    return {
        "brief_id": "BRIEF-001",
        "reporting_period": REPORTING_PERIOD,
        "executive_assessment": "Synthetic executive assessment.",
        "regulatory_tempo": {
            "summary": "Synthetic tempo summary.",
            "items": [{"text": "Tempo item.", "story_ids": ["CS-01"], "evidence_ids": ["EV-1"]}],
        },
        "targeting_and_policy_direction": {
            "summary": "Synthetic targeting summary.",
            "items": [{"text": "Targeting item.", "story_ids": ["CS-01"], "evidence_ids": ["EV-2"]}],
        },
        "key_developments": {
            "summary": "Synthetic key developments summary.",
            "items": [{"text": "CS-01 development.", "story_ids": ["CS-01"], "evidence_ids": ["EV-1", "EV-2"]}],
        },
        "cross_agency_signals": {"summary": "Synthetic cross-agency summary.", "items": []},
        "emerging_patterns": {
            "summary": "Synthetic emerging patterns summary.",
            "items": [{"text": "Pattern across both.", "story_ids": ["CS-01", "CS-02"], "evidence_ids": []}],
        },
        "watchlist": {
            "summary": "Synthetic watchlist summary.",
            "items": [{"text": "CS-02 watchlist item.", "story_ids": ["CS-02"], "evidence_ids": ["EV-3"]}],
        },
        "methodology_and_sources": {
            "summary": "Synthetic methodology summary.",
            "stories_included": ["CS-01", "CS-02"],
            "stories_excluded": [],
            "evidence_ids_referenced": ["EV-1", "EV-2", "EV-3"],
        },
        "confidence": "medium",
        "stories_included": ["CS-01", "CS-02"],
        "stories_excluded": [],
    }


def _tuple_stub(run_id, reporting_period, editor_inputs, api_key):
    raw = _valid_brief()
    raw_text = json.dumps(raw)
    response_meta = {
        "stop_reason": "end_turn", "stop_sequence": None, "model": ed.EDITOR_MODEL,
        "usage": {"input_tokens": 1200, "output_tokens": 600},
    }
    return raw, raw_text, response_meta


# ===========================================================================
# A. Successful mocked live call
# ===========================================================================
outcome = ed.run_intelligence_editor(
    "run-1", REPORTING_PERIOD, EDITOR_INPUTS, api_key="fake-key-not-real", call_analyst=_tuple_stub,
)
check("A1. successful mocked call produces a VALID outcome", outcome.is_valid, str(outcome.validation_errors))
check("A2. exactly one attempt was recorded", len(outcome.attempts) == 1)
check("A3. the recorded attempt's request_succeeded is True", outcome.attempts[0].request_succeeded)
check("A4. the recorded attempt captures stop_reason/usage from response_meta",
      outcome.attempts[0].stop_reason == "end_turn" and outcome.attempts[0].output_tokens == 600)

# ===========================================================================
# B. Malformed JSON — both attempts fail, diagnostics preserve raw_text
# ===========================================================================


def _malformed_json_stub(run_id, reporting_period, editor_inputs, api_key):
    try:
        json.loads("{not valid json")
    except json.JSONDecodeError as e:
        raise ed.EditorJSONDecodeError(
            e, raw_text="{not valid json",
            content_blocks=[{"type": "text", "text": "{not valid json"}],
            response_meta={
                "stop_reason": "end_turn", "stop_sequence": None, "model": ed.EDITOR_MODEL,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        ) from e


outcome = ed.run_intelligence_editor(
    "run-1", REPORTING_PERIOD, EDITOR_INPUTS, api_key="fake-key-not-real", call_analyst=_malformed_json_stub,
)
check("B1. malformed JSON on every attempt is reported via failure_reason, not raised",
      outcome.failure_reason == "editor_call_failed")
check("B2. a malformed-JSON outcome is not valid", not outcome.is_valid)
check("B3. both configured attempts were made", len(outcome.attempts) == ed.EDITOR_MAX_ATTEMPTS)
check("B4. the attempt diagnostics preserve the raw (unparseable) text",
      outcome.attempts[0].raw_text == "{not valid json" and outcome.attempts[0].parse_succeeded is False)

# ===========================================================================
# C. Timeout / transport failure (no response body at all)
# ===========================================================================


def _timeout_stub(run_id, reporting_period, editor_inputs, api_key):
    raise TimeoutError("simulated Editor request timeout")


outcome = ed.run_intelligence_editor(
    "run-1", REPORTING_PERIOD, EDITOR_INPUTS, api_key="fake-key-not-real", call_analyst=_timeout_stub,
)
check("C1. a timeout/transport failure is reported via failure_reason, not raised",
      outcome.failure_reason == "editor_call_failed")
check("C2. the attempt diagnostics record transport_error (no response body existed)",
      outcome.attempts[0].transport_error is not None
      and "simulated Editor request timeout" in outcome.attempts[0].transport_error)
check("C3. request_succeeded is False for a transport-level failure",
      outcome.attempts[0].request_succeeded is False)

# ===========================================================================
# D. Bounded retry: attempt 1 fails transiently, attempt 2 succeeds
# ===========================================================================
_retry_calls = {"n": 0}


def _retry_then_succeed_stub(run_id, reporting_period, editor_inputs, api_key):
    _retry_calls["n"] += 1
    if _retry_calls["n"] == 1:
        raise ConnectionError("simulated transient connection failure")
    return _tuple_stub(run_id, reporting_period, editor_inputs, api_key)


outcome = ed.run_intelligence_editor(
    "run-1", REPORTING_PERIOD, EDITOR_INPUTS, api_key="fake-key-not-real", call_analyst=_retry_then_succeed_stub,
)
check("D1. a transient failure followed by success yields a VALID outcome",
      outcome.is_valid, str(outcome.validation_errors))
check("D2. exactly two attempts were recorded (one retry)", len(outcome.attempts) == 2)
check("D3. the retry count never exceeds EDITOR_MAX_ATTEMPTS", len(outcome.attempts) <= ed.EDITOR_MAX_ATTEMPTS)

# ===========================================================================
# E. Invalid story_id / evidence_id end-to-end (schema wired correctly)
# ===========================================================================


def _invented_story_id_stub(run_id, reporting_period, editor_inputs, api_key):
    raw = _valid_brief()
    raw["key_developments"]["items"][0]["story_ids"] = ["CS-99-NEVER-GIVEN"]
    return raw


outcome = ed.run_intelligence_editor(
    "run-1", REPORTING_PERIOD, EDITOR_INPUTS, api_key="fake-key-not-real", call_analyst=_invented_story_id_stub,
)
check("E1. an invented story_id fails end-to-end validation", not outcome.is_valid)
check("E2. the error names the invented story_id",
      any("CS-99-NEVER-GIVEN" in e for e in outcome.validation_errors), str(outcome.validation_errors))


def _borrowed_evidence_stub(run_id, reporting_period, editor_inputs, api_key):
    raw = _valid_brief()
    raw["key_developments"]["items"][0]["story_ids"] = ["CS-01"]
    raw["key_developments"]["items"][0]["evidence_ids"] = ["EV-3"]  # belongs to CS-02
    return raw


outcome = ed.run_intelligence_editor(
    "run-1", REPORTING_PERIOD, EDITOR_INPUTS, api_key="fake-key-not-real", call_analyst=_borrowed_evidence_stub,
)
check("E3. an item borrowing another story's evidence_id fails end-to-end", not outcome.is_valid)

# ===========================================================================
# F. NO RESURRECTION end-to-end — a Brief that restates a removed claim or
# a contradicted alternative hypothesis verbatim fails validation, even
# though it is schema-shape-complete otherwise
# ===========================================================================


def _resurrecting_stub(run_id, reporting_period, editor_inputs, api_key):
    raw = _valid_brief()
    raw["executive_assessment"] = f"Confirmed: {REMOVED_CLAIM}"
    return raw


outcome = ed.run_intelligence_editor(
    "run-1", REPORTING_PERIOD, EDITOR_INPUTS, api_key="fake-key-not-real", call_analyst=_resurrecting_stub,
)
check("F1. a Brief resurrecting a removed claim verbatim fails end-to-end validation", not outcome.is_valid)
check("F2. the error flags the resurrected text",
      any("resurrect" in e for e in outcome.validation_errors), str(outcome.validation_errors))


def _resurrecting_hyp_stub(run_id, reporting_period, editor_inputs, api_key):
    raw = _valid_brief()
    raw["watchlist"]["items"][0]["text"] = CONTRADICTED_HYP
    return raw


outcome = ed.run_intelligence_editor(
    "run-1", REPORTING_PERIOD, EDITOR_INPUTS, api_key="fake-key-not-real", call_analyst=_resurrecting_hyp_stub,
)
check("F3. a Brief resurrecting a contradicted alternative hypothesis verbatim fails end-to-end",
      not outcome.is_valid)

# ===========================================================================
# G. Story exclusion is allowed end-to-end — a Brief that excludes a
# candidate story from sections 1-7 but still accounts for it in
# stories_excluded/methodology_and_sources is VALID
# ===========================================================================


def _excluding_stub(run_id, reporting_period, editor_inputs, api_key):
    raw = _valid_brief()
    raw["key_developments"]["items"] = [
        i for i in raw["key_developments"]["items"] if "CS-02" not in i["story_ids"]
    ]
    raw["watchlist"]["items"] = []
    raw["emerging_patterns"]["items"] = []
    raw["stories_included"] = ["CS-01"]
    raw["stories_excluded"] = [{"story_id": "CS-02", "reason": "Administrative, not substantive."}]
    raw["methodology_and_sources"]["stories_included"] = ["CS-01"]
    raw["methodology_and_sources"]["stories_excluded"] = [
        {"story_id": "CS-02", "reason": "Administrative, not substantive."}
    ]
    raw["methodology_and_sources"]["evidence_ids_referenced"] = ["EV-1", "EV-2"]
    return raw


outcome = ed.run_intelligence_editor(
    "run-1", REPORTING_PERIOD, EDITOR_INPUTS, api_key="fake-key-not-real", call_analyst=_excluding_stub,
)
check("G1. a Brief excluding a candidate story entirely yields a VALID outcome",
      outcome.is_valid, str(outcome.validation_errors))

# ===========================================================================
# H. No tool / web_search configuration anywhere in the live call -- spy on
# the ACTUAL request body call_anthropic_editor would send (not source
# text, which may legitimately mention "tools"/"web_search" in the system
# prompt explaining their deliberate absence).
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
                "content": [{"type": "text", "text": '{"brief_id": "BRIEF-001"}'}],
                "stop_reason": "end_turn", "stop_sequence": None, "model": ed.EDITOR_MODEL,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
    return _FakeResp()


_requests_module.post = _spy_post
try:
    ed.call_anthropic_editor("run-1", REPORTING_PERIOD, EDITOR_INPUTS, "fake-key-not-real")
finally:
    _requests_module.post = _guard_requests_post

check("H1. call_anthropic_editor's actual request body never includes a 'tools' key",
      "tools" not in (_captured_request.get("json") or {}))
check("H2. the actual request body's top-level keys are exactly model/max_tokens/system/messages "
      "(no tools, no extra keys)",
      set((_captured_request.get("json") or {}).keys()) == {"model", "max_tokens", "system", "messages"})
check("H3. the user payload sends only story_id/title/candidate_materiality/pass2_output per story "
      "(never raw corpus text or evidence_retrieval document bodies)",
      "document_text" not in json.dumps(_captured_request.get("json") or {})
      and "retrieved_documents" not in json.dumps(_captured_request.get("json") or {}))

# ===========================================================================
# I. Isolation: no coupling to any other Regulus module
# ===========================================================================
check("I1. intelligence_editor.py does not import dd_pipeline/dd_schema/regulus_v3",
      not hasattr(ed, "dd_pipeline") and not hasattr(ed, "dd_schema") and not hasattr(ed, "regulus_v3"))
check("I2. intelligence_editor.py does not import corpus_analyst/corpus_analyst_schema",
      not hasattr(ed, "corpus_analyst") and not hasattr(ed, "corpus_analyst_schema"))
check("I3. intelligence_editor.py does not import evidence_analyst/evidence_analyst_schema/"
      "evidence_retrieval",
      not hasattr(ed, "evidence_analyst") and not hasattr(ed, "evidence_analyst_schema")
      and not hasattr(ed, "evidence_retrieval"))
check("I4. intelligence_editor.py does not import intelligence_analyst_pass2",
      not hasattr(ed, "intelligence_analyst_pass2"))

# ===========================================================================
# J. No credentials persisted
# ===========================================================================
_SECRET_API_KEY = "sk-ant-TOTALLY-SECRET-TEST-KEY-should-never-appear-anywhere"
outcome = ed.run_intelligence_editor(
    "run-1", REPORTING_PERIOD, EDITOR_INPUTS, api_key=_SECRET_API_KEY, call_analyst=_tuple_stub,
)
_serialized = json.dumps(outcome.raw) + json.dumps([a.to_dict() for a in outcome.attempts])
check("J1. the configured api_key never appears anywhere in the outcome or its diagnostics",
      _SECRET_API_KEY not in _serialized)
check("J2. EditorAttemptDiagnostics.to_dict() has no api_key/credential field at all",
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
