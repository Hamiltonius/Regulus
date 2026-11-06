#!/usr/bin/env python3
"""
intelligence_analyst_pass2.py — Intelligence Analyst, SECOND pass: evidence
reassessment (Role #3 in the Regulus corpus-intelligence pipeline).

    corpus_extractor.get_corpus()
            -> Intelligence Analyst PASS #1 (corpus_analyst.py)
            -> candidate_story (preliminary_hypothesis, alternative_
               hypotheses, research_questions, evidence_needed,
               disconfirming_evidence_needed)
            -> Evidence Analyst (evidence_analyst.py)
            -> validated evidence package for that same story
            -> INTELLIGENCE ANALYST PASS #2 (this module)
            -> structured reassessment (validated by
               intelligence_analyst_pass2_schema.validate_pass2_reassessment)
            -> (future) Intelligence Editor

This is an ANALYTICAL REASSESSMENT step, not a research step: Pass #2
receives exactly three inputs -- the original Pass #1 candidate_story, the
already-validated Evidence Analyst package for that same story, and the
story_id -- and must decide what survives, what changed, and what was
disproven. It has NO web_search tool and NO other tool; it may not
retrieve additional sources, and it may not invent facts beyond what the
supplied evidence package already contains. It is explicitly REQUIRED to
be capable of changing its mind -- confirming, modifying, weakening, or
contradicting the original hypothesis, not merely validating it.

Isolation discipline: imports only intelligence_analyst_pass2_schema (this
same new, standalone schema module) and `requests`/`json`/stdlib for the
live Anthropic call. Does NOT import dd_pipeline, dd_schema, regulus_v3,
corpus_analyst, corpus_analyst_schema, evidence_analyst, evidence_analyst_
schema, or evidence_retrieval -- no coupling to any of those modules'
validation, retrieval, or call logic. Makes no database connection and
performs no persistence of any kind; this module's only side effect is
the one outbound Anthropic Messages API request made by
call_anthropic_pass2 (no web_search tool, no other tool, no tool use at
all).
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests

import intelligence_analyst_pass2_schema as schema

log = logging.getLogger("regulus.intelligence_analyst_pass2")

SCHEMA_VERSION = "1.0"
PROMPT_VERSION = "1.0"

# Same model family already used for Stage 1/2/3, the Corpus Analyst, and
# the Evidence Analyst elsewhere in Regulus -- no new model introduced.
PASS2_MODEL = "claude-sonnet-4-6"

# Sizing rationale -- first-principles estimate, NOT yet live-measured
# (unlike CORPUS_ANALYST_MAX_TOKENS/EVIDENCE_ANALYST_MAX_TOKENS, both of
# which were revised upward only after live max_tokens-truncation
# evidence). Pass #2's output is a single reassessment of ONE already-
# produced evidence package -- materially smaller in scope than the
# Corpus Analyst's whole-corpus output or the Evidence Analyst's own
# research-and-retrieval output -- so a smaller starting ceiling is a
# reasonable first value, subject to the same kind of live-measurement
# revision as those two roles if CS-01 acceptance shows truncation.
PASS2_MAX_TOKENS = 8000

# No web_search tool is used here (spec: "It does NOT receive web-search
# capability... It must not retrieve additional sources") -- there are no
# server-side search rounds to wait on, so this mirrors CORPUS_ANALYST_
# TIMEOUT_SECONDS's original (pre-revision) reasoning rather than Stage
# 2/Evidence Analyst's 600s tool-using budget.
PASS2_TIMEOUT_SECONDS = 300

# Mirrors the existing STAGE2_MAX_ATTEMPTS/CORPUS_ANALYST_MAX_ATTEMPTS/
# EVIDENCE_ANALYST_MAX_ATTEMPTS pattern -- one retry on call/parse/
# transport failure, no autonomous loop.
PASS2_MAX_ATTEMPTS = 2


PASS2_SYSTEM_PROMPT = """You are the Intelligence Analyst performing the SECOND pass over one
candidate story: EVIDENCE REASSESSMENT. This is an analytical step, not a
research step. You have NO web_search tool and NO other tool -- you may
not retrieve additional sources. You must reason only from the two inputs
you are given.

WHAT YOU ARE GIVEN:
  - "original_story": the FIRST-PASS candidate_story exactly as produced
    by the Corpus Analyst, BEFORE any primary document was read --
    preliminary_hypothesis, alternative_hypotheses, research_questions,
    evidence_needed, disconfirming_evidence_needed, observational_basis,
    etc. This is a candidate judgment made from titles/abstracts alone; it
    may simply be wrong.
  - "evidence_package": the already-validated Evidence Analyst output for
    this SAME story -- research_status, hypothesis_assessment,
    question_findings, evidence_records (each with an evidence_id),
    contradictions, remaining_gaps, disconfirming_evidence_found, and
    overall_assessment. This evidence package SUPERSEDES any unsupported
    assumption the original_story made. You may reason from it, but you
    may NOT invent additional facts beyond what it actually contains.

YOUR JOB: explicitly compare the original hypothesis against the
evidence and decide, for the story as a whole and for each distinct
factual claim within it, what survived, what changed, what was
disproven, and what remains unknown. You MUST be capable of changing
your mind -- confirming, modifying, weakening, or contradicting the
original hypothesis are all legitimate outcomes; your job is NOT to
rescue or defend the original_story.

EVIDENCE AUTHORITY AND EPISTEMIC DISCIPLINE:
  - Unknown remains unknown. Do not resolve an unresolved question just
    to produce a decisive answer.
  - Absence of evidence must NOT become evidence of absence unless the
    evidence_package itself explicitly supports that conclusion (e.g. it
    explicitly states a search found nothing, not merely that it didn't
    look).
  - You must distinguish, for every claim you make: a DOCUMENTED FACT
    (directly stated by an evidence_record), a SUPPORTED ANALYTICAL
    INFERENCE (reasonably drawn from the evidence but not directly
    stated), and an UNRESOLVED HYPOTHESIS (neither confirmed nor refuted).
  - You must NOT convert: temporal proximity into coordination;
    publication volume into policy intensity; administrative codification
    into new policy; lack of observed action into proof that no action
    occurred; or partial regulatory relaxation into complete
    deregulation.
  - You must preserve meaningful disconfirming evidence. Do not silently
    omit a finding that damages the original hypothesis -- record it in
    weakened_or_rejected_findings and/or material_changes, and reflect it
    in assessment_disposition and confidence.
  - Every evidence_id you cite anywhere MUST already exist in the
    supplied evidence_package's evidence_records. Never invent, guess, or
    paraphrase an evidence_id. Source URLs do not need to be copied into
    your output -- traceability resolves entirely through evidence_ids.
  - You must assess EVERY alternative_hypothesis the original_story
    proposed -- none dropped, none substituted for different text.

OUTPUT: return ONLY valid JSON, no prose, no markdown fences, matching
EXACTLY this shape (empty arrays are fine where you genuinely have
nothing to report; omitting a required key is not):

{
  "story_id": "",
  "assessment_disposition": "confirmed|confirmed_with_modification|weakened|contradicted|insufficient_evidence",
  "original_hypothesis": "",
  "revised_hypothesis": "",
  "material_changes": [
    {
      "original_claim": "",
      "disposition": "retained|modified|removed|unresolved",
      "revised_claim": "",
      "reason": "",
      "evidence_ids": []
    }
  ],
  "supported_findings": [],
  "weakened_or_rejected_findings": [],
  "remaining_uncertainties": [],
  "alternative_hypotheses_assessment": [
    {
      "hypothesis": "",
      "disposition": "supported|partially_supported|weakened|contradicted|unresolved",
      "explanation": "",
      "evidence_ids": []
    }
  ],
  "intelligence_assessment": "",
  "confidence": "high|medium|low",
  "editor_eligibility": "eligible|eligible_with_caveats|not_eligible",
  "editor_caveats": []
}

"material_changes" should track each distinct factual claim from the
original_story that the evidence bears on (whether it survives
unchanged, is modified, is removed, or remains unresolved) -- it is not
required to enumerate every sentence of the original story, only the
claims that matter to the hypothesis. "alternative_hypotheses_assessment"
MUST have exactly one entry per alternative_hypothesis in original_story
-- copy each hypothesis string verbatim as given. revised_hypothesis and
intelligence_assessment must both be non-empty.
"""


def _extract_json_text(content_blocks):
    """Pull the final JSON text out of a Messages API response's content
    blocks. Mirrors dd_pipeline._extract_json_text / corpus_analyst.
    _extract_json_text / evidence_analyst._extract_json_text exactly,
    reimplemented locally so this module has no dependency on any of
    them. No web_search tool is used by this role, so there should never
    be a server_tool_use/web_search_tool_result block here in practice --
    this still only concatenates "text" blocks, defensively, in case any
    other block type were ever present."""
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    text = text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return text


class Pass2JSONDecodeError(ValueError):
    """Raised by call_anthropic_pass2 when the extracted text is not
    valid JSON. Mirrors dd_pipeline.Stage2JSONDecodeError / corpus_
    analyst.CorpusAnalystJSONDecodeError / evidence_analyst.
    EvidenceAnalystJSONDecodeError exactly -- DIAGNOSTIC-ONLY, str(this)
    is IDENTICAL to str() of the underlying json.JSONDecodeError.
    Response-side only; never carries the API key or any request header.
    """

    def __init__(self, json_error: json.JSONDecodeError, *, raw_text: str,
                 content_blocks: list, response_meta: dict):
        super().__init__(str(json_error))
        self.raw_text = raw_text
        self.content_blocks = content_blocks
        self.response_meta = response_meta


def _build_pass2_user_payload(story_id: str, original_story: dict, evidence_package: dict) -> dict:
    """Build the user-turn payload for the live call: the story_id, the
    original Pass #1 candidate_story EXACTLY as produced (unmodified),
    and the already-validated Evidence Analyst package for that same
    story EXACTLY as produced (unmodified). No corpus, no retrieval
    bundle, no web content -- this role reasons only over these two
    already-produced artifacts."""
    return {
        "story_id": story_id,
        "original_story": original_story,
        "evidence_package": evidence_package,
    }


def call_anthropic_pass2(story_id: str, original_story: dict, evidence_package: dict, api_key: str):
    """Real Intelligence Analyst Pass #2 call: Anthropic Messages API.
    NO tools -- this role gets no web_search and no other tool; it sees
    only the supplied original_story and evidence_package.

    Returns (parsed_json, raw_text, response_meta) on success, mirroring
    corpus_analyst.call_anthropic_corpus_analyst / evidence_analyst.
    call_anthropic_evidence_analyst's return shape exactly. Raises
    Pass2JSONDecodeError (carrying raw_text/content_blocks/response_meta)
    on a JSON parse failure; any other exception (network error, HTTP
    error, timeout) propagates as-is. Never persists or logs the API
    key -- it is used only in the one outbound request header.
    """
    payload = _build_pass2_user_payload(story_id, original_story, evidence_package)

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": PASS2_MODEL,
            "max_tokens": PASS2_MAX_TOKENS,
            "system": PASS2_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": json.dumps(payload, indent=2)}],
            # Deliberately NO "tools" key -- this role has no web_search
            # and no other tool, per spec.
        },
        timeout=PASS2_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    response_json = resp.json()
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
        raise Pass2JSONDecodeError(
            e, raw_text=text, content_blocks=content, response_meta=response_meta
        ) from e

    return parsed, text, response_meta


@dataclass
class Pass2AttemptDiagnostics:
    """Diagnostic record for ONE attempt inside run_intelligence_pass2's
    retry loop. Mirrors CorpusAnalystAttemptDiagnostics / EvidenceAnalyst
    AttemptDiagnostics field-for-field -- see either class's docstring
    for the full request_succeeded/transport_error vs. parse_succeeded/
    parse_error rationale. Captured for every attempt, success or
    failure.

    SECURITY: never carries ANTHROPIC_API_KEY, the x-api-key header, or
    any other credential material -- only the configured model/
    max_tokens/timeout (public constants) and the model's own response
    content/metadata.
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
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
        }


@dataclass
class Pass2Outcome:
    """Result of run_intelligence_pass2(). Mirrors CorpusAnalysisOutcome/
    EvidenceAnalysisOutcome's shape (raw / validation_status /
    validation_errors / failure_reason / is_valid / attempts). There is
    no `retrieval` field here (unlike EvidenceAnalysisOutcome) -- Pass #2
    makes no retrieval call of its own; it only reasons over the two
    inputs it was given."""
    raw: Optional[dict] = None
    validation_status: str = "invalid"   # "valid" | "invalid"
    validation_errors: list = field(default_factory=list)
    failure_reason: Optional[str] = None  # set on call/parse failure only
    attempts: list = field(default_factory=list)  # list[Pass2AttemptDiagnostics]

    @property
    def is_valid(self) -> bool:
        return self.validation_status == "valid"


def _run_single_attempt(attempt_number: int, story_id: str, original_story: dict,
                         evidence_package: dict, api_key: Optional[str], caller: Callable):
    """Run exactly one caller(story_id, original_story, evidence_package,
    api_key) attempt and return (raw_or_None, Pass2AttemptDiagnostics,
    error_or_None). Mirrors corpus_analyst._run_single_attempt /
    evidence_analyst._run_single_live_attempt exactly."""
    started = datetime.now(timezone.utc)
    diag = Pass2AttemptDiagnostics(
        attempt_number=attempt_number,
        model=PASS2_MODEL,
        max_tokens=PASS2_MAX_TOKENS,
        timeout_seconds=PASS2_TIMEOUT_SECONDS,
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
        result = caller(story_id, original_story, evidence_package, api_key)
    except Exception as e:  # network error, HTTP error, JSON parse error, stub failure
        _finish()
        raw_text = getattr(e, "raw_text", None)
        response_meta = getattr(e, "response_meta", None)
        if raw_text is not None or response_meta is not None:
            diag.request_succeeded = True
            diag.raw_text = raw_text
            diag.raw_text_length = len(raw_text) if raw_text is not None else None
            diag.parse_succeeded = False
            diag.parse_error = str(e)
            _apply_response_meta(response_meta)
        else:
            diag.request_succeeded = False
            diag.transport_error = str(e)
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


def run_intelligence_pass2(story_id: str, original_story: dict, evidence_package: dict, *,
                            api_key: Optional[str] = None,
                            call_analyst: Optional[Callable[[str, dict, dict, str], Any]] = None,
                            ) -> Pass2Outcome:
    """Run the Intelligence Analyst's second pass (evidence reassessment)
    for exactly ONE candidate_story, and deterministically validate the
    result.

    Inputs are exactly the three the spec calls for: story_id,
    original_story (the Pass #1 candidate_story), and evidence_package
    (the validated Evidence Analyst output for that same story). Before
    any model call, this function requires original_story["story_id"]
    and evidence_package["story_id"] (when present) to agree with
    story_id -- a caller-level wiring mismatch is a bug, not a model/
    network failure, so it raises ValueError immediately rather than
    being silently absorbed into a "call failed" outcome.

    This function:
      - makes NO web_search/tool call, and does not import dd_pipeline,
        dd_schema, regulus_v3, corpus_analyst, corpus_analyst_schema,
        evidence_analyst, evidence_analyst_schema, or evidence_retrieval;
      - retries up to PASS2_MAX_ATTEMPTS on call/parse/transport failure
        only (never on a structurally-valid-but-schema-invalid model
        output -- a model-quality issue, not a transient failure, exactly
        like run_corpus_analysis/run_live_evidence_analysis's own retry
        discipline);
      - validates with intelligence_analyst_pass2_schema.validate_pass2_
        reassessment against the exact evidence_id set present in
        evidence_package, so an invented evidence_id is always caught;
      - records one Pass2AttemptDiagnostics per attempt made (success or
        failure) onto the returned outcome's `attempts` list.

    `call_analyst`, when supplied (e.g. in tests), is called as
    call_analyst(story_id, original_story, evidence_package, api_key) and
    must return either a bare dict (legacy/stub shape) or a
    (parsed, raw_text, response_meta) tuple (the real call_anthropic_
    pass2's shape). Defaults to the real call_anthropic_pass2 -- unlike
    evidence_analyst.run_evidence_analysis's foundation-only interface,
    this task explicitly calls for the live implementation, so no
    NotImplementedError gate exists here; every test in this codebase
    that exercises this function supplies its own call_analyst so no real
    network call is ever made outside an explicit, separately-authorized
    live run.

    Never raises on a model/network/parse failure -- caught, logged, and
    reported via the returned Pass2Outcome.failure_reason.
    """
    if isinstance(original_story, dict) and original_story.get("story_id") not in (None, story_id):
        raise ValueError(
            f"story_id mismatch: story_id={story_id!r} but "
            f"original_story['story_id']={original_story.get('story_id')!r}"
        )
    if isinstance(evidence_package, dict) and evidence_package.get("story_id") not in (None, story_id):
        raise ValueError(
            f"story_id mismatch: story_id={story_id!r} but "
            f"evidence_package['story_id']={evidence_package.get('story_id')!r}"
        )

    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    caller = call_analyst or call_anthropic_pass2

    attempts = []
    raw = None
    last_error = None
    last_validation_result = None
    for attempt_number in range(1, PASS2_MAX_ATTEMPTS + 1):
        attempt_raw, diag, err = _run_single_attempt(
            attempt_number, story_id, original_story, evidence_package, api_key, caller,
        )
        if err is None:
            result = schema.validate_pass2_reassessment(
                attempt_raw, story_id=story_id, original_story=original_story,
                evidence_package=evidence_package,
            )
            diag.validation_succeeded = result.is_valid
            diag.validation_errors = result.validation_errors
            attempts.append(diag)
            raw = attempt_raw
            last_error = None
            last_validation_result = result
            break
        else:
            attempts.append(diag)
            last_error = err
            log.warning("Intelligence Analyst Pass #2 attempt %d/%d failed for story_id=%s: %s",
                        attempt_number, PASS2_MAX_ATTEMPTS, story_id, err)

    if last_error is not None:
        return Pass2Outcome(
            raw=None, validation_status="invalid",
            validation_errors=[f"pass2_call_failed: {last_error}"],
            failure_reason="pass2_call_failed",
            attempts=attempts,
        )

    return Pass2Outcome(
        raw=raw,
        validation_status="valid" if last_validation_result.is_valid else "invalid",
        validation_errors=last_validation_result.validation_errors,
        attempts=attempts,
    )
