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
