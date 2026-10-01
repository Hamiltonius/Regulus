#!/usr/bin/env python3
"""
Tests for the Stage 1 `change_type` -> deterministic DD gate contract.

Prior defect: Stage 1 emitted `change_type` as unconstrained free text
while dd_pipeline.HIGH_IMPACT_ACTIONS matched against exact controlled
strings Stage 1 was never told about. This is the smallest fix: a
canonical controlled vocabulary (dd_pipeline.CHANGE_TYPE_VALUES), shared
by regulus_v3.ANALYSIS_SCHEMA_PROMPT (single source of truth) and by
dd_pipeline.needs_due_diligence()'s deterministic validation.

Approved failure policy (Option 3): an invalid or "unknown" change_type
fails SAFE to DD escalation. It must never:
  - set analysis=None
  - suppress the alert / email / PDF processing
  - be silently normalized or fuzzy-matched into another category
  - be treated as routine

No micro-classifier, no new LLM call, no Stage 1 retry subsystem, no
changes to Stage 2/3/C2/source validation/model/token settings.

Run: python3 tests/test_stage1_change_type_contract.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == "FAIL" else ""))
    return condition


import dd_pipeline as ddp  # noqa: E402

DOC = {"title": "Some Federal Register document", "abstract": "Routine administrative notice."}


def analysis_with(change_type, **overrides):
    base = {
        "title": "t", "confidence": "High", "change_type": change_type,
        "countries": [], "unresolved_questions": [],
    }
    base.update(overrides)
    return base


# ===========================================================================
# 1. Canonical vocabulary preserves the existing 6 + adds the 6 new +
#    includes other/unknown
# ===========================================================================

EXPECTED_PRESERVED = {
    "entity_list_addition", "entity_list_removal", "license_policy_change",
    "country_group_change", "ccl_amendment", "sanctions_waiver",
}
EXPECTED_NEW = {
    "sanctions_regime_imposed", "sanctions_regime_terminated",
    "arms_embargo_imposed", "arms_embargo_removed",
    "terrorism_designation", "terrorism_designation_rescinded",
}
EXPECTED_FAILSAFE_VALUES = {"other", "unknown"}

check("1a. all 6 pre-existing change_type values preserved in CHANGE_TYPE_VALUES",
      EXPECTED_PRESERVED <= ddp.CHANGE_TYPE_VALUES)
check("1b. all 6 new high-impact change_type values added to CHANGE_TYPE_VALUES",
      EXPECTED_NEW <= ddp.CHANGE_TYPE_VALUES)
check("1c. 'other' and 'unknown' are part of the controlled vocabulary",
      EXPECTED_FAILSAFE_VALUES <= ddp.CHANGE_TYPE_VALUES)
check("1d. CHANGE_TYPE_VALUES has exactly 14 values (6 + 6 + other + unknown, no extras)",
      len(ddp.CHANGE_TYPE_VALUES) == 14, str(sorted(ddp.CHANGE_TYPE_VALUES)))

# ===========================================================================
# 2. HIGH_IMPACT_ACTIONS: existing 6 preserved + 6 new added; other/unknown excluded
# ===========================================================================

check("2a. all 6 pre-existing HIGH_IMPACT_ACTIONS preserved",
      EXPECTED_PRESERVED <= ddp.HIGH_IMPACT_ACTIONS)
check("2b. all 6 new high-impact categories added to HIGH_IMPACT_ACTIONS",
      EXPECTED_NEW <= ddp.HIGH_IMPACT_ACTIONS)
check("2c. 'other' is NOT in HIGH_IMPACT_ACTIONS", "other" not in ddp.HIGH_IMPACT_ACTIONS)
check("2d. 'unknown' is NOT in HIGH_IMPACT_ACTIONS", "unknown" not in ddp.HIGH_IMPACT_ACTIONS)
check("2e. HIGH_IMPACT_ACTIONS has exactly 12 values", len(ddp.HIGH_IMPACT_ACTIONS) == 12,
      str(sorted(ddp.HIGH_IMPACT_ACTIONS)))

# ===========================================================================
# 3. is_valid_change_type / change_type_requires_failsafe_escalation
# ===========================================================================

for value in sorted(ddp.CHANGE_TYPE_VALUES):
    check(f"3. every controlled value is accepted as valid: {value!r}", ddp.is_valid_change_type(value))

for bad in ["Entity List Addition", "entity list addition", "", None, 123, ["entity_list_addition"], "made up"]:
    check(f"3b. non-controlled/malformed value rejected: {bad!r}", not ddp.is_valid_change_type(bad))

check("3c. a missing change_type (None) requires fail-safe escalation",
      ddp.change_type_requires_failsafe_escalation(None))
check("3d. an unrecognized free-text change_type requires fail-safe escalation",
      ddp.change_type_requires_failsafe_escalation("Entity List Addition"))
check("3e. a non-string change_type requires fail-safe escalation",
      ddp.change_type_requires_failsafe_escalation(123))
check("3f. the valid, explicit 'unknown' value ALSO requires fail-safe escalation",
      ddp.change_type_requires_failsafe_escalation("unknown"))
check("3g. a valid 'other' value does NOT require fail-safe escalation",
      not ddp.change_type_requires_failsafe_escalation("other"))
for value in sorted(ddp.HIGH_IMPACT_ACTIONS):
    check(f"3h. a valid high-impact value does not itself require fail-safe escalation: {value!r}",
          not ddp.change_type_requires_failsafe_escalation(value))

# ===========================================================================
# 4. needs_due_diligence: unknown/invalid -> DD YES (fail-safe), via the
#    REAL gate function with Stage-1-shaped input, not isolated strings
# ===========================================================================

check("4a. unknown change_type -> DD YES",
      ddp.needs_due_diligence(analysis_with("unknown"), DOC))
check("4b. invalid free-text change_type -> DD YES",
      ddp.needs_due_diligence(analysis_with("Entity List Addition (new)"), DOC))
check("4c. missing change_type key entirely -> DD YES", ddp.needs_due_diligence(
    {"title": "t", "confidence": "High", "countries": [], "unresolved_questions": []}, DOC))
check("4d. change_type explicitly None -> DD YES", ddp.needs_due_diligence(analysis_with(None), DOC))
check("4e. non-string change_type (wrong type from a malformed Stage 1 response) -> DD YES",
      ddp.needs_due_diligence(analysis_with(["entity_list_addition"]), DOC))

# ===========================================================================
# 5. every HIGH_IMPACT_ACTION deterministically -> DD YES
# ===========================================================================

for action in sorted(ddp.HIGH_IMPACT_ACTIONS):
    check(f"5. HIGH_IMPACT_ACTION {action!r} -> DD YES",
          ddp.needs_due_diligence(analysis_with(action), DOC))

# ===========================================================================
# 6. 'other' + High confidence + no other trigger -> DD NO (routine stays routine)
# ===========================================================================

routine_doc = {"title": "Minor technical amendment", "abstract": "No substantive change."}
check("6. 'other' + High confidence + no country/unresolved-question trigger -> DD NO",
      ddp.needs_due_diligence(analysis_with("other"), routine_doc) is False)

# A genuinely routine, non-enum legacy value should ALSO escalate now (it's
# invalid, not "other") -- proves we didn't quietly carve out an exception
# for old-style free text.
check("6b. an old-style free-text 'correction' (never in the controlled vocabulary) -> DD YES "
      "(invalid, fails safe -- this is the actual bug being fixed)",
      ddp.needs_due_diligence(analysis_with("correction"), routine_doc))

# ===========================================================================
# 7. invalid classification must NOT discard the rest of Stage 1 analysis
# ===========================================================================

rich_analysis = analysis_with(
    "not-a-real-value",
    summary="Full Stage 1 summary text.", entities=["Acme Corp"], eccns=["3A001"],
)
# needs_due_diligence is a pure predicate -- it must not mutate its input.
_before = dict(rich_analysis)
result = ddp.needs_due_diligence(rich_analysis, DOC)
check("7a. gate call does not mutate the analysis dict", rich_analysis == _before, str(rich_analysis))
check("7b. gate call preserves every other Stage 1 field intact",
      rich_analysis["summary"] == "Full Stage 1 summary text."
      and rich_analysis["entities"] == ["Acme Corp"]
      and rich_analysis["eccns"] == ["3A001"])
check("7c. and still escalates to DD on the invalid change_type", result is True)

# ===========================================================================
# 8. existing OR-branches are unchanged
# ===========================================================================

# confidence trigger
check("8a. confidence != High -> DD YES (unchanged)",
      ddp.needs_due_diligence(analysis_with("other", confidence="Medium"), routine_doc))
check("8b. confidence == High + valid non-high-impact change_type + no other trigger -> DD NO",
      ddp.needs_due_diligence(analysis_with("other", confidence="High"), routine_doc) is False)

# jurisdiction trigger (via analysis.countries)
check("8c. HIGH_CONTEXT_JURISDICTIONS country match -> DD YES (unchanged)",
      ddp.needs_due_diligence(analysis_with("other", countries=["Syria"]), routine_doc))

# unresolved_questions trigger
check("8d. non-empty unresolved_questions -> DD YES (unchanged)",
      ddp.needs_due_diligence(analysis_with("other", unresolved_questions=["is this final?"]), routine_doc))

# raw-text jurisdiction fallback (doc title/abstract, independent of Stage 1 tagging)
raw_text_doc = {"title": "Sanctions update", "abstract": "Concerns the situation in Syria."}
check("8e. raw-text jurisdiction fallback on doc title/abstract -> DD YES (unchanged)",
      ddp.needs_due_diligence(analysis_with("other", countries=[]), raw_text_doc))

# and the negative control: none of the triggers fire -> DD NO
check("8f. negative control: valid 'other', High confidence, no jurisdiction, no unresolved "
      "questions, no high-impact action -> DD NO",
      ddp.needs_due_diligence(analysis_with("other"), routine_doc) is False)

# ===========================================================================
# 9. ANALYSIS_SCHEMA_PROMPT actually carries the controlled vocabulary
#    (single source of truth, not a second hand-copied list)
# ===========================================================================

import regulus_v3 as rv  # noqa: E402

check("9a. ANALYSIS_SCHEMA_PROMPT contains every CHANGE_TYPE_VALUES entry",
      all(v in rv.ANALYSIS_SCHEMA_PROMPT for v in ddp.CHANGE_TYPE_VALUES))
check("9b. ANALYSIS_SCHEMA_PROMPT instructs change_type MUST be exactly one controlled value",
      "MUST be exactly one value from this controlled" in rv.ANALYSIS_SCHEMA_PROMPT)
check("9c. ANALYSIS_SCHEMA_PROMPT does not ask Stage 1 to judge importance/DD-worthiness",
      "due diligence" not in rv.ANALYSIS_SCHEMA_PROMPT.lower()
      and "escalat" not in rv.ANALYSIS_SCHEMA_PROMPT.lower())

# ===========================================================================
# 10. Scope guarantees: Stage 2/3/C2/source-identity untouched by this round
# ===========================================================================

import dd_schema as schema  # noqa: E402

check("10a. dd_schema.validate_stage3_sources (C2) still rejects a fabricated source (unchanged)",
      len(schema.validate_stage3_sources(
          [{"url": "https://not-real.example.com", "source_type": "x", "agency": "", "date": "",
            "supports": [], "primary_source": False}], [])) == 1)
check("10b. STAGE2_MAX_TOKENS/STAGE3_MAX_TOKENS/STAGE3_TIMEOUT_SECONDS untouched",
      ddp.STAGE2_MAX_TOKENS == 20000 and ddp.STAGE3_MAX_TOKENS == 4000
      and ddp.STAGE3_TIMEOUT_SECONDS == 180)
check("10c. STAGE2_MODEL/STAGE3_MODEL untouched",
      ddp.STAGE2_MODEL == "claude-sonnet-4-6" and ddp.STAGE3_MODEL == "claude-sonnet-4-6")
check("10d. no Stage 1 retry constant was introduced (no retry subsystem added)",
      not hasattr(rv, "STAGE1_MAX_ATTEMPTS"))

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
