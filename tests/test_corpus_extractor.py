#!/usr/bin/env python3
"""
Tests for corpus_extractor.py — the deterministic, read-only
reporting-window corpus extractor (Step 1 of the corpus-intelligence
workflow).

Uses ONLY a temporary SQLite database (via regulus_v3.get_db() pointed at
a throwaway DB_PATH, same pattern as tests/test_dd_pipeline.py). Never
touches production bis_watcher.db. No LLM/network call is made anywhere
in this file.

Run: python3 tests/test_corpus_extractor.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == "FAIL" else ""))
    return condition


def fresh_db_path():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="regulus_corpus_test_")
    os.close(fd)
    os.remove(path)
    return path


TMP_DB = fresh_db_path()
os.environ["DB_PATH"] = TMP_DB
import importlib
import regulus_v3 as rv
importlib.reload(rv)
import corpus_extractor as ce

conn = rv.get_db()


def insert_row(conn, document_number, pub_date, *, title="Test doc", score=15,
                agency=None, countries=None, entities=None, eccns=None,
                change_type=None, summary=None, effective_date=None,
                primary_source_url=None, analyzed=False, due_diligence_ran=0):
    """Insert a synthetic alerts row with just the fields this extractor
    reads. `analyzed=True` sets analysis_generated_at (and analysis_model),
    mirroring exactly what regulus_v3.main() does when Stage 1 succeeds —
    tier is derived from this, never from change_type/countries content."""
    doc_hash = "hash-" + document_number
    row = {
        "doc_hash": doc_hash,
        "document_number": document_number,
        "title": title,
        "pub_date": pub_date,
        "effective_date": effective_date,
        "agency": json.dumps(agency) if agency is not None else None,
        "score": score,
        "countries": json.dumps(countries) if countries is not None else None,
        "entities": json.dumps(entities) if entities is not None else None,
        "eccns": json.dumps(eccns) if eccns is not None else None,
        "change_type": change_type,
        "summary": summary,
        "primary_source_url": primary_source_url,
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "due_diligence_ran": due_diligence_ran,
    }
    if analyzed:
        row["analysis_model"] = "claude-sonnet-4-6"
        row["analysis_generated_at"] = "2026-01-01T00:00:00+00:00"
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO alerts ({cols}) VALUES ({placeholders})", list(row.values()))
    conn.commit()
    return doc_hash


# ===========================================================================
# A. Inclusive reporting-window boundaries
# ===========================================================================
insert_row(conn, "2026-30001", "2026-09-14")  # == start_date
insert_row(conn, "2026-30002", "2026-10-05")  # == end_date

corpus = ce.get_corpus(conn, "2026-09-14", "2026-10-05")
doc_numbers = {o.document_number for o in corpus.observations}
check("A1. start_date boundary included", "2026-30001" in doc_numbers)
check("A2. end_date boundary included", "2026-30002" in doc_numbers)

# ===========================================================================
# B. Records outside the requested window are excluded
# ===========================================================================
insert_row(conn, "2026-30003", "2026-09-13")  # one day before window
insert_row(conn, "2026-30004", "2026-10-06")  # one day after window

corpus = ce.get_corpus(conn, "2026-09-14", "2026-10-05")
doc_numbers = {o.document_number for o in corpus.observations}
check("B1. record before window excluded", "2026-30003" not in doc_numbers)
check("B2. record after window excluded", "2026-30004" not in doc_numbers)

# ===========================================================================
# C. Both analyzed and metadata-only records are returned
# ===========================================================================
insert_row(conn, "2026-30005", "2026-09-20", analyzed=True, change_type="other",
           summary="Analyzed summary.")
insert_row(conn, "2026-30006", "2026-09-20", analyzed=False, score=0)

corpus = ce.get_corpus(conn, "2026-09-14", "2026-10-05")
by_doc = {o.document_number: o for o in corpus.observations}
check("C1. analyzed record present with tier=analyzed",
      by_doc["2026-30005"].tier == "analyzed")
check("C2. metadata-only record present with tier=metadata_only",
      by_doc["2026-30006"].tier == "metadata_only")

# ===========================================================================
# D. Low-score records are NOT automatically excluded
# ===========================================================================
check("D1. score=0 record still returned", "2026-30006" in by_doc)
check("D2. score=0 value preserved, not dropped/altered", by_doc["2026-30006"].score == 0)

# ===========================================================================
# E. Stable deterministic ordering (publication_date ASC, document_number ASC)
# ===========================================================================
insert_row(conn, "2026-30099", "2026-09-20")  # same date as 30005/30006, sorts last by doc#
insert_row(conn, "2026-30000", "2026-09-20")  # same date, sorts first by doc#

corpus = ce.get_corpus(conn, "2026-09-20", "2026-09-20")
ordered_nums = [o.document_number for o in corpus.observations]
check("E1. same-date records ordered by document_number ASC",
      ordered_nums == sorted(ordered_nums),
      detail=str(ordered_nums))

corpus_full = ce.get_corpus(conn, "2026-09-14", "2026-10-05")
pub_dates = [o.publication_date for o in corpus_full.observations]
check("E2. overall ordering is non-decreasing by publication_date",
      pub_dates == sorted(pub_dates), detail=str(pub_dates))

# ===========================================================================
# F. Null analytical fields remain null (not invented/coerced)
# ===========================================================================
insert_row(conn, "2026-30007", "2026-09-21", analyzed=False)  # no countries/entities/eccns/change_type/summary
corpus = ce.get_corpus(conn, "2026-09-14", "2026-10-05")
rec = next(o for o in corpus.observations if o.document_number == "2026-30007")
check("F1. countries stays None", rec.countries is None)
check("F2. entities stays None", rec.entities is None)
check("F3. eccns stays None", rec.eccns is None)
check("F4. change_type stays None", rec.change_type is None)
check("F5. summary stays None", rec.summary is None)

# ===========================================================================
# G. Historical/legacy change_type values are returned unchanged
# ===========================================================================
LEGACY_VALUES = [
    "Notice – SDN List Addition",
    "Sanctions Designation / SDN List Addition",
    "Rule - Publication of General Licenses",
    "correction",
]
for i, legacy in enumerate(LEGACY_VALUES):
    insert_row(conn, f"2026-301{i:02d}", "2026-09-22", analyzed=True, change_type=legacy)
corpus = ce.get_corpus(conn, "2026-09-14", "2026-10-05")
legacy_out = {o.document_number: o.change_type for o in corpus.observations
              if o.document_number.startswith("2026-301")}
check("G1. all legacy change_type strings preserved verbatim, untouched",
      all(legacy_out[f"2026-301{i:02d}"] == legacy for i, legacy in enumerate(LEGACY_VALUES)),
      detail=str(legacy_out))

# ===========================================================================
# H. Calling the extractor performs no database mutation
# ===========================================================================
before_count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
before_rows = conn.execute("SELECT * FROM alerts ORDER BY id").fetchall()
ce.get_corpus(conn, "2026-09-14", "2026-10-05")
ce.get_corpus(conn, "2026-01-01", "2026-12-31")
after_count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
after_rows = conn.execute("SELECT * FROM alerts ORDER BY id").fetchall()
check("H1. row count unchanged after calling get_corpus", before_count == after_count)
check("H2. row content byte-for-byte unchanged after calling get_corpus", before_rows == after_rows)

# ===========================================================================
# I. Stage 1 / Stage 2 / Stage 3 / network/API functions are not invoked
# ===========================================================================
import dd_pipeline as ddp


def _boom(*a, **kw):
    raise AssertionError("corpus_extractor must never call this function")


_orig_stage2 = ddp.call_anthropic_stage2
_orig_stage3 = ddp.call_anthropic_stage3
_orig_needs_dd = ddp.needs_due_diligence
_orig_analyze = rv.analyze_with_llm
ddp.call_anthropic_stage2 = _boom
ddp.call_anthropic_stage3 = _boom
ddp.needs_due_diligence = _boom
rv.analyze_with_llm = _boom
try:
    corpus = ce.get_corpus(conn, "2026-09-14", "2026-10-05")
    check("I1. get_corpus completes with Stage1/2/3 functions patched to raise if called",
          corpus.observation_count > 0)
finally:
    ddp.call_anthropic_stage2 = _orig_stage2
    ddp.call_anthropic_stage3 = _orig_stage3
    ddp.needs_due_diligence = _orig_needs_dd
    rv.analyze_with_llm = _orig_analyze

check("I2. corpus_extractor module does not import dd_pipeline",
      "dd_pipeline" not in ce.__dict__ and not hasattr(ce, "dd_pipeline"))
check("I3. corpus_extractor module does not import regulus_v3",
      not hasattr(ce, "regulus_v3") and not hasattr(ce, "rv"))
check("I4. corpus_extractor module does not import requests",
      "requests" not in dir(ce))

# ===========================================================================
# J. due_diligence_ran is accurate
# ===========================================================================
insert_row(conn, "2026-30201", "2026-09-23", analyzed=True, due_diligence_ran=1)
insert_row(conn, "2026-30202", "2026-09-23", analyzed=True, due_diligence_ran=0)
corpus = ce.get_corpus(conn, "2026-09-14", "2026-10-05")
by_doc = {o.document_number: o for o in corpus.observations}
check("J1. due_diligence_ran=1 row reported as True",
      by_doc["2026-30201"].due_diligence_ran is True)
check("J2. due_diligence_ran=0 row reported as False",
      by_doc["2026-30202"].due_diligence_ran is False)

# ===========================================================================
# Output shape sanity (section 9 of the spec)
# ===========================================================================
corpus = ce.get_corpus(conn, "2026-09-14", "2026-10-05")
d = corpus.to_dict()
check("K1. to_dict() has reporting_period/observation_count/observations keys",
      set(d.keys()) == {"reporting_period", "observation_count", "observations"})
check("K2. reporting_period echoes requested start/end",
      d["reporting_period"] == {"start": "2026-09-14", "end": "2026-10-05"})
check("K3. observation_count matches len(observations)",
      d["observation_count"] == len(d["observations"]))
check("K4. no story/cluster/hypothesis/materiality keys anywhere in output",
      not any(k in d for k in ("stories", "clusters", "hypotheses", "materiality"))
      and all(set(o.keys()) == {
          "document_number", "publication_date", "effective_date", "title", "agency",
          "score", "countries", "entities", "eccns", "change_type", "summary",
          "primary_source_url", "tier", "due_diligence_ran",
      } for o in d["observations"]))
check("K5. to_json() round-trips through json.loads", json.loads(corpus.to_json()) == d)

# ===========================================================================
# Summary
# ===========================================================================
conn.close()
os.remove(TMP_DB)

failed = [r for r in results if r[1] == "FAIL"]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
if failed:
    print("FAILURES:")
    for name, status, detail in failed:
        print(f"  - {name}: {detail}")
    sys.exit(1)
sys.exit(0)
