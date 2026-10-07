#!/usr/bin/env python3
"""
intelligence_analyst_pass2_schema.py — deterministic structural validation
for the Intelligence Analyst's SECOND pass (evidence reassessment) over
exactly ONE candidate_story.

    corpus_analyst candidate_story (Pass #1)
            + validated Evidence Analyst package (for the same story)
            -> INTELLIGENCE ANALYST PASS #2 (intelligence_analyst_pass2.py)
            -> structured reassessment (validated by THIS module)
            -> (future) Intelligence Editor

This is a NEW, SMALL, standalone schema -- mirroring corpus_analyst_schema.py
and evidence_analyst_schema.py's own pattern exactly (same helper
functions, same ValidationResult-style result object, same "reject on
missing/invalid, never silently repair" discipline). It does not import,
reuse, or modify dd_schema.py, dd_pipeline.py, corpus_analyst_schema.py,
evidence_analyst_schema.py, or regulus_v3.py. Nothing here touches Stage
1/2/3 validation, the DD Gate, retrieval, or any production database
behavior.

SCOPE: Pass #2 is an ANALYTICAL REASSESSMENT step, not a research step.
It reasons only over the original candidate_story (Pass #1) and the
already-validated Evidence Analyst package for that same story -- it may
not invent facts, and every evidence_id it cites anywhere must already
exist in the supplied evidence package's evidence_records. This module
enforces exactly that traceability, plus the required schema shape, enum
values, and the "no claim silently dropped" discipline already
established for the Corpus Analyst's research_questions and the Evidence
Analyst's evidence/question coverage.

What this module deliberately does NOT check: whether a disposition is
intellectually CORRECT, whether the revised_hypothesis is a good one, or
whether the analyst's reasoning is sound. Those are analytical judgments
reserved for the model and human review -- this module only enforces the
SHAPE of the output and its traceability back to real, existing input
data (the original_story's alternative_hypotheses, and the evidence
package's own evidence_ids).
"""

from dataclasses import dataclass, field
from typing import Any, Optional


ASSESSMENT_DISPOSITION_VALUES = {
    "confirmed", "confirmed_with_modification", "weakened", "contradicted", "insufficient_evidence",
}
MATERIAL_CHANGE_DISPOSITION_VALUES = {"retained", "modified", "removed", "unresolved"}
ALT_HYPOTHESIS_DISPOSITION_VALUES = {
    "supported", "partially_supported", "weakened", "contradicted", "unresolved",
}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
EDITOR_ELIGIBILITY_VALUES = {"eligible", "eligible_with_caveats", "not_eligible"}

_TOP_LEVEL_REQUIRED_FIELDS = [
    "story_id", "assessment_disposition", "original_hypothesis", "revised_hypothesis",
    "material_changes", "supported_findings", "weakened_or_rejected_findings",
    "remaining_uncertainties", "alternative_hypotheses_assessment", "intelligence_assessment",
    "confidence", "editor_eligibility", "editor_caveats",
]

_MATERIAL_CHANGE_REQUIRED_FIELDS = ["original_claim", "disposition", "revised_claim", "reason", "evidence_ids"]
_ALT_HYPOTHESIS_REQUIRED_FIELDS = ["hypothesis", "disposition", "explanation", "evidence_ids"]


@dataclass
class Pass2ValidationResult:
    is_valid: bool
    validation_errors: list = field(default_factory=list)


def _invalid(errors):
    return Pass2ValidationResult(is_valid=False, validation_errors=errors)


def _valid():
    return Pass2ValidationResult(is_valid=True, validation_errors=[])


def _err(errors, path, msg):
    errors.append(f"{path}: {msg}")


def _check_str(value, path, errors, allow_empty=False):
    if not isinstance(value, str):
        _err(errors, path, f"must be a string, got {type(value).__name__}")
        return False
    if not allow_empty and not value.strip():
        _err(errors, path, "must not be empty/blank")
        return False
    return True


def _check_enum(value, allowed, path, errors):
    if value not in allowed:
        _err(errors, path, f"must be one of {sorted(allowed)}, got {value!r}")
        return False
    return True


def _check_list(value, path, errors, *, nonempty):
    if not isinstance(value, list):
        _err(errors, path, f"must be a list, got {type(value).__name__}")
        return False
    if nonempty and len(value) == 0:
        _err(errors, path, "must be a non-empty list")
        return False
    return True


def _check_list_of_str(value, path, errors, *, nonempty):
    if not _check_list(value, path, errors, nonempty=nonempty):
        return False
    ok = True
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            _err(errors, f"{path}[{i}]", "must be a non-empty string")
            ok = False
    return ok


def _check_evidence_ids_exist(evidence_ids, valid_evidence_ids, path, errors):
    ok = True
    for i, eid in enumerate(evidence_ids):
        if eid not in valid_evidence_ids:
            _err(errors, f"{path}[{i}]",
                 f"evidence_id {eid!r} does not exist in the supplied Evidence Analyst "
                 "package's evidence_records (Pass #2 may not cite invented evidence)")
            ok = False
    return ok


def _validate_material_change(item, index, valid_evidence_ids, errors):
    path = f"material_changes[{index}]"
    if not isinstance(item, dict):
        _err(errors, path, f"must be an object, got {type(item).__name__}")
        return

    missing = [f for f in _MATERIAL_CHANGE_REQUIRED_FIELDS if f not in item]
    if missing:
        _err(errors, path, f"missing required field(s): {missing}")

    if "original_claim" in item:
        _check_str(item["original_claim"], f"{path}.original_claim", errors)
    if "reason" in item:
        _check_str(item["reason"], f"{path}.reason", errors)
    if "revised_claim" in item:
        _check_str(item["revised_claim"], f"{path}.revised_claim", errors, allow_empty=True)
    if "disposition" in item and isinstance(item["disposition"], str):
        _check_enum(item["disposition"], MATERIAL_CHANGE_DISPOSITION_VALUES,
                    f"{path}.disposition", errors)

    evidence_ids = item.get("evidence_ids")
    if "evidence_ids" in item:
        if _check_list_of_str(evidence_ids, f"{path}.evidence_ids", errors, nonempty=False):
            _check_evidence_ids_exist(evidence_ids, valid_evidence_ids, f"{path}.evidence_ids", errors)


def _validate_alt_hypothesis_assessment(item, index, valid_evidence_ids, errors):
    path = f"alternative_hypotheses_assessment[{index}]"
    if not isinstance(item, dict):
        _err(errors, path, f"must be an object, got {type(item).__name__}")
        return None

    missing = [f for f in _ALT_HYPOTHESIS_REQUIRED_FIELDS if f not in item]
    if missing:
        _err(errors, path, f"missing required field(s): {missing}")

    hypothesis = item.get("hypothesis")
    if "hypothesis" in item:
        _check_str(hypothesis, f"{path}.hypothesis", errors)
    if "explanation" in item:
        _check_str(item["explanation"], f"{path}.explanation", errors)
    if "disposition" in item and isinstance(item["disposition"], str):
        _check_enum(item["disposition"], ALT_HYPOTHESIS_DISPOSITION_VALUES,
                    f"{path}.disposition", errors)

    evidence_ids = item.get("evidence_ids")
    if "evidence_ids" in item:
        if _check_list_of_str(evidence_ids, f"{path}.evidence_ids", errors, nonempty=False):
            _check_evidence_ids_exist(evidence_ids, valid_evidence_ids, f"{path}.evidence_ids", errors)

    return hypothesis if isinstance(hypothesis, str) else None


def validate_pass2_reassessment(raw: Any, *, story_id: str, original_story: Any,
                                 evidence_package: Any) -> Pass2ValidationResult:
    """Validate an Intelligence Analyst Pass #2 output against the schema
    this module defines, for exactly ONE candidate_story.

    Args:
      raw: the candidate reassessment dict to validate.
      story_id: the story_id this run was invoked for -- the single source
        of truth story_id consistency is checked against (not merely
        original_story's own story_id, since a caller-level mismatch
        between story_id/original_story/evidence_package is itself an
        error Pass #2 must never paper over).
      original_story: the Pass #1 candidate_story dict this reassessment is
        about. Only original_story["alternative_hypotheses"] is read here
        (for 1:1 coverage checking) -- this function does not otherwise
        interpret the Corpus Analyst schema.
      evidence_package: the validated Evidence Analyst output dict for this
        same story. Only evidence_package["evidence_records"] is read here
        (to build the valid evidence_id set) -- this function does not
        otherwise interpret or re-validate the Evidence Analyst schema
        (that already happened upstream).
    """
    if not isinstance(raw, dict):
        return _invalid([f"root: must be an object, got {type(raw).__name__}"])
    if not isinstance(original_story, dict):
        return _invalid(["root: 'original_story' argument must be the Pass #1 candidate_story object"])
    if not isinstance(evidence_package, dict):
        return _invalid(["root: 'evidence_package' argument must be the Evidence Analyst output object"])

    errors: list = []

    missing = [f for f in _TOP_LEVEL_REQUIRED_FIELDS if f not in raw]
    if missing:
        _err(errors, "root", f"missing required top-level field(s): {missing}")
        return _invalid(errors)

    story_id_value = raw.get("story_id")
    if not _check_str(story_id_value, "root.story_id", errors):
        pass
    elif story_id_value != story_id:
        _err(errors, "root.story_id",
             f"{story_id_value!r} does not match the story_id this reassessment was invoked "
             f"for ({story_id!r}) -- unknown/mismatched story_id")

    if "assessment_disposition" in raw:
        _check_enum(raw.get("assessment_disposition"), ASSESSMENT_DISPOSITION_VALUES,
                    "root.assessment_disposition", errors)
    if "confidence" in raw:
        _check_enum(raw.get("confidence"), CONFIDENCE_LEVELS, "root.confidence", errors)
    if "editor_eligibility" in raw:
        _check_enum(raw.get("editor_eligibility"), EDITOR_ELIGIBILITY_VALUES,
                    "root.editor_eligibility", errors)

    if "original_hypothesis" in raw:
        _check_str(raw["original_hypothesis"], "root.original_hypothesis", errors)
    if "revised_hypothesis" in raw:
        _check_str(raw["revised_hypothesis"], "root.revised_hypothesis", errors)
    if "intelligence_assessment" in raw:
        _check_str(raw["intelligence_assessment"], "root.intelligence_assessment", errors)

    # --- valid evidence_id set, derived from the supplied (already-
    # validated) Evidence Analyst package -- never from raw itself. ---
    evidence_records = evidence_package.get("evidence_records")
    valid_evidence_ids = set()
    if isinstance(evidence_records, list):
        for rec in evidence_records:
            if isinstance(rec, dict) and isinstance(rec.get("evidence_id"), str):
                valid_evidence_ids.add(rec["evidence_id"])

    # --- material_changes ---
    if _check_list(raw["material_changes"], "root.material_changes", errors, nonempty=False):
        for i, item in enumerate(raw["material_changes"]):
            _validate_material_change(item, i, valid_evidence_ids, errors)

    # --- plain string-array fields ---
    _check_list_of_str(raw["supported_findings"], "root.supported_findings", errors, nonempty=False)
    _check_list_of_str(raw["weakened_or_rejected_findings"], "root.weakened_or_rejected_findings",
                        errors, nonempty=False)
    _check_list_of_str(raw["remaining_uncertainties"], "root.remaining_uncertainties", errors, nonempty=False)
    _check_list_of_str(raw["editor_caveats"], "root.editor_caveats", errors, nonempty=False)

    # --- alternative_hypotheses_assessment: 1:1 coverage of
    # original_story's own alternative_hypotheses (none dropped, none
    # duplicated, none substituted for different hypothesis text) --
    # mirrors the research_questions coverage discipline already
    # established for the Corpus Analyst / Evidence Analyst schemas. ---
    hypotheses_covered = []
    if _check_list(raw["alternative_hypotheses_assessment"], "root.alternative_hypotheses_assessment",
                   errors, nonempty=False):
        for i, item in enumerate(raw["alternative_hypotheses_assessment"]):
            h = _validate_alt_hypothesis_assessment(item, i, valid_evidence_ids, errors)
            if h is not None:
                hypotheses_covered.append(h)

    expected_hypotheses = original_story.get("alternative_hypotheses")
    if isinstance(expected_hypotheses, list):
        expected_set = set(expected_hypotheses)
        covered_set = set(hypotheses_covered)
        dropped = expected_set - covered_set
        if dropped:
            _err(errors, "root.alternative_hypotheses_assessment",
                 f"alternative_hypothesis/hypotheses from the original Pass #1 story were "
                 f"silently dropped (no assessment entry): {sorted(dropped)}")
        unexpected = covered_set - expected_set
        if unexpected:
            _err(errors, "root.alternative_hypotheses_assessment",
                 f"assessment entry/entries do not match any alternative_hypothesis supplied "
                 f"by the original Pass #1 story: {sorted(unexpected)}")
        dup_hypotheses = {h for h in hypotheses_covered if hypotheses_covered.count(h) > 1}
        if dup_hypotheses:
            _err(errors, "root.alternative_hypotheses_assessment",
                 f"duplicate assessment entries for the same alternative_hypothesis: {sorted(dup_hypotheses)}")

    if errors:
        return _invalid(errors)
    return _valid()
