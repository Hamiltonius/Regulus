#!/usr/bin/env python3
"""
dd_schema.py — Structural schemas and deterministic validators for the
Due Diligence (DD) pipeline defined in docs/REGULUS_DD_SPEC_v1.0.md
(now merged into main, including clarifications C1/C2).

Scope of this module — schemas + deterministic validation ONLY:
  - Stage 2 evidence-package structural schema and validator
  - Stage 3 executive-brief structural schema and validator
  - Stage 3 source-reuse enforcement (spec clarification C2)
  - validation_status (structural validity) kept strictly separate from
    research_status (evidence/research completeness) per spec rule 9 and
    clarification C1.

Explicitly OUT of scope for this module (lives in dd_pipeline.py instead):
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

Per spec clarification C1: validation_status and validation_errors are
NEVER part of what the Stage 2 or Stage 3 model produces. They are
OUTPUTS computed exclusively by validate_stage2_record()/
validate_stage3_record(). If an incoming record happens to carry its own
"validation_status"/"validation_errors" keys, those keys are ignored —
only this module's own judgment sets these fields.

Per spec clarification C2: Stage 3 sources must be a subset of validated
Stage 2 sources (same object, not re-derived). validate_stage3_sources()
below enforces this deterministically — the system prompt asking the
model nicely is not sufficient on its own.
"""

import re
import urllib.parse
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
    """Deterministically validate a Stage 3 executive-brief record's
    STRUCTURE only (shape/types/enums). Per spec clarification C2, each
    Stage 3 source item must match the same object shape as a Stage 2
    source (url, source_type, agency, date, supports, primary_source).

    This function does NOT check that those sources actually come from a
    given Stage 2 record — that's a cross-record check, not a structural
    one. Use validate_stage3_sources() (below) against the specific Stage
    2 record this Stage 3 output was synthesized from for that check.
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


# ---------------------------------------------------------------------------
# Spec clarification C2 — Stage 3 may only reuse Stage 2 sources verbatim
# ---------------------------------------------------------------------------

def _source_identity(item):
    """A source's identity for reuse comparison is its full provenance
    tuple, not just its URL — spec C2 says Stage 3 may not "alter source
    provenance" either, so a Stage 3 source with Stage 2's URL but a
    different agency/date/primary_source flag is still a violation, not a
    harmless rewrite."""
    if not isinstance(item, dict):
        return None
    return (
        item.get("url"), item.get("source_type"), item.get("agency"),
        item.get("date"), item.get("primary_source"),
    )


def validate_stage3_sources(stage3_sources, stage2_sources) -> list:
    """Deterministically enforce spec clarification C2: every Stage 3
    source must be identical, verbatim, to a source already present in the
    validated Stage 2 record it was synthesized from. Returns a list of
    error strings (empty means compliant). Does not repair or drop
    offending entries — reports them.

    This is NOT a structural check (see validate_stage3_record for that);
    both inputs are assumed to already be lists of source-shaped dicts.
    """
    errors = []
    if not isinstance(stage3_sources, list):
        return [f"stage3_sources: expected list, got {type(stage3_sources).__name__}"]
    if not isinstance(stage2_sources, list):
        return [f"stage2_sources: expected list, got {type(stage2_sources).__name__}"]

    allowed = {_source_identity(s) for s in stage2_sources}
    allowed.discard(None)

    for i, item in enumerate(stage3_sources):
        identity = _source_identity(item)
        if identity is None or identity not in allowed:
            url = item.get("url") if isinstance(item, dict) else item
            errors.append(
                f"stage3_sources[{i}]: source (url={url!r}) is not present verbatim in the "
                f"validated Stage 2 evidence — Stage 3 may only reuse existing Stage 2 sources "
                f"(spec clarification C2), never discover, invent, or alter one"
            )
    return errors


# ---------------------------------------------------------------------------
# Post go-live audit hardening (first successful live Syria run, commit
# c41fd91, document_number=2026-18918): two deterministic provenance
# checks found by manual audit of that run's diagnostic. Both are pure
# functions over plain dicts, per this module's existing constraints --
# no LLM calls, no network calls, no database access. Neither weakens,
# bypasses, or replaces spec clarification C2 above, which remains the
# sole authority on Stage 3 source reuse.
# ---------------------------------------------------------------------------

# --- Defect 1: Federal Register / GovInfo source identity -----------------

# Domains whose URLs are expected to encode a Federal Register document
# number directly in the path (federalregister.gov/documents/.../<num>/...,
# govinfo.gov's FR package .../pdf/<num>.pdf or .../html/<num>.htm).
_FR_IDENTITY_DOMAINS = ("federalregister.gov", "govinfo.gov")
_FR_DOCUMENT_NUMBER_RE = re.compile(r"\b(\d{4}-\d{4,6})\b")


def _hostname(url: Any) -> Optional[str]:
    """Lowercased hostname of a URL, or None if unparseable/not a
    non-empty string. Local to this module (no dependency on
    scripts/dd_syria_acceptance_test.py's own copy) and deliberately
    simple -- callers below compare against a domain or "any subdomain
    of" a domain, so a leading 'www.' needs no special-casing here."""
    if not isinstance(url, str) or not url:
        return None
    try:
        netloc = urllib.parse.urlparse(url).netloc
    except ValueError:
        return None
    host = netloc.split("@")[-1].split(":")[0].lower()
    return host or None


def _domain_or_subdomain(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith("." + domain)


def extract_federal_register_document_number(url: Any) -> Optional[str]:
    """Best-effort, deterministic extraction of a Federal Register
    document number (e.g. '2026-18918') encoded in a federalregister.gov
    or govinfo.gov URL. Returns None for any other domain -- this
    function never guesses an FR-shaped number out of an unrelated
    host's URL, which is what keeps it from ever flagging a non-FR
    source (e.g. state.gov, legal500.com) -- or if the URL doesn't
    actually contain that pattern."""
    hostname = _hostname(url)
    if hostname is None or not any(
        _domain_or_subdomain(hostname, d) for d in _FR_IDENTITY_DOMAINS
    ):
        return None
    m = _FR_DOCUMENT_NUMBER_RE.search(url)
    return m.group(1) if m else None


def validate_source_identity(source: Any, target_document_number: Any) -> Optional[str]:
    """Deterministically check ONE Stage 2 source against the document
    it is claimed to be evidence for. Only checks a source that BOTH:

      (a) lists "current_event" in its "supports" array -- i.e. it is
          presented as evidence for the CURRENT target document, not
          historical precedent, and
      (b) has a URL on a Federal Register/GovInfo identity domain that
          actually encodes an FR document number

    A historical-precedent source (e.g. supports=["historical_context"])
    with a different, older document number is legitimate evidence and
    is never flagged by this function -- it only catches a
    current-event-supporting source whose OWN embedded document number
    conflicts with the document it's claimed to support. Returns an
    error string on conflict, None otherwise (including when
    target_document_number is falsy/unknown -- this never invents a
    target to compare against, it simply skips the check)."""
    if not isinstance(source, dict) or not target_document_number:
        return None
    supports = source.get("supports")
    if not isinstance(supports, list) or "current_event" not in supports:
        return None
    encoded = extract_federal_register_document_number(source.get("url"))
    if encoded is None or encoded == target_document_number:
        return None
    return (
        f"source (url={source.get('url')!r}) supports 'current_event' but its encoded "
        f"Federal Register/GovInfo document number ({encoded!r}) does not match the "
        f"target document_number ({target_document_number!r})"
    )


def validate_stage2_source_identities(sources: Any, target_document_number: Any) -> list:
    """Run validate_source_identity() across an entire Stage 2 sources
    array. Returns a list of error strings (empty means every
    current-event-supporting source's encoded FR/GovInfo identity, where
    present, agrees with the target document). Reporting only -- mirrors
    validate_stage3_sources() above in shape and never drops, repairs, or
    alters any source itself."""
    errors: list = []
    if not isinstance(sources, list):
        return errors
    for i, s in enumerate(sources):
        err = validate_source_identity(s, target_document_number)
        if err:
            errors.append(f"sources[{i}]: {err}")
    return errors


# --- Defect 2: primary-source domain normalization -------------------------

# Conservative allowlist of U.S. government domains actually relevant to
# Regulus's export-control/sanctions beat. A model-supplied
# primary_source=true is NEVER sufficient on its own (see
# classify_primary_source) -- only a domain on, or a subdomain of, this
# list can be PRIMARY. Deliberately an allowlist/domain rule, not a
# reputation or authority-tier score: a domain either qualifies or it
# doesn't, with no LLM, no network call, and no database involved.
_PRIMARY_SOURCE_DOMAINS = frozenset({
    "federalregister.gov",
    "govinfo.gov",
    "uscode.house.gov",
    "congress.gov",
    "state.gov",
    "bis.gov",
    "commerce.gov",
    "treasury.gov",
    "ofac.treasury.gov",
    "whitehouse.gov",
})


def classify_primary_source(url: Any) -> bool:
    """Deterministically decide whether a URL's domain qualifies as a
    PRIMARY (issuing-government) source, independent of whatever the
    model itself claimed. A domain qualifies only if its hostname is
    exactly one of _PRIMARY_SOURCE_DOMAINS or a subdomain of one of them
    (e.g. 'www.state.gov' and 'www.federalregister.gov' both qualify via
    'state.gov'/'federalregister.gov'). Every mirror, secondary-analysis,
    or non-government site -- law.cornell.edu, thefederalregister.org,
    fdassociates.net, goodwinlaw.com, legal500.com, unblocksyria.com,
    zyphe.com, or anything else not on the list -- returns False,
    regardless of source_type, agency, or what the model itself returned
    for primary_source."""
    hostname = _hostname(url)
    if hostname is None:
        return False
    return any(_domain_or_subdomain(hostname, d) for d in _PRIMARY_SOURCE_DOMAINS)


def normalize_source_primary(source: Any) -> Any:
    """Return a NEW source dict with primary_source replaced by the
    trusted, domain-based classification from classify_primary_source()
    -- the model's own primary_source value is never trusted on its own.
    Every other key is preserved unchanged (same object shape the schema
    and C2 expect). Non-dict input is returned unchanged (nothing to
    normalize; never raises)."""
    if not isinstance(source, dict):
        return source
    normalized = dict(source)
    normalized["primary_source"] = classify_primary_source(source.get("url"))
    return normalized


def normalize_sources_primary(sources: Any) -> Any:
    """Map normalize_source_primary() across a whole sources array,
    preserving order and length. Non-list input is returned unchanged."""
    if not isinstance(sources, list):
        return sources
    return [normalize_source_primary(s) for s in sources]
