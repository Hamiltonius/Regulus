#!/usr/bin/env python3
"""
Tests for evidence_analyst_schema.py — deterministic structural
validation of a (future) Evidence Analyst output for one candidate_story.

No live Anthropic call is made anywhere in this file -- every "output"
validated here is a hand-built dict, never a model response.

Run: python3 tests/test_evidence_analyst_schema.py
"""
import copy
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


import evidence_analyst_schema as eas
from _evidence_test_helpers import load_acceptance_3, get_story, load_corpus_acceptance_3

ACCEPTANCE_3 = load_acceptance_3()
CS01_STORY = get_story(ACCEPTANCE_3, "CS-01")
CS01_DOCS = CS01_STORY["supporting_document_numbers"]
CS01_QUESTIONS = CS01_STORY["research_questions"]
DEV_CORPUS = load_corpus_acceptance_3()
VALID_DOC_NUMBERS = {o.document_number for o in DEV_CORPUS.observations}


def make_valid_output(story=CS01_STORY):
    """A hand-built, structurally complete Evidence Analyst output for
    CS-01 -- one question_finding per research question, two evidence
    records, one contradiction, all cross-referenced correctly."""
    questions = story["research_questions"]
    docs = story["supporting_document_numbers"]
    question_findings = [
        {
            "question": q,
            "status": "answered" if i == 0 else "unanswered",
            "finding": "Synthetic finding text." if i == 0 else "",
            "evidence_ids": ["EV-1"] if i == 0 else [],
            "confidence": "medium",
        }
        for i, q in enumerate(questions)
    ]
    return {
        "story_id": story["story_id"],
        "research_status": "partial",
        "hypothesis_assessment": "unresolved",
        "question_findings": question_findings,
        "evidence_records": [
            {
                "evidence_id": "EV-1",
                "document_number": docs[0],
                "source_title": "Synthetic primary document title",
                "source_url": f"https://www.federalregister.gov/documents/2026/09/16/{docs[0]}",
                "source_type": "primary",
                "primary_source": True,
                "retrieved_at": "2026-10-06T12:00:00+00:00",
                "source_identity_status": "verified",
                "publication_date": "2026-09-16",
                "effective_date": "2026-09-16",
                "relevant_excerpt": "Synthetic excerpt text from the primary source.",
                "supports": ["preliminary_hypothesis"],
                "contradicts": [],
                "limitations": "Excerpt only; full document not reviewed.",
            },
            {
                "evidence_id": "EV-2",
                "document_number": docs[1] if len(docs) > 1 else docs[0],
                "source_title": "Synthetic secondary context",
                "source_url": "https://example.com/context",
                "source_type": "secondary",
                "primary_source": False,
                "retrieved_at": None,
                "source_identity_status": "not_applicable",
                "publication_date": None,
                "effective_date": None,
                "evidence_summary": "Synthetic contextual evidence summary.",
                "supports": [],
                "contradicts": ["preliminary_hypothesis"],
                "limitations": "",
            },
        ],
        "contradictions": [
            {
                "description": "EV-2 contextual evidence is in tension with the leading hypothesis.",
                "evidence_ids": ["EV-2"],
                "significance": "minor",
            }
        ],
        "remaining_gaps": ["Upstream authority not yet confirmed."],
        "disconfirming_evidence_found": ["EV-2 suggests an alternative explanation."],
        "overall_assessment": "Synthetic overall assessment for schema-shape testing.",
        "confidence": "medium",
    }


# ===========================================================================
# A. A structurally valid output passes
# ===========================================================================
valid_output = make_valid_output()
result = eas.validate_evidence_analysis(
    valid_output, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS,
)
check("A1. structurally complete CS-01 output is valid", result.is_valid, str(result.validation_errors))

# ===========================================================================
# B. Unknown / mismatched story_id is rejected
# ===========================================================================
bad = copy.deepcopy(valid_output)
bad["story_id"] = "CS-99"
r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("B1. mismatched story_id fails", not r.is_valid)
check("B2. mismatched story_id error names both ids",
      "CS-99" in r.validation_errors[0] and "CS-01" in r.validation_errors[0], str(r.validation_errors))

# ===========================================================================
# C. Evidence references to nonexistent evidence_ids are rejected
# ===========================================================================
bad = copy.deepcopy(valid_output)
bad["question_findings"][0]["evidence_ids"] = ["EV-DOES-NOT-EXIST"]
r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("C1. question_finding citing a nonexistent evidence_id fails", not r.is_valid)
check("C2. error names the offending evidence_id",
      any("EV-DOES-NOT-EXIST" in e for e in r.validation_errors), str(r.validation_errors))

# ===========================================================================
# D. Unsupported document numbers are rejected
# ===========================================================================
bad = copy.deepcopy(valid_output)
bad["evidence_records"][0]["document_number"] = "9999-99999"
r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("D1. evidence_record citing a document_number outside the corpus fails", not r.is_valid)
check("D2. error names the invented document_number",
      any("9999-99999" in e for e in r.validation_errors), str(r.validation_errors))

# ===========================================================================
# E. Invalid enum values are rejected
# ===========================================================================
for field_name, bad_value in [
    ("research_status", "bogus"),
    ("hypothesis_assessment", "bogus"),
    ("confidence", "bogus"),
]:
    bad = copy.deepcopy(valid_output)
    bad[field_name] = bad_value
    r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
    check(f"E. invalid enum {field_name}={bad_value!r} fails", not r.is_valid, str(r.validation_errors))

bad = copy.deepcopy(valid_output)
bad["question_findings"][0]["status"] = "bogus"
r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("E4. invalid question_finding.status fails", not r.is_valid)

bad = copy.deepcopy(valid_output)
bad["evidence_records"][0]["source_identity_status"] = "bogus"
r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("E5. invalid source_identity_status fails", not r.is_valid)

# ===========================================================================
# F. Missing / dropped / duplicated research-question coverage
# ===========================================================================
bad = copy.deepcopy(valid_output)
bad["question_findings"] = bad["question_findings"][1:]  # drop the first question
r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("F1. a silently dropped research question fails", not r.is_valid)
check("F2. error names the dropped question",
      any(CS01_QUESTIONS[0] in e for e in r.validation_errors), str(r.validation_errors))

bad = copy.deepcopy(valid_output)
bad["question_findings"].append(dict(bad["question_findings"][0]))  # duplicate first question
r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("F3. a duplicated question_findings entry fails", not r.is_valid)

bad = copy.deepcopy(valid_output)
bad["question_findings"][0]["question"] = "A question the candidate_story never asked."
r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("F4. a substituted/invented question fails (and the real one is now missing)", not r.is_valid)

# ===========================================================================
# G. Source identity mismatch cross-check against retrieval_results
# ===========================================================================


class _FakeRetrievedDoc:
    def __init__(self, identity_status):
        self.identity_status = identity_status


good = copy.deepcopy(valid_output)
retrieval_results_agree = {CS01_DOCS[0]: _FakeRetrievedDoc("verified")}
r = eas.validate_evidence_analysis(
    good, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS,
    retrieval_results=retrieval_results_agree,
)
check("G1. evidence_record source_identity_status agreeing with retrieval passes", r.is_valid,
      str(r.validation_errors))

retrieval_results_disagree = {CS01_DOCS[0]: _FakeRetrievedDoc("mismatch")}
r = eas.validate_evidence_analysis(
    good, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS,
    retrieval_results=retrieval_results_disagree,
)
check("G2. evidence_record claiming 'verified' when retrieval found 'mismatch' fails", not r.is_valid)
check("G3. error names both the claimed and actual identity_status",
      any("verified" in e and "mismatch" in e for e in r.validation_errors), str(r.validation_errors))

# ===========================================================================
# H. Factual findings claiming evidence without evidence_ids
# ===========================================================================
bad = copy.deepcopy(valid_output)
bad["question_findings"][0]["status"] = "answered"
bad["question_findings"][0]["evidence_ids"] = []
r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("H1. status='answered' with zero evidence_ids fails", not r.is_valid)

# ===========================================================================
# I. Contradictions referencing nonexistent evidence
# ===========================================================================
bad = copy.deepcopy(valid_output)
bad["contradictions"][0]["evidence_ids"] = ["EV-GHOST"]
r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("I1. contradiction citing a nonexistent evidence_id fails", not r.is_valid)

bad = copy.deepcopy(valid_output)
bad["contradictions"][0]["evidence_ids"] = []
r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("I2. contradiction with zero evidence_ids fails (unsupported contradiction claim)", not r.is_valid)

# ===========================================================================
# J. Duplicate evidence IDs
# ===========================================================================
bad = copy.deepcopy(valid_output)
bad["evidence_records"][1]["evidence_id"] = "EV-1"  # collide with the first record
r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("J1. duplicate evidence_id values fail", not r.is_valid)
check("J2. error names the duplicated id", any("EV-1" in e for e in r.validation_errors), str(r.validation_errors))

# ===========================================================================
# K. research_status=insufficient_evidence is a VALID successful outcome
# (epistemic rule from spec section 4) -- zero evidence_records, zero
# answered questions, hypothesis_assessment=unresolved must still pass.
# ===========================================================================
insufficient = {
    "story_id": "CS-01",
    "research_status": "insufficient_evidence",
    "hypothesis_assessment": "unresolved",
    "question_findings": [
        {"question": q, "status": "unanswered", "finding": "", "evidence_ids": [], "confidence": "low"}
        for q in CS01_QUESTIONS
    ],
    "evidence_records": [],
    "contradictions": [],
    "remaining_gaps": ["No primary documents could be retrieved."],
    "disconfirming_evidence_found": [],
    "overall_assessment": "No usable primary source material could be retrieved for this story.",
    "confidence": "low",
}
r = eas.validate_evidence_analysis(insufficient, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("K1. insufficient_evidence with zero evidence_records is a VALID outcome", r.is_valid,
      str(r.validation_errors))

# ===========================================================================
# L. A non-unresolved hypothesis_assessment requires at least one evidence_record
# ===========================================================================
bad = copy.deepcopy(insufficient)
bad["hypothesis_assessment"] = "supported"
r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("L1. hypothesis_assessment='supported' with zero evidence_records fails", not r.is_valid)

# ===========================================================================
# M. relevant_excerpt-or-evidence_summary requirement
# ===========================================================================
bad = copy.deepcopy(valid_output)
bad["evidence_records"][0].pop("relevant_excerpt", None)
r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("M1. evidence_record with neither relevant_excerpt nor evidence_summary fails", not r.is_valid)

# ===========================================================================
# N. Missing top-level fields
# ===========================================================================
bad = copy.deepcopy(valid_output)
del bad["evidence_records"]
r = eas.validate_evidence_analysis(bad, story=CS01_STORY, valid_document_numbers=VALID_DOC_NUMBERS)
check("N1. missing top-level required field fails", not r.is_valid)

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
