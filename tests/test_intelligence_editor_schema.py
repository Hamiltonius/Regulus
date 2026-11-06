#!/usr/bin/env python3
"""
Tests for intelligence_editor_schema.py — deterministic structural
validation of the Intelligence Editor's output (REGULUS INTELLIGENCE
BRIEF #001).

No live Anthropic call is made anywhere in this file -- every "Brief"
validated here is a hand-built dict, never a model response. editor_inputs
are hand-built, schema-shaped stand-ins built around the REAL CS-01 story
from the golden Corpus Analyst fixture (for title/materiality context) --
validate_brief only reads editor_inputs' own fields (story_id,
pass2_output, evidence_ids), so it does not need a real live Pass #2
artifact to be tested.

Run: python3 tests/test_intelligence_editor_schema.py
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


import intelligence_editor_schema as es
from _evidence_test_helpers import load_acceptance_3, get_story

ACCEPTANCE_3 = load_acceptance_3()
CS01_STORY = get_story(ACCEPTANCE_3, "CS-01")


def _pass2_output(story_id, disposition="confirmed_with_modification",
                   removed_claim=None, contradicted_hyp=None):
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
        "editor_eligibility": "eligible",
        "editor_caveats": [],
    }


EDITOR_INPUTS = [
    {
        "story_id": "CS-01",
        "original_story": CS01_STORY,
        "pass2_output": _pass2_output(
            "CS-01",
            removed_claim="The withdrawn claim that should never resurface.",
            contradicted_hyp="The contradicted alternative hypothesis that should never resurface.",
        ),
        "evidence_ids": ["EV-1", "EV-2"],
    },
    {
        "story_id": "CS-02",
        "original_story": {"story_id": "CS-02", "title": "Second story", "candidate_materiality": "low"},
        "pass2_output": _pass2_output("CS-02"),
        "evidence_ids": ["EV-3"],
    },
]


def make_valid_brief():
    return {
        "brief_id": "BRIEF-001",
        "reporting_period": {"start": "2026-07-01", "end": "2026-09-30"},
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
        "cross_agency_signals": {
            "summary": "Synthetic cross-agency summary.",
            "items": [],
        },
        "emerging_patterns": {
            "summary": "Synthetic emerging patterns summary.",
            "items": [{"text": "Pattern across CS-01 and CS-02.", "story_ids": ["CS-01", "CS-02"], "evidence_ids": []}],
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


# ===========================================================================
# A. A structurally valid Brief passes
# ===========================================================================
valid_brief = make_valid_brief()
result = es.validate_brief(valid_brief, editor_inputs=EDITOR_INPUTS)
check("A1. structurally complete Brief is valid", result.is_valid, str(result.validation_errors))

# ===========================================================================
# B. Missing top-level required fields
# ===========================================================================
bad = copy.deepcopy(valid_brief)
del bad["executive_assessment"]
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("B1. missing top-level required field fails", not r.is_valid)

# ===========================================================================
# C. Section shape — missing summary/items
# ===========================================================================
bad = copy.deepcopy(valid_brief)
del bad["regulatory_tempo"]["items"]
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("C1. itemized section missing 'items' fails", not r.is_valid)

bad = copy.deepcopy(valid_brief)
bad["key_developments"] = "not an object"
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("C2. non-object itemized section fails", not r.is_valid)

# Empty items arrays are fine where a section genuinely has nothing.
good = copy.deepcopy(valid_brief)
good["cross_agency_signals"]["items"] = []
r = es.validate_brief(good, editor_inputs=EDITOR_INPUTS)
check("C3. an empty items array is valid", r.is_valid, str(r.validation_errors))

# ===========================================================================
# D. Section item required fields
# ===========================================================================
bad = copy.deepcopy(valid_brief)
del bad["key_developments"]["items"][0]["evidence_ids"]
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("D1. section item missing 'evidence_ids' fails", not r.is_valid)

bad = copy.deepcopy(valid_brief)
bad["key_developments"]["items"][0] = "not an object"
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("D2. non-object section item fails", not r.is_valid)

# ===========================================================================
# E. TRACEABILITY — story_ids must be among those the Editor was given
# ===========================================================================
bad = copy.deepcopy(valid_brief)
bad["key_developments"]["items"][0]["story_ids"] = ["CS-99-NEVER-GIVEN"]
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("E1. an item citing a story_id never supplied to the Editor fails", not r.is_valid)
check("E2. the error names the offending story_id",
      any("CS-99-NEVER-GIVEN" in e for e in r.validation_errors), str(r.validation_errors))

# ===========================================================================
# F. TRACEABILITY — evidence_ids must belong to the item's own cited
# story_ids' evidence packages; an item may not borrow another story's
# evidence
# ===========================================================================
bad = copy.deepcopy(valid_brief)
# CS-01's item citing CS-02's evidence (EV-3) -- cross-story borrowing.
bad["key_developments"]["items"][0]["story_ids"] = ["CS-01"]
bad["key_developments"]["items"][0]["evidence_ids"] = ["EV-3"]
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("F1. an item borrowing another story's evidence_id fails", not r.is_valid)
check("F2. the error names the borrowed evidence_id",
      any("EV-3" in e for e in r.validation_errors), str(r.validation_errors))

bad = copy.deepcopy(valid_brief)
bad["key_developments"]["items"][0]["evidence_ids"] = ["EV-INVENTED"]
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("F3. an item citing an invented evidence_id fails", not r.is_valid)

# An item citing both stories may cite the union of their evidence.
good = copy.deepcopy(valid_brief)
good["emerging_patterns"]["items"][0]["story_ids"] = ["CS-01", "CS-02"]
good["emerging_patterns"]["items"][0]["evidence_ids"] = ["EV-1", "EV-3"]
r = es.validate_brief(good, editor_inputs=EDITOR_INPUTS)
check("F4. an item citing both stories may cite the union of their evidence_ids",
      r.is_valid, str(r.validation_errors))

# ===========================================================================
# G. NO RESURRECTION — verbatim reuse of a removed claim or a
# contradicted/weakened alternative hypothesis is rejected, anywhere in a
# free-text field
# ===========================================================================
REMOVED_CLAIM = "The withdrawn claim that should never resurface."
CONTRADICTED_HYP = "The contradicted alternative hypothesis that should never resurface."

bad = copy.deepcopy(valid_brief)
bad["executive_assessment"] = f"As before: {REMOVED_CLAIM}"
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("G1. verbatim resurrection of a removed claim in executive_assessment fails", not r.is_valid)
check("G2. the error flags the resurrected text",
      any("resurrect" in e for e in r.validation_errors), str(r.validation_errors))

bad = copy.deepcopy(valid_brief)
bad["watchlist"]["items"][0]["text"] = CONTRADICTED_HYP
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("G3. verbatim resurrection of a contradicted hypothesis in a section item fails", not r.is_valid)

bad = copy.deepcopy(valid_brief)
bad["key_developments"]["summary"] = f"Summary mentioning: {REMOVED_CLAIM}"
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("G4. verbatim resurrection inside a section 'summary' field fails", not r.is_valid)

# Describing that something was investigated and rejected, without restating
# the rejected text verbatim, is fine (schema only catches literal reuse).
good = copy.deepcopy(valid_brief)
good["executive_assessment"] = "An earlier claim about this story was investigated and found unsupported."
r = es.validate_brief(good, editor_inputs=EDITOR_INPUTS)
check("G5. paraphrased, non-verbatim mention of a rejected claim is valid "
      "(schema catches literal reuse only, per its documented scope)",
      r.is_valid, str(r.validation_errors))

# ===========================================================================
# H. Enum validation
# ===========================================================================
bad = copy.deepcopy(valid_brief)
bad["confidence"] = "bogus"
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("H1. invalid confidence enum fails", not r.is_valid)

# ===========================================================================
# I. methodology_and_sources
# ===========================================================================
bad = copy.deepcopy(valid_brief)
del bad["methodology_and_sources"]["evidence_ids_referenced"]
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("I1. methodology_and_sources missing a required field fails", not r.is_valid)

bad = copy.deepcopy(valid_brief)
bad["methodology_and_sources"]["stories_included"] = ["CS-99-NEVER-GIVEN"]
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("I2. methodology_and_sources.stories_included citing an unsupplied story_id fails", not r.is_valid)

bad = copy.deepcopy(valid_brief)
bad["methodology_and_sources"]["evidence_ids_referenced"] = ["EV-INVENTED"]
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("I3. methodology_and_sources.evidence_ids_referenced citing an invented id fails", not r.is_valid)

bad = copy.deepcopy(valid_brief)
del bad["methodology_and_sources"]["stories_excluded"][:]
bad["methodology_and_sources"]["stories_excluded"].append({"story_id": "CS-03"})  # missing "reason"
r = es.validate_brief(bad, editor_inputs=EDITOR_INPUTS)
check("I4. a stories_excluded entry missing 'reason' fails", not r.is_valid)

# ===========================================================================
# J. STORY EXCLUSION IS ALLOWED — a Brief that excludes a candidate story
# entirely (not forced into sections 1-7) is still a valid shape
# ===========================================================================
excluding_brief = make_valid_brief()
excluding_brief["key_developments"]["items"] = [
    i for i in excluding_brief["key_developments"]["items"] if "CS-02" not in i["story_ids"]
]
excluding_brief["watchlist"]["items"] = []
excluding_brief["emerging_patterns"]["items"] = [
    i for i in excluding_brief["emerging_patterns"]["items"] if "CS-02" not in i["story_ids"]
]
excluding_brief["stories_included"] = ["CS-01"]
excluding_brief["stories_excluded"] = [{"story_id": "CS-02", "reason": "Administrative, not substantive."}]
excluding_brief["methodology_and_sources"]["stories_included"] = ["CS-01"]
excluding_brief["methodology_and_sources"]["stories_excluded"] = [
    {"story_id": "CS-02", "reason": "Administrative, not substantive."}
]
excluding_brief["methodology_and_sources"]["evidence_ids_referenced"] = ["EV-1", "EV-2"]
r = es.validate_brief(excluding_brief, editor_inputs=EDITOR_INPUTS)
check("J1. a Brief excluding a candidate story entirely from sections 1-7 is a valid shape",
      r.is_valid, str(r.validation_errors))

# ===========================================================================
# K. root-level argument checks
# ===========================================================================
r = es.validate_brief("not a dict", editor_inputs=EDITOR_INPUTS)
check("K1. a non-dict raw Brief fails", not r.is_valid)

r = es.validate_brief(valid_brief, editor_inputs="not a list")
check("K2. a non-list editor_inputs argument fails", not r.is_valid)

# ===========================================================================
# L. ground truth derives from editor_inputs, never from raw itself
# ===========================================================================
narrower_inputs = [EDITOR_INPUTS[0]]  # only CS-01 supplied this time
r = es.validate_brief(valid_brief, editor_inputs=narrower_inputs)
check("L1. a Brief referencing a story not in THIS call's editor_inputs fails "
      "(ground truth is the editor_inputs argument, not anything in raw)", not r.is_valid)

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
