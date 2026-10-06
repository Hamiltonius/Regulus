#!/usr/bin/env python3
"""
Tests for evidence_analyst.py — the future-call interface skeleton
(run_evidence_analysis / EvidenceAnalysisOutcome).

No live Anthropic call is made anywhere in this file. call_analyst is
always an injected stub; retrieval is always mocked. Omitting
call_analyst must raise EvidenceAnalystNotImplementedError BEFORE any
network/model activity — proving this module cannot make a live call on
its own.

Run: python3 tests/test_evidence_analyst.py
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


import evidence_analyst as ea
import evidence_retrieval as er
from _evidence_test_helpers import load_acceptance_3, get_story, load_corpus_acceptance_3

ACCEPTANCE_3 = load_acceptance_3()
CS01_STORY = get_story(ACCEPTANCE_3, "CS-01")
DEV_CORPUS = load_corpus_acceptance_3()


def _fake_retrieve(candidate_story, corpus, **kwargs):
    """A retrieval stub that never touches the network -- returns a
    bundle with one successfully 'retrieved' RetrievedDocument per
    supporting_document_number."""
    docs = []
    for doc_num in candidate_story["supporting_document_numbers"]:
        docs.append(er.RetrievedDocument(
            document_number=doc_num, status="retrieved", identity_status="verified",
            source_url=f"https://www.federalregister.gov/documents/x/{doc_num}",
            primary_source=True, text="Synthetic retrieved text.", text_source="pdf_full",
            retrieved_at="2026-10-06T00:00:00+00:00",
        ))
    return er.EvidenceRetrievalBundle(story_id=candidate_story["story_id"], documents=docs)


def _valid_stub_output(candidate_story, retrieval_bundle, corpus):
    docs = candidate_story["supporting_document_numbers"]
    questions = candidate_story["research_questions"]
    return {
        "story_id": candidate_story["story_id"],
        "research_status": "partial",
        "hypothesis_assessment": "unresolved",
        "question_findings": [
            {"question": q, "status": "unanswered", "finding": "", "evidence_ids": [], "confidence": "low"}
            for q in questions
        ],
        "evidence_records": [
            {
                "evidence_id": "EV-1",
                "document_number": docs[0],
                "source_title": "Synthetic title",
                "source_url": "https://www.federalregister.gov/documents/x",
                "source_type": "primary",
                "primary_source": True,
                "retrieved_at": "2026-10-06T00:00:00+00:00",
                "source_identity_status": "verified",
                "publication_date": "2026-09-16",
                "effective_date": "2026-09-16",
                "relevant_excerpt": "Synthetic excerpt.",
                "supports": [],
                "contradicts": [],
                "limitations": "",
            }
        ],
        "contradictions": [],
        "remaining_gaps": [],
        "disconfirming_evidence_found": [],
        "overall_assessment": "Synthetic overall assessment.",
        "confidence": "low",
    }


# ===========================================================================
# A. No call_analyst -> EvidenceAnalystNotImplementedError, no network call
# ===========================================================================
raised = False
try:
    ea.run_evidence_analysis(CS01_STORY, DEV_CORPUS, retrieve=_fake_retrieve)
except ea.EvidenceAnalystNotImplementedError:
    raised = True
check("A1. omitting call_analyst raises EvidenceAnalystNotImplementedError", raised)

# Confirm it raises even if api_key is supplied (never silently uses it to call out).
raised = False
try:
    ea.run_evidence_analysis(CS01_STORY, DEV_CORPUS, api_key="fake-key", retrieve=_fake_retrieve)
except ea.EvidenceAnalystNotImplementedError:
    raised = True
check("A2. still raises even when an api_key is supplied", raised)

# ===========================================================================
# B. With a stub call_analyst, retrieval runs first and is attached to the outcome
# ===========================================================================
captured = {}


def _capturing_stub(candidate_story, retrieval_bundle, corpus):
    captured["retrieval_bundle"] = retrieval_bundle
    captured["corpus"] = corpus
    return _valid_stub_output(candidate_story, retrieval_bundle, corpus)


outcome = ea.run_evidence_analysis(
    CS01_STORY, DEV_CORPUS, call_analyst=_capturing_stub, retrieve=_fake_retrieve,
)
check("B1. outcome.is_valid is True given a structurally valid stub output",
      outcome.is_valid, str(outcome.validation_errors))
check("B2. outcome.retrieval is the EvidenceRetrievalBundle produced by `retrieve`",
      outcome.retrieval is not None and outcome.retrieval.story_id == "CS-01")
check("B3. call_analyst received the SAME retrieval bundle object run_evidence_analysis attached",
      captured["retrieval_bundle"] is outcome.retrieval)
check("B4. call_analyst received the corpus object", captured["corpus"] is DEV_CORPUS)

# ===========================================================================
# C. call_analyst failure is caught, never raised, retrieval still attached
# ===========================================================================


def _always_fails(candidate_story, retrieval_bundle, corpus):
    raise RuntimeError("simulated Evidence Analyst call failure")


outcome = ea.run_evidence_analysis(
    CS01_STORY, DEV_CORPUS, call_analyst=_always_fails, retrieve=_fake_retrieve,
)
check("C1. a call_analyst failure is reported via failure_reason, not raised",
      outcome.failure_reason == "evidence_analyst_call_failed")
check("C2. a call_analyst failure outcome is not valid", not outcome.is_valid)
check("C3. retrieval is STILL attached to the outcome even though call_analyst failed",
      outcome.retrieval is not None and len(outcome.retrieval.documents) > 0)

# ===========================================================================
# D. Partial research result and insufficient_evidence result both validate
# ===========================================================================


def _partial_stub(candidate_story, retrieval_bundle, corpus):
    out = _valid_stub_output(candidate_story, retrieval_bundle, corpus)
    out["research_status"] = "partial"
    return out


outcome = ea.run_evidence_analysis(CS01_STORY, DEV_CORPUS, call_analyst=_partial_stub, retrieve=_fake_retrieve)
check("D1. research_status='partial' outcome validates", outcome.is_valid, str(outcome.validation_errors))


def _insufficient_stub(candidate_story, retrieval_bundle, corpus):
    questions = candidate_story["research_questions"]
    return {
        "story_id": candidate_story["story_id"],
        "research_status": "insufficient_evidence",
        "hypothesis_assessment": "unresolved",
        "question_findings": [
            {"question": q, "status": "unanswered", "finding": "", "evidence_ids": [], "confidence": "low"}
            for q in questions
        ],
        "evidence_records": [],
        "contradictions": [],
        "remaining_gaps": ["No primary documents could be retrieved."],
        "disconfirming_evidence_found": [],
        "overall_assessment": "No usable primary source material could be retrieved.",
        "confidence": "low",
    }


outcome = ea.run_evidence_analysis(
    CS01_STORY, DEV_CORPUS, call_analyst=_insufficient_stub, retrieve=_fake_retrieve,
)
check("D2. research_status='insufficient_evidence' with zero evidence is a VALID outcome",
      outcome.is_valid, str(outcome.validation_errors))

# ===========================================================================
# E. Source-identity cross-check flows end-to-end from retrieval -> validation
# ===========================================================================


def _mismatch_retrieve(candidate_story, corpus, **kwargs):
    docs = []
    for i, doc_num in enumerate(candidate_story["supporting_document_numbers"]):
        docs.append(er.RetrievedDocument(
            document_number=doc_num, status="retrieved",
            identity_status="mismatch" if i == 0 else "verified",
        ))
    return er.EvidenceRetrievalBundle(story_id=candidate_story["story_id"], documents=docs)


def _claims_verified_stub(candidate_story, retrieval_bundle, corpus):
    out = _valid_stub_output(candidate_story, retrieval_bundle, corpus)
    out["evidence_records"][0]["source_identity_status"] = "verified"  # disagrees with retrieval
    return out


outcome = ea.run_evidence_analysis(
    CS01_STORY, DEV_CORPUS, call_analyst=_claims_verified_stub, retrieve=_mismatch_retrieve,
)
check("E1. an evidence_record claiming 'verified' when retrieval found 'mismatch' fails end-to-end",
      not outcome.is_valid)
check("E2. the end-to-end error names the mismatch",
      any("mismatch" in e for e in outcome.validation_errors), str(outcome.validation_errors))

# ===========================================================================
# F. No REAL network call is ever attempted by this test file.
#
# evidence_analyst.py now imports `requests` at module scope by design --
# call_anthropic_evidence_analyst (the live implementation) uses it to
# make the real Anthropic Messages API call. Every test below supplies
# its own call_analyst/retrieve stub to run_live_evidence_analysis, so
# that real function (and the network) should never actually be invoked.
# requests.post is monkeypatched here to RAISE if called at all, which is
# a stronger guarantee than merely checking imports -- it would fail loudly
# the instant any test in this file forgot to stub call_analyst.
# ===========================================================================
import requests as _requests_module

_real_requests_post = _requests_module.post
_network_call_attempted = {"flag": False}


def _guard_requests_post(*args, **kwargs):
    _network_call_attempted["flag"] = True
    raise AssertionError(
        "test_evidence_analyst.py attempted a REAL network POST -- "
        "every test must supply its own call_analyst/retrieve stub"
    )


_requests_module.post = _guard_requests_post

# ===========================================================================
# G. Live interface (run_live_evidence_analysis): successful mocked call
# ===========================================================================


def _fake_retrieve_live(candidate_story, corpus, **kwargs):
    docs = []
    for doc_num in candidate_story["supporting_document_numbers"]:
        docs.append(er.RetrievedDocument(
            document_number=doc_num, status="retrieved", identity_status="verified",
            source_url=f"https://www.federalregister.gov/documents/x/{doc_num}",
            primary_source=True, text="Synthetic retrieved text.", text_source="pdf_full",
            retrieved_at="2026-10-06T00:00:00+00:00",
        ))
    return er.EvidenceRetrievalBundle(story_id=candidate_story["story_id"], documents=docs)


def _valid_live_output(candidate_story, retrieval_bundle):
    """A structurally valid Evidence Analyst output that cites EVERY
    successfully-retrieved document (so it also passes the "no silently
    dropped document" check) and covers every research question exactly
    once."""
    questions = candidate_story["research_questions"]
    doc_numbers = [d.document_number for d in retrieval_bundle.retrieved_documents]
    evidence_records = [
        {
            "evidence_id": f"EV-{i + 1}",
            "document_number": doc_num,
            "source_title": f"Synthetic title for {doc_num}",
            "source_url": f"https://www.federalregister.gov/documents/x/{doc_num}",
            "source_type": "primary",
            "primary_source": True,
            "retrieved_at": "2026-10-06T00:00:00+00:00",
            "source_identity_status": "verified",
            "publication_date": "2026-09-16",
            "effective_date": "2026-09-16",
            "relevant_excerpt": f"Synthetic excerpt for {doc_num}.",
            "supports": [],
            "contradicts": [],
            "limitations": "",
        }
        for i, doc_num in enumerate(doc_numbers)
    ]
    evidence_ids = [r["evidence_id"] for r in evidence_records]
    question_findings = [
        {
            "question": q,
            "status": "answered" if i == 0 else "unanswered",
            "finding": "Synthetic finding." if i == 0 else "",
            "evidence_ids": evidence_ids[:1] if i == 0 else [],
            "confidence": "medium",
        }
        for i, q in enumerate(questions)
    ]
    return {
        "story_id": candidate_story["story_id"],
        "research_status": "partial",
        "hypothesis_assessment": "unresolved",
        "question_findings": question_findings,
        "evidence_records": evidence_records,
        "contradictions": [],
        "remaining_gaps": [],
        "disconfirming_evidence_found": [],
        "overall_assessment": "Synthetic live overall assessment.",
        "confidence": "low",
    }


def _tuple_stub(candidate_story, retrieval_bundle, corpus, api_key):
    """Mimics call_anthropic_evidence_analyst's real return shape:
    (parsed, raw_text, response_meta) -- never touches the network."""
    raw = _valid_live_output(candidate_story, retrieval_bundle)
    raw_text = json.dumps(raw)
    response_meta = {
        "stop_reason": "end_turn", "stop_sequence": None,
        "model": ea.EVIDENCE_ANALYST_MODEL,
        "usage": {"input_tokens": 1000, "output_tokens": 500},
    }
    return raw, raw_text, response_meta


outcome = ea.run_live_evidence_analysis(
    CS01_STORY, DEV_CORPUS, api_key="fake-key-not-real",
    call_analyst=_tuple_stub, retrieve=_fake_retrieve_live,
)
check("G1. successful mocked live call produces a VALID outcome",
      outcome.is_valid, str(outcome.validation_errors))
check("G2. exactly one attempt was recorded", len(outcome.attempts) == 1)
check("G3. the recorded attempt's request_succeeded is True", outcome.attempts[0].request_succeeded)
check("G4. the recorded attempt captures stop_reason/usage from response_meta",
      outcome.attempts[0].stop_reason == "end_turn" and outcome.attempts[0].output_tokens == 500)
check("G5. retrieval is attached to the outcome",
      outcome.retrieval is not None and len(outcome.retrieval.documents) == 5)

# ===========================================================================
# H. Malformed JSON -- both attempts fail, diagnostics preserve raw_text
# ===========================================================================


def _malformed_json_stub(candidate_story, retrieval_bundle, corpus, api_key):
    try:
        json.loads("{not valid json")
    except json.JSONDecodeError as e:
        raise ea.EvidenceAnalystJSONDecodeError(
            e, raw_text="{not valid json",
            content_blocks=[{"type": "text", "text": "{not valid json"}],
            response_meta={
                "stop_reason": "end_turn", "stop_sequence": None,
                "model": ea.EVIDENCE_ANALYST_MODEL,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        ) from e


outcome = ea.run_live_evidence_analysis(
    CS01_STORY, DEV_CORPUS, api_key="fake-key-not-real",
    call_analyst=_malformed_json_stub, retrieve=_fake_retrieve_live,
)
check("H1. malformed JSON on every attempt is reported via failure_reason, not raised",
      outcome.failure_reason == "evidence_analyst_call_failed")
check("H2. a malformed-JSON outcome is not valid", not outcome.is_valid)
check("H3. both configured attempts were made", len(outcome.attempts) == ea.EVIDENCE_ANALYST_MAX_ATTEMPTS)
check("H4. the attempt diagnostics preserve the raw (unparseable) text",
      outcome.attempts[0].raw_text == "{not valid json" and outcome.attempts[0].parse_succeeded is False)
check("H5. request_succeeded is True (a response body WAS received, parsing just failed)",
      outcome.attempts[0].request_succeeded is True)

# ===========================================================================
# I. max_tokens truncation is distinguishable in the attempt diagnostics
# ===========================================================================


def _max_tokens_truncation_stub(candidate_story, retrieval_bundle, corpus, api_key):
    try:
        json.loads('{"story_id": "CS-01", "evidence_records": [')  # truncated mid-array
    except json.JSONDecodeError as e:
        raise ea.EvidenceAnalystJSONDecodeError(
            e, raw_text='{"story_id": "CS-01", "evidence_records": [',
            content_blocks=[{"type": "text", "text": '{"story_id": "CS-01", "evidence_records": ['}],
            response_meta={
                "stop_reason": "max_tokens", "stop_sequence": None,
                "model": ea.EVIDENCE_ANALYST_MODEL,
                "usage": {"input_tokens": 5000, "output_tokens": ea.EVIDENCE_ANALYST_MAX_TOKENS},
            },
        ) from e


outcome = ea.run_live_evidence_analysis(
    CS01_STORY, DEV_CORPUS, api_key="fake-key-not-real",
    call_analyst=_max_tokens_truncation_stub, retrieve=_fake_retrieve_live,
)
check("I1. max_tokens truncation outcome is not valid", not outcome.is_valid)
check("I2. the attempt diagnostics record stop_reason='max_tokens'",
      outcome.attempts[0].stop_reason == "max_tokens")
check("I3. output_tokens recorded equals the configured ceiling (proof of truncation, not natural stop)",
      outcome.attempts[0].output_tokens == ea.EVIDENCE_ANALYST_MAX_TOKENS)

# ===========================================================================
# J. Timeout/transport failure (no response body at all)
# ===========================================================================


def _timeout_stub(candidate_story, retrieval_bundle, corpus, api_key):
    raise TimeoutError("simulated Evidence Analyst request timeout")


outcome = ea.run_live_evidence_analysis(
    CS01_STORY, DEV_CORPUS, api_key="fake-key-not-real",
    call_analyst=_timeout_stub, retrieve=_fake_retrieve_live,
)
check("J1. a timeout/transport failure is reported via failure_reason, not raised",
      outcome.failure_reason == "evidence_analyst_call_failed")
check("J2. the attempt diagnostics record transport_error (no response body existed)",
      outcome.attempts[0].transport_error is not None
      and "simulated Evidence Analyst request timeout" in outcome.attempts[0].transport_error)
check("J3. request_succeeded is False for a transport-level failure",
      outcome.attempts[0].request_succeeded is False)
check("J4. raw_text stays None (never fabricated) for a transport-level failure",
      outcome.attempts[0].raw_text is None)

# ===========================================================================
# K. Bounded retry: attempt 1 fails transiently, attempt 2 succeeds
# ===========================================================================
_retry_calls = {"n": 0}


def _retry_then_succeed_stub(candidate_story, retrieval_bundle, corpus, api_key):
    _retry_calls["n"] += 1
    if _retry_calls["n"] == 1:
        raise ConnectionError("simulated transient connection failure")
    return _tuple_stub(candidate_story, retrieval_bundle, corpus, api_key)


outcome = ea.run_live_evidence_analysis(
    CS01_STORY, DEV_CORPUS, api_key="fake-key-not-real",
    call_analyst=_retry_then_succeed_stub, retrieve=_fake_retrieve_live,
)
check("K1. a transient failure followed by success yields a VALID outcome",
      outcome.is_valid, str(outcome.validation_errors))
check("K2. exactly two attempts were recorded (one retry)", len(outcome.attempts) == 2)
check("K3. attempt 1 recorded the transport failure",
      outcome.attempts[0].transport_error is not None)
check("K4. attempt 2 recorded success", outcome.attempts[1].request_succeeded is True)
check("K5. the retry count never exceeds EVIDENCE_ANALYST_MAX_ATTEMPTS",
      len(outcome.attempts) <= ea.EVIDENCE_ANALYST_MAX_ATTEMPTS)

# ===========================================================================
# L. A supplied, successfully-retrieved primary document cannot be
# silently dropped from evidence_records
# ===========================================================================


def _drops_one_document_stub(candidate_story, retrieval_bundle, corpus, api_key):
    raw = _valid_live_output(candidate_story, retrieval_bundle)
    # Drop the LAST evidence record (not referenced by any question_finding,
    # so this exercises only the silently-dropped-document check, not the
    # evidence_ids-must-exist check).
    raw["evidence_records"] = raw["evidence_records"][:-1]
    return raw


outcome = ea.run_live_evidence_analysis(
    CS01_STORY, DEV_CORPUS, api_key="fake-key-not-real",
    call_analyst=_drops_one_document_stub, retrieve=_fake_retrieve_live,
)
check("L1. silently dropping a supplied, successfully-retrieved document fails validation",
      not outcome.is_valid)
check("L2. the error explicitly names the silent-drop condition",
      any("silently dropped" in e for e in outcome.validation_errors), str(outcome.validation_errors))

# ===========================================================================
# M. research_status='insufficient_evidence' / contradictory evidence are
# both VALID live outcomes (epistemic rules, not failures)
# ===========================================================================


def _insufficient_evidence_live_stub(candidate_story, retrieval_bundle, corpus, api_key):
    questions = candidate_story["research_questions"]
    return {
        "story_id": candidate_story["story_id"],
        "research_status": "insufficient_evidence",
        "hypothesis_assessment": "unresolved",
        "question_findings": [
            {"question": q, "status": "unanswered", "finding": "", "evidence_ids": [], "confidence": "low"}
            for q in questions
        ],
        "evidence_records": [],
        "contradictions": [],
        "remaining_gaps": ["No primary documents could be retrieved."],
        "disconfirming_evidence_found": [],
        "overall_assessment": "No usable primary source material could be retrieved for this story.",
        "confidence": "low",
    }


# Pair this stub with a retrieval bundle where every document genuinely
# failed, so the "silently dropped" check has nothing to complain about.
def _all_failed_retrieve(candidate_story, corpus, **kwargs):
    docs = [
        er.RetrievedDocument(document_number=d, status="failed", failure_reason="primary_text_unavailable")
        for d in candidate_story["supporting_document_numbers"]
    ]
    return er.EvidenceRetrievalBundle(story_id=candidate_story["story_id"], documents=docs)


outcome = ea.run_live_evidence_analysis(
    CS01_STORY, DEV_CORPUS, api_key="fake-key-not-real",
    call_analyst=_insufficient_evidence_live_stub, retrieve=_all_failed_retrieve,
)
check("M1. research_status='insufficient_evidence' with zero evidence is a VALID live outcome",
      outcome.is_valid, str(outcome.validation_errors))


def _contradictory_evidence_stub(candidate_story, retrieval_bundle, corpus, api_key):
    raw = _valid_live_output(candidate_story, retrieval_bundle)
    raw["evidence_records"].append({
        "evidence_id": "EV-CONTRA",
        "document_number": None,
        "source_title": "Synthetic contradicting secondary source",
        "source_url": "https://example.com/contra",
        "source_type": "secondary",
        "primary_source": False,
        "retrieved_at": None,
        "source_identity_status": "not_applicable",
        "publication_date": None,
        "effective_date": None,
        "evidence_summary": "Synthetic evidence that contradicts the leading hypothesis.",
        "supports": [],
        "contradicts": ["preliminary_hypothesis"],
        "limitations": "",
    })
    raw["contradictions"] = [{
        "description": "EV-CONTRA conflicts with the primary-document evidence.",
        "evidence_ids": ["EV-CONTRA"],
        "significance": "major",
    }]
    raw["disconfirming_evidence_found"] = ["EV-CONTRA suggests the hypothesis may not hold."]
    return raw


outcome = ea.run_live_evidence_analysis(
    CS01_STORY, DEV_CORPUS, api_key="fake-key-not-real",
    call_analyst=_contradictory_evidence_stub, retrieve=_fake_retrieve_live,
)
check("N1. contradictory evidence is preserved and still produces a VALID outcome",
      outcome.is_valid, str(outcome.validation_errors))
check("N2. the contradiction and disconfirming evidence survive onto the outcome",
      len(outcome.raw["contradictions"]) == 1 and len(outcome.raw["disconfirming_evidence_found"]) == 1)

# ===========================================================================
# O. No credentials are ever persisted onto the outcome/diagnostics
# ===========================================================================
_SECRET_API_KEY = "sk-ant-TOTALLY-SECRET-TEST-KEY-should-never-appear-anywhere"
outcome = ea.run_live_evidence_analysis(
    CS01_STORY, DEV_CORPUS, api_key=_SECRET_API_KEY,
    call_analyst=_tuple_stub, retrieve=_fake_retrieve_live,
)
_serialized = json.dumps(outcome.raw) + json.dumps([a.to_dict() for a in outcome.attempts])
check("O1. the configured api_key never appears anywhere in the outcome or its diagnostics",
      _SECRET_API_KEY not in _serialized)
check("O2. EvidenceAnalystAttemptDiagnostics.to_dict() has no api_key/credential field at all",
      "api_key" not in outcome.attempts[0].to_dict()
      and "x-api-key" not in outcome.attempts[0].to_dict())

# ===========================================================================
# P. Confirm no real network call was EVER attempted anywhere above
# ===========================================================================
check("P1. no real network POST was attempted anywhere in this test file",
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
