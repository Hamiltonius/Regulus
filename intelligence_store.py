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
