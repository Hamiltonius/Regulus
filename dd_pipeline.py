#!/usr/bin/env python3
"""
dd_pipeline.py — Gate, Stage 2 research, Stage 3 synthesis, and
due_diligence_records persistence for the DD pipeline defined in
docs/REGULUS_DD_SPEC_v1.0.md (frozen, merged into main, including
clarifications C1/C2).

This module owns everything dd_schema.py deliberately excludes: the LLM
calls, the database writes, and the orchestration that ties the gate,
Stage 2, the validator, Stage 3, and persistence together in the order
the spec mandates:

    Stage 1 -> Gate -> [NO: existing Regulus behavior, untouched]
                     -> [YES: Stage 2 -> Schema Validator
                              (INVALID: no Stage 3, never proceeds)
                              (VALID: DD record) -> Stage 3 -> Output Validator
                              -> Stage 3 source-reuse check]

Every LLM-calling function here takes an injectable `call_*` callable
(defaulting to the real Anthropic Messages API call). This is not a
generic abstraction for its own sake — it's what makes the orchestration,
retry, and failure-handling logic testable deterministically without
spending real API calls or depending on model non-determinism, while the
default path is the real integration used in production.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests

import dd_schema as schema

log = logging.getLogger("regulus.dd")

SCHEMA_VERSION = "1.0"
PROMPT_VERSION = "1.0"
STAGE2_MODEL = "claude-sonnet-4-6"
STAGE3_MODEL = "claude-sonnet-4-6"

# 4000 -> 8000 -> 20000. Two successive live Syria acceptance runs both
# hit stop_reason="max_tokens": first at 4000 (single ~15K-char final text
# block cut off mid-string), then at 8000 -- one attempt truncated before
# the JSON even started (narration + a ```json fence at the very end of
# budget), the other reached ~29.5K characters and was still inside the
# sources array when it ran out. Server-side web_search activity (each
# search round's narration + query) also consumes this same output-token
# budget before the model starts writing the evidence package, on top of
# the package's own size. 20000 gives real headroom for both, well under
# claude-sonnet-4-6's 128K synchronous output ceiling. Prompt, schema,
# model, web_search config, extraction/parsing, retry, validators, Gate,
# and Stage 3 are all untouched -- this is the only thing that changed.
STAGE2_MAX_TOKENS = 20000

# Raised 180 -> 600 alongside the max_tokens increase: a 20000-token
# completion with 5-6 web_search rounds can legitimately take several
# minutes end to end, and 180s left no margin. Read-timeout only --
# does not change retry count, backoff, or any other pipeline behavior.
STAGE2_TIMEOUT_SECONDS = 600

# Sentinel research_status used ONLY for pipeline-level failures (the LLM
# call itself failed, or its output could not be parsed as JSON at all) —
# distinct from the model's own semantic enum (complete|partial|
# insufficient_data), which describes research completeness, not pipeline
# health. Never returned by validate_stage2_record's echo logic; only used
# on the failure path in run_due_diligence below.
RESEARCH_STATUS_ERROR = "error"

STAGE2_MAX_ATTEMPTS = 2
STAGE3_MAX_ATTEMPTS = 2


# ---------------------------------------------------------------------------
# Gate (deterministic, no LLM call) — spec section "Gate"
# ---------------------------------------------------------------------------

HIGH_CONTEXT_JURISDICTIONS = {"syria", "iran", "cuba", "north korea", "venezuela", "russia"}
HIGH_IMPACT_ACTIONS = {
    "entity_list_addition", "entity_list_removal", "license_policy_change",
    "country_group_change", "ccl_amendment", "sanctions_waiver",
}


def needs_due_diligence(analysis: dict, doc: dict) -> bool:
    """Deterministic escalation gate. No LLM call, no network call.

    Takes both the Stage 1 analysis AND the original document
    metadata/content, so a Stage 1 omission (e.g. the LLM fails to tag a
    jurisdiction or change_type that's plainly present in the source)
    cannot by itself prevent an otherwise-significant document from
    escalating.
    """
    analysis = analysis or {}
    doc = doc or {}

    if analysis.get("confidence") != "High":
        return True
    if analysis.get("change_type") in HIGH_IMPACT_ACTIONS:
        return True
    countries = [str(c).lower() for c in (analysis.get("countries") or [])]
    if any(j in countries for j in HIGH_CONTEXT_JURISDICTIONS):
        return True
    if analysis.get("unresolved_questions"):
        return True

    raw_text = " ".join(filter(None, [
        doc.get("title", "") or "", doc.get("abstract", "") or "",
    ])).lower()
    if any(j in raw_text for j in HIGH_CONTEXT_JURISDICTIONS):
        return True

    return False


# ---------------------------------------------------------------------------
# Stage 2 — due-diligence research (source hierarchy is non-negotiable)
# ---------------------------------------------------------------------------

STAGE2_SYSTEM_PROMPT = """You are a due-diligence research analyst for a U.S. export-controls and
sanctions compliance organization. You are given a Stage 1 analysis of a
Federal Register document. Research the historical/regulatory context
needed to assess its significance and produce a structured evidence
package.

SOURCE PRIORITY (non-negotiable):
1. Federal Register / GovInfo / eCFR
2. BIS / Treasury / OFAC / State / DDTC
3. White House / other relevant U.S. government agencies
4. Congressional/statutory sources
5. Reputable secondary analysis only when primary sources don't answer
   the historical question.

Distinguish primary-source fact, agency characterization, secondary
interpretation, and model inference. Never use a secondary source to
contradict an available primary regulatory instrument without explicitly
identifying the discrepancy. If no meaningful precedent can be
established, return research_status: "insufficient_data" — do not
manufacture one.

Do not infer any fact not directly supported by a source you actually
found. Every list field must contain plain strings, never objects.

Return ONLY valid JSON, no prose, no markdown fences, matching exactly
this shape:

{
  "research_question": "",
  "current_event": {
    "action": "", "date": "", "effective_date": "",
    "agency": [], "authority": [], "jurisdictions": [],
    "entities": [], "controls_affected": []
  },
  "historical_context": {
    "program_origin": "",
    "major_prior_actions": [],
    "most_relevant_precedent": {
      "date": null, "description": "", "entities_involved": [],
      "authority": [], "mechanism": ""
    }
  },
  "precedent_comparison": {
    "similarities": [], "differences": [],
    "trend_classification": "consistent|escalation|relaxation|reversal|novel|insufficient_data"
  },
  "legal_regulatory_effect": {
    "changed": [], "unchanged": [], "superseded": [],
    "remaining_restrictions": [], "effective_date": ""
  },
  "scope": {
    "affected_countries": [], "affected_entities": [],
    "affected_item_categories": [], "affected_transaction_types": [],
    "affected_compliance_workflows": []
  },
  "impact_assessment": {
    "immediate": [], "operational": [], "licensing": [],
    "screening": [], "classification": [], "authorization_management": []
  },
  "follow_on_indicators": {
    "historically_observed_next_steps": [],
    "current_unresolved_actions": [],
    "items_to_monitor": []
  },
  "open_questions": [],
  "sources": [
    {"url": "", "source_type": "", "agency": "", "date": "",
     "supports": [], "primary_source": true}
  ],
  "research_status": "complete|partial|insufficient_data",
  "due_diligence_confidence": "High|Medium|Low"
}

Do NOT include "validation_status" or "validation_errors" — those are
never part of your output; they are assigned afterward by deterministic
application code.
"""


def _extract_json_text(content_blocks):
    """Pull the final JSON text out of a Messages API response's content
    blocks, ignoring any server_tool_use / web_search_tool_result blocks
    (those are the model's search activity, not its answer)."""
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    text = text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return text


class Stage2JSONDecodeError(ValueError):
    """Raised by call_anthropic_stage2 when the text _extract_json_text
    produced is not valid JSON. This is a DIAGNOSTIC-ONLY addition: it
    changes nothing about parsing, repair, retry, or validation — it
    exists solely so a caller that wants to preserve the failure for
    inspection (e.g. the isolated acceptance-test runner) can do so,
    without call_anthropic_stage2 itself performing any disk I/O, repair,
    regex cleanup, or fallback parsing.

    str(this) is IDENTICAL to str() of the underlying json.JSONDecodeError
    — every existing `except Exception as e: ...str(e)...` caller (the
    retry loop in run_due_diligence, the acceptance runner) sees the exact
    same message it saw before this class existed. Only callers that
    explicitly look for these new attributes see anything different.

    Attributes (all non-secret — no request headers, no API key, nothing
    from the request side is captured here, only response-side data):
      raw_text        the exact string that was passed to json.loads()
      content_blocks  the full, unmodified content-block array from the
                       Anthropic response (preserves block order/types —
                       text vs server_tool_use vs web_search_tool_result —
                       so a reviewer can see exactly how the response was
                       structured around web_search activity)
      response_meta   dict of non-secret response metadata: stop_reason,
                       model, usage, content_block_count, content_block_types
    """

    def __init__(self, json_error: json.JSONDecodeError, *, raw_text: str,
                 content_blocks: list, response_meta: dict):
        super().__init__(str(json_error))
        self.raw_text = raw_text
        self.content_blocks = content_blocks
        self.response_meta = response_meta


def call_anthropic_stage2(doc: dict, analysis: dict, api_key: str) -> dict:
    """Real Stage 2 call: Anthropic Messages API with the server-side web
    search tool enabled, so the model can actually research primary
    sources rather than relying on training data alone. Raises on any
    HTTP/parse failure — callers are responsible for retry/failure
    handling (see run_due_diligence)."""
    user_content = json.dumps({
        "stage1_analysis": analysis,
        "source_document": {
            "title": doc.get("title"),
            "agencies": doc.get("agencies"),
            "type": doc.get("type"),
            "publication_date": doc.get("publication_date"),
            "effective_on": doc.get("effective_on"),
            "citation": doc.get("citation"),
            "html_url": doc.get("html_url"),
            "abstract": doc.get("abstract"),
        },
    }, indent=2)

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": STAGE2_MODEL,
            "max_tokens": STAGE2_MAX_TOKENS,
            "system": STAGE2_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_content}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
        },
        timeout=STAGE2_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    response_json = resp.json()
    content = response_json["content"]
    text = _extract_json_text(content)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Diagnostic-only: preserve exactly what failed to parse and how
        # the response was shaped, without repairing, re-parsing, or
        # falling back to anything. Re-raised, never swallowed — the
        # retry/failure handling in run_due_diligence is unchanged.
        response_meta = {
            "stop_reason": response_json.get("stop_reason"),
            "model": response_json.get("model"),
            "usage": response_json.get("usage"),
            "content_block_count": len(content) if isinstance(content, list) else None,
            "content_block_types": (
                [b.get("type") for b in content] if isinstance(content, list) else None
            ),
        }
        raise Stage2JSONDecodeError(
            e, raw_text=text, content_blocks=content, response_meta=response_meta
        ) from e


def due_diligence_review(doc: dict, analysis: dict, *, api_key: Optional[str] = None,
                          call_stage2: Optional[Callable[[dict, dict, str], dict]] = None) -> dict:
    """Run Stage 2. Thin wrapper so run_due_diligence's retry logic has one
    call site regardless of whether it's hitting the real API or a test
    stub."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    caller = call_stage2 or call_anthropic_stage2
    return caller(doc, analysis, api_key)


# ---------------------------------------------------------------------------
# Stage 3 — value-only synthesis (reuse-only sources, per spec C2)
# ---------------------------------------------------------------------------

STAGE3_SYSTEM_PROMPT = """You are the final intelligence editor. Your inputs have already been
researched and validated. Do not perform additional research or
introduce facts not contained in the supplied evidence. Remove
procedural detail, duplicated information, generic regulatory
background, and low-value observations. Preserve material caveats and
uncertainty. Produce only information that changes the reader's
understanding of the event, its significance, its precedent, its
compliance consequences, or what requires monitoring. Never convert
"partial" or "insufficient_data" research into apparent certainty. Never
predict agency behavior beyond what precedent explicitly supports.

SOURCES: your "sources" field may ONLY contain source objects copied
VERBATIM from the supplied Stage 2 evidence's own "sources" list — same
url, source_type, agency, date, supports, and primary_source values,
exactly as given. You may not search for, invent, add, or modify any
source. If a claim isn't backed by one of the supplied sources, omit the
claim rather than fabricate support for it.

Return ONLY valid JSON, no prose, no markdown fences, matching exactly
this shape:

{
  "headline": "", "bottom_line": "", "what_changed": "",
  "why_it_matters": "", "historical_significance": "",
  "what_did_not_change": "", "compliance_attention": [],
  "watch_next": [],
  "confidence": "High|Medium|Low",
  "sources": [
    {"url": "", "source_type": "", "agency": "", "date": "",
     "supports": [], "primary_source": true}
  ]
}
"""


def call_anthropic_stage3(analysis: dict, dd_record: dict, api_key: str) -> dict:
    """Real Stage 3 call. No tools — Stage 3 may not search (spec rule 7)."""
    user_content = json.dumps({
        "stage1_analysis": analysis,
        "validated_dd_evidence": dd_record,
    }, indent=2)

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": STAGE3_MODEL,
            "max_tokens": 1500,
            "system": STAGE3_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_content}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["content"]
    text = _extract_json_text(content)
    return json.loads(text)


def synthesize_final(analysis: dict, dd_record: dict, *, api_key: Optional[str] = None,
                      call_stage3: Optional[Callable[[dict, dict, str], dict]] = None) -> dict:
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    caller = call_stage3 or call_anthropic_stage3
    return caller(analysis, dd_record, api_key)


# ---------------------------------------------------------------------------
# Persistence — due_diligence_records (additive schema, versioned)
# ---------------------------------------------------------------------------

def ensure_dd_schema(conn) -> None:
    """Additive-only migration for the DD pipeline. Uses the same
    `_ensure_column`-equivalent approach as regulus_v3.get_db(): never
    drops, renames, or alters an existing column, never touches the
    `alerts` primary/foreign key shape, only adds what's missing.

    Called from regulus_v3.get_db() — see integration point there. Safe
    to call repeatedly (idempotent), exactly like _ensure_column.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS due_diligence_records (
            dd_id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_number TEXT NOT NULL,
            doc_hash TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            model TEXT,
            generated_at TEXT NOT NULL,
            dd_json TEXT NOT NULL,
            validation_status TEXT NOT NULL,
            validation_errors TEXT,
            research_status TEXT NOT NULL,
            confidence TEXT NOT NULL,
            FOREIGN KEY (doc_hash) REFERENCES alerts(doc_hash)
        )
    """)

    # Additive to due_diligence_records itself, NOT to alerts: the
    # validated Stage 3 output (when Stage 3 ran and passed both its
    # structural validator and the C2 source-reuse check), keyed to the
    # exact dd_id/Stage 2 evidence it was synthesized from. This is what
    # lets the unsent-alert retry path in regulus_v3.main() recover a
    # DD-escalated alert's original validated sources without re-running
    # Stage 2/Stage 3, without web research, and without a new
    # due_diligence_records row — see get_latest_valid_stage3() below.
    dd_cols = {row[1] for row in conn.execute("PRAGMA table_info(due_diligence_records)")}
    if "final_json" not in dd_cols:
        conn.execute("ALTER TABLE due_diligence_records ADD COLUMN final_json TEXT")
        log.info("Migrated schema: added column due_diligence_records.final_json")

    alerts_cols = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
    additions = {
        "due_diligence_ran": "INTEGER DEFAULT 0",
        "headline": "TEXT",
        "bottom_line": "TEXT",
        "why_it_matters": "TEXT",
        "historical_significance": "TEXT",
        "what_did_not_change": "TEXT",
        "compliance_attention": "TEXT",
        "watch_next": "TEXT",
        "final_confidence": "TEXT",
    }
    for col, coltype in additions.items():
        if col not in alerts_cols:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} {coltype}")
            log.info("Migrated schema: added column alerts.%s", col)
    conn.commit()


def persist_due_diligence(conn, *, document_number, doc_hash, dd_json_raw,
                           validation_status, validation_errors, research_status,
                           confidence, model, schema_version=SCHEMA_VERSION,
                           prompt_version=PROMPT_VERSION, generated_at=None) -> int:
    """Insert one due_diligence_records row. Always inserts a NEW row
    (dd_id autoincrement, never overwrites) — see spec's reasoning for why
    document_number is not the primary key: re-running DD after a
    prompt/model change must not destroy the prior audit trail."""
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO due_diligence_records
            (document_number, doc_hash, schema_version, prompt_version, model,
             generated_at, dd_json, validation_status, validation_errors,
             research_status, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_number, doc_hash, schema_version, prompt_version, model,
            generated_at, json.dumps(dd_json_raw), validation_status,
            json.dumps(validation_errors) if validation_errors else None,
            research_status, confidence,
        ),
    )
    conn.commit()
    return cur.lastrowid


def persist_stage3_result(conn, dd_id: int, stage3_json: dict) -> None:
    """UPDATE (never INSERT) the existing due_diligence_records row for
    this dd_id with its validated Stage 3 output. Called exactly once, from
    inside run_due_diligence, only after Stage 3 has passed both its
    structural validator and the C2 source-reuse check — never from the
    retry path, which only reads this column back."""
    conn.execute(
        "UPDATE due_diligence_records SET final_json = ? WHERE dd_id = ?",
        (json.dumps(stage3_json), dd_id),
    )
    conn.commit()


def get_latest_valid_stage3(conn, doc_hash: str) -> Optional[dict]:
    """Recover the most recent validated Stage 3 output for a document,
    for the unsent-alert retry path in regulus_v3.main(). Read-only: never
    calls Stage 2/Stage 3, never performs web research, never writes a new
    due_diligence_records row. Returns None if no valid record with a
    persisted Stage 3 result exists — callers must fail safely to existing
    Stage 1 retry behavior in that case, never invent or reconstruct
    evidence.

    "Latest" is by dd_id (insertion order), matching the spec's own
    reasoning for why dd_id is autoincrement rather than document_number
    being the primary key: DD may be re-run, and the newest validated run
    is the one that should govern.
    """
    row = conn.execute(
        "SELECT final_json FROM due_diligence_records "
        "WHERE doc_hash = ? AND validation_status = 'valid' AND final_json IS NOT NULL "
        "ORDER BY dd_id DESC LIMIT 1",
        (doc_hash,),
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        # Corrupt stored JSON is a data problem, not a reason to invent a
        # substitute — fail safe, same as "no record found".
        log.error("Corrupt final_json in due_diligence_records for doc_hash=%s", doc_hash)
        return None


# ---------------------------------------------------------------------------
# Orchestration — the one place that enforces pipeline order end to end
# ---------------------------------------------------------------------------

@dataclass
class DDOutcome:
    """Result of running the full DD pipeline for one escalated document."""
    dd_id: Optional[int]
    stage2_raw: Optional[dict]
    stage2_validation_status: str          # "valid" | "invalid"
    stage2_validation_errors: list = field(default_factory=list)
    research_status: Optional[str] = None
    due_diligence_confidence: Optional[str] = None
    stage3: Optional[dict] = None          # final synthesized brief, or None
    stage3_valid: bool = False
    stage3_validation_errors: list = field(default_factory=list)
    failure_reason: Optional[str] = None   # set on pipeline-level (non-model) failure


def run_due_diligence(doc: dict, analysis: dict, conn, doc_hash: str, document_number: str,
                       *, api_key: Optional[str] = None,
                       call_stage2: Optional[Callable] = None,
                       call_stage3: Optional[Callable] = None) -> DDOutcome:
    """Enforces the full frozen pipeline order for one escalated document:

        Stage 2 -> Schema Validator
            INVALID -> never proceeds to Stage 3 (record persisted anyway,
                       for auditability of the failure)
            VALID   -> Stage 3 -> Output Validator -> source-reuse check
                       (either failing means no Stage 3 output is usable)

    Never raises on a Stage 2/Stage 3 model or network failure — those are
    caught, logged, and reported in the returned DDOutcome.failure_reason
    so main() can fall back to existing Stage-1-only behavior rather than
    crashing the whole ingestion run over one document's DD failure.
    Retries each stage once (STAGE2_MAX_ATTEMPTS / STAGE3_MAX_ATTEMPTS)
    before giving up.
    """
    stage2_raw = None
    last_error = None
    for attempt in range(1, STAGE2_MAX_ATTEMPTS + 1):
        try:
            stage2_raw = due_diligence_review(doc, analysis, api_key=api_key, call_stage2=call_stage2)
            last_error = None
            break
        except Exception as e:  # network error, HTTP error, JSON parse error — all non-fatal to main()
            last_error = e
            log.warning("Stage 2 attempt %d/%d failed for %s: %s",
                        attempt, STAGE2_MAX_ATTEMPTS, document_number, e)

    if last_error is not None:
        # Pipeline-level failure — never disguised as confidence (rule 9).
        # Persist for auditability; research_status uses the RESEARCH_STATUS_ERROR
        # sentinel, distinct from the model's own completeness enum, since no
        # model ever ran to report completeness.
        dd_id = persist_due_diligence(
            conn, document_number=document_number, doc_hash=doc_hash,
            dd_json_raw={"error": f"stage2_call_failed: {last_error}"},
            validation_status="invalid",
            validation_errors=[f"stage2_call_failed: {last_error}"],
            research_status=RESEARCH_STATUS_ERROR, confidence="Low", model=STAGE2_MODEL,
        )
        return DDOutcome(
            dd_id=dd_id, stage2_raw=None, stage2_validation_status="invalid",
            stage2_validation_errors=[f"stage2_call_failed: {last_error}"],
            failure_reason="stage2_call_failed",
        )

    stage2_result = schema.validate_stage2_record(stage2_raw)
    dd_id = persist_due_diligence(
        conn, document_number=document_number, doc_hash=doc_hash, dd_json_raw=stage2_raw,
        validation_status=stage2_result.validation_status,
        validation_errors=stage2_result.validation_errors,
        research_status=stage2_result.research_status or RESEARCH_STATUS_ERROR,
        confidence=stage2_result.due_diligence_confidence or "Low",
        model=STAGE2_MODEL,
    )

    outcome = DDOutcome(
        dd_id=dd_id, stage2_raw=stage2_raw,
        stage2_validation_status=stage2_result.validation_status,
        stage2_validation_errors=stage2_result.validation_errors,
        research_status=stage2_result.research_status,
        due_diligence_confidence=stage2_result.due_diligence_confidence,
    )

    if not stage2_result.is_valid:
        # Malformed DD cannot silently proceed (spec, FAILURE HANDLING).
        # Stage 3 is never invoked.
        outcome.failure_reason = "stage2_invalid"
        return outcome

    # Stage 2 is structurally valid (independent of research_status — an
    # honestly-reported insufficient_data record is valid and proceeds).
    stage3_raw = None
    last_error = None
    for attempt in range(1, STAGE3_MAX_ATTEMPTS + 1):
        try:
            stage3_raw = synthesize_final(analysis, stage2_raw, api_key=api_key, call_stage3=call_stage3)
            last_error = None
            break
        except Exception as e:
            last_error = e
            log.warning("Stage 3 attempt %d/%d failed for %s: %s",
                        attempt, STAGE3_MAX_ATTEMPTS, document_number, e)

    if last_error is not None:
        outcome.failure_reason = "stage3_call_failed"
        outcome.stage3_validation_errors = [f"stage3_call_failed: {last_error}"]
        return outcome

    stage3_struct_result = schema.validate_stage3_record(stage3_raw)
    errors = list(stage3_struct_result.validation_errors)

    if stage3_struct_result.is_valid:
        # Structural validity established — now enforce C2 (reuse-only
        # sources) deterministically, never trusting the prompt alone.
        reuse_errors = schema.validate_stage3_sources(stage3_raw.get("sources", []), stage2_raw.get("sources", []))
        errors.extend(reuse_errors)

    outcome.stage3_validation_errors = errors
    if errors:
        outcome.failure_reason = "stage3_invalid"
        outcome.stage3 = None
        outcome.stage3_valid = False
    else:
        outcome.stage3 = stage3_raw
        outcome.stage3_valid = True
        # Persist the validated Stage 3 output onto its Stage 2 row (UPDATE,
        # not INSERT) so the unsent-alert retry path can recover it later
        # without re-running Stage 2/Stage 3 or performing web research.
        persist_stage3_result(conn, dd_id, stage3_raw)

    return outcome
