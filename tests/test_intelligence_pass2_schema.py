#!/usr/bin/env python3
"""
Tests for intelligence_analyst_pass2_schema.py — deterministic structural
validation of a (future) Intelligence Analyst Pass #2 output for one
candidate_story.

No live Anthropic call is made anywhere in this file -- every "output"
validated here is a hand-built dict, never a model response. The
original_story used throughout is the REAL CS-01 story from the golden
Corpus Analyst fixture (tests/fixtures/corpus_analyst_acceptance_3.json);
the evidence_package is a hand-built, schema-shaped stand-in (Pass #2's
own schema only reads evidence_package["evidence_records"], so it does
not need a real live Evidence Analyst artifact to be tested).

Run: python3 tests/test_intelligence_pass2_schema.py
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


import intelligence_analyst_pass2_schema as p2s
from _evidence_test_helpers import load_acceptance_3, get_story

ACCEPTANCE_3 = load_acceptance_3()
CS01_STORY = get_story(ACCEPTANCE_3, "CS-01")
CS01_ALT_HYPOTHESES = CS01_STORY["alternative_hypotheses"]
assert len(CS01_ALT_HYPOTHESES) >= 1, "fixture assumption: CS-01 has alternative_hypotheses"

EVIDENCE_PACKAGE = {
    "story_id": "CS-01",
    "research_status": "partial",
    "evidence_records": [
        {"evidence_id": "EV-1", "document_number": "2026-18918"},
        {"evidence_id": "EV-2", "document_number": "2026-19161"},
    ],
}
VALID_EVIDENCE_IDS = {"EV-1", "EV-2"}


def make_valid_output(story=CS01_STORY, alt_hypotheses=None):
    """A hand-built, structurally complete Pass #2 output for CS-01 --
    one alternative_hypotheses_assessment entry per alt hypothesis, one
    material_change, all evidence_ids valid."""
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


# ===========================================================================
# A. A structurally valid output passes
# ===========================================================================
valid_output = make_valid_output()
result = p2s.validate_pass2_reassessment(
    valid_output, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE,
)
check("A1. structurally complete CS-01 reassessment is valid", result.is_valid, str(result.validation_errors))

# ===========================================================================
# B. Missing top-level required fields
# ===========================================================================
bad = copy.deepcopy(valid_output)
del bad["revised_hypothesis"]
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("B1. missing top-level required field fails", not r.is_valid)

# ===========================================================================
# C. story_id consistency
# ===========================================================================
bad = copy.deepcopy(valid_output)
bad["story_id"] = "CS-99"
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("C1. mismatched story_id fails", not r.is_valid)
check("C2. mismatched story_id error names both ids",
      "CS-99" in r.validation_errors[0] and "CS-01" in r.validation_errors[0], str(r.validation_errors))

# ===========================================================================
# D. Enum validation
# ===========================================================================
for field_name, bad_value in [
    ("assessment_disposition", "bogus"),
    ("confidence", "bogus"),
    ("editor_eligibility", "bogus"),
]:
    bad = copy.deepcopy(valid_output)
    bad[field_name] = bad_value
    r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
    check(f"D. invalid enum {field_name}={bad_value!r} fails", not r.is_valid, str(r.validation_errors))

bad = copy.deepcopy(valid_output)
bad["material_changes"][0]["disposition"] = "bogus"
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("D4. invalid material_changes[].disposition fails", not r.is_valid)

bad = copy.deepcopy(valid_output)
bad["alternative_hypotheses_assessment"][0]["disposition"] = "bogus"
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("D5. invalid alternative_hypotheses_assessment[].disposition fails", not r.is_valid)

# ===========================================================================
# E. nonempty revised_hypothesis / intelligence_assessment
# ===========================================================================
bad = copy.deepcopy(valid_output)
bad["revised_hypothesis"] = ""
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("E1. empty revised_hypothesis fails", not r.is_valid)

bad = copy.deepcopy(valid_output)
bad["intelligence_assessment"] = "   "
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("E2. blank intelligence_assessment fails", not r.is_valid)

# ===========================================================================
# F. Evidence ID referential integrity
# ===========================================================================
bad = copy.deepcopy(valid_output)
bad["material_changes"][0]["evidence_ids"] = ["EV-DOES-NOT-EXIST"]
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("F1. material_changes citing a nonexistent evidence_id fails", not r.is_valid)
check("F2. error names the offending evidence_id",
      any("EV-DOES-NOT-EXIST" in e for e in r.validation_errors), str(r.validation_errors))

bad = copy.deepcopy(valid_output)
bad["alternative_hypotheses_assessment"][0]["evidence_ids"] = ["EV-GHOST"]
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("F3. alternative_hypotheses_assessment citing a nonexistent evidence_id fails", not r.is_valid)

# ===========================================================================
# G. material_changes structure
# ===========================================================================
bad = copy.deepcopy(valid_output)
del bad["material_changes"][0]["reason"]
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("G1. material_changes entry missing 'reason' fails", not r.is_valid)

bad = copy.deepcopy(valid_output)
bad["material_changes"][0]["original_claim"] = ""
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("G2. material_changes entry with empty original_claim fails", not r.is_valid)

bad = copy.deepcopy(valid_output)
bad["material_changes"][0] = "not an object"
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("G3. non-object material_changes entry fails", not r.is_valid)

# revised_claim may legitimately be empty (e.g. disposition="removed")
good = copy.deepcopy(valid_output)
good["material_changes"][0]["disposition"] = "removed"
good["material_changes"][0]["revised_claim"] = ""
r = p2s.validate_pass2_reassessment(good, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("G4. disposition='removed' with an empty revised_claim is still valid", r.is_valid, str(r.validation_errors))

# ===========================================================================
# H. alternative_hypotheses_assessment: 1:1 coverage of original_story's
# own alternative_hypotheses (none dropped, none duplicated, none
# substituted)
# ===========================================================================
bad = copy.deepcopy(valid_output)
bad["alternative_hypotheses_assessment"] = bad["alternative_hypotheses_assessment"][1:]
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("H1. a silently dropped alternative_hypothesis fails", not r.is_valid)
check("H2. error names the dropped hypothesis",
      any(CS01_ALT_HYPOTHESES[0] in e for e in r.validation_errors), str(r.validation_errors))

bad = copy.deepcopy(valid_output)
bad["alternative_hypotheses_assessment"].append(dict(bad["alternative_hypotheses_assessment"][0]))
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("H3. a duplicated alternative_hypotheses_assessment entry fails", not r.is_valid)

bad = copy.deepcopy(valid_output)
bad["alternative_hypotheses_assessment"][0]["hypothesis"] = "A hypothesis the original story never proposed."
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("H4. a substituted/invented alternative hypothesis fails (and the real one is now missing)", not r.is_valid)

# ===========================================================================
# I. Plain string-array fields reject non-string entries
# ===========================================================================
bad = copy.deepcopy(valid_output)
bad["supported_findings"] = [123]
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("I1. non-string entry in supported_findings fails", not r.is_valid)

bad = copy.deepcopy(valid_output)
bad["editor_caveats"] = [""]
r = p2s.validate_pass2_reassessment(bad, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("I2. blank entry in editor_caveats fails", not r.is_valid)

# Empty arrays are fine where genuinely nothing to report.
good = copy.deepcopy(valid_output)
good["supported_findings"] = []
good["weakened_or_rejected_findings"] = []
good["remaining_uncertainties"] = []
good["editor_caveats"] = []
r = p2s.validate_pass2_reassessment(good, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("I3. empty supported_findings/weakened_or_rejected_findings/remaining_uncertainties/editor_caveats are valid",
      r.is_valid, str(r.validation_errors))

# ===========================================================================
# J. editor_eligibility is an independent enum (no invented cross-field
# coupling to assessment_disposition -- schema validates only membership)
# ===========================================================================
good = copy.deepcopy(valid_output)
good["assessment_disposition"] = "contradicted"
good["editor_eligibility"] = "eligible"
r = p2s.validate_pass2_reassessment(good, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("J1. editor_eligibility is validated independently of assessment_disposition "
      "(no undocumented cross-field rule invented)", r.is_valid, str(r.validation_errors))

# ===========================================================================
# K. insufficient_evidence disposition with zero material_changes is a
# VALID shape (Pass #2 may conclude there isn't enough evidence to
# reassess material claims at all)
# ===========================================================================
insufficient = {
    "story_id": "CS-01",
    "assessment_disposition": "insufficient_evidence",
    "original_hypothesis": CS01_STORY["preliminary_hypothesis"],
    "revised_hypothesis": "Evidence remains insufficient to revise the hypothesis.",
    "material_changes": [],
    "supported_findings": [],
    "weakened_or_rejected_findings": [],
    "remaining_uncertainties": ["Nothing could be reassessed from the supplied evidence package."],
    "alternative_hypotheses_assessment": [
        {"hypothesis": h, "disposition": "unresolved", "explanation": "Insufficient evidence.", "evidence_ids": []}
        for h in CS01_ALT_HYPOTHESES
    ],
    "intelligence_assessment": "Insufficient evidence to reassess this story.",
    "confidence": "low",
    "editor_eligibility": "not_eligible",
    "editor_caveats": ["Insufficient evidence."],
}
r = p2s.validate_pass2_reassessment(insufficient, story_id="CS-01", original_story=CS01_STORY, evidence_package=EVIDENCE_PACKAGE)
check("K1. insufficient_evidence with zero material_changes is a VALID shape", r.is_valid, str(r.validation_errors))

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
