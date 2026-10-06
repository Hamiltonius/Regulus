#!/usr/bin/env python3
"""
evidence_analyst.py — FOUNDATION ONLY future-call interface for the
Evidence Analyst role (Role #2 in the Regulus corpus-intelligence
pipeline).

    corpus_analyst candidate_story + corpus_extractor.Corpus
            -> evidence_retrieval.build_evidence_source_material()
            -> EVIDENCE ANALYST (this module's run_evidence_analysis)
            -> structured evidence package (validated by
               evidence_analyst_schema.validate_evidence_analysis)
            -> (future) Intelligence Analyst / Corpus Analyst pass 2

This module makes NO live Anthropic/model call. There is no default,
real `call_analyst` implementation here (unlike corpus_analyst.py, whose
call_anthropic_corpus_analyst is both real and the default) -- supplying
`call_analyst` is REQUIRED, and omitting it raises
EvidenceAnalystNotImplementedError rather than silently doing nothing or
reaching for a network call. This is a deliberate foundation-task
constraint: run_evidence_analysis() cannot make a live API call on its
own, no matter how it is invoked, until a future, separately-approved
task adds a real call_anthropic_evidence_analyst().

The function signature mirrors corpus_analyst.run_corpus_analysis's
injectable-caller shape (api_key / call_analyst, never raises on a
call/parse failure -- reports via the returned outcome) so that future
addition is a drop-in default, not a signature change.

Isolation discipline: imports evidence_analyst_schema and
evidence_retrieval (both new, adjacent intelligence-layer modules) and
corpus_extractor (for the Corpus type, same as corpus_analyst.py already
does). Does NOT import dd_pipeline, dd_schema, regulus_v3, corpus_analyst,
or corpus_analyst_schema directly -- those dependencies, where needed,
live in evidence_retrieval.py, not here. Makes no database connection and
performs no persistence of any kind.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import evidence_analyst_schema as schema
from corpus_extractor import Corpus
from evidence_retrieval import EvidenceRetrievalBundle, build_evidence_source_material

log = logging.getLogger("regulus.evidence_analyst")

# Foundation schema/prompt version markers. SCHEMA_VERSION tracks
# evidence_analyst_schema.py's shape; there is no PROMPT_VERSION yet
# because there is no prompt yet -- a real Evidence Analyst system prompt
# is future, separately-approved work. The epistemic rules this future
# prompt MUST encode (per task spec section 4) are recorded here as a
# plain list, not a prompt string, so this foundation task cannot be
# mistaken for having written the prompt:
EVIDENCE_ANALYST_SCHEMA_VERSION = "0.1"

EVIDENCE_ANALYST_EPISTEMIC_RULES = [
    "Evidence is not inference.",
    "Unknown is not false.",
    "Absence of reporting is not evidence of absence.",
    "Temporal proximity does not prove coordination.",
    "Publication does not equal effective date.",
    "Proposal does not equal final rule.",
    "Codification does not automatically equal substantive change.",
    "Frequency does not equal significance.",
    "Agency characterization must be distinguished from analyst inference.",
    "Primary sources control legal/regulatory claims.",
    "Secondary sources may contextualize but may not override primary sources.",
    "Unsupported intent attribution is prohibited.",
    "Every material factual conclusion must trace to evidence_records.",
    "Contradictory evidence must be preserved, not silently reconciled.",
    "A preliminary Corpus Analyst hypothesis may be wrong.",
    "The Evidence Analyst's job is NOT to defend it.",
    "Disconfirming evidence must receive equal treatment.",
    "research_status=insufficient_evidence is a valid successful outcome.",
]


class EvidenceAnalystNotImplementedError(NotImplementedError):
    """Raised by run_evidence_analysis() when no call_analyst is supplied.

    This is intentional: this foundation task must not make any live
    Anthropic/API call, so there is no default real Evidence Analyst
    caller to fall back to (contrast corpus_analyst.run_corpus_analysis,
    whose default is the real call_anthropic_corpus_analyst). A future,
    separately-approved task may add a real call_anthropic_evidence_analyst
    and wire it in as the default — until then, every call to
    run_evidence_analysis() must supply call_analyst explicitly (e.g. a
    test stub), or this exception is raised before any network/model
    activity could occur.
    """


@dataclass
class EvidenceAnalysisOutcome:
    """Result of run_evidence_analysis(). Mirrors corpus_analyst.
    CorpusAnalysisOutcome's shape (raw / validation_status /
    validation_errors / failure_reason / is_valid) plus `retrieval`, the
    EvidenceRetrievalBundle produced for this story — so a caller can
    inspect exactly what was (or wasn't) retrieved regardless of whether
    the (future) model call itself succeeded."""
    raw: Optional[dict] = None
    validation_status: str = "invalid"   # "valid" | "invalid"
    validation_errors: list = field(default_factory=list)
    failure_reason: Optional[str] = None  # set on call/parse failure only
    retrieval: Optional[EvidenceRetrievalBundle] = None

    @property
    def is_valid(self) -> bool:
        return self.validation_status == "valid"


def run_evidence_analysis(candidate_story: dict, corpus: Corpus, *,
                           api_key: Optional[str] = None,
                           call_analyst: Optional[Callable[[dict, EvidenceRetrievalBundle, Corpus], dict]] = None,
                           retrieve: Optional[Callable[..., EvidenceRetrievalBundle]] = None,
                           retrieval_kwargs: Optional[dict] = None,
                           ) -> EvidenceAnalysisOutcome:
    """Future-call interface for the Evidence Analyst over exactly ONE
    candidate_story.

    Always runs deterministic retrieval preparation first (no LLM, no
    database write) via `retrieve` (defaults to evidence_retrieval.
    build_evidence_source_material) — the retrieval bundle is attached to
    the returned outcome's `.retrieval` field even when call_analyst is
    never invoked (including when it raises EvidenceAnalystNotImplementedError),
    so retrieval diagnostics are never lost.

    `call_analyst` is REQUIRED in this foundation task — passing None
    raises EvidenceAnalystNotImplementedError rather than making (or
    silently skipping) a live model call. When supplied (e.g. in tests,
    a stub), it is called as call_analyst(candidate_story,
    retrieval_bundle, corpus) -> dict, and the result is deterministically
    validated via evidence_analyst_schema.validate_evidence_analysis(),
    using the retrieval bundle's own per-document identity_status findings
    as the ground truth for the source_identity_status cross-check.

    Never raises on a call/parse failure from call_analyst — caught,
    logged, and reported via the returned outcome's failure_reason,
    matching corpus_analyst.run_corpus_analysis's convention.
    """
    retrieve_fn = retrieve or build_evidence_source_material
    retrieval_bundle = retrieve_fn(candidate_story, corpus, **(retrieval_kwargs or {}))

    if call_analyst is None:
        raise EvidenceAnalystNotImplementedError(
            "run_evidence_analysis() has no default live Evidence Analyst call in "
            "this foundation task. Pass call_analyst explicitly (e.g. a test stub) "
            "to exercise validation/retrieval wiring; a real implementation is "
            "future, separately-approved work."
        )

    try:
        raw = call_analyst(candidate_story, retrieval_bundle, corpus)
    except Exception as e:  # network error, HTTP error, JSON parse error, stub failure
        log.warning("Evidence Analyst call failed for story_id=%s: %s",
                    candidate_story.get("story_id"), e)
        return EvidenceAnalysisOutcome(
            raw=None, validation_status="invalid",
            validation_errors=[f"evidence_analyst_call_failed: {e}"],
            failure_reason="evidence_analyst_call_failed",
            retrieval=retrieval_bundle,
        )

    valid_document_numbers = {
        o.document_number for o in corpus.observations if o.document_number is not None
    }
    result = schema.validate_evidence_analysis(
        raw, story=candidate_story, valid_document_numbers=valid_document_numbers,
        retrieval_results=retrieval_bundle.by_document_number(),
    )
    return EvidenceAnalysisOutcome(
        raw=raw,
        validation_status="valid" if result.is_valid else "invalid",
        validation_errors=result.validation_errors,
        retrieval=retrieval_bundle,
    )
