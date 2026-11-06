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

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests

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
    # Per-attempt diagnostics (see EvidenceAnalystAttemptDiagnostics below),
    # populated only by run_live_evidence_analysis -- always [] for
    # run_evidence_analysis (the foundation-only interface above), which
    # makes no retry loop and has no diagnostics to report. Additive field;
    # does not change the meaning of any field above.
    attempts: list = field(default_factory=list)

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


# ===========================================================================
# LIVE EVIDENCE ANALYST -- real Anthropic call (separately approved task).
#
# Everything below is ADDITIVE: run_evidence_analysis() above is completely
# unchanged (still requires an explicit call_analyst, still raises
# EvidenceAnalystNotImplementedError when one isn't supplied) -- it remains
# the foundation-only interface for structural/unit testing against any
# stub. run_live_evidence_analysis() below is the new, separate entry point
# that actually talks to the Anthropic API, mirroring corpus_analyst.py's
# run_corpus_analysis()/call_anthropic_corpus_analyst()/
# CorpusAnalystAttemptDiagnostics pattern exactly: same model-family
# convention, same bounded retry, same per-attempt diagnostics shape, same
# "never raise on call/parse failure -- report via the outcome" contract.
#
# This module still does NOT import dd_pipeline, dd_schema, regulus_v3,
# corpus_analyst, or corpus_analyst_schema -- the real API call below uses
# only `requests` and the Anthropic Messages API directly, exactly like
# dd_pipeline.call_anthropic_stage2 / corpus_analyst.call_anthropic_corpus_
# analyst do, with no new dependency on either of those modules.
# ===========================================================================

# Same model family/configuration convention already used by Stage 2/3 and
# the Corpus Analyst elsewhere in Regulus -- no new model introduced.
EVIDENCE_ANALYST_MODEL = "claude-sonnet-4-6"

# Sizing follows the same convention as CORPUS_ANALYST_MAX_TOKENS/
# STAGE2_MAX_TOKENS after their own live-measurement corrections (both
# ultimately raised to 20000 after measured max_tokens truncation at their
# prior ceilings). The Evidence Analyst's output is bounded by the same
# EvidenceAnalysisOutcome schema shape (one candidate_story's worth of
# question_findings/evidence_records/contradictions, not a whole corpus),
# so 20000 is adopted as the proven-safe starting ceiling for this role
# too, pending the same kind of live-measurement review if CS-01
# acceptance shows truncation.
EVIDENCE_ANALYST_MAX_TOKENS = 20000

# Mirrors STAGE2_TIMEOUT_SECONDS (600s) rather than CORPUS_ANALYST_
# TIMEOUT_SECONDS: like Stage 2 (and unlike the Corpus Analyst), this role
# uses the server-side web_search tool, which can involve multiple search
# rounds before the model produces its final text -- the same timeout
# budget already proven sufficient for Stage 2's tool-using calls at this
# token ceiling.
EVIDENCE_ANALYST_TIMEOUT_SECONDS = 600

# Mirrors the existing STAGE2_MAX_ATTEMPTS/CORPUS_ANALYST_MAX_ATTEMPTS
# pattern -- one retry on call/parse/transport failure, no autonomous loop.
EVIDENCE_ANALYST_MAX_ATTEMPTS = 2

# Bounded, not an unrestricted crawler (spec section 3): the model may
# issue AT MOST this many server-side web_search calls per attempt, each
# one a query the model itself formulates -- never a scheduled or
# open-ended autonomous research loop, and never a bulk/site-wide crawl.
# Mirrors dd_pipeline.call_anthropic_stage2's "max_uses": 8 web_search
# convention; set slightly higher here (10) because this role may need to
# both verify primary documents' authorities AND search for a distinct
# companion agency action, where Stage 2 only researches one document.
EVIDENCE_ANALYST_WEB_SEARCH_MAX_USES = 10

PROMPT_VERSION = "1.0"

_EVIDENCE_ANALYST_EPISTEMIC_RULES_TEXT = "\n".join(
    f"  - {rule}" for rule in EVIDENCE_ANALYST_EPISTEMIC_RULES
)

EVIDENCE_ANALYST_SYSTEM_PROMPT = f"""You are the Evidence Analyst in a two-pass regulatory intelligence
pipeline. A first-pass Corpus Analyst has already proposed ONE candidate
story -- a preliminary_hypothesis, alternative_hypotheses, research_
questions, evidence_needed, and disconfirming_evidence_needed -- from
titles/abstracts/automated summaries alone, WITHOUT reading any primary
government document. Your job is to actually investigate that one story
by reading authoritative primary sources and reporting what the evidence
shows -- which may fully support, partially support, weaken, contradict,
or fail to resolve the preliminary hypothesis. insufficient_evidence is a
valid, successful outcome of your work; it is not a failure to produce one.

YOU ARE ADVERSARIAL TOWARD THE PRELIMINARY HYPOTHESIS, NOT AN ADVOCATE FOR
IT. The Corpus Analyst's hypothesis may simply be wrong. Your job is to
test it and its alternatives against real evidence, and to actively
look for evidence that would weaken or contradict it -- not merely to
confirm it is plausible.

EPISTEMIC RULES -- apply every one of these, not merely avoid
contradicting them:
{_EVIDENCE_ANALYST_EPISTEMIC_RULES_TEXT}

WHAT YOU ARE GIVEN:
  - "candidate_story": the Corpus Analyst's full, unmodified output for
    this one story (story_id, title, preliminary_hypothesis,
    alternative_hypotheses, research_questions, evidence_needed,
    disconfirming_evidence_needed, supporting_document_numbers, etc).
  - "retrieved_documents": a deterministic, non-LLM retrieval layer has
    ALREADY fetched each of this story's supporting_document_numbers from
    the Federal Register and verified its identity before you ever saw
    it. Each entry has "status" ("retrieved" or "failed"),
    "identity_status" ("verified" means the deterministic layer confirmed
    this document's identity independently -- you may rely on this;
    "mismatch" means identity could NOT be confirmed -- treat any such
    document as unusable, do not cite it as evidence), "text" (the
    authoritative document text actually fetched -- null if retrieval
    failed), "text_source" ("pdf_full" = the complete document;
    "pdf_excerpt" = ONLY a bounded excerpt, NOT the full document --
    "truncated": true marks this), "source_url", "publication_date", and
    "effective_date" (these two are frequently DIFFERENT dates -- never
    conflate them). A "failed" entry means no authoritative text could be
    retrieved for that document_number; you may not substitute anything
    else (an abstract, a title, your own prior knowledge) as if it were
    that document's primary text, and you must account for it in
    remaining_gaps rather than silently ignoring it.

FULL-DOCUMENT DISCIPLINE:
  - When text_source="pdf_full", you have the complete document; read it
    fully (preamble, operative text, definitions, exceptions, authorities
    cited, effective-date provisions) before answering the research
    questions.
  - When text_source="pdf_excerpt" (truncated=true), you have ONLY a
    bounded excerpt, not the full document. Explicitly say so in
    "limitations" for any evidence_record built from it, and NEVER claim
    or imply you reviewed sections beyond what was actually supplied.
  - Never rely on a Federal Register abstract/title alone when operative
    primary text was actually supplied to you -- read the text itself.

RESEARCH BEYOND THE SUPPLIED DOCUMENTS: you MAY use the web_search tool,
but this is NOT a license to crawl broadly. Use it only when the
candidate_story's research_questions, evidence_needed, or
disconfirming_evidence_needed, or an authority/citation named INSIDE a
retrieved primary document, or an obvious companion government action
(e.g. a parallel rule from a different agency implementing the same
upstream decision) genuinely requires it. When you do search, prefer
sources in this order -- primary sources CONTROL legal/regulatory
conclusions, secondary sources may only provide context and never
override a primary source:
  1. Federal Register (federalregister.gov) / GovInfo (govinfo.gov) / eCFR
  2. BIS / Treasury / OFAC / State / DDTC / FinCEN (bis.doc.gov,
     treasury.gov, ofac.treasury.gov, state.gov, commerce.gov)
  3. White House / other authoritative U.S. agency sites
  4. Congress / statutory text (congress.gov, uscode.house.gov)
  5. Secondary sources (news coverage, law-firm summaries, etc.) -- only
     when needed for context; never as the basis for a legal/regulatory
     conclusion, and never presented as equivalent in authority to a
     primary source.

SOURCE IDENTITY -- you must NOT claim "verified" for any evidence_record
unless it corresponds to one of the "retrieved_documents" entries the
deterministic retrieval layer already marked identity_status="verified".
For anything YOU find yourself via web_search, set source_identity_status
to "unverified" (if it looks like a primary government document you have
not independently had cross-checked) or "not_applicable" (if it is a
secondary/contextual source making no claim to be a specific government
document). Only set "document_number" when you are citing one of this
story's supplied supporting_document_numbers; for anything else you find
via web_search, leave document_number null and identify it by
source_title/source_url instead -- never invent, guess, or paraphrase a
Federal Register document_number for a document you were not given.

TRACEABILITY: every material factual finding in question_findings and
every contradiction must trace to one or more evidence_ids that actually
exist in your own evidence_records. A question_finding with status
"answered" or "partially_answered" must cite the evidence_ids that
answer it. Every "retrieved_documents" entry with status="retrieved" that
you were actually given MUST be accounted for in evidence_records (cited
by its document_number) -- you may not silently drop a supplied primary
document; if, after reading it, it turns out not to bear on this story,
say so explicitly in that evidence_record's "limitations" rather than
omitting it. Preserve contradictory evidence rather than silently
reconciling it away -- if two sources disagree, record both and say so in
"contradictions", don't quietly pick the one that fits your assessment.

OUTPUT: return ONLY valid JSON, no prose, no markdown fences, matching
EXACTLY this shape (empty arrays are fine where you genuinely found
nothing to report; omitting a required key is not):

{{
  "story_id": "",
  "research_status": "complete|partial|insufficient_evidence",
  "hypothesis_assessment": "supported|weakened|contradicted|unresolved",
  "question_findings": [
    {{
      "question": "",
      "status": "answered|partially_answered|unanswered",
      "finding": "",
      "evidence_ids": [],
      "confidence": "high|medium|low"
    }}
  ],
  "evidence_records": [
    {{
      "evidence_id": "",
      "document_number": null,
      "source_title": "",
      "source_url": "",
      "source_type": "primary|secondary",
      "primary_source": true,
      "retrieved_at": null,
      "source_identity_status": "verified|mismatch|unverified|not_applicable",
      "publication_date": null,
      "effective_date": null,
      "relevant_excerpt": "",
      "supports": [],
      "contradicts": [],
      "limitations": ""
    }}
  ],
  "contradictions": [
    {{"description": "", "evidence_ids": [], "significance": ""}}
  ],
  "remaining_gaps": [],
  "disconfirming_evidence_found": [],
  "overall_assessment": "",
  "confidence": "high|medium|low"
}}

Either "relevant_excerpt" or "evidence_summary" must be a non-empty string
on every evidence_record (use "evidence_summary" instead of
"relevant_excerpt" for a secondary/contextual source with no literal
excerpt to quote). question_findings must cover EVERY research_question
in the candidate_story exactly once -- none dropped, none duplicated,
none substituted for different question text. A non-"unresolved"
hypothesis_assessment requires at least one evidence_record; it cannot
rest on zero evidence.
"""


def _extract_json_text(content_blocks):
    """Pull the final JSON text out of a Messages API response's content
    blocks, ignoring any server_tool_use / web_search_tool_result blocks
    (those are the model's search activity, not its answer). Mirrors
    dd_pipeline._extract_json_text / corpus_analyst._extract_json_text
    exactly, reimplemented locally so this module keeps its existing
    zero-dependency isolation from dd_pipeline/corpus_analyst."""
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    text = text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return text


class EvidenceAnalystJSONDecodeError(ValueError):
    """Raised by call_anthropic_evidence_analyst when the MODEL'S OWN
    extracted answer text is not valid JSON (the HTTP response envelope
    itself parsed fine -- resp.json() succeeded; it's the model's text
    content, after _extract_json_text(), that doesn't parse). Mirrors
    dd_pipeline.Stage2JSONDecodeError / corpus_analyst.
    CorpusAnalystJSONDecodeError exactly -- DIAGNOSTIC-ONLY, str(this) is
    IDENTICAL to str() of the underlying json.JSONDecodeError. Response-
    side only; never carries the API key or any request header.

    error_category (class attribute, read via getattr by callers that
    don't import this module) distinguishes this from
    EvidenceAnalystAPIResponseJSONDecodeError below -- both exception
    types raise with byte-identical str() text when the underlying
    json.JSONDecodeError message happens to match (e.g. an empty string
    at either decode point produces the same "Expecting value: line 1
    column 1 (char 0)"), so the message ALONE never distinguishes them;
    this attribute (and the different fields each class carries) does.
    """

    error_category = "model_output_json_decode_error"

    def __init__(self, json_error: json.JSONDecodeError, *, raw_text: str,
                 content_blocks: list, response_meta: dict):
        super().__init__(str(json_error))
        self.raw_text = raw_text
        self.content_blocks = content_blocks
        self.response_meta = response_meta


_RESPONSE_BODY_PREVIEW_MAX_CHARS = 500


def _sanitized_body_preview(body_text: Optional[str], *, max_chars: int = _RESPONSE_BODY_PREVIEW_MAX_CHARS) -> Optional[str]:
    """Bound a raw HTTP response BODY (never request headers, never the
    api_key) to a fixed length for diagnostics. Truncation is marked
    explicitly so a preview is never mistaken for the complete body."""
    if body_text is None:
        return None
    if len(body_text) <= max_chars:
        return body_text
    return body_text[:max_chars] + f"... [truncated, {len(body_text)} chars total]"


class EvidenceAnalystAPIResponseJSONDecodeError(ValueError):
    """Raised by call_anthropic_evidence_analyst when resp.json() itself
    fails -- the HTTP response ENVELOPE was not valid JSON (distinct from
    EvidenceAnalystJSONDecodeError above, where the envelope parses fine
    but the model's own answer text inside it doesn't). This is the
    "api_response_json_decode_error" category: it means something
    between the client and the Anthropic API returned a non-JSON (often
    empty) body on an HTTP status that did not itself raise via
    resp.raise_for_status() (a 2xx with an unparseable/empty body, or a
    non-2xx whose body also happened not to be JSON -- status_code below
    disambiguates which).

    Carries HTTP status_code, content_type, body_length (bytes), and a
    bounded/sanitized body_preview (see _sanitized_body_preview) for
    diagnostics -- enough to tell a truncated/empty response apart from
    an HTML error page, without ever persisting request headers, the
    api_key, or any credential. Response-side only, exactly like
    EvidenceAnalystJSONDecodeError.
    """

    error_category = "api_response_json_decode_error"

    def __init__(self, json_error: json.JSONDecodeError, *, status_code: Optional[int],
                 content_type: Optional[str], body_length: Optional[int], body_preview: Optional[str]):
        super().__init__(str(json_error))
        self.status_code = status_code
        self.content_type = content_type
        self.body_length = body_length
        self.body_preview = body_preview


def _build_evidence_analyst_user_payload(candidate_story: dict,
                                          retrieval_bundle: EvidenceRetrievalBundle,
                                          corpus: Corpus) -> dict:
    """Build the user-turn payload for the live call: the candidate_story
    EXACTLY as produced by the Corpus Analyst (unmodified), the deterministic
    retrieval layer's own findings for this story's supporting documents
    (never the whole 87-observation corpus -- this role investigates ONE
    story, not the full reporting window), and the corpus's reporting
    period for date context only."""
    return {
        "reporting_period": {"start": corpus.start_date, "end": corpus.end_date},
        "candidate_story": candidate_story,
        "retrieved_documents": [d.to_dict() for d in retrieval_bundle.documents],
    }


def call_anthropic_evidence_analyst(candidate_story: dict,
                                     retrieval_bundle: EvidenceRetrievalBundle,
                                     corpus: Corpus, api_key: str):
    """Real Evidence Analyst call: Anthropic Messages API with the
    server-side web_search tool enabled (bounded by
    EVIDENCE_ANALYST_WEB_SEARCH_MAX_USES -- never an unrestricted crawler),
    so the model can investigate beyond the supplied retrieved_documents
    when the story's research questions genuinely require it.

    Returns (parsed_json, raw_text, response_meta) on success, mirroring
    corpus_analyst.call_anthropic_corpus_analyst's return shape exactly.
    Raises EvidenceAnalystAPIResponseJSONDecodeError if the HTTP response
    ENVELOPE itself is not valid JSON (resp.json() failure -- status_code/
    content_type/body_length/body_preview captured BEFORE the decode
    attempt, so this failure mode is never silently indistinguishable
    from "no response received at all"), or EvidenceAnalystJSONDecodeError
    (carrying raw_text/content_blocks/response_meta) if the envelope
    parses fine but the MODEL'S own extracted answer text does not. Any
    other exception (network error, HTTP error, timeout) propagates
    as-is. Never persists or logs the API key, any request header, or
    the full response body -- only a length-bounded body preview, and
    only of the response, never the request.
    """
    payload = _build_evidence_analyst_user_payload(candidate_story, retrieval_bundle, corpus)

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": EVIDENCE_ANALYST_MODEL,
            "max_tokens": EVIDENCE_ANALYST_MAX_TOKENS,
            "system": EVIDENCE_ANALYST_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": json.dumps(payload, indent=2)}],
            "tools": [{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": EVIDENCE_ANALYST_WEB_SEARCH_MAX_USES,
            }],
        },
        timeout=EVIDENCE_ANALYST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()

    # Capture response-envelope diagnostics BEFORE attempting to decode it
    # as JSON, so a decode failure here is never left with nothing but a
    # bare "Expecting value..." message -- status/content-type/body
    # length/a bounded body preview are always available for the
    # EvidenceAnalystAPIResponseJSONDecodeError raised below, should
    # resp.json() fail. Never captures request headers or the api_key.
    status_code = resp.status_code
    content_type = resp.headers.get("content-type")
    body_length = len(resp.content) if resp.content is not None else None

    try:
        response_json = resp.json()
    except json.JSONDecodeError as e:
        raise EvidenceAnalystAPIResponseJSONDecodeError(
            e, status_code=status_code, content_type=content_type, body_length=body_length,
            body_preview=_sanitized_body_preview(resp.text),
        ) from e

    content = response_json["content"]
    text = _extract_json_text(content)

    response_meta = {
        "stop_reason": response_json.get("stop_reason"),
        "stop_sequence": response_json.get("stop_sequence"),
        "model": response_json.get("model"),
        "usage": response_json.get("usage"),
        "content_block_count": len(content) if isinstance(content, list) else None,
        "content_block_types": (
            [b.get("type") for b in content] if isinstance(content, list) else None
        ),
    }

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise EvidenceAnalystJSONDecodeError(
            e, raw_text=text, content_blocks=content, response_meta=response_meta
        ) from e

    return parsed, text, response_meta


@dataclass
class EvidenceAnalystAttemptDiagnostics:
    """Diagnostic record for ONE attempt inside run_live_evidence_analysis's
    retry loop. Mirrors corpus_analyst.CorpusAnalystAttemptDiagnostics
    field-for-field and the same request_succeeded/transport_error vs.
    parse_succeeded/parse_error distinction -- see that class's docstring
    for the full rationale. Captured for every attempt, success or
    failure, so a JSON parse failure or max_tokens truncation never
    destroys what the model actually returned.

    SECURITY: never carries ANTHROPIC_API_KEY, the x-api-key header, or any
    other credential material -- only the configured model/max_tokens/
    timeout (public constants) and the model's own response content/
    metadata.

    error_category distinguishes WHICH of three failure shapes this
    attempt hit (set only when request_succeeded/parse_succeeded indicate
    a failure): "model_output_json_decode_error" (the HTTP response
    envelope parsed fine, but the model's own extracted text did not --
    see EvidenceAnalystJSONDecodeError), "api_response_json_decode_error"
    (the envelope itself did not parse as JSON -- see
    EvidenceAnalystAPIResponseJSONDecodeError; http_status_code/
    http_content_type/http_body_length/http_body_preview are populated
    only in this case), "transport_error" (no response was received at
    all -- network/timeout/connection failure), or a caller-supplied
    category string (e.g. "budget_exhausted", read via the same
    getattr(e, "error_category", None) convention from any exception a
    wrapping caller raises -- this module does not need to know about
    such callers by name). This additive field never changes the
    pre-existing request_succeeded/parse_succeeded/transport_error/
    parse_error semantics -- it is a classification on top of them.
    """
    attempt_number: int
    model: str
    max_tokens: int
    timeout_seconds: int
    request_succeeded: bool = False
    stop_reason: Optional[str] = None
    stop_sequence: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    raw_text: Optional[str] = None
    raw_text_length: Optional[int] = None
    parse_succeeded: Optional[bool] = None
    parse_error: Optional[str] = None
    validation_succeeded: Optional[bool] = None
    validation_errors: list = field(default_factory=list)
    transport_error: Optional[str] = None
    error_category: Optional[str] = None
    http_status_code: Optional[int] = None
    http_content_type: Optional[str] = None
    http_body_length: Optional[int] = None
    http_body_preview: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "attempt_number": self.attempt_number,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "request_succeeded": self.request_succeeded,
            "stop_reason": self.stop_reason,
            "stop_sequence": self.stop_sequence,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "raw_text": self.raw_text,
            "raw_text_length": self.raw_text_length,
            "parse_succeeded": self.parse_succeeded,
            "parse_error": self.parse_error,
            "validation_succeeded": self.validation_succeeded,
            "validation_errors": self.validation_errors,
            "transport_error": self.transport_error,
            "error_category": self.error_category,
            "http_status_code": self.http_status_code,
            "http_content_type": self.http_content_type,
            "http_body_length": self.http_body_length,
            "http_body_preview": self.http_body_preview,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
        }


def _run_single_live_attempt(attempt_number: int, candidate_story: dict,
                              retrieval_bundle: EvidenceRetrievalBundle, corpus: Corpus,
                              api_key: Optional[str], caller: Callable):
    """Run exactly one caller(candidate_story, retrieval_bundle, corpus,
    api_key) attempt and return (raw_or_None, EvidenceAnalystAttemptDiagnostics,
    error_or_None). Mirrors corpus_analyst._run_single_attempt exactly --
    see that function's docstring for the full request_succeeded/
    transport_error vs. parse_succeeded/parse_error rationale."""
    started = datetime.now(timezone.utc)
    diag = EvidenceAnalystAttemptDiagnostics(
        attempt_number=attempt_number,
        model=EVIDENCE_ANALYST_MODEL,
        max_tokens=EVIDENCE_ANALYST_MAX_TOKENS,
        timeout_seconds=EVIDENCE_ANALYST_TIMEOUT_SECONDS,
        started_at=started.isoformat(),
    )

    def _finish():
        finished = datetime.now(timezone.utc)
        diag.finished_at = finished.isoformat()
        diag.duration_seconds = (finished - started).total_seconds()

    def _apply_response_meta(response_meta):
        if not response_meta:
            return
        diag.stop_reason = response_meta.get("stop_reason")
        diag.stop_sequence = response_meta.get("stop_sequence")
        usage = response_meta.get("usage") or {}
        diag.input_tokens = usage.get("input_tokens")
        diag.output_tokens = usage.get("output_tokens")

    try:
        result = caller(candidate_story, retrieval_bundle, corpus, api_key)
    except Exception as e:  # network error, HTTP error, JSON parse error, stub failure
        _finish()
        error_category = getattr(e, "error_category", None)
        raw_text = getattr(e, "raw_text", None)
        response_meta = getattr(e, "response_meta", None)
        status_code = getattr(e, "status_code", None)
        content_type = getattr(e, "content_type", None)
        body_length = getattr(e, "body_length", None)
        body_preview = getattr(e, "body_preview", None)

        if error_category == "model_output_json_decode_error" or (
            error_category is None and (raw_text is not None or response_meta is not None)
        ):
            # The HTTP response envelope parsed fine; the model's own
            # extracted answer text did not (EvidenceAnalystJSONDecodeError).
            diag.request_succeeded = True
            diag.raw_text = raw_text
            diag.raw_text_length = len(raw_text) if raw_text is not None else None
            diag.parse_succeeded = False
            diag.parse_error = str(e)
            diag.error_category = error_category or "model_output_json_decode_error"
            _apply_response_meta(response_meta)
        elif error_category == "api_response_json_decode_error" or (
            error_category is None and status_code is not None
        ):
            # The HTTP response envelope itself did not parse as JSON
            # (EvidenceAnalystAPIResponseJSONDecodeError) -- a response
            # WAS received (hence request_succeeded=True), just not a
            # decodable one. Distinct from both the case above and from
            # "no response at all" below.
            diag.request_succeeded = True
            diag.parse_succeeded = False
            diag.parse_error = str(e)
            diag.error_category = error_category or "api_response_json_decode_error"
            diag.http_status_code = status_code
            diag.http_content_type = content_type
            diag.http_body_length = body_length
            diag.http_body_preview = body_preview
        else:
            # No response body was ever available -- a transport/API-level
            # failure (network error, timeout, a legacy stub that simply
            # raises), OR a caller-supplied exception (e.g. a run-budget
            # guard) that made no request at all. error_category, when
            # the exception supplied one, still records WHICH such
            # failure this was without implying a response was received.
            diag.request_succeeded = False
            diag.transport_error = str(e)
            diag.error_category = error_category or "transport_error"
        return None, diag, e

    _finish()
    diag.request_succeeded = True
    diag.parse_succeeded = True

    if isinstance(result, tuple) and len(result) == 3:
        raw, raw_text, response_meta = result
    else:
        # Legacy/test-stub shape: a bare dict, no diagnostic metadata
        # available for this attempt -- not fabricated.
        raw, raw_text, response_meta = result, None, None

    diag.raw_text = raw_text
    diag.raw_text_length = len(raw_text) if raw_text is not None else None
    _apply_response_meta(response_meta)

    return raw, diag, None


def _find_silently_dropped_documents(raw: Any, retrieval_bundle: EvidenceRetrievalBundle) -> list:
    """Deterministic check (spec section 9): a supplied primary document
    that the retrieval layer successfully retrieved for this story may not
    be silently absent from evidence_records. Returns a list of human-
    readable error strings (empty if none were dropped). This is a
    run_live_evidence_analysis-level check, not a change to evidence_
    analyst_schema.py -- the approved schema module is left exactly as
    delivered in the foundation task; this is additive traceability
    enforcement specific to the live call."""
    if not isinstance(raw, dict):
        return []
    evidence_records = raw.get("evidence_records")
    if not isinstance(evidence_records, list):
        return []
    cited_document_numbers = {
        rec.get("document_number") for rec in evidence_records if isinstance(rec, dict)
    }
    errors = []
    for doc in retrieval_bundle.retrieved_documents:
        if doc.document_number not in cited_document_numbers:
            errors.append(
                f"root.evidence_records: supplied primary document "
                f"{doc.document_number!r} was successfully retrieved but never cited "
                "in evidence_records (a supplied document may not be silently dropped)"
            )
    return errors


def run_live_evidence_analysis(candidate_story: dict, corpus: Corpus, *,
                                api_key: Optional[str] = None,
                                call_analyst: Optional[Callable[[dict, EvidenceRetrievalBundle, Corpus, str], Any]] = None,
                                retrieve: Optional[Callable[..., EvidenceRetrievalBundle]] = None,
                                retrieval_kwargs: Optional[dict] = None,
                                ) -> EvidenceAnalysisOutcome:
    """Live entry point for the Evidence Analyst over exactly ONE
    candidate_story. Separate from run_evidence_analysis() above (which
    remains the foundation-only, no-default-caller interface): this
    function DOES default call_analyst to the real
    call_anthropic_evidence_analyst, mirroring corpus_analyst.
    run_corpus_analysis's architecture exactly --

      - always runs deterministic retrieval first (no LLM, no database
        write), via `retrieve` (defaults to evidence_retrieval.
        build_evidence_source_material) -- attached to the outcome's
        `.retrieval` even on total failure;
      - retries up to EVIDENCE_ANALYST_MAX_ATTEMPTS on call/parse/transport
        failure only (never on a structurally-valid-but-schema-invalid
        model output -- that is a model-quality issue, not a transient
        failure, exactly like run_corpus_analysis's own retry discipline);
      - validates with evidence_analyst_schema.validate_evidence_analysis
        PLUS the additional _find_silently_dropped_documents check (spec
        section 9) -- invalid model output can never become a valid
        evidence package, and a supplied primary document can never be
        silently absent from evidence_records;
      - records one EvidenceAnalystAttemptDiagnostics per attempt made
        (success or failure) onto the returned outcome's `.attempts` list.

    `call_analyst`, when supplied (e.g. in tests), is called as
    call_analyst(candidate_story, retrieval_bundle, corpus, api_key) and
    must return either a bare dict (legacy/stub shape) or a
    (parsed, raw_text, response_meta) tuple (the real call_anthropic_
    evidence_analyst's shape) -- never makes a live network call when a
    test supplies its own call_analyst/retrieve.

    Never raises on a model/network/parse failure -- caught, logged, and
    reported via the returned EvidenceAnalysisOutcome.failure_reason.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    caller = call_analyst or call_anthropic_evidence_analyst
    retrieve_fn = retrieve or build_evidence_source_material
    retrieval_bundle = retrieve_fn(candidate_story, corpus, **(retrieval_kwargs or {}))

    valid_document_numbers = {
        o.document_number for o in corpus.observations if o.document_number is not None
    }

    attempts = []
    raw = None
    last_error = None
    last_is_valid = False
    last_validation_errors: list = []
    for attempt_number in range(1, EVIDENCE_ANALYST_MAX_ATTEMPTS + 1):
        attempt_raw, diag, err = _run_single_live_attempt(
            attempt_number, candidate_story, retrieval_bundle, corpus, api_key, caller,
        )
        if err is None:
            result = schema.validate_evidence_analysis(
                attempt_raw, story=candidate_story,
                valid_document_numbers=valid_document_numbers,
                retrieval_results=retrieval_bundle.by_document_number(),
            )
            dropped_errors = _find_silently_dropped_documents(attempt_raw, retrieval_bundle)
            combined_errors = list(result.validation_errors) + dropped_errors
            is_valid = result.is_valid and not dropped_errors

            diag.validation_succeeded = is_valid
            diag.validation_errors = combined_errors
            attempts.append(diag)

            raw = attempt_raw
            last_error = None
            last_is_valid = is_valid
            last_validation_errors = combined_errors
            break
        else:
            attempts.append(diag)
            last_error = err
            log.warning("Evidence Analyst live attempt %d/%d failed for story_id=%s: %s",
                        attempt_number, EVIDENCE_ANALYST_MAX_ATTEMPTS,
                        candidate_story.get("story_id"), err)

    if last_error is not None:
        return EvidenceAnalysisOutcome(
            raw=None, validation_status="invalid",
            validation_errors=[f"evidence_analyst_call_failed: {last_error}"],
            failure_reason="evidence_analyst_call_failed",
            retrieval=retrieval_bundle,
            attempts=attempts,
        )

    return EvidenceAnalysisOutcome(
        raw=raw,
        validation_status="valid" if last_is_valid else "invalid",
        validation_errors=last_validation_errors,
        retrieval=retrieval_bundle,
        attempts=attempts,
    )
