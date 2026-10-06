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
# F. No Anthropic invocation anywhere in this test file
# ===========================================================================
check("F1. evidence_analyst.py imports no Anthropic/requests client at module scope",
      not hasattr(ea, "requests") and not hasattr(ea, "anthropic"))

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
