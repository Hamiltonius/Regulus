#!/usr/bin/env python3
"""
intelligence_editor.py — the Intelligence Editor (Role #4 in the Regulus
corpus-intelligence pipeline): compiles REGULUS INTELLIGENCE BRIEF #001
from already-validated Pass #2 story reassessments.

    Intelligence Analyst Pass #2 (intelligence_analyst_pass2.py)
            -> validated reassessment per story, editor_eligibility != "not_eligible"
            -> INTELLIGENCE EDITOR (this module)
            -> structured Brief (validated by
               intelligence_editor_schema.validate_brief)
            -> (future) email/PDF/GUI rendering

The Editor receives ONLY validated Pass #2 story assessments -- it has NO
web_search tool, NO other tool, performs NO independent research, and
adds NO new facts. It may organize, prioritize, compress, and explain the
validated intelligence it is given; it may also exclude a weak,
administrative, duplicative, or insufficiently supported story from the
Brief entirely (stories_excluded). It must NEVER resurrect a claim Pass
#2 marked removed, or an alternative_hypothesis Pass #2 assessed
contradicted/weakened, as though it still held.

Isolation discipline: imports only intelligence_editor_schema (this same
new, standalone schema module) and `requests`/`json`/stdlib for the live
Anthropic call. Does NOT import dd_pipeline, dd_schema, regulus_v3,
corpus_analyst, corpus_analyst_schema, evidence_analyst, evidence_
analyst_schema, evidence_retrieval, or intelligence_analyst_pass2 --
no coupling to any of those modules' validation, retrieval, or call
logic (the orchestrator is the only place those are wired together).
Makes no database connection and performs no persistence of any kind;
this module's only side effect is the one outbound Anthropic Messages
API request made by call_anthropic_editor (no web_search tool, no other
tool, no tool use at all).
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests

import intelligence_editor_schema as schema

log = logging.getLogger("regulus.intelligence_editor")

SCHEMA_VERSION = "1.0"
PROMPT_VERSION = "1.0"

# Same model family already used throughout Regulus -- no new model
# introduced.
EDITOR_MODEL = "claude-sonnet-4-6"

# Sizing rationale -- first-principles estimate, NOT yet live-measured.
# The Editor's output is a single, bounded Brief document (8 fixed
# sections) over however many stories survive to be eligible -- for a
# single reporting-period run of Regulus's current scale (a handful of
# candidate stories per corpus), this is comparable in scope to the
# Corpus Analyst's own per-corpus output, so the same starting ceiling is
# adopted, subject to the same kind of live-measurement revision as every
# other role if Brief #001 acceptance shows truncation.
EDITOR_MAX_TOKENS = 20000

# No web_search tool is used here (same as Pass #2 -- the Editor does no
# research of its own) -- mirrors PASS2_TIMEOUT_SECONDS's reasoning
# rather than Stage 2/Evidence Analyst's tool-using budget.
EDITOR_TIMEOUT_SECONDS = 300

# Mirrors the existing *_MAX_ATTEMPTS pattern everywhere else in Regulus
# -- one retry on call/parse/transport failure, no autonomous loop.
EDITOR_MAX_ATTEMPTS = 2


EDITOR_SYSTEM_PROMPT = """You are the Intelligence Editor, the final step before a human reads
REGULUS INTELLIGENCE BRIEF #001. You receive ONLY already-validated
Intelligence Analyst Pass #2 reassessments -- never raw corpus
observations, never a first-pass hypothesis that hasn't been evidence-
tested. You have NO web_search tool and NO other tool. You add NO new
facts and do NO independent research; you ORGANIZE, PRIORITIZE,
COMPRESS, and EXPLAIN the validated intelligence you are given.

WHAT YOU ARE GIVEN: "stories", a list of already-reassessed candidate
stories for this reporting period. Each entry has a story_id, a title and
candidate_materiality (from the original first-pass story, for context
only), and "pass2_output" -- the full, validated Pass #2 reassessment
(assessment_disposition, revised_hypothesis, material_changes,
supported_findings, weakened_or_rejected_findings, remaining_
uncertainties, alternative_hypotheses_assessment, intelligence_
assessment, confidence, editor_eligibility, editor_caveats). Every
evidence_id referenced inside a story's pass2_output.material_changes/
alternative_hypotheses_assessment came from that SAME story's own
Evidence Analyst package -- you may cite those same evidence_ids in your
Brief for that story, but never another story's evidence_ids, and never
an evidence_id you invent.

YOUR JOB: produce a single, cohesive Brief organized into exactly these
eight sections, in this order:
  1. Executive Assessment -- a short narrative synthesis across all
     included stories: what matters most this period and why.
  2. Regulatory Tempo -- the pace/volume/character of regulatory activity
     this period, as actually supported by the included stories (do not
     convert publication volume into policy intensity on your own
     authority -- if a story's own pass2_output already made that
     distinction, reflect it; do not re-introduce a volume=intensity
     conflation Pass #2 corrected).
  3. Targeting & Policy Direction -- which countries/entities/sectors/
     technologies are being targeted or affected, and in which direction
     (tightening, relaxing, mixed).
  4. Key Developments -- the substantive individual stories themselves,
     each traceable to its own pass2_output.
  5. Cross-Agency Signals -- patterns spanning multiple agencies, ONLY
     where the underlying pass2_output(s) actually support it (do not
     assert interagency coordination a story's own reassessment left
     unresolved or explicitly could not confirm).
  6. Emerging Patterns -- trends visible across stories, if any.
  7. Watchlist -- stories/questions worth tracking into the next
     reporting period (remaining_uncertainties, insufficient_evidence
     stories, or weak-but-not-yet-disproven threads).
  8. Methodology / Sources -- which stories were included, which were
     excluded (and why), and which evidence_ids were drawn on overall.

YOU MAY EXCLUDE A STORY FROM THE BRIEF ENTIRELY. Do not force every
candidate story in -- weak, administrative, duplicative, or
insufficiently supported stories may be left out of sections 1-7 (but
must still be accounted for in stories_excluded / Methodology-Sources,
with a reason). A story with assessment_disposition="insufficient_
evidence" is not automatically excluded -- it may belong in the
Watchlist instead of Key Developments.

ABSOLUTE RULE -- NO RESURRECTION: if a story's own pass2_output marked a
claim's disposition as "removed" (in material_changes), or assessed an
alternative_hypothesis as "contradicted" or "weakened", you must NEVER
restate that removed/contradicted/weakened claim in your Brief as though
it still held. You may mention that something was investigated and
rejected (that is accurate reporting of the reassessment), but you may
not present the rejected claim itself as a current finding.

TRACEABILITY: every item inside sections 2, 3, 4, 5, 6, and 7 must cite
the story_id(s) it is about, and may cite only evidence_ids that belong
to one of those same story_ids' own evidence package. Do not copy source
URLs into your output -- traceability resolves entirely through
story_ids and evidence_ids.

OUTPUT: return ONLY valid JSON, no prose, no markdown fences, matching
EXACTLY this shape (empty "items" arrays are fine where a section
genuinely has nothing to report for this period; omitting a required key
is not):

{
  "brief_id": "",
  "reporting_period": {"start": "", "end": ""},
  "executive_assessment": "",
  "regulatory_tempo": {"summary": "", "items": [{"text": "", "story_ids": [], "evidence_ids": []}]},
  "targeting_and_policy_direction": {"summary": "", "items": [{"text": "", "story_ids": [], "evidence_ids": []}]},
  "key_developments": {"summary": "", "items": [{"text": "", "story_ids": [], "evidence_ids": []}]},
  "cross_agency_signals": {"summary": "", "items": [{"text": "", "story_ids": [], "evidence_ids": []}]},
  "emerging_patterns": {"summary": "", "items": [{"text": "", "story_ids": [], "evidence_ids": []}]},
  "watchlist": {"summary": "", "items": [{"text": "", "story_ids": [], "evidence_ids": []}]},
  "methodology_and_sources": {
    "summary": "",
    "stories_included": [],
    "stories_excluded": [{"story_id": "", "reason": ""}],
    "evidence_ids_referenced": []
  },
  "confidence": "high|medium|low",
  "stories_included": [],
  "stories_excluded": [{"story_id": "", "reason": ""}]
}

"stories_included"/"stories_excluded" at the top level must be consistent
with methodology_and_sources' own fields. Every story_id you reference
anywhere must be one of the story_ids you were actually given; never
invent one.
"""


def _extract_json_text(content_blocks):
    """Pull the final JSON text out of a Messages API response's content
    blocks. Mirrors every other Regulus LLM-call module's _extract_
    json_text exactly, reimplemented locally so this module has no
    dependency on any of them."""
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    text = text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return text


class EditorJSONDecodeError(ValueError):
    """Raised by call_anthropic_editor when the extracted text is not
    valid JSON. Mirrors every other Regulus *JSONDecodeError class
    exactly -- DIAGNOSTIC-ONLY, str(this) is IDENTICAL to str() of the
    underlying json.JSONDecodeError. Response-side only; never carries
    the API key or any request header."""

    def __init__(self, json_error: json.JSONDecodeError, *, raw_text: str,
                 content_blocks: list, response_meta: dict):
        super().__init__(str(json_error))
        self.raw_text = raw_text
        self.content_blocks = content_blocks
        self.response_meta = response_meta


def _build_editor_user_payload(run_id: str, reporting_period: dict, editor_inputs: list) -> dict:
    """Build the user-turn payload for the live call: run_id, the
    reporting_period (supplied deterministically by the caller, never
    left for the model to invent), and, per story, only story_id/title/
    candidate_materiality (context) plus the full validated pass2_output
    -- never the original full candidate_story, never the Evidence
    Analyst's retrieved document text. Traceability resolves entirely
    through the evidence_ids already embedded in pass2_output."""
    stories_payload = []
    for entry in editor_inputs:
        original_story = entry.get("original_story") or {}
        stories_payload.append({
            "story_id": entry["story_id"],
            "title": original_story.get("title"),
            "candidate_materiality": original_story.get("candidate_materiality"),
            "pass2_output": entry["pass2_output"],
        })
    return {
        "run_id": run_id,
        "reporting_period": reporting_period,
        "stories": stories_payload,
    }


def call_anthropic_editor(run_id: str, reporting_period: dict, editor_inputs: list, api_key: str):
    """Real Intelligence Editor call: Anthropic Messages API. NO tools --
    this role gets no web_search and no other tool; it sees only the
    supplied, already-validated Pass #2 outputs.

    Returns (parsed_json, raw_text, response_meta) on success, mirroring
    every other Regulus live-call module's return shape exactly. Raises
    EditorJSONDecodeError (carrying raw_text/content_blocks/response_meta)
    on a JSON parse failure; any other exception propagates as-is. Never
    persists or logs the API key.
    """
    payload = _build_editor_user_payload(run_id, reporting_period, editor_inputs)

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": EDITOR_MODEL,
            "max_tokens": EDITOR_MAX_TOKENS,
            "system": EDITOR_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": json.dumps(payload, indent=2)}],
            # Deliberately NO "tools" key -- this role has no web_search
            # and no other tool, per spec.
        },
        timeout=EDITOR_TIMEOUT_SECONDS,
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
        raise EditorJSONDecodeError(
            e, raw_text=text, content_blocks=content, response_meta=response_meta
        ) from e

    return parsed, text, response_meta


@dataclass
class EditorAttemptDiagnostics:
    """Diagnostic record for ONE attempt inside run_intelligence_editor's
    retry loop. Mirrors every other Regulus *AttemptDiagnostics class
    field-for-field. Captured for every attempt, success or failure.

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
class EditorOutcome:
    """Result of run_intelligence_editor(). Mirrors every other Regulus
    *Outcome dataclass's shape (raw / validation_status / validation_
    errors / failure_reason / is_valid / attempts)."""
    raw: Optional[dict] = None
    validation_status: str = "invalid"   # "valid" | "invalid"
    validation_errors: list = field(default_factory=list)
    failure_reason: Optional[str] = None  # set on call/parse failure only
    attempts: list = field(default_factory=list)  # list[EditorAttemptDiagnostics]

    @property
    def is_valid(self) -> bool:
        return self.validation_status == "valid"


def _run_single_attempt(attempt_number: int, run_id: str, reporting_period: dict,
                         editor_inputs: list, api_key: Optional[str], caller: Callable):
    """Run exactly one caller(run_id, reporting_period, editor_inputs,
    api_key) attempt and return (raw_or_None, EditorAttemptDiagnostics,
    error_or_None). Mirrors every other Regulus _run_single_attempt
    exactly."""
    started = datetime.now(timezone.utc)
    diag = EditorAttemptDiagnostics(
        attempt_number=attempt_number,
        model=EDITOR_MODEL,
        max_tokens=EDITOR_MAX_TOKENS,
        timeout_seconds=EDITOR_TIMEOUT_SECONDS,
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
        result = caller(run_id, reporting_period, editor_inputs, api_key)
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
        raw, raw_text, response_meta = result, None, None

    diag.raw_text = raw_text
    diag.raw_text_length = len(raw_text) if raw_text is not None else None
    _apply_response_meta(response_meta)

    return raw, diag, None


def run_intelligence_editor(run_id: str, reporting_period: dict, editor_inputs: list, *,
                             api_key: Optional[str] = None,
                             call_analyst: Optional[Callable[[str, dict, list, str], Any]] = None,
                             ) -> EditorOutcome:
    """Run the Intelligence Editor over the supplied, already-validated
    Pass #2 editor_inputs and deterministically validate the resulting
    Brief.

    editor_inputs: list of {"story_id": str, "original_story": dict (the
    Pass #1 candidate_story, for title/materiality context only),
    "pass2_output": dict (the validated Pass #2 reassessment),
    "evidence_ids": iterable[str] (that story's Evidence Analyst
    package's evidence_ids)}. Every entry here is assumed to already be
    validated upstream (is_valid Pass #2 output, editor_eligibility !=
    "not_eligible") -- this function does not re-derive eligibility, it
    only validates the Editor's OWN output against exactly these inputs.

    This function:
      - makes NO web_search/tool call, and does not import dd_pipeline,
        dd_schema, regulus_v3, corpus_analyst, corpus_analyst_schema,
        evidence_analyst, evidence_analyst_schema, evidence_retrieval, or
        intelligence_analyst_pass2;
      - retries up to EDITOR_MAX_ATTEMPTS on call/parse/transport failure
        only (never on a structurally-valid-but-schema-invalid model
        output);
      - validates with intelligence_editor_schema.validate_brief against
        exactly these editor_inputs, so an invented story_id/evidence_id
        or a resurrected rejected claim is always caught;
      - records one EditorAttemptDiagnostics per attempt made onto the
        returned outcome's `attempts` list.

    `call_analyst`, when supplied (e.g. in tests), is called as
    call_analyst(run_id, reporting_period, editor_inputs, api_key) and
    must return either a bare dict or a (parsed, raw_text, response_meta)
    tuple. Defaults to the real call_anthropic_editor.

    Never raises on a model/network/parse failure -- caught, logged, and
    reported via the returned EditorOutcome.failure_reason.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    caller = call_analyst or call_anthropic_editor

    attempts = []
    raw = None
    last_error = None
    last_validation_result = None
    for attempt_number in range(1, EDITOR_MAX_ATTEMPTS + 1):
        attempt_raw, diag, err = _run_single_attempt(
            attempt_number, run_id, reporting_period, editor_inputs, api_key, caller,
        )
        if err is None:
            result = schema.validate_brief(attempt_raw, editor_inputs=editor_inputs)
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
            log.warning("Intelligence Editor attempt %d/%d failed for run_id=%s: %s",
                        attempt_number, EDITOR_MAX_ATTEMPTS, run_id, err)

    if last_error is not None:
        return EditorOutcome(
            raw=None, validation_status="invalid",
            validation_errors=[f"editor_call_failed: {last_error}"],
            failure_reason="editor_call_failed",
            attempts=attempts,
        )

    return EditorOutcome(
        raw=raw,
        validation_status="valid" if last_validation_result.is_valid else "invalid",
        validation_errors=last_validation_result.validation_errors,
        attempts=attempts,
    )
