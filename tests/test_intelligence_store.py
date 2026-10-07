#!/usr/bin/env python3
"""
Tests for intelligence_store.py — persistence for Regulus intelligence-
product state (story artifacts across stages + compiled Briefs),
including reconstruction of editor_inputs from persisted state.

Every test uses its own throwaway SQLite file inside a tempdir (via
db_path=) -- never INTELLIGENCE_DB_PATH's default and never regulus_v3's
bis_watcher.db. The file is removed at the end of the run.

Run: python3 tests/test_intelligence_store.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == "FAIL" else ""))
    return condition


import intelligence_store as st

_TMPDIR = tempfile.mkdtemp(prefix="regulus_intelligence_store_test_")


def _fresh_db_path(name):
    return os.path.join(_TMPDIR, name)


# ===========================================================================
# A. Schema init is idempotent; isolated from the production DD database
# ===========================================================================
db_path = _fresh_db_path("a.db")
conn1 = st.get_connection(db_path)
conn1.close()
conn2 = st.get_connection(db_path)  # re-opening/re-initializing must not error
conn2.close()
check("A1. get_connection is idempotent (safe to call repeatedly on the same file)", True)
check("A2. INTELLIGENCE_DB_PATH default is NOT bis_watcher.db / regulus_v3's DB_PATH",
      st.INTELLIGENCE_DB_PATH != "bis_watcher.db")
check("A3. intelligence_store.py does not import regulus_v3/dd_pipeline/dd_schema",
      not hasattr(st, "regulus_v3") and not hasattr(st, "dd_pipeline") and not hasattr(st, "dd_schema"))

# ===========================================================================
# B. Save / load a single story artifact round-trip, per stage
# ===========================================================================
db_path = _fresh_db_path("b.db")
corpus_payload = {"story_id": "CS-01", "title": "Test story", "candidate_materiality": "high"}
st.save_story_artifact("run-1", "CS-01", "corpus_analyst", corpus_payload, is_valid=True, db_path=db_path)
rec = st.load_story_artifact("run-1", "CS-01", "corpus_analyst", db_path=db_path)
check("B1. a saved corpus_analyst artifact round-trips exactly", rec is not None and rec.payload == corpus_payload)
check("B2. is_valid round-trips as True", rec.is_valid is True)
check("B3. loading a never-saved (run_id, story_id, stage) returns None",
      st.load_story_artifact("run-1", "CS-99", "corpus_analyst", db_path=db_path) is None)

# ===========================================================================
# C. save_story_artifact rejects an unknown stage
# ===========================================================================
raised = False
try:
    st.save_story_artifact("run-1", "CS-01", "bogus_stage", {}, is_valid=True, db_path=db_path)
except ValueError:
    raised = True
check("C1. an unknown stage value raises ValueError", raised)

# ===========================================================================
# D. Overwrite semantics: re-saving the same (run_id, story_id, stage)
# replaces the prior record, not append
# ===========================================================================
db_path = _fresh_db_path("d.db")
st.save_story_artifact("run-1", "CS-01", "pass2", {"story_id": "CS-01", "v": 1},
                        is_valid=False, validation_errors=["first attempt bad"], db_path=db_path)
st.save_story_artifact("run-1", "CS-01", "pass2", {"story_id": "CS-01", "v": 2},
                        is_valid=True, db_path=db_path)
rec = st.load_story_artifact("run-1", "CS-01", "pass2", db_path=db_path)
check("D1. re-saving the same (run_id, story_id, stage) overwrites rather than duplicates",
      rec.payload["v"] == 2 and rec.is_valid is True)
all_rows = st.list_story_artifacts("run-1", stage="pass2", db_path=db_path)
check("D2. exactly one row exists for that (run_id, stage) after the overwrite", len(all_rows) == 1)

# ===========================================================================
# E. Failure/invalidity is persisted too (not just successes) -- the
# orchestrator needs this for per-story failure isolation/reporting
# ===========================================================================
db_path = _fresh_db_path("e.db")
st.save_story_artifact("run-1", "CS-02", "evidence_analyst", {"story_id": "CS-02"},
                        is_valid=False, validation_errors=["evidence_analyst_call_failed: timeout"],
                        failure_reason="evidence_analyst_call_failed", db_path=db_path)
rec = st.load_story_artifact("run-1", "CS-02", "evidence_analyst", db_path=db_path)
check("E1. a failed/invalid story artifact persists with is_valid=False", rec.is_valid is False)
check("E2. failure_reason persists", rec.failure_reason == "evidence_analyst_call_failed")
check("E3. validation_errors persists", rec.validation_errors == ["evidence_analyst_call_failed: timeout"])

# ===========================================================================
# F. list_story_artifacts across stages/stories for one run_id
# ===========================================================================
db_path = _fresh_db_path("f.db")
st.save_story_artifact("run-1", "CS-01", "corpus_analyst", {"story_id": "CS-01"}, is_valid=True, db_path=db_path)
st.save_story_artifact("run-1", "CS-01", "evidence_analyst", {"story_id": "CS-01"}, is_valid=True, db_path=db_path)
st.save_story_artifact("run-1", "CS-02", "corpus_analyst", {"story_id": "CS-02"}, is_valid=True, db_path=db_path)
st.save_story_artifact("run-2", "CS-01", "corpus_analyst", {"story_id": "CS-01", "other_run": True},
                        is_valid=True, db_path=db_path)
rows = st.list_story_artifacts("run-1", db_path=db_path)
check("F1. list_story_artifacts returns all rows for run-1 only (run_id scoping)",
      len(rows) == 3 and all(r.run_id == "run-1" for r in rows))
rows_stage = st.list_story_artifacts("run-1", stage="corpus_analyst", db_path=db_path)
check("F2. stage filtering narrows correctly", len(rows_stage) == 2
      and {r.story_id for r in rows_stage} == {"CS-01", "CS-02"})

# ===========================================================================
# G. Brief save/load round-trip
# ===========================================================================
db_path = _fresh_db_path("g.db")
reporting_period = {"start": "2026-07-01", "end": "2026-09-30"}
brief_payload = {"brief_id": "BRIEF-001", "executive_assessment": "Synthetic."}
st.save_brief("run-1", "BRIEF-001", reporting_period, brief_payload, is_valid=True, db_path=db_path)
brief_rec = st.load_brief("run-1", "BRIEF-001", db_path=db_path)
check("G1. a saved Brief round-trips exactly", brief_rec is not None and brief_rec.payload == brief_payload)
check("G2. reporting_period round-trips", brief_rec.reporting_period == reporting_period)
check("G3. loading a never-saved brief_id returns None",
      st.load_brief("run-1", "BRIEF-999", db_path=db_path) is None)

# A failed Brief run (no payload at all) still persists its failure_reason.
st.save_brief("run-1", "BRIEF-002", reporting_period, None, is_valid=False,
              validation_errors=["editor_call_failed: timeout"], failure_reason="editor_call_failed",
              db_path=db_path)
brief_rec2 = st.load_brief("run-1", "BRIEF-002", db_path=db_path)
check("G4. a failed Brief run persists with payload=None and failure_reason set",
      brief_rec2.payload is None and brief_rec2.failure_reason == "editor_call_failed")

# ===========================================================================
# H. reconstruct_editor_inputs — full happy path
# ===========================================================================
db_path = _fresh_db_path("h.db")
st.save_story_artifact("run-1", "CS-01", "corpus_analyst",
                        {"story_id": "CS-01", "title": "Story one", "candidate_materiality": "high"},
                        is_valid=True, db_path=db_path)
st.save_story_artifact("run-1", "CS-01", "evidence_analyst",
                        {"story_id": "CS-01", "evidence_records": [
                            {"evidence_id": "EV-1", "document_number": "D1"},
                            {"evidence_id": "EV-2", "document_number": "D2"},
                        ]}, is_valid=True, db_path=db_path)
st.save_story_artifact("run-1", "CS-01", "pass2",
                        {"story_id": "CS-01", "editor_eligibility": "eligible",
                         "assessment_disposition": "confirmed"},
                        is_valid=True, db_path=db_path)

editor_inputs = st.reconstruct_editor_inputs("run-1", db_path=db_path)
check("H1. reconstruct_editor_inputs returns exactly one entry for the one eligible story",
      len(editor_inputs) == 1)
check("H2. the reconstructed entry carries the right story_id/pass2_output/evidence_ids",
      editor_inputs[0]["story_id"] == "CS-01"
      and editor_inputs[0]["pass2_output"]["assessment_disposition"] == "confirmed"
      and set(editor_inputs[0]["evidence_ids"]) == {"EV-1", "EV-2"})
check("H3. the reconstructed entry carries original_story context from the corpus_analyst stage",
      editor_inputs[0]["original_story"]["title"] == "Story one")

# ===========================================================================
# I. reconstruct_editor_inputs — eligibility filtering
# ===========================================================================
db_path = _fresh_db_path("i.db")
# CS-01: valid pass2, but editor_eligibility == "not_eligible" -> excluded
st.save_story_artifact("run-1", "CS-01", "pass2",
                        {"story_id": "CS-01", "editor_eligibility": "not_eligible"},
                        is_valid=True, db_path=db_path)
# CS-02: pass2 itself invalid -> excluded, regardless of its editor_eligibility field
st.save_story_artifact("run-1", "CS-02", "pass2",
                        {"story_id": "CS-02", "editor_eligibility": "eligible"},
                        is_valid=False, validation_errors=["bad"], db_path=db_path)
# CS-03: valid pass2, eligible_with_caveats -> included
st.save_story_artifact("run-1", "CS-03", "pass2",
                        {"story_id": "CS-03", "editor_eligibility": "eligible_with_caveats"},
                        is_valid=True, db_path=db_path)

editor_inputs = st.reconstruct_editor_inputs("run-1", db_path=db_path)
included_ids = {e["story_id"] for e in editor_inputs}
check("I1. a story with editor_eligibility='not_eligible' is excluded from reconstruction",
      "CS-01" not in included_ids)
check("I2. a story whose pass2 artifact itself is_valid=False is excluded from reconstruction",
      "CS-02" not in included_ids)
check("I3. a story with editor_eligibility='eligible_with_caveats' IS included", "CS-03" in included_ids)

# ===========================================================================
# J. reconstruct_editor_inputs tolerates a missing evidence_analyst/
# corpus_analyst stage for an otherwise-eligible story (never fabricates
# evidence_ids or an original_story)
# ===========================================================================
db_path = _fresh_db_path("j.db")
st.save_story_artifact("run-1", "CS-04", "pass2",
                        {"story_id": "CS-04", "editor_eligibility": "eligible"},
                        is_valid=True, db_path=db_path)
editor_inputs = st.reconstruct_editor_inputs("run-1", db_path=db_path)
check("J1. a story with no persisted evidence_analyst/corpus_analyst stage is still reconstructed",
      len(editor_inputs) == 1)
check("J2. its evidence_ids is an empty list, never fabricated",
      editor_inputs[0]["evidence_ids"] == [])
check("J3. its original_story is an empty dict, never fabricated",
      editor_inputs[0]["original_story"] == {})

# ===========================================================================
# K. reconstruct_editor_inputs scopes strictly to the given run_id
# ===========================================================================
db_path = _fresh_db_path("k.db")
st.save_story_artifact("run-1", "CS-01", "pass2", {"story_id": "CS-01", "editor_eligibility": "eligible"},
                        is_valid=True, db_path=db_path)
st.save_story_artifact("run-2", "CS-05", "pass2", {"story_id": "CS-05", "editor_eligibility": "eligible"},
                        is_valid=True, db_path=db_path)
editor_inputs_run1 = st.reconstruct_editor_inputs("run-1", db_path=db_path)
check("K1. reconstruction for run-1 does not pull in run-2's stories",
      {e["story_id"] for e in editor_inputs_run1} == {"CS-01"})

# ===========================================================================
# L. A shared connection (conn=) works identically to db_path= for every
# function (orchestrator convenience: one open connection per run)
# ===========================================================================
db_path = _fresh_db_path("l.db")
shared_conn = st.get_connection(db_path)
st.save_story_artifact("run-1", "CS-01", "corpus_analyst", {"story_id": "CS-01"}, is_valid=True, conn=shared_conn)
rec = st.load_story_artifact("run-1", "CS-01", "corpus_analyst", conn=shared_conn)
check("L1. save/load via a shared conn= works and does not close the caller's connection",
      rec is not None and rec.payload == {"story_id": "CS-01"})
shared_conn.close()

# ===========================================================================
# M. compute_fingerprint() determinism
# ===========================================================================
fp1 = st.compute_fingerprint({"story_id": "CS-01", "a": 1, "b": 2})
fp2 = st.compute_fingerprint({"b": 2, "a": 1, "story_id": "CS-01"})  # same content, different key order
check("M1. compute_fingerprint is stable under key reordering (sort_keys)", fp1 == fp2)

fp3 = st.compute_fingerprint({"story_id": "CS-01", "a": 1, "b": 3})  # genuinely different content
check("M2. compute_fingerprint differs when content differs", fp1 != fp3)

fp_multi_a = st.compute_fingerprint({"story_id": "CS-01"}, {"evidence_records": ["EV-1"]})
fp_multi_b = st.compute_fingerprint({"story_id": "CS-01"}, {"evidence_records": ["EV-2"]})
check("M3. compute_fingerprint over multiple parts (story + evidence package) distinguishes them",
      fp_multi_a != fp_multi_b)

# ===========================================================================
# N. find_reusable_story_artifact: the core reuse-safety contract
# ===========================================================================
db_path = _fresh_db_path("n.db")
story_v1 = {"story_id": "CS-01", "research_questions": ["Q1?"]}
fp_v1 = st.compute_fingerprint(story_v1)

st.save_story_artifact("run-1", "CS-01", "evidence_analyst", {"story_id": "CS-01", "evidence_records": []},
                        is_valid=True, input_fingerprint=fp_v1, db_path=db_path)

reusable = st.find_reusable_story_artifact("run-1", "CS-01", "evidence_analyst", fp_v1, db_path=db_path)
check("N1. a VALID artifact with a MATCHING fingerprint IS returned as reusable", reusable is not None)

# N2: an INVALID artifact is never reused, even with a matching fingerprint.
db_path = _fresh_db_path("n2.db")
st.save_story_artifact("run-1", "CS-02", "evidence_analyst", {"story_id": "CS-02"},
                        is_valid=False, validation_errors=["evidence_analyst_call_failed: boom"],
                        input_fingerprint=fp_v1, db_path=db_path)
reusable = st.find_reusable_story_artifact("run-1", "CS-02", "evidence_analyst", fp_v1, db_path=db_path)
check("N2. an INVALID artifact is never returned as reusable, even with a matching fingerprint",
      reusable is None)

# N3: a mismatched fingerprint (the story/input changed) prevents reuse of an otherwise-valid artifact.
db_path = _fresh_db_path("n3.db")
story_v2 = {"story_id": "CS-01", "research_questions": ["Q1?", "A DIFFERENT QUESTION NOW"]}
fp_v2 = st.compute_fingerprint(story_v2)
st.save_story_artifact("run-1", "CS-01", "evidence_analyst", {"story_id": "CS-01"},
                        is_valid=True, input_fingerprint=fp_v1, db_path=db_path)
reusable = st.find_reusable_story_artifact("run-1", "CS-01", "evidence_analyst", fp_v2, db_path=db_path)
check("N3. a VALID artifact whose stored fingerprint does NOT match the current input's fingerprint "
      "is never returned as reusable (mismatched story/input)", reusable is None)

# N4: no artifact at all -> None, not an error.
db_path = _fresh_db_path("n4.db")
reusable = st.find_reusable_story_artifact("run-1", "CS-99", "evidence_analyst", fp_v1, db_path=db_path)
check("N4. find_reusable_story_artifact returns None (not an error) when nothing was ever persisted",
      reusable is None)

# N5: a legacy-shaped row with NO stored fingerprint (input_fingerprint=None) is never treated as
# reusable, even though it's otherwise valid -- "no fingerprint" means "not proven compatible."
db_path = _fresh_db_path("n5.db")
st.save_story_artifact("run-1", "CS-03", "evidence_analyst", {"story_id": "CS-03"},
                        is_valid=True, db_path=db_path)  # input_fingerprint intentionally omitted
reusable = st.find_reusable_story_artifact("run-1", "CS-03", "evidence_analyst", fp_v1, db_path=db_path)
check("N5. a valid artifact with NO stored fingerprint at all is never treated as reusable",
      reusable is None)

# N6: reuse never crosses run_id, even with an identical fingerprint and a valid artifact.
db_path = _fresh_db_path("n6.db")
st.save_story_artifact("run-A", "CS-01", "evidence_analyst", {"story_id": "CS-01"},
                        is_valid=True, input_fingerprint=fp_v1, db_path=db_path)
reusable = st.find_reusable_story_artifact("run-B", "CS-01", "evidence_analyst", fp_v1, db_path=db_path)
check("N6. a valid, fingerprint-matching artifact from a DIFFERENT run_id is never reused",
      reusable is None)

# N7: Pass #2 reuse works the same way as Evidence Analyst reuse (same function, different stage).
db_path = _fresh_db_path("n7.db")
fp_pass2 = st.compute_fingerprint(story_v1, {"evidence_records": [{"evidence_id": "EV-1"}]})
st.save_story_artifact("run-1", "CS-01", "pass2", {"story_id": "CS-01", "editor_eligibility": "eligible"},
                        is_valid=True, input_fingerprint=fp_pass2, db_path=db_path)
reusable = st.find_reusable_story_artifact("run-1", "CS-01", "pass2", fp_pass2, db_path=db_path)
check("N7. Pass #2 artifacts are reusable through the same find_reusable_story_artifact function",
      reusable is not None and reusable.payload["editor_eligibility"] == "eligible")

# ===========================================================================
# O. Schema migration: a pre-existing story_artifacts table with no
# input_fingerprint column gets the column added automatically, without
# touching existing row data.
# ===========================================================================
import sqlite3 as _sqlite3

db_path = _fresh_db_path("o.db")
_legacy_conn = _sqlite3.connect(db_path)
_legacy_conn.executescript("""
CREATE TABLE story_artifacts (
    run_id TEXT NOT NULL, story_id TEXT NOT NULL, stage TEXT NOT NULL,
    payload_json TEXT NOT NULL, is_valid INTEGER NOT NULL,
    validation_errors_json TEXT NOT NULL, failure_reason TEXT, saved_at TEXT NOT NULL,
    PRIMARY KEY (run_id, story_id, stage)
);
""")
_legacy_conn.execute(
    "INSERT INTO story_artifacts (run_id, story_id, stage, payload_json, is_valid, "
    "validation_errors_json, failure_reason, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    ("run-legacy", "CS-01", "corpus_analyst", json.dumps({"story_id": "CS-01"}), 1, "[]", None, "2026-01-01T00:00:00+00:00"),
)
_legacy_conn.commit()
_legacy_conn.close()

migrated_conn = st.get_connection(db_path)  # must not raise on the pre-existing, column-less table
rec = st.load_story_artifact("run-legacy", "CS-01", "corpus_analyst", conn=migrated_conn)
migrated_conn.close()
check("O1. get_connection migrates a legacy table missing input_fingerprint without raising", rec is not None)
check("O2. the pre-existing row's data survives the migration untouched",
      rec.payload == {"story_id": "CS-01"} and rec.is_valid is True)
check("O3. the migrated column reads back as None for the pre-existing row (never fabricated)",
      rec.input_fingerprint is None)

# ===========================================================================
# P. provenance_json: additive, store-level-only metadata distinguishing a
# legacy-artifact-migration bootstrap from normal, freshly-generated
# persistence. Mirrors section O's migration pattern exactly, and proves
# this new field never affects find_reusable_story_artifact()'s decision.
# ===========================================================================

# P1: save/load round-trip -- a provenance dict passed to
# save_story_artifact() reads back identically via load_story_artifact().
db_path = _fresh_db_path("p1.db")
provenance_p1 = {
    "kind": "legacy_artifact_migration",
    "source_artifact_path": "/some/path/evidence_artifact.json",
    "historical_input_fingerprint_verified": False,
    "compatibility_established_by": "revalidation_against_current_candidate_story",
    "migrated_at": "2026-10-06T00:00:00+00:00",
}
st.save_story_artifact("run-p1", "CS-01", "evidence_analyst", {"story_id": "CS-01"},
                        is_valid=True, input_fingerprint="fp-p1", provenance=provenance_p1, db_path=db_path)
rec_p1 = st.load_story_artifact("run-p1", "CS-01", "evidence_analyst", db_path=db_path)
check("P1. a provenance dict passed to save_story_artifact() round-trips byte-for-byte "
      "through load_story_artifact()", rec_p1 is not None and rec_p1.provenance == provenance_p1)

# P2: omitting provenance entirely (the normal, live-artifact path) persists
# provenance=None -- explicitly distinguishing "normal" from "legacy migration".
db_path = _fresh_db_path("p2.db")
st.save_story_artifact("run-p2", "CS-01", "evidence_analyst", {"story_id": "CS-01"},
                        is_valid=True, input_fingerprint="fp-p2", db_path=db_path)  # provenance omitted
rec_p2 = st.load_story_artifact("run-p2", "CS-01", "evidence_analyst", db_path=db_path)
check("P2. a normally-persisted artifact saved with no provenance argument has "
      "provenance=None after loading back", rec_p2 is not None and rec_p2.provenance is None)

# P3: list_story_artifacts() also reads provenance back correctly, for both
# a legacy-migrated record and a normal (provenance=None) one in the same run.
db_path = _fresh_db_path("p3.db")
st.save_story_artifact("run-p3", "CS-01", "evidence_analyst", {"story_id": "CS-01"},
                        is_valid=True, input_fingerprint="fp-p3a", provenance=provenance_p1, db_path=db_path)
st.save_story_artifact("run-p3", "CS-02", "evidence_analyst", {"story_id": "CS-02"},
                        is_valid=True, input_fingerprint="fp-p3b", db_path=db_path)
listed_p3 = {r.story_id: r for r in st.list_story_artifacts("run-p3", stage="evidence_analyst", db_path=db_path)}
check("P3. list_story_artifacts() reads back provenance for a legacy-migrated record",
      listed_p3["CS-01"].provenance == provenance_p1)
check("P3b. list_story_artifacts() reads back provenance=None for a normal record, in the "
      "same listing -- both kinds are distinguishable side by side",
      listed_p3["CS-02"].provenance is None)

# P4: provenance has ZERO effect on find_reusable_story_artifact()'s
# reuse decision -- a legacy-migrated artifact (provenance set) and a
# normal artifact (provenance=None) are reusable under the exact same
# is_valid + fingerprint-equality rule, with no special-casing either way.
db_path = _fresh_db_path("p4.db")
st.save_story_artifact("run-p4", "CS-01", "evidence_analyst", {"story_id": "CS-01"},
                        is_valid=True, input_fingerprint="fp-p4", provenance=provenance_p1, db_path=db_path)
reusable_p4_legacy = st.find_reusable_story_artifact("run-p4", "CS-01", "evidence_analyst", "fp-p4", db_path=db_path)
st.save_story_artifact("run-p4", "CS-02", "evidence_analyst", {"story_id": "CS-02"},
                        is_valid=True, input_fingerprint="fp-p4b", db_path=db_path)
reusable_p4_normal = st.find_reusable_story_artifact("run-p4", "CS-02", "evidence_analyst", "fp-p4b", db_path=db_path)
check("P4. a legacy-migrated (provenance set) artifact is reusable under the unmodified rule",
      reusable_p4_legacy is not None)
check("P4b. a normal (provenance=None) artifact is reusable under that identical, unmodified "
      "rule -- proving provenance is informational only, never consulted by the reuse decision",
      reusable_p4_normal is not None)
# A mismatched fingerprint still correctly refuses reuse regardless of provenance.
reusable_p4_mismatch = st.find_reusable_story_artifact("run-p4", "CS-01", "evidence_analyst", "wrong-fp", db_path=db_path)
check("P4c. a legacy-migrated artifact is still correctly refused reuse on fingerprint "
      "mismatch -- provenance grants no exemption from fingerprint discipline",
      reusable_p4_mismatch is None)

# P5: schema migration -- a pre-existing story_artifacts table with no
# provenance_json column (but WITH input_fingerprint, e.g. a db file from
# before this feature) gets the column added automatically, mirroring O1-O3.
db_path = _fresh_db_path("p5.db")
_legacy_conn_p5 = _sqlite3.connect(db_path)
_legacy_conn_p5.executescript("""
CREATE TABLE story_artifacts (
    run_id TEXT NOT NULL, story_id TEXT NOT NULL, stage TEXT NOT NULL,
    payload_json TEXT NOT NULL, is_valid INTEGER NOT NULL,
    validation_errors_json TEXT NOT NULL, failure_reason TEXT, input_fingerprint TEXT,
    saved_at TEXT NOT NULL,
    PRIMARY KEY (run_id, story_id, stage)
);
""")
_legacy_conn_p5.execute(
    "INSERT INTO story_artifacts (run_id, story_id, stage, payload_json, is_valid, "
    "validation_errors_json, failure_reason, input_fingerprint, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
    ("run-legacy-p5", "CS-01", "evidence_analyst", json.dumps({"story_id": "CS-01"}), 1, "[]", None,
     "fp-legacy-p5", "2026-01-01T00:00:00+00:00"),
)
_legacy_conn_p5.commit()
_legacy_conn_p5.close()

migrated_conn_p5 = st.get_connection(db_path)  # must not raise on the pre-existing, column-less table
rec_p5 = st.load_story_artifact("run-legacy-p5", "CS-01", "evidence_analyst", conn=migrated_conn_p5)
migrated_conn_p5.close()
check("P5. get_connection migrates a table missing provenance_json without raising", rec_p5 is not None)
check("P5b. the pre-existing row's data (including its prior input_fingerprint) survives "
      "the migration untouched", rec_p5.payload == {"story_id": "CS-01"} and rec_p5.input_fingerprint == "fp-legacy-p5")
check("P5c. the migrated provenance column reads back as None for the pre-existing row "
      "(never fabricated)", rec_p5.provenance is None)

# ===========================================================================
# Summary
# ===========================================================================
import shutil
shutil.rmtree(_TMPDIR, ignore_errors=True)

failed = [r for r in results if r[1] == "FAIL"]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
if failed:
    print("FAILURES:")
    for name, status, detail in failed:
        print(f"  - {name}: {detail}")
    sys.exit(1)
sys.exit(0)
