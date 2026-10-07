#!/usr/bin/env python3
"""
corpus_extractor.py — deterministic, read-only reporting-window corpus
extraction for the future Regulus corpus-intelligence workflow.

This module is Step 1 of that workflow ONLY:

    database -> THIS EXTRACTOR

It does not analyze, cluster, classify, or judge anything. It retrieves the
observations Regulus already persisted in the `alerts` table for a
requested publication-date window and returns them in a normalized,
deterministic shape. Every other step in the conceptual pipeline (Corpus
Analyst, research requirements, full-document evidence, analyst
reassessment, final brief) is explicitly out of scope here and is not even
stubbed out.

Isolation from the existing event/DD pipeline (frozen for this task):
  - This module does not import dd_pipeline or dd_schema.
  - It performs no INSERT/UPDATE/DELETE/ALTER — SELECT only.
  - It makes no network request, no LLM/API call, no email, no PDF.
  - It is not imported by regulus_v3.main() or dd_pipeline.run_due_diligence
    — nothing in the existing collection/DD/email/PDF path calls this, and
    this does not call into any of them.
  - Calling get_corpus() has zero side effects beyond reading from the
    connection it is given.

Reporting-window semantics (see get_corpus docstring): membership is
decided by the `pub_date` column (publication date) alone, using inclusive
string-comparison bounds against ISO 8601 "YYYY-MM-DD" dates — never
fetched_at, source_retrieved_at, effective_date, or analysis_generated_at,
which represent different events and are returned as separate, distinct
fields.

Tier assignment is deterministic and structural, not semantic: a record is
"analyzed" iff `analysis_generated_at` is non-NULL, which regulus_v3.main()
sets unconditionally (alongside analysis_model) at the one point Stage 1
analysis succeeded — never inferred from whether change_type, countries,
etc. happen to be populated. due_diligence_ran is read directly from the
`alerts.due_diligence_ran` column, which main() sets unconditionally (1/0)
for every row, independent of whether Stage 1 or DD actually ran — this is
a reliably-established fact from the existing schema, not a heuristic.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional


# Columns pulled directly from `alerts`. Deliberately a fixed, explicit
# list (not SELECT *) so a future unrelated column added to `alerts` can
# never silently change this extractor's output shape.
_SELECT_COLUMNS = [
    "document_number",
    "pub_date",
    "effective_date",
    "title",
    "agency",
    "score",
    "countries",
    "entities",
    "eccns",
    "change_type",
    "summary",
    "primary_source_url",
    "analysis_generated_at",
    "due_diligence_ran",
]

# Columns that may be legitimately absent on an older/historical `alerts`
# schema, discovered via introspection rather than assumed. Only
# due_diligence_ran is known to vary today (see _introspect_columns and
# get_corpus) -- a column in this set that turns out to be missing gets a
# deterministic None rather than causing the SELECT to fail with
# "no such column". analysis_generated_at is NOT in this set: it is
# documented/confirmed present on production, and tier determination
# continues to rely on it exactly as before.
_OPTIONAL_COLUMNS = {"due_diligence_ran"}

_JSON_COLUMNS = {"agency", "countries", "entities", "eccns"}


def _decode_json_field(raw: Optional[str]) -> Any:
    """Decode a column that was stored as a JSON-encoded string (agency,
    countries, entities, eccns), returning the original structured value
    (a list, or a list of agency dicts). This is a format decode, not an
    inference: the column's stored content already IS this structure,
    serialized to text by the existing pipeline (json.dumps(...)) when the
    row was written. If the stored text isn't valid JSON (or is NULL), the
    raw value is returned unchanged rather than guessed at or dropped —
    preserving the original stored value exactly, per the data-discipline
    rule against inventing or coercing analytical content."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw


def _tier(analysis_generated_at: Optional[str]) -> str:
    """Deterministic, structural tier assignment. "analyzed" iff Stage 1
    analysis is known to have succeeded for this row (analysis_generated_at
    is set), never decided by looking at the content of change_type,
    countries, entities, eccns, or summary."""
    return "analyzed" if analysis_generated_at is not None else "metadata_only"


@dataclass
class CorpusObservation:
    """One row of the extracted corpus. Field names intentionally mirror
    the persisted columns (plus `tier` and the renamed `publication_date`
    for clarity) — no field here is computed from document content.

    due_diligence_ran is TRI-STATE: True/False when the database schema
    establishes DD status (the due_diligence_ran column exists and holds
    0/1), or None when the available schema cannot establish it (the
    column doesn't exist on this database at all). None is never a
    stand-in for False — see get_corpus's schema-introspection docstring.
    """
    document_number: Optional[str]
    publication_date: Optional[str]
    effective_date: Optional[str]
    title: Optional[str]
    agency: Any
    score: Optional[int]
    countries: Any
    entities: Any
    eccns: Any
    change_type: Optional[str]
    summary: Optional[str]
    primary_source_url: Optional[str]
    tier: str
    due_diligence_ran: Optional[bool]

    def to_dict(self) -> dict:
        return {
            "document_number": self.document_number,
            "publication_date": self.publication_date,
            "effective_date": self.effective_date,
            "title": self.title,
            "agency": self.agency,
            "score": self.score,
            "countries": self.countries,
            "entities": self.entities,
            "eccns": self.eccns,
            "change_type": self.change_type,
            "summary": self.summary,
            "primary_source_url": self.primary_source_url,
            "tier": self.tier,
            "due_diligence_ran": self.due_diligence_ran,
        }


@dataclass
class Corpus:
    """Result of get_corpus(). Carries only deterministic, persisted/derived
    observation data and the reporting-window parameters used to produce
    it — no stories, clusters, hypotheses, or materiality judgments; those
    belong to a later step, not this one."""
    start_date: str
    end_date: str
    observations: list = field(default_factory=list)

    @property
    def observation_count(self) -> int:
        return len(self.observations)

    def to_dict(self) -> dict:
        return {
            "reporting_period": {"start": self.start_date, "end": self.end_date},
            "observation_count": self.observation_count,
            "observations": [o.to_dict() for o in self.observations],
        }

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), **kwargs)


def _existing_columns(conn: sqlite3.Connection, table: str) -> set:
    """Read-only schema introspection via PRAGMA table_info — never an
    ALTER/INSERT/UPDATE/DELETE, never a migration, never a write of any
    kind. Used to detect whether a given column exists on THIS database's
    `alerts` table before the SELECT is built, so a historical schema
    missing a column (e.g. due_diligence_ran, absent on the current
    node02 production database) fails safe to None for that column
    instead of raising sqlite3.OperationalError."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def get_corpus(conn: sqlite3.Connection, start_date: str, end_date: str) -> Corpus:
    """Read-only retrieval of every `alerts` row whose publication date
    falls within [start_date, end_date] inclusive.

    SCHEMA COMPATIBILITY: before building the SELECT, this function
    introspects which columns actually exist on this database's `alerts`
    table (via _existing_columns/PRAGMA table_info — read-only, no
    migration). Any column in _OPTIONAL_COLUMNS that is missing is
    dropped from the SELECT and reported as None on every observation,
    rather than raising sqlite3.OperationalError or being silently
    inferred from some other column. Today the only such column is
    due_diligence_ran: on a historical/production database that predates
    it, every observation's due_diligence_ran is None (tri-state
    "unknown from this schema"), never False — see CorpusObservation's
    docstring. analysis_generated_at is NOT optional: it is required for
    tier determination and assumed present, exactly as before this fix.

    Reporting-window semantics:
      - Membership is decided SOLELY by the `pub_date` column (the
        document's Federal Register publication date), never fetched_at,
        source_retrieved_at, effective_date, or analysis_generated_at.
      - Bounds are inclusive: start_date <= pub_date <= end_date.
      - start_date/end_date must be "YYYY-MM-DD" strings (ISO 8601 date,
        no time component) — the same format regulus_v3 persists into
        pub_date (Federal Register API's publication_date field). ISO
        8601 date strings compare correctly as plain strings, so this is
        a simple SQL BETWEEN-style comparison, not a parsed-date
        comparison — deliberately avoiding any date-library behavior
        (timezone handling, etc.) that could make membership
        non-deterministic.
      - A NULL pub_date can never match a bounded comparison and is
        therefore excluded — there is no record in the existing schema
        where pub_date would legitimately be NULL (main() always sets it
        from the Federal Register document), so this has no practical
        effect on real data; it is noted here rather than special-cased.

    Ordering is deterministic: publication_date ASC, then document_number
    ASC (a plain string sort — Federal Register document numbers are
    "YYYY-NNNNN"-shaped and this ordering is stable and reproducible, not
    semantically meaningful).

    This function issues exactly one SELECT. It never writes to `conn`,
    never calls out to any LLM/API/network function, and does not import
    or invoke anything from dd_pipeline, dd_schema, or regulus_v3.
    """
    existing = _existing_columns(conn, "alerts")
    select_columns = [c for c in _SELECT_COLUMNS if c not in _OPTIONAL_COLUMNS or c in existing]
    absent_optional_columns = [c for c in _SELECT_COLUMNS if c in _OPTIONAL_COLUMNS and c not in existing]

    columns_sql = ", ".join(select_columns)
    rows = conn.execute(
        f"""
        SELECT {columns_sql}
        FROM alerts
        WHERE pub_date >= ? AND pub_date <= ?
        ORDER BY pub_date ASC, document_number ASC
        """,
        (start_date, end_date),
    ).fetchall()

    observations = []
    for row in rows:
        record = dict(zip(select_columns, row))
        for col in absent_optional_columns:
            record[col] = None  # schema cannot establish this -- not inferred as False
        for col in _JSON_COLUMNS:
            record[col] = _decode_json_field(record[col])

        raw_dd_ran = record["due_diligence_ran"]

        observations.append(CorpusObservation(
            document_number=record["document_number"],
            publication_date=record["pub_date"],
            effective_date=record["effective_date"],
            title=record["title"],
            agency=record["agency"],
            score=record["score"],
            countries=record["countries"],
            entities=record["entities"],
            eccns=record["eccns"],
            change_type=record["change_type"],
            summary=record["summary"],
            primary_source_url=record["primary_source_url"],
            tier=_tier(record["analysis_generated_at"]),
            # Tri-state: None (column absent, or NULL even though present)
            # stays None -- never coerced to False. Only an actual 0/1
            # value becomes a real bool.
            due_diligence_ran=None if raw_dd_ran is None else bool(raw_dd_ran),
        ))

    return Corpus(start_date=start_date, end_date=end_date, observations=observations)


if __name__ == "__main__":
    # Read-only diagnostic CLI: dump a reporting window as JSON.
    #   python3 corpus_extractor.py 2026-09-14 2026-10-05 [db_path]
    # Defaults to DB_PATH env var / ./bis_watcher.db, matching regulus_v3's
    # own default, but opens it with a plain read-only sqlite3 connection —
    # this file never calls regulus_v3.get_db(), so it never runs that
    # function's schema-migration ALTER TABLE statements against whatever
    # database path is passed in.
    import os
    import sys

    if len(sys.argv) < 3:
        print("usage: corpus_extractor.py START_DATE END_DATE [db_path]", file=sys.stderr)
        sys.exit(2)

    start, end = sys.argv[1], sys.argv[2]
    db_path = sys.argv[3] if len(sys.argv) > 3 else os.environ.get("DB_PATH", "bis_watcher.db")

    ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        corpus = get_corpus(ro_conn, start, end)
        print(corpus.to_json(indent=2))
    finally:
        ro_conn.close()
