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
