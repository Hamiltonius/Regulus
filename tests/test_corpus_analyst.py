#!/usr/bin/env python3
"""
Tests for corpus_analyst.py / corpus_analyst_schema.py — the Step 2
first-pass Corpus Analyst and its deterministic output validator.

No live Anthropic call is made anywhere in this file. Every analyst
invocation uses an injected stub `call_analyst`, matching the existing
dd_pipeline test pattern of injecting `call_stage2`/`call_stage3` stubs.

Run: python3 tests/test_corpus_analyst.py
"""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == "FAIL" else ""))
    return condition


import corpus_analyst as ca
import corpus_analyst_schema as schema
from corpus_extractor import Corpus, CorpusObservation


# ===========================================================================
# Fixture corpus: small, synthetic, no resemblance to any real prior
# analysis -- deliberately generic document numbers/titles/agencies, per
# the answer-leakage protection requirement for this feature.
# ===========================================================================

def make_fixture_corpus():
    obs = [
        CorpusObservation(
            document_number="2099-00001", publication_date="2099-01-01",
            effective_date=None, title="Example Notice One",
            agency=[{"name": "Example Agency"}], score=18,
            countries=["Exampleland"], entities=[], eccns=[],
            change_type="entity_list_addition", summary="An example analyzed summary.",
            primary_source_url="https://example.gov/1", tier="analyzed",
            due_diligence_ran=True,
        ),
        CorpusObservation(
            document_number="2099-00002", publication_date="2099-01-02",
            effective_date=None, title="Example Notice Two",
            agency=[{"name": "Example Agency"}], score=14,
            countries=[], entities=[], eccns=[],
            change_type="other", summary="Another example analyzed summary.",
            primary_source_url="https://example.gov/2", tier="analyzed",
            due_diligence_ran=False,
        ),
        CorpusObservation(
            document_number="2099-00003", publication_date="2099-01-03",
            effective_date=None, title="Example Metadata-Only Record",
            agency=[{"name": "Other Agency"}], score=0,
            countries=None, entities=None, eccns=None,
            change_type=None, summary=None,
            primary_source_url="https://example.gov/3", tier="metadata_only",
            due_diligence_ran=False,
        ),
        CorpusObservation(
            document_number="2099-00004", publication_date="2099-01-04",
            effective_date=None, title="Example Low-Score Record",
            agency=[{"name": "Other Agency"}], score=2,
            countries=None, entities=None, eccns=None,
            change_type=None, summary=None,
            primary_source_url="https://example.gov/4", tier="metadata_only",
            due_diligence_ran=False,
        ),
    ]
    return Corpus(start_date="2099-01-01", end_date="2099-01-04", observations=obs)


VALID_DOC_NUMBERS = {"2099-00001", "2099-00002", "2099-00003", "2099-00004"}


def make_valid_output():
    return {
        "reporting_period": {"start": "2099-01-01", "end": "2099-01-04"},
        "corpus_assessment": {
            "observation_count": 4,
            "overall_activity_characterization": "Low-volume example period.",
            "important_caveats": ["Fixture data only."],
        },
        "candidate_stories": [
            {
                "story_id": "story-1",
                "title": "Example candidate story",
                "supporting_document_numbers": ["2099-00001", "2099-00002"],
                "candidate_materiality": "medium",
                "why_it_deserves_investigation": "Two related example actions.",
                "preliminary_hypothesis": "These two actions may be related.",
                "alternative_hypotheses": ["They may be unrelated coincidental timing."],
                "observational_basis": [
                    {"document_number": "2099-00001", "observation": "First example action."},
                    {"document_number": "2099-00002", "observation": "Second example action."},
                ],
                "intelligence_gaps": ["Unknown whether a shared authority is cited."],
                "research_questions": [
                    "Do both documents cite the same underlying statutory authority?"
                ],
                "evidence_needed": ["The full text of both documents' authority citations."],
                "disconfirming_evidence_needed": ["Evidence the two authorities are unrelated."],
                "research_priority": "high",
                "preliminary_confidence": "low",
            }
        ],
        "potential_administrative_activity": [
            {
                "document_numbers": ["2099-00003"],
                "reason": "Appears to be a routine metadata-only record.",
                "research_priority": "low",
            }
        ],
        "unclustered_observations_of_interest": [
            {"document_number": "2099-00004", "reason": "Low score but unclear content."},
        ],
        "corpus_level_gaps": ["Only four observations in this fixture period."],
    }


# ===========================================================================
# A. Valid analyst output passes
# ===========================================================================
result = schema.validate_corpus_analysis(make_valid_output(), VALID_DOC_NUMBERS)
check("A1. valid output passes validation", result.is_valid, str(result.validation_errors))

# ===========================================================================
# B. Missing required story fields fail
# ===========================================================================
bad = make_valid_output()
del bad["candidate_stories"][0]["title"]
result = schema.validate_corpus_analysis(bad, VALID_DOC_NUMBERS)
check("B1. missing story field (title) fails", not result.is_valid)

# ===========================================================================
# C. Invalid enum values fail
# ===========================================================================
bad = make_valid_output()
bad["candidate_stories"][0]["candidate_materiality"] = "critical"
result = schema.validate_corpus_analysis(bad, VALID_DOC_NUMBERS)
check("C1. invalid candidate_materiality enum fails", not result.is_valid)

bad = make_valid_output()
bad["candidate_stories"][0]["research_priority"] = "urgent"
result = schema.validate_corpus_analysis(bad, VALID_DOC_NUMBERS)
check("C2. invalid research_priority enum fails", not result.is_valid)

bad = make_valid_output()
bad["candidate_stories"][0]["preliminary_confidence"] = "certain"
result = schema.validate_corpus_analysis(bad, VALID_DOC_NUMBERS)
check("C3. invalid preliminary_confidence enum fails", not result.is_valid)

# ===========================================================================
# D. Invented supporting document number fails
# ===========================================================================
bad = make_valid_output()
bad["candidate_stories"][0]["supporting_document_numbers"] = ["2099-99999"]
result = schema.validate_corpus_analysis(bad, VALID_DOC_NUMBERS)
check("D1. invented supporting_document_number fails", not result.is_valid)

# ===========================================================================
# E. Invented observational_basis document number fails
# ===========================================================================
bad = make_valid_output()
bad["candidate_stories"][0]["observational_basis"] = [
    {"document_number": "2099-99999", "observation": "Invented reference."}
]
result = schema.validate_corpus_analysis(bad, VALID_DOC_NUMBERS)
check("E1. invented observational_basis document number fails", not result.is_valid)

# ===========================================================================
# F-J. Required non-empty lists fail when empty
# ===========================================================================
for field_name in ["supporting_document_numbers", "research_questions",
                    "intelligence_gaps", "evidence_needed", "disconfirming_evidence_needed"]:
    bad = make_valid_output()
    bad["candidate_stories"][0][field_name] = []
    result = schema.validate_corpus_analysis(bad, VALID_DOC_NUMBERS)
    check(f"F-J. empty {field_name} fails", not result.is_valid, field_name)

# Banned placeholder research question
bad = make_valid_output()
bad["candidate_stories"][0]["research_questions"] = ["We need more research."]
result = schema.validate_corpus_analysis(bad, VALID_DOC_NUMBERS)
check("F-J-extra. banned placeholder research question fails", not result.is_valid)

# ===========================================================================
# K. Duplicate story_id fails
# ===========================================================================
bad = make_valid_output()
second_story = copy.deepcopy(bad["candidate_stories"][0])
second_story["supporting_document_numbers"] = ["2099-00002"]
bad["candidate_stories"].append(second_story)  # same story_id as first
result = schema.validate_corpus_analysis(bad, VALID_DOC_NUMBERS)
check("K1. duplicate story_id fails", not result.is_valid)

# ===========================================================================
# L. Metadata-only observations remain eligible input
# M. Low-score observations remain eligible input
# ===========================================================================
fixture_corpus = make_fixture_corpus()
captured_payload = {}


def capturing_stub(corpus_payload, api_key):
    captured_payload.update(corpus_payload)
    return make_valid_output()


outcome = ca.run_corpus_analysis(fixture_corpus, api_key="fake", call_analyst=capturing_stub)
sent_doc_numbers = {o["document_number"] for o in captured_payload.get("observations", [])}
check("L1. metadata-only observation present in payload sent to the model",
      "2099-00003" in sent_doc_numbers)
check("M1. low-score observation present in payload sent to the model",
      "2099-00004" in sent_doc_numbers)
check("L2/M2. run_corpus_analysis result is valid given a valid stub response", outcome.is_valid,
      str(outcome.validation_errors))

# ===========================================================================
# N. No web-search/tool capability is exposed
# ===========================================================================
captured_request = {}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _fake_post(url, headers=None, json=None, timeout=None):
    captured_request["json"] = json
    fake_text = __import__("json").dumps(make_valid_output())
    return _FakeResponse({
        "content": [{"type": "text", "text": fake_text}],
        "stop_reason": "end_turn", "model": ca.CORPUS_ANALYST_MODEL, "usage": {},
    })


_orig_post = ca.requests.post
ca.requests.post = _fake_post
try:
    ca.call_anthropic_corpus_analyst({"reporting_period": {}, "observations": []}, "fake-key")
    check("N1. request sent with no 'tools' key at all",
          "tools" not in captured_request["json"], str(captured_request["json"].keys()))
    check("N2. request model matches CORPUS_ANALYST_MODEL",
          captured_request["json"]["model"] == ca.CORPUS_ANALYST_MODEL)
    check("N3. request max_tokens matches CORPUS_ANALYST_MAX_TOKENS",
          captured_request["json"]["max_tokens"] == ca.CORPUS_ANALYST_MAX_TOKENS)
finally:
    ca.requests.post = _orig_post

# ===========================================================================
# O. Existing Stage 1/Stage 2/Stage 3 functions are not invoked
# ===========================================================================
check("O1. corpus_analyst module does not import dd_pipeline",
      not hasattr(ca, "dd_pipeline"))
check("O2. corpus_analyst module does not import dd_schema",
      not hasattr(ca, "dd_schema"))
check("O3. corpus_analyst module does not import regulus_v3",
      not hasattr(ca, "regulus_v3") and not hasattr(ca, "rv"))

import dd_pipeline as ddp


def _boom(*a, **kw):
    raise AssertionError("corpus_analyst must never call this function")


_orig_stage2 = ddp.call_anthropic_stage2
_orig_stage3 = ddp.call_anthropic_stage3
_orig_needs_dd = ddp.needs_due_diligence
ddp.call_anthropic_stage2 = _boom
ddp.call_anthropic_stage3 = _boom
ddp.needs_due_diligence = _boom
try:
    outcome2 = ca.run_corpus_analysis(fixture_corpus, api_key="fake", call_analyst=capturing_stub)
    check("O4. run_corpus_analysis completes with Stage1/2/3-adjacent functions patched to raise",
          outcome2.is_valid)
finally:
    ddp.call_anthropic_stage2 = _orig_stage2
    ddp.call_anthropic_stage3 = _orig_stage3
    ddp.needs_due_diligence = _orig_needs_dd

# ===========================================================================
# P. Existing DD behavior remains unchanged (spot-check key constants)
# ===========================================================================
check("P1. dd_pipeline.STAGE2_MAX_TOKENS untouched", ddp.STAGE2_MAX_TOKENS == 20000)
check("P2. dd_pipeline.STAGE3_MAX_TOKENS untouched", ddp.STAGE3_MAX_TOKENS == 4000)
check("P3. dd_pipeline.STAGE2_MODEL/STAGE3_MODEL untouched",
      ddp.STAGE2_MODEL == "claude-sonnet-4-6" and ddp.STAGE3_MODEL == "claude-sonnet-4-6")
check("P4. dd_pipeline.CHANGE_TYPE_VALUES untouched (14 controlled values)",
      len(ddp.CHANGE_TYPE_VALUES) == 14)
check("P5. corpus_analyst introduces no Stage-1 retry constant",
      not hasattr(ddp, "STAGE1_MAX_ATTEMPTS"))

# ===========================================================================
# R. due_diligence_ran tri-state: prompt explicitly tells the model null
# is not false (schema-compatibility fix). A text-content check on the
# prompt itself, not a claim about what the model will actually do.
# ===========================================================================
check("R1. system prompt explicitly addresses due_diligence_ran null case",
      "null" in ca.CORPUS_ANALYST_SYSTEM_PROMPT.lower()
      and "due_diligence_ran" in ca.CORPUS_ANALYST_SYSTEM_PROMPT)
_normalized_prompt = " ".join(ca.CORPUS_ANALYST_SYSTEM_PROMPT.lower().split())
check("R2. system prompt explicitly forbids treating null as false",
      "never treat null as equivalent to false" in _normalized_prompt)

# Payload with due_diligence_ran=None (historical-schema shape) still
# reaches run_corpus_analysis and serializes as JSON null, not coerced.
hist_shape_corpus = make_fixture_corpus()
hist_shape_corpus.observations[0].due_diligence_ran = None
captured_payload_2 = {}


def capturing_stub_2(corpus_payload, api_key):
    captured_payload_2.update(corpus_payload)
    return make_valid_output()


ca.run_corpus_analysis(hist_shape_corpus, api_key="fake", call_analyst=capturing_stub_2)
sent_obs = {o["document_number"]: o for o in captured_payload_2.get("observations", [])}
check("R3. due_diligence_ran=None observation passed through as null, not coerced to False",
      sent_obs["2099-00001"]["due_diligence_ran"] is None)

# ===========================================================================
# Call-failure path: retries once, reports failure_reason, never raises
# ===========================================================================
attempts = {"count": 0}


def _always_fails(corpus_payload, api_key):
    attempts["count"] += 1
    raise RuntimeError("simulated network failure")


outcome3 = ca.run_corpus_analysis(fixture_corpus, api_key="fake", call_analyst=_always_fails)
check("Q1. call failure is retried exactly CORPUS_ANALYST_MAX_ATTEMPTS times",
      attempts["count"] == ca.CORPUS_ANALYST_MAX_ATTEMPTS, str(attempts["count"]))
check("Q2. call failure reported via failure_reason, not raised",
      outcome3.failure_reason == "corpus_analyst_call_failed")
check("Q3. call failure outcome is not valid", not outcome3.is_valid)

# ===========================================================================
# S. Failed-response observability fix: per-attempt diagnostics survive a
# JSON parse failure, a transport failure, and a validation failure, and
# are never destroyed when a later attempt also fails. No live Anthropic
# call is made anywhere below -- ca.requests.post is monkeypatched.
# ===========================================================================


def _fake_post_factory(*, text, stop_reason="end_turn", stop_sequence=None,
                        usage=None, raise_http_error=False):
    """Builds a fake requests.post replacement that returns a Messages-API
    -shaped response carrying the given raw text/metadata, OR raises a
    requests-style transport error (no response body at all) when
    raise_http_error=True."""
    def _fake_post(url, headers=None, json=None, timeout=None):
        if raise_http_error:
            raise ca.requests.exceptions.ConnectionError("simulated transport failure")
        return _FakeResponse({
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "stop_sequence": stop_sequence,
            "model": ca.CORPUS_ANALYST_MODEL,
            "usage": usage or {"input_tokens": 111, "output_tokens": 222},
        })
    return _fake_post


# S1/S2/S3: a single attempt whose response is truncated/malformed JSON --
# raw_text is preserved, non-empty, length matches, and parse_succeeded is
# False, exactly the node02 symptom (response received, parse failed).
_truncated_text = '{"reporting_period": {"start": "2099-01-01", "end": "2099-01-07"'  # unterminated
_orig_post = ca.requests.post
ca.requests.post = _fake_post_factory(text=_truncated_text, stop_reason="max_tokens")
try:
    raw1, diag1, err1 = ca._run_single_attempt(
        1, {"reporting_period": {}, "observations": []}, "fake-key",
        ca.call_anthropic_corpus_analyst,
    )
finally:
    ca.requests.post = _orig_post

check("S1. raw text is preserved on a JSON parse failure", diag1.raw_text == _truncated_text)
check("S2. raw_text_length matches the preserved raw text's length",
      diag1.raw_text_length == len(_truncated_text))
check("S3. parse_succeeded is False and parse_error is populated on parse failure",
      diag1.parse_succeeded is False and diag1.parse_error, str(diag1.parse_error))
check("S3b. request_succeeded is True on a parse failure (a response WAS received)",
      diag1.request_succeeded is True)
check("S3c. raw (parsed) result is None when parsing failed", raw1 is None and err1 is not None)

# S4/S5/S6: stop_reason, stop_sequence, and token usage are captured when
# the model response provides them -- including on the parse-failure path
# above (stop_reason == "max_tokens" is the direct truncation signal).
check("S4. stop_reason captured on a parse-failure attempt", diag1.stop_reason == "max_tokens")

_orig_post = ca.requests.post
ca.requests.post = _fake_post_factory(
    text='{"unterminated": "oops',
    stop_reason="stop_sequence",
    stop_sequence="\n\nEND",
    usage={"input_tokens": 333, "output_tokens": 444},
)
try:
    raw1b, diag1b, err1b = ca._run_single_attempt(
        2, {"reporting_period": {}, "observations": []}, "fake-key",
        ca.call_anthropic_corpus_analyst,
    )
finally:
    ca.requests.post = _orig_post

check("S5. stop_sequence captured when the model response provides one",
      diag1b.stop_sequence == "\n\nEND")
check("S6. input_tokens/output_tokens captured from usage",
      diag1b.input_tokens == 333 and diag1b.output_tokens == 444)

# S7: both configured attempts failing (both parse failures) means BOTH
# attempts' diagnostics are present afterward -- attempt 1's record is not
# overwritten or discarded when attempt 2 also fails.
_orig_post = ca.requests.post
ca.requests.post = _fake_post_factory(text='{"still": "broken', stop_reason="max_tokens")
try:
    outcome_both_fail = ca.run_corpus_analysis(
        fixture_corpus, api_key="fake", call_analyst=ca.call_anthropic_corpus_analyst,
    )
finally:
    ca.requests.post = _orig_post

check("S7a. both-fail outcome records exactly 2 attempts",
      len(outcome_both_fail.attempts) == 2, str(len(outcome_both_fail.attempts)))
check("S7b. attempt 1's diagnostics survive after attempt 2 also fails",
      outcome_both_fail.attempts[0].attempt_number == 1
      and outcome_both_fail.attempts[0].raw_text == '{"still": "broken')
check("S7c. attempt 2's diagnostics are also present and distinct from attempt 1",
      outcome_both_fail.attempts[1].attempt_number == 2
      and outcome_both_fail.attempts[1].raw_text == '{"still": "broken')
check("S7d. outcome.raw is still None on total failure (unchanged existing behavior)",
      outcome_both_fail.raw is None)
check("S7e. outcome.failure_reason is still corpus_analyst_call_failed (unchanged)",
      outcome_both_fail.failure_reason == "corpus_analyst_call_failed")

# S8: a SUCCESSFUL parse (via the real call_anthropic_corpus_analyst, not
# a legacy plain-dict stub) still records diagnostic metadata -- diagnosing
# failures should not be the only path that gets instrumentation.
_orig_post = ca.requests.post
ca.requests.post = _fake_post_factory(
    text=json.dumps(make_valid_output()), stop_reason="end_turn",
    usage={"input_tokens": 50, "output_tokens": 75},
)
try:
    outcome_success = ca.run_corpus_analysis(
        fixture_corpus, api_key="fake", call_analyst=ca.call_anthropic_corpus_analyst,
    )
finally:
    ca.requests.post = _orig_post

check("S8a. successful outcome still records exactly 1 attempt",
      len(outcome_success.attempts) == 1, str(len(outcome_success.attempts)))
check("S8b. successful attempt's diagnostics include stop_reason/usage",
      outcome_success.attempts[0].stop_reason == "end_turn"
      and outcome_success.attempts[0].input_tokens == 50
      and outcome_success.attempts[0].output_tokens == 75)
check("S8c. successful outcome.raw is unchanged / still the parsed dict",
      outcome_success.raw == make_valid_output())
check("S8d. successful outcome.is_valid is unchanged (True)", outcome_success.is_valid)

# S9: a JSON payload that PARSES but FAILS schema validation is
# distinguishable from a parse failure -- parse_succeeded True,
# validation_succeeded False, with validation_errors populated.
_invalid_output = make_valid_output()
_invalid_output["candidate_stories"][0]["supporting_document_numbers"] = ["9999-99999"]
_orig_post = ca.requests.post
ca.requests.post = _fake_post_factory(text=json.dumps(_invalid_output), stop_reason="end_turn")
try:
    outcome_invalid = ca.run_corpus_analysis(
        fixture_corpus, api_key="fake", call_analyst=ca.call_anthropic_corpus_analyst,
    )
finally:
    ca.requests.post = _orig_post

check("S9a. schema-invalid-but-parseable output is NOT retried (matches pre-existing "
      "behavior: only a call/parse failure triggers a retry, never a validation failure)",
      len(outcome_invalid.attempts) == 1, str(len(outcome_invalid.attempts)))
check("S9b. parse_succeeded True but validation_succeeded False on the invalid-schema attempt",
      outcome_invalid.attempts[0].parse_succeeded is True
      and outcome_invalid.attempts[0].validation_succeeded is False
      and outcome_invalid.attempts[0].validation_errors)
check("S9c. outcome.validation_status is invalid (unchanged existing behavior)",
      outcome_invalid.validation_status == "invalid")

# S10: a transport/network failure (no response body at all) is
# distinguishable from a parse failure -- transport_error is set, and
# raw_text/stop_reason/usage stay None rather than being fabricated.
_orig_post = ca.requests.post
ca.requests.post = _fake_post_factory(text="unused", raise_http_error=True)
try:
    raw_t, diag_t, err_t = ca._run_single_attempt(
        1, {"reporting_period": {}, "observations": []}, "fake-key",
        ca.call_anthropic_corpus_analyst,
    )
finally:
    ca.requests.post = _orig_post

check("S10a. transport failure sets request_succeeded False", diag_t.request_succeeded is False)
check("S10b. transport failure sets transport_error", bool(diag_t.transport_error))
check("S10c. transport failure leaves raw_text/stop_reason/usage as None (not fabricated)",
      diag_t.raw_text is None and diag_t.stop_reason is None
      and diag_t.input_tokens is None and diag_t.output_tokens is None)

# S11: no credential material is ever present in a diagnostics record,
# even as a substring of raw_text or any other field, for either a
# successful or a failed attempt.
for _diag in (diag1, diag1b, diag_t, outcome_success.attempts[0], outcome_invalid.attempts[0]):
    _diag_dict = _diag.to_dict()
    _serialized = json.dumps(_diag_dict, default=str)
    check(f"S11. no API key/Authorization material in attempt {_diag.attempt_number} diagnostics",
          "fake-key" not in _serialized and "x-api-key" not in _serialized.lower()
          and "authorization" not in _serialized.lower())

# S12: existing CorpusAnalysisOutcome public fields behave exactly as
# before this fix, for both the success and total-failure paths (already
# exercised above by outcome_success/outcome_both_fail) -- explicit
# regression check on the exact field set a pre-existing caller relies on.
check("S12a. success path: raw/validation_status/validation_errors/failure_reason unchanged shape",
      isinstance(outcome_success.raw, dict) and outcome_success.validation_status == "valid"
      and outcome_success.validation_errors == [] and outcome_success.failure_reason is None)
check("S12b. total-failure path: raw/validation_status/failure_reason unchanged shape",
      outcome_both_fail.raw is None and outcome_both_fail.validation_status == "invalid"
      and outcome_both_fail.failure_reason == "corpus_analyst_call_failed")

# S13: attempt_number sequencing is correct and sequential starting at 1
# in both the success-on-first-try and both-fail cases.
check("S13a. success-on-first-try: single attempt numbered 1",
      [a.attempt_number for a in outcome_success.attempts] == [1])
check("S13b. both-fail: attempts numbered 1 then 2, in order",
      [a.attempt_number for a in outcome_both_fail.attempts] == [1, 2])

# ===========================================================================
# T. Output-capacity / timeout revision (Corpus Analyst Acceptance #2):
# CORPUS_ANALYST_MAX_TOKENS 12000 -> 20000, CORPUS_ANALYST_TIMEOUT_SECONDS
# 300 -> 600. Pins the literal new values (not just "whatever the
# constant currently is") so a future accidental revert is caught, and
# proves the new values actually reach the Anthropic request and the
# per-attempt diagnostics. No live Anthropic call anywhere below.
# ===========================================================================
check("T1. CORPUS_ANALYST_MAX_TOKENS is exactly 20000", ca.CORPUS_ANALYST_MAX_TOKENS == 20000)
check("T2. CORPUS_ANALYST_TIMEOUT_SECONDS is exactly 600", ca.CORPUS_ANALYST_TIMEOUT_SECONDS == 600)
check("T3. CORPUS_ANALYST_MAX_ATTEMPTS is still 2 (unchanged)", ca.CORPUS_ANALYST_MAX_ATTEMPTS == 2)
check("T4. PROMPT_VERSION is still '1.1' (unchanged)", ca.PROMPT_VERSION == "1.1")
check("T5. CORPUS_ANALYST_MODEL is still 'claude-sonnet-4-6' (unchanged)",
      ca.CORPUS_ANALYST_MODEL == "claude-sonnet-4-6")

_captured_request_t = {}


def _fake_post_t(url, headers=None, json=None, timeout=None):
    _captured_request_t["json"] = json
    _captured_request_t["timeout"] = timeout
    fake_text = __import__("json").dumps(make_valid_output())
    return _FakeResponse({
        "content": [{"type": "text", "text": fake_text}],
        "stop_reason": "end_turn", "model": ca.CORPUS_ANALYST_MODEL, "usage": {},
    })


_orig_post = ca.requests.post
ca.requests.post = _fake_post_t
try:
    ca.call_anthropic_corpus_analyst({"reporting_period": {}, "observations": []}, "fake-key")
    check("T6. request sent with max_tokens=20000 (the new configured value)",
          _captured_request_t["json"]["max_tokens"] == 20000,
          str(_captured_request_t["json"]["max_tokens"]))
    check("T7. requests.post called with timeout=600 (the new configured value)",
          _captured_request_t["timeout"] == 600, str(_captured_request_t["timeout"]))
finally:
    ca.requests.post = _orig_post

# T8/T9: per-attempt diagnostics report the new configured max_tokens/
# timeout_seconds, for both a successful and a failed attempt.
ca.requests.post = _fake_post_t
try:
    raw_t89, diag_t89, err_t89 = ca._run_single_attempt(
        1, {"reporting_period": {}, "observations": []}, "fake-key",
        ca.call_anthropic_corpus_analyst,
    )
finally:
    ca.requests.post = _orig_post

check("T8. successful-attempt diagnostics report max_tokens=20000",
      diag_t89.max_tokens == 20000)
check("T9. successful-attempt diagnostics report timeout_seconds=600",
      diag_t89.timeout_seconds == 600)

ca.requests.post = _fake_post_factory(text='{"broken", stop_reason="max_tokens')
try:
    raw_t10, diag_t10, err_t10 = ca._run_single_attempt(
        1, {"reporting_period": {}, "observations": []}, "fake-key",
        ca.call_anthropic_corpus_analyst,
    )
finally:
    ca.requests.post = _orig_post

check("T10. failed-attempt diagnostics also report max_tokens=20000/timeout_seconds=600",
      diag_t10.max_tokens == 20000 and diag_t10.timeout_seconds == 600)

# T11: existing observability behavior (per-attempt capture, both-attempts-
# survive, security exclusion) remains intact under the new config values --
# re-exercise the both-fail path (S7) with the new constants in effect.
ca.requests.post = _fake_post_factory(text='{"still": "broken', stop_reason="max_tokens")
try:
    outcome_t11 = ca.run_corpus_analysis(
        fixture_corpus, api_key="fake", call_analyst=ca.call_anthropic_corpus_analyst,
    )
finally:
    ca.requests.post = _orig_post

check("T11a. both-fail outcome under new config still records exactly 2 attempts",
      len(outcome_t11.attempts) == 2, str(len(outcome_t11.attempts)))
check("T11b. both-fail attempts under new config both report max_tokens=20000",
      all(a.max_tokens == 20000 for a in outcome_t11.attempts))
check("T11c. both-fail attempts under new config both report timeout_seconds=600",
      all(a.timeout_seconds == 600 for a in outcome_t11.attempts))
check("T11d. no credential material leaked under new config either",
      "fake-key" not in json.dumps([a.to_dict() for a in outcome_t11.attempts], default=str))

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
