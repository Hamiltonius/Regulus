#!/usr/bin/env python3
"""
Compatibility tests for corpus_extractor.py against database schemas that
predate the due_diligence_ran column (e.g. the current node02 production
`alerts` table, discovered during the first live acceptance preflight —
it has analysis_generated_at but no due_diligence_ran column and no
due_diligence_records table at all).

due_diligence_ran must be tri-state: True/False when the schema
establishes it, None when the schema cannot establish it. None must
never be coerced to False, and a missing column must never raise
sqlite3.OperationalError.

Uses two kinds of database:
  - "modern" schema: built via regulus_v3.get_db() against a throwaway
    DB_PATH, same pattern as other tests in this suite.
  - "historical" schema: a hand-built, bare sqlite3 in-memory `alerts`
    table with ONLY the columns a pre-due_diligence_ran database would
    have (including analysis_generated_at, which IS present on the real
    node02 production schema per the reported preflight failure) —
    deliberately NOT going through regulus_v3.get_db() or
    dd_pipeline.ensure_dd_schema, since the point is to simulate a
    database that predates those migrations ever running.

No live Anthropic call is made anywhere in this file.

Run: python3 tests/test_corpus_extractor_schema_compat.py
"""
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
    fd, path = tempfile.mkstemp(suffix=".db", prefix="regulus_compat_test_")
    os.close(fd)
    os.remove(path)
    return path


TMP_DB = fresh_db_path()
os.environ["DB_PATH"] = TMP_DB
import importlib
import regulus_v3 as rv
importlib.reload(rv)
import corpus_extractor as ce

import sqlite3

# ===========================================================================
# 1-2. Modern schema: due_diligence_ran=true/false returns True/False
# ===========================================================================
modern_conn = rv.get_db()
modern_conn.execute(
    "INSERT INTO alerts (doc_hash, document_number, pub_date, title, score, "
    "fetched_at, analysis_generated_at, due_diligence_ran) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    ("h1", "2099-10001", "2099-02-01", "Modern DD-ran doc", 15,
     "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", 1),
)
modern_conn.execute(
    "INSERT INTO alerts (doc_hash, document_number, pub_date, title, score, "
    "fetched_at, analysis_generated_at, due_diligence_ran) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    ("h2", "2099-10002", "2099-02-01", "Modern DD-not-ran doc", 15,
     "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", 0),
)
modern_conn.commit()

modern_corpus = ce.get_corpus(modern_conn, "2099-02-01", "2099-02-01")
by_doc = {o.document_number: o for o in modern_corpus.observations}
check("1. modern schema due_diligence_ran=1 returns True",
      by_doc["2099-10001"].due_diligence_ran is True)
check("2. modern schema due_diligence_ran=0 returns False",
      by_doc["2099-10002"].due_diligence_ran is False)
modern_conn.close()
os.remove(TMP_DB)


# ===========================================================================
# Historical schema fixture: no due_diligence_ran column, no
# due_diligence_records table -- matches the reported node02 production
# shape (analysis_generated_at present, due_diligence_ran absent).
# ===========================================================================
def make_historical_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY,
            doc_hash TEXT UNIQUE,
            document_number TEXT,
            title TEXT,
            agency TEXT,
            pub_date TEXT,
            effective_date TEXT,
            countries TEXT,
            entities TEXT,
            eccns TEXT,
            change_type TEXT,
            summary TEXT,
            primary_source_url TEXT,
            score INTEGER,
            fetched_at TEXT,
            analysis_generated_at TEXT
        )
    """)
    conn.commit()
    return conn


def insert_historical_row(conn, document_number, pub_date, *, title="Historical doc",
                           score=15, analyzed=False, change_type=None, summary=None):
    conn.execute(
        "INSERT INTO alerts (doc_hash, document_number, title, pub_date, score, "
        "fetched_at, analysis_generated_at, change_type, summary) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("hist-" + document_number, document_number, title, pub_date, score,
         "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:00+00:00" if analyzed else None,
         change_type, summary),
    )
    conn.commit()


hist_conn = make_historical_conn()
insert_historical_row(hist_conn, "2099-20001", "2099-03-01", analyzed=True,
                       change_type="other", summary="Historical analyzed summary.")
insert_historical_row(hist_conn, "2099-20002", "2099-03-01", analyzed=False, score=0)

# ===========================================================================
# 3-4. Historical schema: due_diligence_ran is None, no OperationalError
# ===========================================================================
try:
    hist_corpus = ce.get_corpus(hist_conn, "2099-03-01", "2099-03-01")
    raised = False
except sqlite3.OperationalError as e:
    hist_corpus = None
    raised = True

check("4. historical schema extraction does not raise OperationalError", not raised)
if hist_corpus is not None:
    hist_by_doc = {o.document_number: o for o in hist_corpus.observations}
    check("3a. historical schema due_diligence_ran is None (analyzed row)",
          hist_by_doc["2099-20001"].due_diligence_ran is None)
    check("3b. historical schema due_diligence_ran is None (metadata-only row)",
          hist_by_doc["2099-20002"].due_diligence_ran is None)

    # =======================================================================
    # 5. Missing DD-status capability does not affect tier determination
    # =======================================================================
    check("5a. tier=analyzed still correctly determined on historical schema",
          hist_by_doc["2099-20001"].tier == "analyzed")
    check("5b. tier=metadata_only still correctly determined on historical schema",
          hist_by_doc["2099-20002"].tier == "metadata_only")

# ===========================================================================
# 6-7. No schema mutation / no ALTER/INSERT/UPDATE/DELETE/migration
# ===========================================================================
before_cols = {row[1] for row in hist_conn.execute("PRAGMA table_info(alerts)")}
before_count = hist_conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
before_rows = hist_conn.execute("SELECT * FROM alerts ORDER BY id").fetchall()

executed_statements = []
hist_conn.set_trace_callback(lambda sql: executed_statements.append(sql))
ce.get_corpus(hist_conn, "2099-03-01", "2099-03-01")
hist_conn.set_trace_callback(None)

after_cols = {row[1] for row in hist_conn.execute("PRAGMA table_info(alerts)")}
after_count = hist_conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
after_rows = hist_conn.execute("SELECT * FROM alerts ORDER BY id").fetchall()

check("6a. no column added to historical schema (due_diligence_ran still absent)",
      "due_diligence_ran" not in after_cols)
check("6b. column set otherwise unchanged", before_cols == after_cols)
check("6c. row count unchanged", before_count == after_count)
check("6d. row content byte-for-byte unchanged", before_rows == after_rows)

mutating_prefixes = ("ALTER", "INSERT", "UPDATE", "DELETE", "DROP", "CREATE")
mutating_statements = [
    s for s in executed_statements
    if s.strip().upper().split(None, 1)[0] in mutating_prefixes
]
check("7. no ALTER/INSERT/UPDATE/DELETE/DROP/CREATE statement executed by get_corpus",
      mutating_statements == [], str(mutating_statements))

hist_conn.close()

# ===========================================================================
# 8-9. Existing corpus-extractor / Corpus Analyst tests remain valid
# ===========================================================================
import subprocess

this_dir = os.path.dirname(os.path.abspath(__file__))
for other_test in ["test_corpus_extractor.py", "test_corpus_analyst.py"]:
    proc = subprocess.run(
        [sys.executable, os.path.join(this_dir, other_test)],
        capture_output=True, text=True,
    )
    check(f"8/9. {other_test} still passes", proc.returncode == 0,
          proc.stdout[-500:] + proc.stderr[-500:])

# ===========================================================================
# Summary
# ===========================================================================
failed = [r for r in results if r[1] == "FAIL"]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
if failed:
    print("FAILURES:")
    for name, status, detail in failed:
        print(f"  - {name}: {detail}")
    sys.exit(1)
sys.exit(0)
