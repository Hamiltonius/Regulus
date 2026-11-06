#!/usr/bin/env python3
"""
corpus_analyst_schema.py — deterministic structural validation for the
Step 2 Corpus Analyst (first-pass discovery) output.

This is a NEW, SMALL, standalone schema — it does not import, reuse, or
modify dd_schema.py, and it validates a completely different shape
(corpus-level candidate stories, not a single-document DD evidence
package). Nothing here touches Stage 1/2/3 validation.

What this module checks, deterministically:
  - required top-level fields are present with the right container types;
  - every candidate story has all required fields, correct types, and
    valid enum values;
  - every document number the model cites (supporting_document_numbers,
    observational_basis, potential_administrative_activity,
    unclustered_observations_of_interest) actually exists in the corpus
    that was supplied to the model — the model may not invent a source;
  - story_id values are unique;
  - the specific banned placeholder research question ("we need more
    research" and trivial rewordings of it) is rejected, because the
    task instructions explicitly forbid it as a non-answerable
    "research question".

What this module deliberately does NOT check: whether a hypothesis is
correct, whether a story is actually important, whether the
characterization of activity is accurate. Those are analytical
judgments reserved for the LLM (and, later, human review) — this module
only enforces the SHAPE of the output and its traceability back to real
input data.
"""

from dataclasses import dataclass, field
from typing import Any, Optional


MATERIALITY_LEVELS = {"high", "medium", "low"}
PRIORITY_LEVELS = {"high", "medium", "low"}
CONFIDENCE_LEVELS = {"high", "medium", "low"}

_STORY_REQUIRED_STR_FIELDS = [
    "story_id", "title", "candidate_materiality", "why_it_deserves_investigation",
    "preliminary_hypothesis", "research_priority", "preliminary_confidence",
]
_STORY_REQUIRED_LIST_FIELDS = [
    "supporting_document_numbers", "alternative_hypotheses", "observational_basis",
    "intelligence_gaps", "research_questions", "evidence_needed",
    "disconfirming_evidence_needed",
]
# Lists that must be non-empty — per task section 10/12, an empty list here
# is a validation failure, not merely an empty analytical finding.
_STORY_NONEMPTY_LIST_FIELDS = [
    "supporting_document_numbers", "intelligence_gaps", "research_questions",
    "evidence_needed", "disconfirming_evidence_needed",
]

_ADMIN_REQUIRED_FIELDS = ["document_numbers", "reason", "research_priority"]
_UNCLUSTERED_REQUIRED_FIELDS = ["document_number", "reason"]

# The task instructions explicitly name this exact non-answer as
# insufficient. Rejecting it (and trivial punctuation/case variants) is a
# narrow, deterministic check — it is not an attempt to judge the overall
# quality of a research question, only to block this one named placeholder.
_BANNED_RESEARCH_QUESTION_PLACEHOLDERS = {
    "we need more research",
    "we need more research.",
    "more research is needed",
    "more research is needed.",
    "further research is needed",
    "further research is needed.",
}


@dataclass
class CorpusAnalysisValidationResult:
    is_valid: bool
    validation_errors: list = field(default_factory=list)


def _invalid(errors):
    return CorpusAnalysisValidationResult(is_valid=False, validation_errors=errors)


def _valid():
    return CorpusAnalysisValidationResult(is_valid=True, validation_errors=[])


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


def _check_research_questions(value, path, errors):
    """List-of-str check, plus the banned-placeholder rule."""
    ok = _check_list_of_str(value, path, errors, nonempty=True)
    if not ok:
        return False
    for i, q in enumerate(value):
        if isinstance(q, str) and q.strip().lower() in _BANNED_RESEARCH_QUESTION_PLACEHOLDERS:
            _err(errors, f"{path}[{i}]",
                 f"is a non-answerable placeholder ({q!r}); a research question must be concrete")
            ok = False
    return ok


def _check_document_numbers_exist(doc_numbers, valid_doc_numbers, path, errors):
    ok = True
    for i, num in enumerate(doc_numbers):
        if num not in valid_doc_numbers:
            _err(errors, f"{path}[{i}]",
                 f"document_number {num!r} does not exist in the supplied corpus "
                 "(the model may not invent source documents)")
            ok = False
    return ok


def _validate_observational_basis(value, path, valid_doc_numbers, errors):
    if not _check_list(value, path, errors, nonempty=False):
        return False
    ok = True
    for i, item in enumerate(value):
        item_path = f"{path}[{i}]"
        if not isinstance(item, dict):
            _err(errors, item_path, f"must be an object, got {type(item).__name__}")
            ok = False
            continue
        if "document_number" not in item or "observation" not in item:
            _err(errors, item_path, "must contain 'document_number' and 'observation'")
            ok = False
            continue
        if not _check_str(item["document_number"], f"{item_path}.document_number", errors):
            ok = False
        elif item["document_number"] not in valid_doc_numbers:
            _err(errors, f"{item_path}.document_number",
                 f"{item['document_number']!r} does not exist in the supplied corpus")
            ok = False
        if not _check_str(item["observation"], f"{item_path}.observation", errors):
            ok = False
    return ok


def _validate_story(story, index, valid_doc_numbers, errors):
    path = f"candidate_stories[{index}]"
    if not isinstance(story, dict):
        _err(errors, path, f"must be an object, got {type(story).__name__}")
        return None

    missing = [f for f in _STORY_REQUIRED_STR_FIELDS + _STORY_REQUIRED_LIST_FIELDS
               if f not in story]
    if missing:
        _err(errors, path, f"missing required field(s): {missing}")

    for f in _STORY_REQUIRED_STR_FIELDS:
        if f in story:
            _check_str(story[f], f"{path}.{f}", errors)

    for f in _STORY_REQUIRED_LIST_FIELDS:
        if f not in story:
            continue
        nonempty = f in _STORY_NONEMPTY_LIST_FIELDS
        if f == "observational_basis":
            _validate_observational_basis(story[f], f"{path}.{f}", valid_doc_numbers, errors)
        elif f == "research_questions":
            _check_research_questions(story[f], f"{path}.{f}", errors)
        else:
            _check_list_of_str(story[f], f"{path}.{f}", errors, nonempty=nonempty)

    if "candidate_materiality" in story and isinstance(story["candidate_materiality"], str):
        _check_enum(story["candidate_materiality"], MATERIALITY_LEVELS,
                    f"{path}.candidate_materiality", errors)
    if "research_priority" in story and isinstance(story["research_priority"], str):
        _check_enum(story["research_priority"], PRIORITY_LEVELS,
                    f"{path}.research_priority", errors)
    if "preliminary_confidence" in story and isinstance(story["preliminary_confidence"], str):
        _check_enum(story["preliminary_confidence"], CONFIDENCE_LEVELS,
                    f"{path}.preliminary_confidence", errors)

    if "supporting_document_numbers" in story and isinstance(story["supporting_document_numbers"], list):
        _check_document_numbers_exist(
            story["supporting_document_numbers"], valid_doc_numbers,
            f"{path}.supporting_document_numbers", errors,
        )

    story_id = story.get("story_id")
    return story_id if isinstance(story_id, str) else None


def _validate_admin_item(item, index, valid_doc_numbers, errors):
    path = f"potential_administrative_activity[{index}]"
    if not isinstance(item, dict):
        _err(errors, path, f"must be an object, got {type(item).__name__}")
        return
    missing = [f for f in _ADMIN_REQUIRED_FIELDS if f not in item]
    if missing:
        _err(errors, path, f"missing required field(s): {missing}")
    if "document_numbers" in item:
        if _check_list_of_str(item["document_numbers"], f"{path}.document_numbers", errors, nonempty=True):
            _check_document_numbers_exist(
                item["document_numbers"], valid_doc_numbers, f"{path}.document_numbers", errors,
            )
    if "reason" in item:
        _check_str(item["reason"], f"{path}.reason", errors)
    if "research_priority" in item and isinstance(item["research_priority"], str):
        _check_enum(item["research_priority"], PRIORITY_LEVELS, f"{path}.research_priority", errors)


def _validate_unclustered_item(item, index, valid_doc_numbers, errors):
    path = f"unclustered_observations_of_interest[{index}]"
    if not isinstance(item, dict):
        _err(errors, path, f"must be an object, got {type(item).__name__}")
        return
    missing = [f for f in _UNCLUSTERED_REQUIRED_FIELDS if f not in item]
    if missing:
        _err(errors, path, f"missing required field(s): {missing}")
    if "document_number" in item:
        if _check_str(item["document_number"], f"{path}.document_number", errors):
            if item["document_number"] not in valid_doc_numbers:
                _err(errors, f"{path}.document_number",
                     f"{item['document_number']!r} does not exist in the supplied corpus")
    if "reason" in item:
        _check_str(item["reason"], f"{path}.reason", errors)


def validate_corpus_analysis(raw: Any, valid_document_numbers: Any) -> CorpusAnalysisValidationResult:
    """Validate a Corpus Analyst first-pass output against the small,
    standalone schema defined in this module.

    `valid_document_numbers` must be the exact set (or iterable) of
    document_number values present in the Corpus that was actually
    supplied to the model for this call — every document-number
    reference the model makes (supporting_document_numbers,
    observational_basis, administrative-activity and unclustered-item
    document numbers) is checked against this set, and any reference
    outside it is a validation failure, deterministically, regardless of
    how plausible it looks.
    """
    if not isinstance(raw, dict):
        return _invalid([f"root: must be an object, got {type(raw).__name__}"])

    valid_doc_numbers = set(valid_document_numbers)
    errors: list = []

    required_top_level = [
        "reporting_period", "corpus_assessment", "candidate_stories",
        "potential_administrative_activity", "unclustered_observations_of_interest",
        "corpus_level_gaps",
    ]
    missing = [f for f in required_top_level if f not in raw]
    if missing:
        _err(errors, "root", f"missing required top-level field(s): {missing}")
        return _invalid(errors)

    rp = raw["reporting_period"]
    if not isinstance(rp, dict) or "start" not in rp or "end" not in rp:
        _err(errors, "root.reporting_period", "must be an object with 'start' and 'end'")
    else:
        _check_str(rp["start"], "root.reporting_period.start", errors)
        _check_str(rp["end"], "root.reporting_period.end", errors)

    ca = raw["corpus_assessment"]
    if not isinstance(ca, dict):
        _err(errors, "root.corpus_assessment", "must be an object")
    else:
        if "observation_count" not in ca or not isinstance(ca["observation_count"], int):
            _err(errors, "root.corpus_assessment.observation_count", "must be present and an integer")
        if "overall_activity_characterization" in ca:
            _check_str(ca["overall_activity_characterization"],
                       "root.corpus_assessment.overall_activity_characterization", errors)
        else:
            _err(errors, "root.corpus_assessment", "missing required field: overall_activity_characterization")
        if "important_caveats" in ca:
            _check_list_of_str(ca["important_caveats"], "root.corpus_assessment.important_caveats",
                                errors, nonempty=False)
        else:
            _err(errors, "root.corpus_assessment", "missing required field: important_caveats")

    story_ids_seen = []
    if not _check_list(raw["candidate_stories"], "root.candidate_stories", errors, nonempty=False):
        pass
    else:
        for i, story in enumerate(raw["candidate_stories"]):
            story_id = _validate_story(story, i, valid_doc_numbers, errors)
            if story_id is not None:
                story_ids_seen.append(story_id)

    dupes = {sid for sid in story_ids_seen if story_ids_seen.count(sid) > 1}
    if dupes:
        _err(errors, "root.candidate_stories", f"duplicate story_id value(s): {sorted(dupes)}")

    if not _check_list(raw["potential_administrative_activity"],
                        "root.potential_administrative_activity", errors, nonempty=False):
        pass
    else:
        for i, item in enumerate(raw["potential_administrative_activity"]):
            _validate_admin_item(item, i, valid_doc_numbers, errors)

    if not _check_list(raw["unclustered_observations_of_interest"],
                        "root.unclustered_observations_of_interest", errors, nonempty=False):
        pass
    else:
        for i, item in enumerate(raw["unclustered_observations_of_interest"]):
            _validate_unclustered_item(item, i, valid_doc_numbers, errors)

    _check_list_of_str(raw["corpus_level_gaps"], "root.corpus_level_gaps", errors, nonempty=False)

    if errors:
        return _invalid(errors)
    return _valid()
