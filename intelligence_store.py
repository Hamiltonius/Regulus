#!/usr/bin/env python3
"""
intelligence_store.py — persistence for Regulus intelligence-product
state: per-story artifacts (Corpus Analyst Pass #1 / Evidence Analyst /
Intelligence Analyst Pass #2) and compiled Briefs, keyed by run_id.

ISOLATION FROM THE PRODUCTION DD DATABASE: regulus_v3.py's DD pipeline
uses DB_PATH = os.environ.get("DB_PATH", "bis_watcher.db") (see
regulus_v3.py line ~61). This module NEVER reads that environment
variable and NEVER connects to that file. It defines its own, completely
separate database file/env var:

    INTELLIGENCE_DB_PATH = os.environ.get("INTELLIGENCE_DB_PATH", "regulus_intelligence.db")

This module does not import regulus_v3, dd_pipeline, dd_schema,
corpus_analyst(_schema), evidence_analyst(_schema), evidence_retrieval,
intelligence_analyst_pass2(_schema), or intelligence_editor(_schema) --
it stores and returns plain JSON-able dicts handed to it by the
orchestrator; it has no opinion about their shape beyond what is needed
to key and reconstruct them.

Schema (two tables):
  story_artifacts  — one row per (run_id, story_id, stage); INSERT OR
                      REPLACE semantics, so re-saving the same
                      (run_id, story_id, stage) overwrites the prior
                      attempt's record for that stage (the latest
                      attempt is truth; prior diagnostics are not an
                      audit log this module is responsible for).
                      stage is one of: "corpus_analyst", "evidence_
                      analyst", "pass2". Carries an input_fingerprint
                      column (see compute_fingerprint() below) used by
                      find_reusable_story_artifact() to decide whether a
                      persisted artifact is compatible with the CURRENT
                      run's input before it is reused instead of calling
                      the model again.
  briefs           — one row per (run_id, brief_id); INSERT OR REPLACE
                      semantics as well.

Every payload is stored as a JSON TEXT column and round-tripped through
json.dumps/json.loads -- this module performs no interpretation of
payload contents beyond the explicit fields called out in
reconstruct_editor_inputs() below (story_id / evidence_ids / pass2_output
/ editor_eligibility / is_valid), all of which are read generically
(dict.get), never assumed present.

REUSE / RESUME DISCIPLINE (find_reusable_story_artifact): a persisted
artifact is only ever returned as reusable when BOTH (a) is_valid=True
(an invalid artifact is never reused -- there is nothing valid in it to
reuse) and (b) its stored input_fingerprint matches the fingerprint of
the CURRENT call's input exactly (a mismatch -- including no fingerprint
stored at all -- means "not proven compatible", never "assume
compatible"). An interrupted/incomplete attempt is never a concern here
by construction: save_story_artifact() is only ever called once a
stage's run_* function actually RETURNS (success or exhausted failure);
a process killed mid-attempt (e.g. Ctrl-C inside the live HTTP call)
never reaches save_story_artifact() at all, so there is no row for that
story/stage to mistakenly treat as reusable. Reuse is scoped to the
SAME run_id -- this module never reads a different run_id's artifacts
when asked about the current one, even if that other run's artifact
would otherwise be fingerprint-compatible (deliberately conservative:
cross-run reuse is not implemented here).

KNOWLEDGE LAYER ADDENDUM: this module also defines three additive tables
-- propositions, knowledge_links, research_telemetry -- populated only by
knowledge_projection.py's deterministic structural projection over
already-validated story_artifacts/briefs rows (never by a frozen analytical
component). `propositions` preserves analyst statements verbatim with their
role, exact upstream disposition, provenance and context; it carries NO
epistemic judgment of its own -- strength lives in an epistemic_scorer.py
scorecard that may be attached later (attach_scorecard). See the KNOWLEDGE
LAYER section near the bottom of this file.
"""

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

INTELLIGENCE_DB_PATH = os.environ.get("INTELLIGENCE_DB_PATH", "regulus_intelligence.db")

STAGE_VALUES = {"corpus_analyst", "evidence_analyst", "pass2"}

# --- Knowledge-layer vocabularies -----------------------------------------
#
# Small, code-maintained controlled vocabularies. ROLE_VALUES says what a
# stored statement IS in the upstream structure (which schema field it came
# from) -- never how strong it is. Epistemic strength comes only from an
# epistemic_scorer.py scorecard.

ROLE_VALUES = {
    "evidence_finding",       # evidence_analyst question_findings[].finding
    "contradiction",          # evidence_analyst contradictions[].description
    "disconfirming_evidence", # evidence_analyst disconfirming_evidence_found[]
    "uncertainty",            # evidence_analyst remaining_gaps[] / pass2 remaining_uncertainties[]
    "supported_finding",      # pass2 supported_findings[]
    "reviewed_finding",       # pass2 weakened_or_rejected_findings[] (polarity-neutral name)
    "original_claim",         # pass2 material_changes[].original_claim
    "revised_claim",          # pass2 material_changes[].revised_claim
    "alternative_hypothesis",  # pass2 alternative_hypotheses_assessment[].hypothesis
}

# Lifecycle only (not epistemic): active; historical = replaced by a successor
# (superseded_by set); contested = removed upstream, or modified with no valid
# successor -- preserved, never deleted.
STATUS_VALUES = {"active", "historical", "contested"}

# Structural/provenance predicates only -- see knowledge_projection.py.
# Deliberately excludes any semantic entity/authority/ECCN/country
# predicate: no upstream schema (corpus_analyst_schema.py,
# evidence_analyst_schema.py, intelligence_analyst_pass2_schema.py,
# intelligence_editor_schema.py) carries a dedicated field for those today.
PREDICATE_VALUES = {
    "cites_document",
    "evidence_for_story",
    "evidence_cites_document",
    "story_included_in_brief",
    "story_excluded_from_brief",
}

KNOWLEDGE_LINK_NODE_TYPES = {"story", "document", "evidence", "brief"}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS story_artifacts (
    run_id              TEXT NOT NULL,
    story_id            TEXT NOT NULL,
    stage               TEXT NOT NULL,
    payload_json        TEXT NOT NULL,
    is_valid            INTEGER NOT NULL,
    validation_errors_json TEXT NOT NULL,
    failure_reason      TEXT,
    input_fingerprint   TEXT,
    provenance_json     TEXT,
    saved_at            TEXT NOT NULL,
    PRIMARY KEY (run_id, story_id, stage)
);

CREATE TABLE IF NOT EXISTS briefs (
    run_id              TEXT NOT NULL,
    brief_id            TEXT NOT NULL,
    reporting_period_json TEXT NOT NULL,
    payload_json        TEXT,
    is_valid            INTEGER NOT NULL,
    validation_errors_json TEXT NOT NULL,
    failure_reason      TEXT,
    saved_at            TEXT NOT NULL,
    PRIMARY KEY (run_id, brief_id)
);

-- Knowledge layer (additive, read-only relative to the two tables above --
-- never written to by any frozen analytical component; populated only by
-- knowledge_projection.py's structural projection from already-validated
-- story_artifacts/briefs rows).

CREATE TABLE IF NOT EXISTS propositions (
    proposition_id      TEXT NOT NULL,
    source_run_id       TEXT NOT NULL,
    source_story_id     TEXT NOT NULL,
    source_stage        TEXT NOT NULL,
    source_field        TEXT NOT NULL,
    source_index        INTEGER NOT NULL,
    statement_role      TEXT NOT NULL,
    statement_text      TEXT NOT NULL,
    upstream_disposition TEXT,
    upstream_confidence TEXT,
    evidence_ids_json   TEXT NOT NULL,
    context_json        TEXT NOT NULL,
    status              TEXT NOT NULL,
    superseded_by       TEXT,
    scorecard_id        TEXT,
    epistemic_rating    TEXT,
    usage_class         TEXT,
    extraction_method   TEXT NOT NULL,
    source_saved_at     TEXT,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (proposition_id)
);

CREATE INDEX IF NOT EXISTS idx_propositions_source
    ON propositions(source_run_id, source_story_id);
CREATE INDEX IF NOT EXISTS idx_propositions_role
    ON propositions(statement_role);
CREATE INDEX IF NOT EXISTS idx_propositions_scorecard
    ON propositions(scorecard_id);

CREATE TABLE IF NOT EXISTS knowledge_links (
    link_id             TEXT NOT NULL,
    subject             TEXT NOT NULL,
    subject_type        TEXT NOT NULL,
    predicate           TEXT NOT NULL,
    object              TEXT NOT NULL,
    object_type         TEXT NOT NULL,
    source_run_id       TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (link_id)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_links_subject
    ON knowledge_links(subject_type, subject);
CREATE INDEX IF NOT EXISTS idx_knowledge_links_predicate
    ON knowledge_links(predicate);
CREATE INDEX IF NOT EXISTS idx_knowledge_links_object
    ON knowledge_links(object_type, object);

CREATE TABLE IF NOT EXISTS research_telemetry (
    telemetry_id        TEXT NOT NULL,
    run_id               TEXT NOT NULL,
    story_id             TEXT,
    stage                TEXT NOT NULL,
    attempt_number       INTEGER,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    duration_seconds       REAL,
    request_succeeded      INTEGER,
    cache_hit               INTEGER,
    avoided_call_count      INTEGER,
    escalation_reason       TEXT,
    recorded_at              TEXT NOT NULL,
    PRIMARY KEY (telemetry_id)
);

CREATE INDEX IF NOT EXISTS idx_research_telemetry_run
    ON research_telemetry(run_id, story_id, stage);
"""


def _ensure_input_fingerprint_column(conn: sqlite3.Connection) -> None:
    """Additive schema migration: a story_artifacts table created by a
    pre-fingerprint version of this module won't have the
    input_fingerprint column yet. CREATE TABLE IF NOT EXISTS (above)
    never adds a column to an existing table, so this checks for it
    explicitly and ALTERs it in -- a no-op on a fresh or already-
    migrated database. Never touches any other column or any row data."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(story_artifacts)").fetchall()}
    if "input_fingerprint" not in cols:
        conn.execute("ALTER TABLE story_artifacts ADD COLUMN input_fingerprint TEXT")
        conn.commit()


def _ensure_provenance_column(conn: sqlite3.Connection) -> None:
    """Additive schema migration, mirroring _ensure_input_fingerprint_
    column() exactly: a story_artifacts table created before this column
    existed won't have it yet. A no-op on a fresh or already-migrated
    database. Never touches any other column or any row data.

    provenance_json is optional, store-level-only metadata about HOW an
    artifact came to be persisted -- e.g. {"kind": "legacy_artifact_
    migration", ...} for one bootstrapped from a pre-fingerprinting
    artifact file via regulus_brief001_acceptance.py's --evidence-
    analysis-artifact (see that module's bootstrap_evidence_analysis_
    artifact() docstring). It is NEVER read by find_reusable_story_
    artifact() or any other reuse-eligibility decision -- reuse remains
    governed exclusively by is_valid + exact input_fingerprint equality,
    unchanged. This column exists purely so a legacy-migrated artifact's
    provenance is explicit and auditable after the fact, without being
    folded into payload_json (the substantive analytical content) or
    conflated with validation_errors_json/failure_reason (which mean
    something materially different: an actual validation problem, not a
    note about how compatibility was established). A normal, freshly-
    generated artifact (e.g. from a live orchestrator run) is saved with
    provenance=None, same as every artifact before this column existed."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(story_artifacts)").fetchall()}
    if "provenance_json" not in cols:
        conn.execute("ALTER TABLE story_artifacts ADD COLUMN provenance_json TEXT")
        conn.commit()


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open (and, if needed, initialize) the intelligence store at
    db_path, defaulting to INTELLIGENCE_DB_PATH -- NEVER regulus_v3.DB_PATH
    / bis_watcher.db. Caller owns closing the returned connection."""
    path = db_path or INTELLIGENCE_DB_PATH
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    _ensure_input_fingerprint_column(conn)
    _ensure_provenance_column(conn)
    return conn


def compute_fingerprint(*parts: Any) -> str:
    """Deterministic identity fingerprint over one or more JSON-able
    objects (e.g. a candidate_story, or a (candidate_story, evidence_
    package) pair) -- used to decide whether a persisted artifact is
    compatible with the CURRENT run's input before it is reused. Keys
    are sorted so field order never changes the fingerprint; this is a
    content fingerprint, not a cryptographic commitment -- it exists
    only to detect "the input changed," not to resist deliberate
    tampering."""
    canonical = json.dumps(list(parts), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StoryArtifactRecord:
    run_id: str
    story_id: str
    stage: str
    payload: Any
    is_valid: bool
    validation_errors: list = field(default_factory=list)
    failure_reason: Optional[str] = None
    input_fingerprint: Optional[str] = None
    provenance: Optional[dict] = None
    saved_at: Optional[str] = None


@dataclass
class BriefRecord:
    run_id: str
    brief_id: str
    reporting_period: dict
    payload: Optional[Any]
    is_valid: bool
    validation_errors: list = field(default_factory=list)
    failure_reason: Optional[str] = None
    saved_at: Optional[str] = None


def save_story_artifact(run_id: str, story_id: str, stage: str, payload: Any, *,
                         is_valid: bool, validation_errors: Optional[list] = None,
                         failure_reason: Optional[str] = None,
                         input_fingerprint: Optional[str] = None,
                         provenance: Optional[dict] = None,
                         db_path: Optional[str] = None,
                         conn: Optional[sqlite3.Connection] = None) -> None:
    """Persist (overwrite) one story's artifact for one pipeline stage.
    `payload` may be any JSON-serializable dict (a candidate_story, an
    evidence_package, or a Pass #2 reassessment) -- this module does not
    interpret its shape beyond what reconstruct_editor_inputs() needs.

    input_fingerprint, when supplied (see compute_fingerprint()), is
    stored alongside the artifact and is what find_reusable_story_
    artifact() later checks before treating this artifact as reusable --
    omit it (leave None) for an artifact that should never be considered
    for reuse (e.g. a failure record with nothing valid in it).

    provenance, when supplied, is an optional store-level-only metadata
    dict about HOW this artifact came to be persisted (e.g. imported from
    a pre-fingerprinting artifact file rather than produced by this run)
    -- see _ensure_provenance_column()'s docstring. It is never consulted
    by find_reusable_story_artifact() or any reuse decision, and it is
    kept entirely separate from `payload` (never merged into it): a
    normal, freshly-generated artifact from a live run omits it (None),
    exactly as before this parameter existed.
    """
    if stage not in STAGE_VALUES:
        raise ValueError(f"stage must be one of {sorted(STAGE_VALUES)}, got {stage!r}")

    owns_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO story_artifacts "
            "(run_id, story_id, stage, payload_json, is_valid, validation_errors_json, "
            "failure_reason, input_fingerprint, provenance_json, saved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, story_id, stage, json.dumps(payload), 1 if is_valid else 0,
                json.dumps(validation_errors or []), failure_reason, input_fingerprint,
                (json.dumps(provenance) if provenance is not None else None), _now_iso(),
            ),
        )
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def load_story_artifact(run_id: str, story_id: str, stage: str, *,
                         db_path: Optional[str] = None,
                         conn: Optional[sqlite3.Connection] = None) -> Optional[StoryArtifactRecord]:
    owns_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT run_id, story_id, stage, payload_json, is_valid, validation_errors_json, "
            "failure_reason, input_fingerprint, provenance_json, saved_at FROM story_artifacts "
            "WHERE run_id = ? AND story_id = ? AND stage = ?",
            (run_id, story_id, stage),
        ).fetchone()
    finally:
        if owns_conn:
            conn.close()
    if row is None:
        return None
    return StoryArtifactRecord(
        run_id=row[0], story_id=row[1], stage=row[2], payload=json.loads(row[3]),
        is_valid=bool(row[4]), validation_errors=json.loads(row[5]),
        failure_reason=row[6], input_fingerprint=row[7],
        provenance=(json.loads(row[8]) if row[8] is not None else None), saved_at=row[9],
    )


def find_reusable_story_artifact(run_id: str, story_id: str, stage: str, input_fingerprint: str, *,
                                  db_path: Optional[str] = None,
                                  conn: Optional[sqlite3.Connection] = None) -> Optional[StoryArtifactRecord]:
    """Return the persisted (run_id, story_id, stage) artifact ONLY IF it
    is reusable: is_valid=True AND its stored input_fingerprint matches
    `input_fingerprint` exactly. Returns None in every other case --
    no artifact at all, an invalid artifact (never reused, regardless of
    fingerprint), or a fingerprint mismatch (including a record with no
    stored fingerprint, e.g. one saved before this reuse mechanism
    existed) -- a mismatch means "not proven compatible," never "assume
    compatible." Never considers a different run_id (reuse is scoped to
    THIS run only -- see the module docstring)."""
    record = load_story_artifact(run_id, story_id, stage, db_path=db_path, conn=conn)
    if record is None:
        return None
    if not record.is_valid:
        return None
    if record.input_fingerprint is None or record.input_fingerprint != input_fingerprint:
        return None
    return record


def list_story_artifacts(run_id: str, *, stage: Optional[str] = None,
                          db_path: Optional[str] = None,
                          conn: Optional[sqlite3.Connection] = None) -> list:
    """All story artifacts for a run_id, optionally filtered to one stage.
    Ordered by story_id for deterministic reconstruction."""
    owns_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        if stage is not None:
            rows = conn.execute(
                "SELECT run_id, story_id, stage, payload_json, is_valid, validation_errors_json, "
                "failure_reason, input_fingerprint, provenance_json, saved_at FROM story_artifacts "
                "WHERE run_id = ? AND stage = ? ORDER BY story_id",
                (run_id, stage),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT run_id, story_id, stage, payload_json, is_valid, validation_errors_json, "
                "failure_reason, input_fingerprint, provenance_json, saved_at FROM story_artifacts "
                "WHERE run_id = ? ORDER BY story_id, stage",
                (run_id,),
            ).fetchall()
    finally:
        if owns_conn:
            conn.close()
    return [
        StoryArtifactRecord(
            run_id=r[0], story_id=r[1], stage=r[2], payload=json.loads(r[3]),
            is_valid=bool(r[4]), validation_errors=json.loads(r[5]),
            failure_reason=r[6], input_fingerprint=r[7],
            provenance=(json.loads(r[8]) if r[8] is not None else None), saved_at=r[9],
        )
        for r in rows
    ]


def save_brief(run_id: str, brief_id: str, reporting_period: dict, payload: Optional[Any], *,
               is_valid: bool, validation_errors: Optional[list] = None,
               failure_reason: Optional[str] = None,
               db_path: Optional[str] = None,
               conn: Optional[sqlite3.Connection] = None) -> None:
    owns_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO briefs "
            "(run_id, brief_id, reporting_period_json, payload_json, is_valid, "
            "validation_errors_json, failure_reason, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, brief_id, json.dumps(reporting_period),
                json.dumps(payload) if payload is not None else None,
                1 if is_valid else 0, json.dumps(validation_errors or []), failure_reason, _now_iso(),
            ),
        )
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def load_brief(run_id: str, brief_id: str, *,
                db_path: Optional[str] = None,
                conn: Optional[sqlite3.Connection] = None) -> Optional[BriefRecord]:
    owns_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT run_id, brief_id, reporting_period_json, payload_json, is_valid, "
            "validation_errors_json, failure_reason, saved_at FROM briefs "
            "WHERE run_id = ? AND brief_id = ?",
            (run_id, brief_id),
        ).fetchone()
    finally:
        if owns_conn:
            conn.close()
    if row is None:
        return None
    return BriefRecord(
        run_id=row[0], brief_id=row[1], reporting_period=json.loads(row[2]),
        payload=(json.loads(row[3]) if row[3] is not None else None),
        is_valid=bool(row[4]), validation_errors=json.loads(row[5]),
        failure_reason=row[6], saved_at=row[7],
    )


def reconstruct_editor_inputs(run_id: str, *,
                               db_path: Optional[str] = None,
                               conn: Optional[sqlite3.Connection] = None) -> list:
    """Rebuild the editor_inputs list (the exact shape
    intelligence_editor.run_intelligence_editor / intelligence_editor_
    schema.validate_brief expect) from persisted story_artifacts for this
    run_id, applying the Pass #2 -> Editor eligibility rule:

        a story is included only if its persisted "pass2" stage artifact
        is_valid == True AND its payload's editor_eligibility != "not_eligible".

    evidence_ids are read from the persisted "evidence_analyst" stage
    artifact's payload["evidence_records"][*]["evidence_id"] (falling
    back to an empty list if that stage was never saved for this story --
    reconstruction never fabricates evidence_ids). original_story (for
    title/candidate_materiality context) is read from the persisted
    "corpus_analyst" stage artifact's payload, when present.

    Returns a plain list -- never raises on a missing/invalid stage for
    one story; that story is simply excluded, consistent with the
    orchestrator's own per-story failure-isolation discipline.
    """
    owns_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        pass2_rows = list_story_artifacts(run_id, stage="pass2", conn=conn)
        evidence_rows = {
            r.story_id: r for r in list_story_artifacts(run_id, stage="evidence_analyst", conn=conn)
        }
        corpus_rows = {
            r.story_id: r for r in list_story_artifacts(run_id, stage="corpus_analyst", conn=conn)
        }
    finally:
        if owns_conn:
            conn.close()

    editor_inputs = []
    for rec in pass2_rows:
        if not rec.is_valid or not isinstance(rec.payload, dict):
            continue
        if rec.payload.get("editor_eligibility") == "not_eligible":
            continue

        evidence_ids = []
        ev_rec = evidence_rows.get(rec.story_id)
        if ev_rec is not None and isinstance(ev_rec.payload, dict):
            evidence_ids = [
                e.get("evidence_id") for e in (ev_rec.payload.get("evidence_records") or [])
                if isinstance(e, dict) and e.get("evidence_id")
            ]

        corpus_rec = corpus_rows.get(rec.story_id)
        original_story = corpus_rec.payload if (corpus_rec and isinstance(corpus_rec.payload, dict)) else {}

        editor_inputs.append({
            "story_id": rec.story_id,
            "original_story": original_story,
            "pass2_output": rec.payload,
            "evidence_ids": evidence_ids,
        })

    return editor_inputs


# ===========================================================================
# KNOWLEDGE LAYER -- propositions / knowledge_links / research_telemetry
# ===========================================================================
#
# Strictly additive. Populated only by knowledge_projection.py over
# ALREADY-VALIDATED story_artifacts/briefs rows. A failure here must never
# affect story_artifacts/briefs persistence.
#
# propositions: verbatim analyst statements + role + exact upstream
# disposition/confidence + evidence IDs + context + provenance + lifecycle
# status (+ supersession). It makes no epistemic claim. scorecard_id /
# epistemic_rating / usage_class are NULL ("unscored") until a real
# epistemic_scorer.py scorecard is attached with attach_scorecard().
#
# knowledge_links: purely structural references between identifiers; no
# confidence or epistemic semantics.
#
# Idempotency: primary keys are deterministic fingerprints of the source
# location; saves are UPSERTs that refresh projection columns only, so
# re-projection never duplicates rows, never changes created_at, and never
# discards an attached scorecard (unless the statement text itself changed,
# in which case the scorecard no longer describes it and is cleared).


@dataclass
class PropositionRecord:
    proposition_id: str
    source_run_id: str
    source_story_id: str
    source_stage: str
    source_field: str
    source_index: int
    statement_role: str
    statement_text: str
    extraction_method: str
    upstream_disposition: Optional[str] = None
    upstream_confidence: Optional[str] = None
    evidence_ids: list = field(default_factory=list)
    context: dict = field(default_factory=dict)
    status: str = "active"
    superseded_by: Optional[str] = None
    scorecard_id: Optional[str] = None
    epistemic_rating: Optional[str] = None
    usage_class: Optional[str] = None
    source_saved_at: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class KnowledgeLinkRecord:
    link_id: str
    subject: str
    subject_type: str
    predicate: str
    object: str
    object_type: str
    source_run_id: str
    created_at: Optional[str] = None


@dataclass
class ResearchTelemetryRecord:
    telemetry_id: str
    run_id: str
    stage: str
    story_id: Optional[str] = None
    attempt_number: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    duration_seconds: Optional[float] = None
    request_succeeded: Optional[bool] = None
    cache_hit: Optional[bool] = None
    avoided_call_count: Optional[int] = None
    escalation_reason: Optional[str] = None
    recorded_at: Optional[str] = None


def save_proposition(p: PropositionRecord, *,
                     db_path: Optional[str] = None,
                     conn: Optional[sqlite3.Connection] = None) -> None:
    """Idempotent UPSERT of one propositions row (see section comment)."""
    if p.statement_role not in ROLE_VALUES:
        raise ValueError(f"statement_role must be one of {sorted(ROLE_VALUES)}, got {p.statement_role!r}")
    if p.status not in STATUS_VALUES:
        raise ValueError(f"status must be one of {sorted(STATUS_VALUES)}, got {p.status!r}")
    if p.superseded_by is not None and p.status != "historical":
        raise ValueError("a proposition with superseded_by must have status 'historical'")
    owns_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO propositions (proposition_id, source_run_id, source_story_id, source_stage, "
            "source_field, source_index, statement_role, statement_text, upstream_disposition, "
            "upstream_confidence, evidence_ids_json, context_json, status, superseded_by, scorecard_id, "
            "epistemic_rating, usage_class, extraction_method, source_saved_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(proposition_id) DO UPDATE SET "
            "scorecard_id = CASE WHEN excluded.statement_text = propositions.statement_text "
            "  THEN propositions.scorecard_id ELSE NULL END, "
            "epistemic_rating = CASE WHEN excluded.statement_text = propositions.statement_text "
            "  THEN propositions.epistemic_rating ELSE NULL END, "
            "usage_class = CASE WHEN excluded.statement_text = propositions.statement_text "
            "  THEN propositions.usage_class ELSE NULL END, "
            "statement_role = excluded.statement_role, statement_text = excluded.statement_text, "
            "upstream_disposition = excluded.upstream_disposition, "
            "upstream_confidence = excluded.upstream_confidence, "
            "evidence_ids_json = excluded.evidence_ids_json, context_json = excluded.context_json, "
            "status = excluded.status, superseded_by = excluded.superseded_by, "
            "extraction_method = excluded.extraction_method, source_saved_at = excluded.source_saved_at",
            (
                p.proposition_id, p.source_run_id, p.source_story_id, p.source_stage, p.source_field,
                p.source_index, p.statement_role, p.statement_text, p.upstream_disposition,
                p.upstream_confidence, json.dumps(p.evidence_ids, sort_keys=True),
                json.dumps(p.context, sort_keys=True), p.status, p.superseded_by, p.scorecard_id,
                p.epistemic_rating, p.usage_class, p.extraction_method, p.source_saved_at,
                p.created_at or _now_iso(),
            ),
        )
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


_PROPOSITION_COLUMNS = (
    "proposition_id, source_run_id, source_story_id, source_stage, source_field, source_index, "
    "statement_role, statement_text, upstream_disposition, upstream_confidence, evidence_ids_json, "
    "context_json, status, superseded_by, scorecard_id, epistemic_rating, usage_class, "
    "extraction_method, source_saved_at, created_at"
)


def list_propositions(*, run_id: Optional[str] = None, story_id: Optional[str] = None,
                      role: Optional[str] = None, status: Optional[str] = None,
                      db_path: Optional[str] = None,
                      conn: Optional[sqlite3.Connection] = None) -> list:
    """propositions rows, optionally filtered; deterministic order (source location)."""
    owns_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        clauses, params = [], []
        for col, val in (("source_run_id", run_id), ("source_story_id", story_id),
                         ("statement_role", role), ("status", status)):
            if val is not None:
                clauses.append(f"{col} = ?")
                params.append(val)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT {_PROPOSITION_COLUMNS} FROM propositions {where} "
            "ORDER BY source_run_id, source_story_id, source_stage, source_field, source_index, "
            "statement_role, proposition_id", params).fetchall()
    finally:
        if owns_conn:
            conn.close()
    return [
        PropositionRecord(
            proposition_id=r[0], source_run_id=r[1], source_story_id=r[2], source_stage=r[3],
            source_field=r[4], source_index=r[5], statement_role=r[6], statement_text=r[7],
            upstream_disposition=r[8], upstream_confidence=r[9], evidence_ids=json.loads(r[10]),
            context=json.loads(r[11]), status=r[12], superseded_by=r[13], scorecard_id=r[14],
            epistemic_rating=r[15], usage_class=r[16], extraction_method=r[17],
            source_saved_at=r[18], created_at=r[19],
        )
        for r in rows
    ]


def attach_scorecard(proposition_id: str, scorecard: dict, *,
                     db_path: Optional[str] = None,
                     conn: Optional[sqlite3.Connection] = None) -> None:
    """Attach a REAL epistemic_scorer.score_card() result to a stored proposition.
    Refuses an invalid scorecard, an unknown proposition, or a scorecard whose
    proposition text differs from the stored statement. Stores only the
    reference (scorecard_id) and the scorer's own rating / usage_class."""
    if not isinstance(scorecard, dict) or scorecard.get("status") == "invalid" or not scorecard.get("computed"):
        raise ValueError("scorecard is missing or invalid")
    import epistemic_scorer as scorer  # contract authority; imported lazily
    rating = scorecard["computed"]["final_rating"]
    usage = scorecard["computed"]["usage_class"]
    if rating not in {f"E{n}" for n in range(6)}:
        raise ValueError(f"unknown rating {rating!r}")
    if usage not in set(scorer.USAGE_BY_RATING.values()) | {"history_only"}:
        raise ValueError(f"unknown usage_class {usage!r}")
    owns_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        row = conn.execute("SELECT statement_text FROM propositions WHERE proposition_id = ?",
                           (proposition_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown proposition_id {proposition_id!r}")
        if scorecard["proposition"]["text"] != row[0]:
            raise ValueError("scorecard proposition text does not match the stored statement")
        conn.execute("UPDATE propositions SET scorecard_id = ?, epistemic_rating = ?, usage_class = ? "
                     "WHERE proposition_id = ?", (scorecard["scorecard_id"], rating, usage, proposition_id))
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def save_knowledge_link(link: KnowledgeLinkRecord, *,
                         db_path: Optional[str] = None,
                         conn: Optional[sqlite3.Connection] = None) -> None:
    """Persist (or idempotently re-persist) one knowledge_links row.
    link.predicate must be one of PREDICATE_VALUES; link.subject_type and
    link.object_type must be one of KNOWLEDGE_LINK_NODE_TYPES -- enforced
    here, deterministically. link.link_id is expected to already be a
    deterministic fingerprint (see knowledge_projection.py)."""
    if link.predicate not in PREDICATE_VALUES:
        raise ValueError(f"predicate must be one of {sorted(PREDICATE_VALUES)}, got {link.predicate!r}")
    if link.subject_type not in KNOWLEDGE_LINK_NODE_TYPES:
        raise ValueError(f"subject_type must be one of {sorted(KNOWLEDGE_LINK_NODE_TYPES)}, "
                          f"got {link.subject_type!r}")
    if link.object_type not in KNOWLEDGE_LINK_NODE_TYPES:
        raise ValueError(f"object_type must be one of {sorted(KNOWLEDGE_LINK_NODE_TYPES)}, "
                          f"got {link.object_type!r}")

    owns_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO knowledge_links "
            "(link_id, subject, subject_type, predicate, object, object_type, source_run_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link.link_id, link.subject, link.subject_type, link.predicate, link.object,
                link.object_type, link.source_run_id, link.created_at or _now_iso(),
            ),
        )
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def list_knowledge_links(*, run_id: Optional[str] = None,
                          subject: Optional[str] = None, subject_type: Optional[str] = None,
                          object: Optional[str] = None, object_type: Optional[str] = None,
                          predicate: Optional[str] = None,
                          db_path: Optional[str] = None,
                          conn: Optional[sqlite3.Connection] = None) -> list:
    """All knowledge_links rows, optionally filtered by any combination of
    source_run_id, subject(+type), object(+type), predicate. Ordered by
    created_at for deterministic output."""
    owns_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        clauses, params = [], []
        if run_id is not None:
            clauses.append("source_run_id = ?")
            params.append(run_id)
        if subject is not None:
            clauses.append("subject = ?")
            params.append(subject)
        if subject_type is not None:
            clauses.append("subject_type = ?")
            params.append(subject_type)
        if object is not None:
            clauses.append("object = ?")
            params.append(object)
        if object_type is not None:
            clauses.append("object_type = ?")
            params.append(object_type)
        if predicate is not None:
            clauses.append("predicate = ?")
            params.append(predicate)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            "SELECT link_id, subject, subject_type, predicate, object, object_type, "
            f"source_run_id, created_at FROM knowledge_links {where} ORDER BY created_at, link_id",
            params,
        ).fetchall()
    finally:
        if owns_conn:
            conn.close()
    return [
        KnowledgeLinkRecord(
            link_id=r[0], subject=r[1], subject_type=r[2], predicate=r[3], object=r[4],
            object_type=r[5], source_run_id=r[6], created_at=r[7],
        )
        for r in rows
    ]


def save_research_telemetry(record: ResearchTelemetryRecord, *,
                             db_path: Optional[str] = None,
                             conn: Optional[sqlite3.Connection] = None) -> None:
    """Persist (or idempotently re-persist) one research_telemetry row.
    record.telemetry_id is expected to already be a deterministic
    fingerprint so re-persisting the same attempt is a no-op. This
    function performs no live-pipeline wiring -- callers (future work, not
    this increment) are responsible for invoking it with real
    AttemptDiagnostics data; nothing in corpus_analyst.py or
    evidence_analyst.py calls this today."""
    owns_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO research_telemetry "
            "(telemetry_id, run_id, story_id, stage, attempt_number, input_tokens, output_tokens, "
            "duration_seconds, request_succeeded, cache_hit, avoided_call_count, escalation_reason, "
            "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.telemetry_id, record.run_id, record.story_id, record.stage,
                record.attempt_number, record.input_tokens, record.output_tokens,
                record.duration_seconds,
                (None if record.request_succeeded is None else int(record.request_succeeded)),
                (None if record.cache_hit is None else int(record.cache_hit)),
                record.avoided_call_count, record.escalation_reason,
                record.recorded_at or _now_iso(),
            ),
        )
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def list_research_telemetry(*, run_id: Optional[str] = None, story_id: Optional[str] = None,
                             stage: Optional[str] = None,
                             db_path: Optional[str] = None,
                             conn: Optional[sqlite3.Connection] = None) -> list:
    owns_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        clauses, params = [], []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if story_id is not None:
            clauses.append("story_id = ?")
            params.append(story_id)
        if stage is not None:
            clauses.append("stage = ?")
            params.append(stage)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            "SELECT telemetry_id, run_id, story_id, stage, attempt_number, input_tokens, "
            "output_tokens, duration_seconds, request_succeeded, cache_hit, avoided_call_count, "
            f"escalation_reason, recorded_at FROM research_telemetry {where} "
            "ORDER BY recorded_at, telemetry_id",
            params,
        ).fetchall()
    finally:
        if owns_conn:
            conn.close()
    return [
        ResearchTelemetryRecord(
            telemetry_id=r[0], run_id=r[1], story_id=r[2], stage=r[3], attempt_number=r[4],
            input_tokens=r[5], output_tokens=r[6], duration_seconds=r[7],
            request_succeeded=(None if r[8] is None else bool(r[8])),
            cache_hit=(None if r[9] is None else bool(r[9])),
            avoided_call_count=r[10], escalation_reason=r[11], recorded_at=r[12],
        )
        for r in rows
    ]
