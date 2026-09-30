#!/usr/bin/env python3
"""
dd_schema.py — Structural schemas and deterministic validators for the
Due Diligence (DD) pipeline defined in docs/REGULUS_DD_SPEC_v1.0.md
(commit content as of the docs/dd-spec-v1.0 branch — see NOTE at the
bottom of this docstring).

Scope of this module — first DD implementation slice, schemas + validation
ONLY:
  - Stage 2 evidence-package structural schema and validator
  - Stage 3 executive-brief structural schema and validator
  - validation_status (structural validity) kept strictly separate from
    research_status (evidence/research completeness) per spec rule 9 and
    the "Separate schema validity from research sufficiency" requirement.

Explicitly OUT of scope for this module/slice (do not add here):
  - the Gate (needs_due_diligence)
  - Stage 2 research/prompting (due_diligence_review)
  - Stage 3 synthesis/prompting (synthesize_final)
  - due_diligence_records persistence
  - any change to the alerts table or existing regulus_v3.py behavior

Hard constraints honored by this module:
  - No LLM calls.
  - No network/API calls.
  - No database access.
  - No side effects — pure functions over plain dicts.
  - Never silently repairs malformed input. A validator either accepts a
    record as structurally valid or reports every structural problem it
    found; it never guesses, coerces, or fills in a "reasonable" value.

NOTE on validation_status as a spec-listed field vs. validator output:
The frozen spec's Stage 2 JSON schema lists "validation_status" as a key
inside the evidence-package object the model would return. That is
architecturally inconsistent with the spec's own rule that validation
must be deterministic, non-LLM, and authoritative ("Do not use an LLM for
validation"): a model cannot certify its own structural validity. This
module therefore treats validation_status and validation_errors as
OUTPUTS computed exclusively by validate_stage2_record()/
validate_stage3_record(), never as trusted input fields. If an incoming
record happens to carry its own "validation_status"/"validation_errors"
keys (e.g. an upstream stub echoing the schema literally), those keys are
ignored for validation purposes and are not required for a record to
pass. This is flagged as a spec ambiguity in the implementation report
rather than assumed to be obviously correct.
"""

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enums allowed by the frozen spec
# ---------------------------------------------------------------------------

VALIDATION_STATUSES = {"valid", "invalid"}
RESEARCH_STATUSES = {"complete", "partial", "insufficient_data"}
CONFIDENCE_LEVELS = {"High", "Medium", "Low"}
TREND_CLASSIFICATIONS = {
    "consistent", "escalation", "relaxation", "reversal", "novel", "insufficient_data",
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    """Outcome of validating one record against a DD structural schema.

    validation_status describes STRUCTURAL VALIDITY ONLY — did the record
    match the required shape, types, and enum values. It says nothing about
    whether the underlying research/synthesis was good.

    research_status / due_diligence_confidence / confidence are echoed back
    from the input ONLY when they are themselves structurally valid enum
    values, independent of whether other parts of the record failed
    validation — a record can be validation_status="invalid" (e.g. a
    malformed nested object elsewhere) while still reporting a valid
    research_status, since the two concepts are deliberately decoupled.
    """
    validation_status: str                      # "valid" | "invalid"
    validation_errors: list = field(default_factory=list)   # list[str], empty iff valid
    research_status: Optional[str] = None        # Stage 2 only
    due_diligence_confidence: Optional[str] = None  # Stage 2 only
    confidence: Optional[str] = None              # Stage 3 only

    @property
    def is_valid(self) -> bool:
        return self.validation_status == "valid"


def _invalid(errors):
    return ValidationResult(validation_status="invalid", validation_errors=list(errors))


def _valid():
    return ValidationResult(validation_status="valid", validation_errors=[])


# ---------------------------------------------------------------------------
# Primitive shape checks — each appends to `errors` and returns nothing.
# None of these raise; malformed input is reported, never repaired.
# ---------------------------------------------------------------------------

def _err(errors, path, msg):
    errors.append(f"{path}: {msg}")


def _require_keys(obj, required_keys, path, errors):
    """Report every missing required key. Does not fail fast."""
    if not isinstance(obj, dict):
        _err(errors, path, f"expected object, got {type(obj).__name__}")
        return False
    missing = [k for k in required_keys if k not in obj]
    for k in missing:
        _err(errors, f"{path}.{k}", "missing required field")
    return not missing


def _check_str(value, path, errors, allow_none=False):
    if value is None:
        if allow_none:
            return
        _err(errors, path, "expected str, got None")
        return
    if not isinstance(value, str):
        _err(errors, path, f"expected str, got {type(value).__name__}")


def _check_bool(value, path, errors):
    if not isinstance(value, bool):
        _err(errors, path, f"expected bool, got {type(value).__name__}")


def _check_enum(value, allowed, path, errors):
    if not isinstance(value, str) or value not in allowed:
        _err(errors, path, f"invalid value {value!r}, must be one of {sorted(allowed)}")


def _check_list_of_str(value, path, errors):
    if not isinstance(value, list):
        _err(errors, path, f"expected list, got {type(value).__name__}")
        return
    for i, item in enumerate(value):
        if not isinstance(item, str):
            _err(errors, f"{path}[{i}]", f"expected str, got {type(item).__name__}")


def _check_dict(value, path, errors):
    if not isinstance(value, dict):
        _err(errors, path, f"expected object, got {type(value).__name__}")
        return False
    return True


# ---------------------------------------------------------------------------
# Stage 2 — evidence package
# ---------------------------------------------------------------------------

_STAGE2_TOP_LEVEL_REQUIRED = [
    "research_question", "current_event", "historical_context",
    "precedent_comparison", "legal_regulatory_effect", "scope",
    "impact_assessment", "follow_on_indicators", "open_questions",
    "sources", "research_status", "due_diligence_confidence",
]

_CURRENT_EVENT_LIST_FIELDS = ["agency", "authority", "jurisdictions", "entities", "controls_affected"]
_CURRENT_EVENT_STR_FIELDS = ["action", "date", "effective_date"]

_PRECEDENT_LIST_FIELDS = ["entities_involved", "authority"]
_PRECEDENT_STR_FIELDS = ["description", "mechanism"]

_LEGAL_EFFECT_LIST_FIELDS = ["changed", "unchanged", "superseded", "remaining_restrictions"]

_SCOPE_LIST_FIELDS = [
    "affected_countries", "affected_entities", "affected_item_categories",
    "affected_transaction_types", "affected_compliance_workflows",
]

_IMPACT_LIST_FIELDS = [
    "immediate", "operational", "licensing", "screening", "classification",
    "authorization_management",
]

_FOLLOW_ON_LIST_FIELDS = [
    "historically_observed_next_steps", "current_unresolved_actions", "items_to_monitor",
]

_SOURCE_REQUIRED_FIELDS = ["url", "source_type", "agency", "date", "supports", "primary_source"]


def _validate_source_item(item, path, errors):
    if not _check_dict(item, path, errors):
        return
    if not _require_keys(item, _SOURCE_REQUIRED_FIELDS, path, errors):
        return
    _check_str(item["url"], f"{path}.url", errors)
    _check_str(item["source_type"], f"{path}.source_type", errors)
    _check_str(item["agency"], f"{path}.agency", errors)
    _check_str(item["date"], f"{path}.date", errors)
    _check_list_of_str(item["supports"], f"{path}.supports", errors)
    _check_bool(item["primary_source"], f"{path}.primary_source", errors)


def validate_stage2_record(raw: Any) -> ValidationResult:
    """Deterministically validate a Stage 2 evidence-package record against
    the frozen spec's structural schema. No LLM/network/DB access.

    Missing required fields, invalid enum values, malformed nesting, or
    wrong field types all produce validation_status="invalid" with a
    validation_errors list describing every problem found (not just the
    first). A structurally valid record with no usable precedent is fully
    representable: validation_status="valid", research_status=
    "insufficient_data".
    """
    errors: list = []

    if not isinstance(raw, dict):
        return _invalid([f"root: expected object, got {type(raw).__name__}"])

    _require_keys(raw, _STAGE2_TOP_LEVEL_REQUIRED, "root", errors)

    # research_question
    if "research_question" in raw:
        _check_str(raw["research_question"], "root.research_question", errors)

    # current_event
    if "current_event" in raw and _check_dict(raw["current_event"], "root.current_event", errors):
        ce = raw["current_event"]
        _require_keys(ce, _CURRENT_EVENT_STR_FIELDS + _CURRENT_EVENT_LIST_FIELDS,
                       "root.current_event", errors)
        for f_ in _CURRENT_EVENT_STR_FIELDS:
            if f_ in ce:
                _check_str(ce[f_], f"root.current_event.{f_}", errors)
        for f_ in _CURRENT_EVENT_LIST_FIELDS:
            if f_ in ce:
                _check_list_of_str(ce[f_], f"root.current_event.{f_}", errors)

    # historical_context
    if "historical_context" in raw and _check_dict(raw["historical_context"], "root.historical_context", errors):
        hc = raw["historical_context"]
        _require_keys(hc, ["program_origin", "major_prior_actions", "most_relevant_precedent"],
                       "root.historical_context", errors)
        if "program_origin" in hc:
            _check_str(hc["program_origin"], "root.historical_context.program_origin", errors)
        if "major_prior_actions" in hc:
            _check_list_of_str(hc["major_prior_actions"], "root.historical_context.major_prior_actions", errors)
        if "most_relevant_precedent" in hc and _check_dict(
                hc["most_relevant_precedent"], "root.historical_context.most_relevant_precedent", errors):
            mrp = hc["most_relevant_precedent"]
            path = "root.historical_context.most_relevant_precedent"
            _require_keys(mrp, ["date"] + _PRECEDENT_STR_FIELDS + _PRECEDENT_LIST_FIELDS, path, errors)
            if "date" in mrp:
                # Spec's own example shows this field as null (unlike other
                # date fields, which default to ""). Nullable per spec example.
                _check_str(mrp["date"], f"{path}.date", errors, allow_none=True)
            for f_ in _PRECEDENT_STR_FIELDS:
                if f_ in mrp:
                    _check_str(mrp[f_], f"{path}.{f_}", errors)
            for f_ in _PRECEDENT_LIST_FIELDS:
                if f_ in mrp:
                    _check_list_of_str(mrp[f_], f"{path}.{f_}", errors)

    # precedent_comparison
    if "precedent_comparison" in raw and _check_dict(raw["precedent_comparison"], "root.precedent_comparison", errors):
        pc = raw["precedent_comparison"]
        path = "root.precedent_comparison"
        _require_keys(pc, ["similarities", "differences", "trend_classification"], path, errors)
        if "similarities" in pc:
            _check_list_of_str(pc["similarities"], f"{path}.similarities", errors)
        if "differences" in pc:
            _check_list_of_str(pc["differences"], f"{path}.differences", errors)
        if "trend_classification" in pc:
            _check_enum(pc["trend_classification"], TREND_CLASSIFICATIONS, f"{path}.trend_classification", errors)

    # legal_regulatory_effect
    if "legal_regulatory_effect" in raw and _check_dict(raw["legal_regulatory_effect"], "root.legal_regulatory_effect", errors):
        lre = raw["legal_regulatory_effect"]
        path = "root.legal_regulatory_effect"
        _require_keys(lre, _LEGAL_EFFECT_LIST_FIELDS + ["effective_date"], path, errors)
        for f_ in _LEGAL_EFFECT_LIST_FIELDS:
            if f_ in lre:
                _check_list_of_str(lre[f_], f"{path}.{f_}", errors)
        if "effective_date" in lre:
            _check_str(lre["effective_date"], f"{path}.effective_date", errors)

    # scope
    if "scope" in raw and _check_dict(raw["scope"], "root.scope", errors):
        sc = raw["scope"]
        path = "root.scope"
        _require_keys(sc, _SCOPE_LIST_FIELDS, path, errors)
        for f_ in _SCOPE_LIST_FIELDS:
            if f_ in sc:
                _check_list_of_str(sc[f_], f"{path}.{f_}", errors)

    # impact_assessment
    if "impact_assessment" in raw and _check_dict(raw["impact_assessment"], "root.impact_assessment", errors):
        ia = raw["impact_assessment"]
        path = "root.impact_assessment"
        _require_keys(ia, _IMPACT_LIST_FIELDS, path, errors)
        for f_ in _IMPACT_LIST_FIELDS:
            if f_ in ia:
                _check_list_of_str(ia[f_], f"{path}.{f_}", errors)

    # follow_on_indicators
    if "follow_on_indicators" in raw and _check_dict(raw["follow_on_indicators"], "root.follow_on_indicators", errors):
        foi = raw["follow_on_indicators"]
        path = "root.follow_on_indicators"
        _require_keys(foi, _FOLLOW_ON_LIST_FIELDS, path, errors)
        for f_ in _FOLLOW_ON_LIST_FIELDS:
            if f_ in foi:
                _check_list_of_str(foi[f_], f"{path}.{f_}", errors)

    # open_questions
    if "open_questions" in raw:
        _check_list_of_str(raw["open_questions"], "root.open_questions", errors)

    # sources
    if "sources" in raw:
        if not isinstance(raw["sources"], list):
            _err(errors, "root.sources", f"expected list, got {type(raw['sources']).__name__}")
        else:
            for i, item in enumerate(raw["sources"]):
                _validate_source_item(item, f"root.sources[{i}]", errors)

    # research_status / due_diligence_confidence — validated as enums, but
    # ALSO echoed into the result independent of other errors (see
    # ValidationResult docstring): research completeness is a distinct
    # concept from structural validity of the rest of the record.
    research_status_out = None
    if "research_status" in raw:
        _check_enum(raw["research_status"], RESEARCH_STATUSES, "root.research_status", errors)
        if isinstance(raw["research_status"], str) and raw["research_status"] in RESEARCH_STATUSES:
            research_status_out = raw["research_status"]

    ddc_out = None
    if "due_diligence_confidence" in raw:
        _check_enum(raw["due_diligence_confidence"], CONFIDENCE_LEVELS, "root.due_diligence_confidence", errors)
        if isinstance(raw["due_diligence_confidence"], str) and raw["due_diligence_confidence"] in CONFIDENCE_LEVELS:
            ddc_out = raw["due_diligence_confidence"]

    if errors:
        result = _invalid(errors)
    else:
        result = _valid()
    result.research_status = research_status_out
    result.due_diligence_confidence = ddc_out
    return result


# ---------------------------------------------------------------------------
# Stage 3 — executive brief
# ---------------------------------------------------------------------------

_STAGE3_STR_FIELDS = [
    "headline", "bottom_line", "what_changed", "why_it_matters",
    "historical_significance", "what_did_not_change",
]
_STAGE3_LIST_FIELDS = ["compliance_attention", "watch_next"]
_STAGE3_REQUIRED = _STAGE3_STR_FIELDS + _STAGE3_LIST_FIELDS + ["confidence", "sources"]


def validate_stage3_record(raw: Any) -> ValidationResult:
    """Deterministically validate a Stage 3 executive-brief record.

    AMBIGUITY (reported, not silently resolved): the frozen spec's Stage 3
    JSON schema shows "sources": [] with no defined per-item shape, unlike
    Stage 2 which fully specifies each source object. Rule 10 of the spec
    ("every material DD-derived final claim must be traceable to
    supporting evidence") requires Stage 3 sources to carry enough
    structure to actually trace back to a source record. This validator
    therefore requires each Stage 3 source item to match the SAME object
    shape as Stage 2 sources (url, source_type, agency, date, supports,
    primary_source). If that's not the intended shape, this is the one
    call in this module that should be revisited against spec intent
    before Stage 3 is implemented.
    """
    errors: list = []

    if not isinstance(raw, dict):
        return _invalid([f"root: expected object, got {type(raw).__name__}"])

    _require_keys(raw, _STAGE3_REQUIRED, "root", errors)

    for f_ in _STAGE3_STR_FIELDS:
        if f_ in raw:
            _check_str(raw[f_], f"root.{f_}", errors)

    for f_ in _STAGE3_LIST_FIELDS:
        if f_ in raw:
            _check_list_of_str(raw[f_], f"root.{f_}", errors)

    confidence_out = None
    if "confidence" in raw:
        _check_enum(raw["confidence"], CONFIDENCE_LEVELS, "root.confidence", errors)
        if isinstance(raw["confidence"], str) and raw["confidence"] in CONFIDENCE_LEVELS:
            confidence_out = raw["confidence"]

    if "sources" in raw:
        if not isinstance(raw["sources"], list):
            _err(errors, "root.sources", f"expected list, got {type(raw['sources']).__name__}")
        else:
            for i, item in enumerate(raw["sources"]):
                _validate_source_item(item, f"root.sources[{i}]", errors)

    if errors:
        result = _invalid(errors)
    else:
        result = _valid()
    result.confidence = confidence_out
    return result
