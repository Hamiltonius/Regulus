#!/usr/bin/env python3
"""
evidence_analyst_schema.py — deterministic structural validation for the
future Evidence Analyst role's output.

FOUNDATION ONLY. This module defines and validates the SHAPE of an
Evidence Analyst output for exactly ONE candidate_story (as produced by
corpus_analyst.py's schema — see corpus_analyst_schema.py). It does not
implement the Evidence Analyst itself, does not call any model, and does
not perform retrieval. See evidence_retrieval.py (deterministic source
material preparation) and evidence_analyst.py (the future-call interface
skeleton).

This is a NEW, SMALL, standalone schema — mirroring corpus_analyst_schema.py's
own pattern exactly (same helper functions, same CorpusAnalysisValidationResult-
style result object, same "reject on missing/invalid, never silently repair"
discipline). It does not import, reuse, or modify dd_schema.py, dd_pipeline.py,
corpus_analyst_schema.py, or regulus_v3.py. Nothing here touches Stage 1/2/3
validation, the DD Gate, or any production database behavior.

What this module checks, deterministically:
  - required top-level fields are present with the right container types
    and valid enum values;
  - story_id matches the candidate_story this output claims to be about
    (an output cannot silently drift onto, or be mistaken for, a
    different story);
  - every research_question supplied by the candidate_story has EXACTLY
    one corresponding question_findings entry — none dropped, none
    duplicated, none substituted for a different question text;
  - a question_finding with status "answered" or "partially_answered"
    must cite at least one evidence_id — a finding cannot claim to answer
    a question without pointing at the evidence that answers it;
  - every evidence_id referenced anywhere (question_findings,
    contradictions) must exist in evidence_records, and evidence_record
    ids must be unique (no duplicate evidence_id values);
  - every document_number cited in evidence_records (when not null) must
    exist in the supplied corpus document-number set — an Evidence
    Analyst may not invent a source document;
  - a contradiction must cite at least one evidence_id — an unsupported
    "contradiction" claim is rejected;
  - when retrieval_results (from evidence_retrieval.py) are supplied, an
    evidence_record's claimed source_identity_status is cross-checked
    against what the deterministic retrieval layer actually determined
    for that document_number — an analyst cannot claim "verified" when
    retrieval found a mismatch, or vice versa;
  - if hypothesis_assessment is anything other than "unresolved", at
    least one evidence_record must exist — a supported/weakened/
    contradicted assessment cannot rest on zero evidence.

What this module deliberately does NOT check: whether a hypothesis
assessment is intellectually CORRECT, whether a finding is actually true,
or whether an excerpt was read carefully. Those are analytical judgments
reserved for the (future) LLM and human review — this module only
enforces the SHAPE of the output and its traceability back to real,
existing input data (the candidate_story's research questions, the
corpus's real document numbers, and the Evidence Analyst's own cited
evidence_ids).
"""

from dataclasses import dataclass, field
from typing import Any, Optional


RESEARCH_STATUS_VALUES = {"complete", "partial", "insufficient_evidence"}
HYPOTHESIS_ASSESSMENT_VALUES = {"supported", "weakened", "contradicted", "unresolved"}
QUESTION_FINDING_STATUS_VALUES = {"answered", "partially_answered", "unanswered"}
CONFIDENCE_LEVELS = {"high", "medium", "low"}

# Mirrors the identity-check vocabulary a deterministic retrieval layer can
# actually establish (see evidence_retrieval.py): "verified" (the
# document's own identity was confirmed to match the target document
# number), "mismatch" (retrieval found a conflicting identity),
# "unverified" (retrieval could not establish identity one way or the
# other -- e.g. no FR/GovInfo identity encoded in the URL, or retrieval
# failed before identity could be checked), "not_applicable" (the
# evidence record is not itself claiming to BE a specific document --
# e.g. a secondary/contextual source with no document_number claim).
SOURCE_IDENTITY_STATUS_VALUES = {"verified", "mismatch", "unverified", "not_applicable"}

_TOP_LEVEL_REQUIRED_FIELDS = [
    "story_id", "research_status", "hypothesis_assessment", "question_findings",
    "evidence_records", "contradictions", "remaining_gaps",
    "disconfirming_evidence_found", "overall_assessment", "confidence",
]

_QUESTION_FINDING_REQUIRED_FIELDS = ["question", "status", "finding", "evidence_ids", "confidence"]

# evidence_summary is accepted as an alternate to relevant_excerpt (see
# section 3 of the spec: "relevant_excerpt or evidence_summary") -- at
# least one of the two must be a non-empty string.
_EVIDENCE_RECORD_REQUIRED_FIELDS = [
    "evidence_id", "document_number", "source_title", "source_url", "source_type",
    "primary_source", "retrieved_at", "source_identity_status",
    "publication_date", "effective_date", "supports", "contradicts", "limitations",
]
_EVIDENCE_RECORD_EXCERPT_FIELDS = ("relevant_excerpt", "evidence_summary")

_CONTRADICTION_REQUIRED_FIELDS = ["description", "evidence_ids", "significance"]


@dataclass
class EvidenceAnalysisValidationResult:
    is_valid: bool
    validation_errors: list = field(default_factory=list)


def _invalid(errors):
    return EvidenceAnalysisValidationResult(is_valid=False, validation_errors=errors)


def _valid():
    return EvidenceAnalysisValidationResult(is_valid=True, validation_errors=[])


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


def _check_optional_str(value, path, errors):
    """None is allowed (e.g. publication_date/effective_date/retrieved_at
    'when available' per spec) -- anything else must be a non-empty str."""
    if value is None:
        return True
    return _check_str(value, path, errors, allow_empty=False)


def _check_bool(value, path, errors):
    if not isinstance(value, bool):
        _err(errors, path, f"must be a boolean, got {type(value).__name__}")
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
                 f"evidence_id {eid!r} does not exist in evidence_records "
                 "(a finding/contradiction may not cite invented evidence)")
            ok = False
    return ok


def _validate_question_finding(item, index, expected_questions, valid_evidence_ids, errors):
    path = f"question_findings[{index}]"
    if not isinstance(item, dict):
        _err(errors, path, f"must be an object, got {type(item).__name__}")
        return None

    missing = [f for f in _QUESTION_FINDING_REQUIRED_FIELDS if f not in item]
    if missing:
        _err(errors, path, f"missing required field(s): {missing}")

    question = item.get("question")
    if "question" in item:
        _check_str(question, f"{path}.question", errors)
    if "finding" in item:
        _check_str(item["finding"], f"{path}.finding", errors, allow_empty=True)
    if "status" in item and isinstance(item["status"], str):
        _check_enum(item["status"], QUESTION_FINDING_STATUS_VALUES, f"{path}.status", errors)
    if "confidence" in item and isinstance(item["confidence"], str):
        _check_enum(item["confidence"], CONFIDENCE_LEVELS, f"{path}.confidence", errors)

    evidence_ids = item.get("evidence_ids")
    if "evidence_ids" in item:
        if _check_list_of_str(evidence_ids, f"{path}.evidence_ids", errors, nonempty=False):
            _check_evidence_ids_exist(evidence_ids, valid_evidence_ids, f"{path}.evidence_ids", errors)
            status = item.get("status")
            if status in ("answered", "partially_answered") and len(evidence_ids) == 0:
                _err(errors, f"{path}.evidence_ids",
                     f"status={status!r} claims a finding but cites zero evidence_ids")

    return question if isinstance(question, str) else None


def _validate_evidence_record(item, index, valid_document_numbers, retrieval_results, errors):
    path = f"evidence_records[{index}]"
    if not isinstance(item, dict):
        _err(errors, path, f"must be an object, got {type(item).__name__}")
        return None

    missing = [f for f in _EVIDENCE_RECORD_REQUIRED_FIELDS if f not in item]
    if missing:
        _err(errors, path, f"missing required field(s): {missing}")

    if not any(item.get(f) for f in _EVIDENCE_RECORD_EXCERPT_FIELDS):
        _err(errors, path,
             f"must provide a non-empty 'relevant_excerpt' or 'evidence_summary' "
             f"(got {[item.get(f) for f in _EVIDENCE_RECORD_EXCERPT_FIELDS]!r})")
    for f in _EVIDENCE_RECORD_EXCERPT_FIELDS:
        if f in item and item[f] is not None:
            _check_str(item[f], f"{path}.{f}", errors, allow_empty=True)

    evidence_id = item.get("evidence_id")
    if "evidence_id" in item:
        _check_str(evidence_id, f"{path}.evidence_id", errors)

    document_number = item.get("document_number")
    if "document_number" in item and document_number is not None:
        if _check_str(document_number, f"{path}.document_number", errors):
            if document_number not in valid_document_numbers:
                _err(errors, f"{path}.document_number",
                     f"{document_number!r} does not exist in the supplied corpus "
                     "(the Evidence Analyst may not invent a source document)")

    for f in ("source_title", "source_url", "source_type"):
        if f in item:
            _check_str(item[f], f"{path}.{f}", errors)
    for f in ("retrieved_at", "publication_date", "effective_date"):
        if f in item:
            _check_optional_str(item[f], f"{path}.{f}", errors)

    if "primary_source" in item:
        _check_bool(item["primary_source"], f"{path}.primary_source", errors)

    source_identity_status = item.get("source_identity_status")
    if "source_identity_status" in item and isinstance(source_identity_status, str):
        _check_enum(source_identity_status, SOURCE_IDENTITY_STATUS_VALUES,
                    f"{path}.source_identity_status", errors)

    for f in ("supports", "contradicts"):
        if f in item:
            _check_list_of_str(item[f], f"{path}.{f}", errors, nonempty=False)

    if "limitations" in item:
        # A string list of limitations, or a single string -- either is
        # accepted; what matters is that it's not an untyped/missing field.
        value = item["limitations"]
        if isinstance(value, list):
            _check_list_of_str(value, f"{path}.limitations", errors, nonempty=False)
        elif not isinstance(value, str):
            _err(errors, f"{path}.limitations",
                 f"must be a string or list of strings, got {type(value).__name__}")

    # Cross-check against the deterministic retrieval layer's own identity
    # finding, when supplied -- an analyst's claimed source_identity_status
    # cannot silently disagree with what retrieval actually determined.
    if (retrieval_results is not None and isinstance(document_number, str)
            and document_number in retrieval_results
            and isinstance(source_identity_status, str)):
        actual = retrieval_results[document_number]
        actual_status = getattr(actual, "identity_status", None)
        if actual_status is not None and actual_status != source_identity_status:
            _err(errors, f"{path}.source_identity_status",
                 f"claims {source_identity_status!r} but the deterministic retrieval "
                 f"layer determined {actual_status!r} for document_number {document_number!r}")

    return evidence_id if isinstance(evidence_id, str) else None


def _validate_contradiction(item, index, valid_evidence_ids, errors):
    path = f"contradictions[{index}]"
    if not isinstance(item, dict):
        _err(errors, path, f"must be an object, got {type(item).__name__}")
        return
    missing = [f for f in _CONTRADICTION_REQUIRED_FIELDS if f not in item]
    if missing:
        _err(errors, path, f"missing required field(s): {missing}")
    if "description" in item:
        _check_str(item["description"], f"{path}.description", errors)
    if "significance" in item:
        _check_str(item["significance"], f"{path}.significance", errors)
    if "evidence_ids" in item:
        if _check_list_of_str(item["evidence_ids"], f"{path}.evidence_ids", errors, nonempty=True):
            _check_evidence_ids_exist(item["evidence_ids"], valid_evidence_ids,
                                       f"{path}.evidence_ids", errors)


def validate_evidence_analysis(raw: Any, *, story: Any, valid_document_numbers: Any,
                                retrieval_results: Optional[dict] = None
                                ) -> EvidenceAnalysisValidationResult:
    """Validate an Evidence Analyst output against the schema this module
    defines, for exactly ONE candidate_story.

    Args:
      raw: the candidate output dict to validate.
      story: the candidate_story dict (from the Corpus Analyst schema) this
        output is supposed to be about. Only story["story_id"] and
        story["research_questions"] are read here -- this function does
        not otherwise interpret the Corpus Analyst schema.
      valid_document_numbers: the exact set (or iterable) of document_number
        values present in the Corpus actually available to the Evidence
        Analyst for this run -- every evidence_records[].document_number
        reference is checked against this set.
      retrieval_results: optional {document_number: <object with an
        .identity_status attribute>} mapping, typically produced by
        evidence_retrieval.py's resolve_story_documents()/retrieve_document().
        When supplied, an evidence_record's claimed source_identity_status
        is cross-checked against this deterministic ground truth.
    """
    if not isinstance(raw, dict):
        return _invalid([f"root: must be an object, got {type(raw).__name__}"])
    if not isinstance(story, dict):
        return _invalid(["root: 'story' argument must be the candidate_story object"])

    errors: list = []

    missing = [f for f in _TOP_LEVEL_REQUIRED_FIELDS if f not in raw]
    if missing:
        _err(errors, "root", f"missing required top-level field(s): {missing}")
        return _invalid(errors)

    expected_story_id = story.get("story_id")
    story_id = raw.get("story_id")
    if not _check_str(story_id, "root.story_id", errors):
        pass
    elif expected_story_id is not None and story_id != expected_story_id:
        _err(errors, "root.story_id",
             f"{story_id!r} does not match the candidate_story this output claims to be "
             f"about ({expected_story_id!r}) -- unknown/mismatched story_id")

    if isinstance(raw.get("research_status"), str) or "research_status" in raw:
        _check_enum(raw.get("research_status"), RESEARCH_STATUS_VALUES,
                    "root.research_status", errors)
    if isinstance(raw.get("hypothesis_assessment"), str) or "hypothesis_assessment" in raw:
        _check_enum(raw.get("hypothesis_assessment"), HYPOTHESIS_ASSESSMENT_VALUES,
                    "root.hypothesis_assessment", errors)
    if isinstance(raw.get("confidence"), str) or "confidence" in raw:
        _check_enum(raw.get("confidence"), CONFIDENCE_LEVELS, "root.confidence", errors)

    valid_doc_numbers = set(valid_document_numbers)

    # --- evidence_records: validate each, collect ids, check uniqueness ---
    evidence_ids_seen = []
    if _check_list(raw["evidence_records"], "root.evidence_records", errors, nonempty=False):
        for i, rec in enumerate(raw["evidence_records"]):
            eid = _validate_evidence_record(rec, i, valid_doc_numbers, retrieval_results, errors)
            if eid is not None:
                evidence_ids_seen.append(eid)

    dupes = {eid for eid in evidence_ids_seen if evidence_ids_seen.count(eid) > 1}
    if dupes:
        _err(errors, "root.evidence_records", f"duplicate evidence_id value(s): {sorted(dupes)}")
    valid_evidence_ids = set(evidence_ids_seen)

    # --- question_findings: validate each, check 1:1 coverage of the
    # candidate_story's research_questions (none dropped, none duplicated,
    # none substituted for a different question) ---
    questions_covered = []
    if _check_list(raw["question_findings"], "root.question_findings", errors, nonempty=False):
        for i, qf in enumerate(raw["question_findings"]):
            q = _validate_question_finding(qf, i, None, valid_evidence_ids, errors)
            if q is not None:
                questions_covered.append(q)

    expected_questions = story.get("research_questions")
    if isinstance(expected_questions, list):
        expected_set = set(expected_questions)
        covered_set = set(questions_covered)
        dropped = expected_set - covered_set
        if dropped:
            _err(errors, "root.question_findings",
                 f"research question(s) from the candidate_story were silently dropped "
                 f"(no question_findings entry): {sorted(dropped)}")
        unexpected = covered_set - expected_set
        if unexpected:
            _err(errors, "root.question_findings",
                 f"question_findings entry/entries do not match any research_question "
                 f"supplied by the candidate_story: {sorted(unexpected)}")
        dup_questions = {q for q in questions_covered if questions_covered.count(q) > 1}
        if dup_questions:
            _err(errors, "root.question_findings",
                 f"duplicate question_findings entries for the same question: {sorted(dup_questions)}")

    # --- contradictions ---
    if _check_list(raw["contradictions"], "root.contradictions", errors, nonempty=False):
        for i, c in enumerate(raw["contradictions"]):
            _validate_contradiction(c, i, valid_evidence_ids, errors)

    _check_list_of_str(raw["remaining_gaps"], "root.remaining_gaps", errors, nonempty=False)
    _check_list_of_str(raw["disconfirming_evidence_found"], "root.disconfirming_evidence_found",
                        errors, nonempty=False)

    if "overall_assessment" in raw:
        _check_str(raw["overall_assessment"], "root.overall_assessment", errors)

    # A non-"unresolved" hypothesis_assessment must rest on at least one
    # evidence_record -- it cannot be supported/weakened/contradicted by
    # zero evidence. insufficient_evidence research_status with
    # hypothesis_assessment=unresolved is the valid "found nothing" shape.
    hyp = raw.get("hypothesis_assessment")
    if hyp in ("supported", "weakened", "contradicted") and len(evidence_ids_seen) == 0:
        _err(errors, "root.hypothesis_assessment",
             f"hypothesis_assessment={hyp!r} requires at least one evidence_record; "
             "got zero")

    if errors:
        return _invalid(errors)
    return _valid()
