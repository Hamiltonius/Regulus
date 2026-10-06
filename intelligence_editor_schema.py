#!/usr/bin/env python3
"""
intelligence_editor_schema.py — deterministic structural validation for
the Intelligence Editor's output: REGULUS INTELLIGENCE BRIEF #001.

    [validated Pass #2 story assessments, eligible for the Editor]
            -> INTELLIGENCE EDITOR (intelligence_editor.py)
            -> Intelligence Brief (validated by THIS module)

This is a NEW, SMALL, standalone schema -- mirroring corpus_analyst_
schema.py / evidence_analyst_schema.py / intelligence_analyst_pass2_
schema.py's own pattern exactly (same helper functions, same
ValidationResult-style result object, same "reject on missing/invalid,
never silently repair" discipline). It does not import, reuse, or modify
any other Regulus schema module.

SCOPE: the Editor ORGANIZES, PRIORITIZES, COMPRESSES, and EXPLAINS
already-validated intelligence -- it adds no new facts, does no research,
and uses no tool. This module enforces three things, deterministically:
  1. the required Brief shape (8 sections + top-level bookkeeping fields),
     enum values, and that every section's "items" cite real story_ids
     and real evidence_ids;
  2. TRACEABILITY: every item's story_ids must be among the stories the
     Editor was actually given (editor_inputs), and every item's
     evidence_ids must exist within the UNION of the evidence_ids
     belonging to the story_ids that same item cites -- an item cannot
     borrow another story's evidence to support its claim;
  3. NO RESURRECTION (where deterministically detectable): no free-text
     field in the Brief may contain, verbatim, the exact text of a claim
     Pass #2 marked disposition="removed" in material_changes, or an
     alternative_hypothesis Pass #2 assessed disposition="contradicted"
     or "weakened" -- built from the SAME editor_inputs the Editor was
     given, not from stories it never saw. This is a verbatim-substring
     check, not semantic paraphrase detection -- it catches literal reuse
     of rejected phrasing, which is the "deterministically detectable"
     form of resurrection the task spec calls for.

What this module deliberately does NOT check: whether the Editor's
prioritization, compression, or prose quality is good, or whether a
story's EXCLUSION from the Brief was the right editorial call. Those are
judgments reserved for the model and human review.
"""

from dataclasses import dataclass, field
from typing import Any, Optional


CONFIDENCE_LEVELS = {"high", "medium", "low"}

_TOP_LEVEL_REQUIRED_FIELDS = [
    "brief_id", "reporting_period", "executive_assessment", "regulatory_tempo",
    "targeting_and_policy_direction", "key_developments", "cross_agency_signals",
    "emerging_patterns", "watchlist", "methodology_and_sources", "confidence",
    "stories_included", "stories_excluded",
]

# Sections 2-7 (regulatory_tempo, targeting_and_policy_direction,
# key_developments, cross_agency_signals, emerging_patterns, watchlist)
# all share the same {"summary": str, "items": [...]} shape.
_ITEMIZED_SECTION_FIELDS = [
    "regulatory_tempo", "targeting_and_policy_direction", "key_developments",
    "cross_agency_signals", "emerging_patterns", "watchlist",
]

_SECTION_ITEM_REQUIRED_FIELDS = ["text", "story_ids", "evidence_ids"]
_METHODOLOGY_REQUIRED_FIELDS = [
    "summary", "stories_included", "stories_excluded", "evidence_ids_referenced",
]
_STORY_EXCLUDED_REQUIRED_FIELDS = ["story_id", "reason"]


@dataclass
class BriefValidationResult:
    is_valid: bool
    validation_errors: list = field(default_factory=list)


def _invalid(errors):
    return BriefValidationResult(is_valid=False, validation_errors=errors)


def _valid():
    return BriefValidationResult(is_valid=True, validation_errors=[])


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


def _build_rejected_claim_texts(editor_inputs: list) -> set:
    """Deterministically collect the exact text of every claim Pass #2
    marked disposition='removed' (material_changes) or disposition in
    ('contradicted', 'weakened') (alternative_hypotheses_assessment),
    from the SAME editor_inputs the Editor was actually given. This is
    the ground truth the "no resurrection" check below is measured
    against -- never from stories the Editor was never shown."""
    rejected = set()
    for entry in editor_inputs:
        pass2_output = entry.get("pass2_output") if isinstance(entry, dict) else None
        if not isinstance(pass2_output, dict):
            continue
        for mc in pass2_output.get("material_changes") or []:
            if (isinstance(mc, dict) and mc.get("disposition") == "removed"
                    and isinstance(mc.get("original_claim"), str) and mc["original_claim"].strip()):
                rejected.add(mc["original_claim"].strip())
        for ah in pass2_output.get("alternative_hypotheses_assessment") or []:
            if (isinstance(ah, dict) and ah.get("disposition") in ("contradicted", "weakened")
                    and isinstance(ah.get("hypothesis"), str) and ah["hypothesis"].strip()):
                rejected.add(ah["hypothesis"].strip())
    return rejected


def _check_no_resurrection(text: str, path: str, rejected_claim_texts: set, errors: list) -> None:
    if not isinstance(text, str) or not rejected_claim_texts:
        return
    for claim in rejected_claim_texts:
        if claim and claim in text:
            _err(errors, path,
                 "appears to resurrect, verbatim, a claim Pass #2 marked removed/contradicted/"
                 f"weakened: {claim[:120]!r}")


def _validate_section_item(item, index, path_prefix, valid_story_ids, evidence_ids_by_story,
                            rejected_claim_texts, errors):
    path = f"{path_prefix}[{index}]"
    if not isinstance(item, dict):
        _err(errors, path, f"must be an object, got {type(item).__name__}")
        return

    missing = [f for f in _SECTION_ITEM_REQUIRED_FIELDS if f not in item]
    if missing:
        _err(errors, path, f"missing required field(s): {missing}")

    text = item.get("text")
    if "text" in item:
        _check_str(text, f"{path}.text", errors)
        _check_no_resurrection(text, f"{path}.text", rejected_claim_texts, errors)

    story_ids = item.get("story_ids")
    cited_story_ids = []
    if "story_ids" in item:
        if _check_list_of_str(story_ids, f"{path}.story_ids", errors, nonempty=True):
            for i, sid in enumerate(story_ids):
                if sid not in valid_story_ids:
                    _err(errors, f"{path}.story_ids[{i}]",
                         f"story_id {sid!r} is not among the stories supplied to the Editor "
                         "(an item may not cite a story the Editor was never given)")
                else:
                    cited_story_ids.append(sid)

    evidence_ids = item.get("evidence_ids")
    if "evidence_ids" in item:
        if _check_list_of_str(evidence_ids, f"{path}.evidence_ids", errors, nonempty=False):
            allowed_evidence_ids = set()
            for sid in cited_story_ids:
                allowed_evidence_ids |= evidence_ids_by_story.get(sid, set())
            for i, eid in enumerate(evidence_ids):
                if eid not in allowed_evidence_ids:
                    _err(errors, f"{path}.evidence_ids[{i}]",
                         f"evidence_id {eid!r} does not belong to any of this item's cited "
                         "story_ids' evidence packages (an item may not borrow another "
                         "story's evidence, or cite an invented evidence_id)")


def _validate_itemized_section(raw_section, section_name, valid_story_ids, evidence_ids_by_story,
                                rejected_claim_texts, errors):
    path = f"root.{section_name}"
    if not isinstance(raw_section, dict):
        _err(errors, path, f"must be an object, got {type(raw_section).__name__}")
        return

    if "summary" not in raw_section or "items" not in raw_section:
        _err(errors, path, "missing required field(s): "
             f"{[f for f in ('summary', 'items') if f not in raw_section]}")
        return

    _check_str(raw_section["summary"], f"{path}.summary", errors)
    _check_no_resurrection(raw_section.get("summary"), f"{path}.summary", rejected_claim_texts, errors)

    if _check_list(raw_section["items"], f"{path}.items", errors, nonempty=False):
        for i, item in enumerate(raw_section["items"]):
            _validate_section_item(item, i, f"{path}.items", valid_story_ids,
                                    evidence_ids_by_story, rejected_claim_texts, errors)


def _validate_story_excluded(item, index, errors):
    path = f"root.stories_excluded[{index}]"
    if not isinstance(item, dict):
        _err(errors, path, f"must be an object, got {type(item).__name__}")
        return
    missing = [f for f in _STORY_EXCLUDED_REQUIRED_FIELDS if f not in item]
    if missing:
        _err(errors, path, f"missing required field(s): {missing}")
    if "story_id" in item:
        _check_str(item["story_id"], f"{path}.story_id", errors)
    if "reason" in item:
        _check_str(item["reason"], f"{path}.reason", errors)


def _validate_methodology_and_sources(raw_section, valid_story_ids, all_valid_evidence_ids, errors):
    path = "root.methodology_and_sources"
    if not isinstance(raw_section, dict):
        _err(errors, path, f"must be an object, got {type(raw_section).__name__}")
        return

    missing = [f for f in _METHODOLOGY_REQUIRED_FIELDS if f not in raw_section]
    if missing:
        _err(errors, path, f"missing required field(s): {missing}")
        return

    _check_str(raw_section["summary"], f"{path}.summary", errors)

    if _check_list_of_str(raw_section["stories_included"], f"{path}.stories_included", errors, nonempty=False):
        for i, sid in enumerate(raw_section["stories_included"]):
            if sid not in valid_story_ids:
                _err(errors, f"{path}.stories_included[{i}]",
                     f"story_id {sid!r} is not among the stories supplied to the Editor")

    if _check_list(raw_section["stories_excluded"], f"{path}.stories_excluded", errors, nonempty=False):
        for i, item in enumerate(raw_section["stories_excluded"]):
            path2 = f"{path}.stories_excluded[{i}]"
            if not isinstance(item, dict):
                _err(errors, path2, f"must be an object, got {type(item).__name__}")
                continue
            missing2 = [f for f in _STORY_EXCLUDED_REQUIRED_FIELDS if f not in item]
            if missing2:
                _err(errors, path2, f"missing required field(s): {missing2}")
            if "story_id" in item:
                _check_str(item["story_id"], f"{path2}.story_id", errors)
            if "reason" in item:
                _check_str(item["reason"], f"{path2}.reason", errors)

    if _check_list_of_str(raw_section["evidence_ids_referenced"], f"{path}.evidence_ids_referenced",
                           errors, nonempty=False):
        for i, eid in enumerate(raw_section["evidence_ids_referenced"]):
            if eid not in all_valid_evidence_ids:
                _err(errors, f"{path}.evidence_ids_referenced[{i}]",
                     f"evidence_id {eid!r} does not exist in any story's evidence package")


def validate_brief(raw: Any, *, editor_inputs: list) -> BriefValidationResult:
    """Validate an Intelligence Editor output (a Brief) against the
    schema this module defines.

    Args:
      raw: the candidate Brief dict to validate.
      editor_inputs: the EXACT list of inputs the Editor was given, each
        a dict with at least "story_id" (str), "pass2_output" (the
        validated Pass #2 dict for that story), and "evidence_ids" (an
        iterable of the evidence_ids that story's Evidence Analyst
        package actually contains). This is the sole source of truth for
        valid story_ids, per-story valid evidence_ids, and the rejected-
        claim-text set used by the no-resurrection check -- never derived
        from raw itself.
    """
    if not isinstance(raw, dict):
        return _invalid([f"root: must be an object, got {type(raw).__name__}"])
    if not isinstance(editor_inputs, list):
        return _invalid(["root: 'editor_inputs' argument must be a list"])

    errors: list = []

    missing = [f for f in _TOP_LEVEL_REQUIRED_FIELDS if f not in raw]
    if missing:
        _err(errors, "root", f"missing required top-level field(s): {missing}")
        return _invalid(errors)

    valid_story_ids = set()
    evidence_ids_by_story = {}
    for entry in editor_inputs:
        if not isinstance(entry, dict) or not isinstance(entry.get("story_id"), str):
            continue
        sid = entry["story_id"]
        valid_story_ids.add(sid)
        evidence_ids_by_story[sid] = set(entry.get("evidence_ids") or [])
    all_valid_evidence_ids = set().union(*evidence_ids_by_story.values()) if evidence_ids_by_story else set()
    rejected_claim_texts = _build_rejected_claim_texts(editor_inputs)

    _check_str(raw.get("brief_id"), "root.brief_id", errors)

    reporting_period = raw.get("reporting_period")
    if not isinstance(reporting_period, dict) or "start" not in reporting_period or "end" not in reporting_period:
        _err(errors, "root.reporting_period", "must be an object with 'start' and 'end'")
    else:
        _check_str(reporting_period.get("start"), "root.reporting_period.start", errors)
        _check_str(reporting_period.get("end"), "root.reporting_period.end", errors)

    _check_str(raw.get("executive_assessment"), "root.executive_assessment", errors)
    _check_no_resurrection(raw.get("executive_assessment"), "root.executive_assessment",
                            rejected_claim_texts, errors)

    for section_name in _ITEMIZED_SECTION_FIELDS:
        _validate_itemized_section(raw.get(section_name), section_name, valid_story_ids,
                                    evidence_ids_by_story, rejected_claim_texts, errors)

    _validate_methodology_and_sources(raw.get("methodology_and_sources"), valid_story_ids,
                                       all_valid_evidence_ids, errors)

    if "confidence" in raw:
        _check_enum(raw.get("confidence"), CONFIDENCE_LEVELS, "root.confidence", errors)

    if _check_list_of_str(raw.get("stories_included"), "root.stories_included", errors, nonempty=False):
        for i, sid in enumerate(raw["stories_included"]):
            if sid not in valid_story_ids:
                _err(errors, f"root.stories_included[{i}]",
                     f"story_id {sid!r} is not among the stories supplied to the Editor")

    if _check_list(raw.get("stories_excluded"), "root.stories_excluded", errors, nonempty=False):
        for i, item in enumerate(raw["stories_excluded"]):
            _validate_story_excluded(item, i, errors)

    if errors:
        return _invalid(errors)
    return _valid()
