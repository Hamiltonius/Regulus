#!/usr/bin/env python3
"""
Tests for pattern_variance.py (Pattern/Variance v1).

Zero network (requests patched to raise); SQLite only in a throwaway tempdir.
Run: python3 tests/test_pattern_variance.py
"""
import copy
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import requests

def _no_network(*a, **k):
    raise AssertionError("pattern_variance tests must never make a network call")
requests.post = _no_network
requests.get = _no_network

import pattern_variance as pv

results = []

def check(name, cond, detail=""):
    results.append((name, "PASS" if cond else "FAIL"))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return cond

def approx(a, b, eps=1e-9):
    return a is not None and abs(a - b) < eps

AG = [{"name": "State Department", "id": 1, "parent_id": None}]
SYN = {"reporting_period": {"start": "2026-09-14", "end": "2026-09-30"}, "observations": [
    {"document_number": "2026-00001", "publication_date": "2026-09-14", "effective_date": "2026-09-14",
     "title": "Alpha rule", "summary": "alpha beta gamma delta", "agency": AG, "score": 10,
     "countries": ["Syria"], "entities": [], "eccns": [], "tier": "analyzed"},
    {"document_number": "2026-00002", "publication_date": "2026-09-16", "effective_date": None,
     "title": "Alpha notice", "summary": "alpha beta epsilon zeta", "agency": AG, "score": 4,
     "countries": ["syria", "Iran"], "entities": [], "eccns": [], "tier": "metadata_only"},
    {"document_number": "2026-00003", "publication_date": "2026-09-20", "effective_date": "2026-09-25",
     "title": "Other thing", "summary": "completely different text citing 2026-00001 and 15 CFR 744.21 and Executive Order 14312",
     "agency": [{"name": "Commerce Department", "id": 2, "parent_id": None}], "score": None,
     "countries": [], "entities": [], "eccns": [], "tier": "analyzed"},
    {"document_number": "2026-00004", "publication_date": "2026-09-21", "title": "Cites 15 CFR 744.21 too",
     "summary": "Executive Order 14312 again", "agency": AG, "score": 2, "countries": ["Iran"],
     "entities": [], "eccns": [], "tier": "metadata_only"},
    {"document_number": None, "publication_date": "2026-09-21", "title": "no number"},
    {"document_number": "2026-00006", "publication_date": "garbage", "title": "bad date"},
]}

# ---- hand-computed values
r = pv.compute_observation(SYN, subject_kind="story", subject_id="S", doc_numbers=["2026-00001", "2026-00002", "2026-00003", "2026-00099"])
m, t = r["metrics_json"], r["telemetry"]
check("counts: n_docs=3, agencies=2, countries=2 (case-insensitive), with_effective=2",
      m["counts"]["n_docs"] == 3 and m["counts"]["n_agencies_distinct"] == 2
      and m["counts"]["n_countries_distinct"] == 2 and m["counts"]["n_with_effective_date"] == 2, str(m["counts"]))
check("temporal: span 6 days, distinct dates 3, gaps [2,4]",
      m["temporal"]["span_days"] == 6 and m["temporal"]["n_distinct_pub_dates"] == 3
      and m["temporal"]["gap_days"]["mean"] == 3 and m["temporal"]["gap_days"]["max"] == 4)
check("effective-minus-publication: values [0,5] -> mean 2.5", approx(m["temporal"]["effective_minus_publication_days"]["mean"], 2.5))
check("shared countries (case-insensitive): Syria in 2 docs", m["shared"]["countries"]["n_shared_by_2plus_docs"] == 1
      and m["shared"]["countries"]["top"][0]["n_docs"] == 2)
top = m["top_pairs_by_summary_jaccard"][0]
check("summary Jaccard of docs 1,2 = 2/6 (hand-computed), days_apart=2",
      (top["doc_a"], top["doc_b"]) == ("2026-00001", "2026-00002") and approx(top["summary_jaccard"], 1 / 3) and top["days_apart"] == 2)
check("title Jaccard of 'Alpha rule' vs 'Alpha notice' = 1/3", approx(top["title_jaccard"], 1 / 3))
check("score_diff 6 for docs 1,2", top["score_diff"] == 6)
check("shared_countries=1 for docs 1,2", top["shared_countries"] == 1)
check("explicit identifiers: intra-set FR citation 3->1 detected",
      m["identifiers"]["intra_set_fr_doc_citations"] == 1 and m["identifiers"]["edges"] == [{"from": "2026-00003", "to": "2026-00001"}])
full = pv.compute_observation(SYN)
check("CFR and EO cites shared by 2 docs are detected (whole corpus)",
      full["metrics_json"]["shared"]["cfr"]["top"][0] == {"item": "15 CFR 744.21", "n_docs": 2}
      and full["metrics_json"]["shared"]["eo"]["top"][0] == {"item": "EO 14312", "n_docs": 2})
check("telemetry: unknown requested doc recorded as error, not crash", t["n_errors"] == 1 and "2026-00099" in t["errors"][0])
check("telemetry: whole-corpus run skips rows with missing number / bad date",
      full["telemetry"]["documents_skipped"] == 2 and full["telemetry"]["documents_processed"] == 4
      and {s["reason"] for s in full["telemetry"]["skipped"]} == {"missing document_number", "missing/unparsable publication_date"})
check("telemetry has runtime, algorithm name/version and timestamp",
      t["runtime_ms"] >= 0 and r["algorithm_name"] == "pattern_variance" and r["algorithm_version"] == "1.0" and r["computed_at"])
check("metrics_json is JSON-serializable", bool(json.dumps(r["metrics_json"])))
flat = pv.flatten_metrics(m)
check("flatten_metrics yields dotted scalar keys", flat["counts.n_docs"] == 3 and flat["temporal.span_days"] == 6 and all(not isinstance(v, (list, dict)) for v in flat.values()))

# ---- determinism / idempotency
r2 = pv.compute_observation(copy.deepcopy(SYN), subject_kind="story", subject_id="S", doc_numbers=["2026-00001", "2026-00002", "2026-00003", "2026-00099"])
check("same input => same observation_id, fingerprint and metrics", r["observation_id"] == r2["observation_id"]
      and r["input_fingerprint"] == r2["input_fingerprint"] and r["metrics_json"] == r2["metrics_json"])
chg = copy.deepcopy(SYN); chg["observations"][0]["summary"] = "different words here"
r3 = pv.compute_observation(chg, subject_kind="story", subject_id="S", doc_numbers=["2026-00001", "2026-00002", "2026-00003", "2026-00099"])
check("changed document text => different observation_id", r3["observation_id"] != r["observation_id"])

# ---- baselines
check("no in-corpus baseline when < 30 corpus pairs", m["baseline_corpus"]["available"] is False)
check("no historical baseline with no history", m["baseline_history"]["available"] is False)

# real fixture: baseline + percentile
fx = os.path.join(HERE, "fixtures")
corpus = json.load(open(os.path.join(fx, "corpus_acceptance_3.json")))
stories = [{"story_id": s["story_id"], "supporting_document_numbers": s["supporting_document_numbers"]}
           for s in json.load(open(os.path.join(fx, "corpus_analyst_acceptance_3.json")))["candidate_stories"]]
recs = pv.compute_all(corpus, stories, run_id="fx")
check("fixture: 1 corpus + 11 story records, none with errors or skips",
      len(recs) == 12 and all(x["telemetry"]["n_errors"] == 0 and x["telemetry"]["documents_skipped"] == 0 for x in recs))
cs01 = next(x for x in recs if x["subject_id"] == "CS-01")
b = cs01["metrics_json"]["baseline_corpus"]
check("fixture CS-01: in-corpus baseline available (>=30 pairs with a summary) and percentile in [0,100]",
      b["available"] and 30 <= b["summary_jaccard"]["baseline_n_pairs"] <= 3741
      and 0 <= b["summary_jaccard"]["percentile_of_subject_mean"] <= 100)
check("fixture CS-01: 5 docs, 10 pairs, 5 distinct publication dates",
      cs01["metrics_json"]["counts"]["n_docs"] == 5 and cs01["telemetry"]["pairs_computed"] == 10
      and cs01["metrics_json"]["temporal"]["n_distinct_pub_dates"] == 5)
check("fixture corpus record: 87 docs, 3741 pairs", recs[0]["metrics_json"]["counts"]["n_docs"] == 87 and recs[0]["telemetry"]["pairs_computed"] == 3741)

# historical baseline
hist = []
for i in range(12):
    h = copy.deepcopy(cs01)
    h["metrics_json"]["counts"]["n_docs"] = 3 + (i % 4)       # 3..6
    hist.append(h)
rh = pv.compute_observation(corpus, subject_kind="story", subject_id="CS-01",
                            doc_numbers=json.load(open(os.path.join(fx, "corpus_analyst_acceptance_3.json")))["candidate_stories"][0]["supporting_document_numbers"],
                            history=hist)
hb = rh["metrics_json"]["baseline_history"]
check("historical baseline computed from 12 prior records (mean 4.5, percentile of 5 = 75)",
      hb["available"] and approx(hb["metrics"]["counts.n_docs"]["prior_mean"], 4.5)
      and approx(hb["metrics"]["counts.n_docs"]["percentile"], 75.0), str(hb["metrics"].get("counts.n_docs")))
rh9 = pv.compute_observation(corpus, subject_kind="story", subject_id="CS-01", doc_numbers=["2026-18918", "2026-19161"], history=hist[:9])
check("historical baseline withheld below 10 prior observations", rh9["metrics_json"]["baseline_history"]["available"] is False)

# ---- storage
with tempfile.TemporaryDirectory() as td:
    db = os.path.join(td, "pv.db")
    for x in recs:
        pv.save_observation(db, x)
    n1 = len(pv.list_observations(db))
    for x in recs:
        pv.save_observation(db, x)
    n2 = len(pv.list_observations(db))
    check("storage: 12 rows, idempotent on re-save", n1 == 12 and n2 == 12, f"{n1},{n2}")
    back = pv.list_observations(db, "story")
    check("storage: story filter returns 11; metrics_json round-trips", len(back) == 11
          and next(x for x in back if x["subject_id"] == "CS-01")["metrics_json"] == cs01["metrics_json"])
    check("report renders identically from stored record and in-memory record",
          pv.render_report(next(x for x in back if x["subject_id"] == "CS-01")) == pv.render_report(cs01))
    # CLI end-to-end
    rd = os.path.join(td, "reports")
    rc = pv.main(["--corpus-json", os.path.join(fx, "corpus_acceptance_3.json"),
                  "--stories-json", os.path.join(fx, "corpus_analyst_acceptance_3.json"),
                  "--db", os.path.join(td, "cli.db"), "--report-dir", rd, "--run-id", "cli"])
    check("CLI: exit 0, 12 reports written, 12 rows stored",
          rc == 0 and len(os.listdir(rd)) == 12 and len(pv.list_observations(os.path.join(td, "cli.db"))) == 12)

# ---- report language: measurement wording only
banned = re.compile(r"coordinat|caus|intent|motiv|purpose|because|driven|due to|reflects|suggests|indicates|implies|deliberate", re.I)
allreports = "\n".join(pv.render_report(x) for x in recs)
check("report text contains no coordination/causation/intent/motive/purpose wording", not banned.search(allreports),
      str(banned.search(allreports)))
check("report contains the measurement sections",
      all(s in allreports for s in ("TELEMETRY", "COUNTS", "TEMPORAL", "SHARED EXPLICIT ITEMS", "TEXT OVERLAP", "BASELINES")))

# ---- isolation / scope
src = open(os.path.join(HERE, "..", "pattern_variance.py")).read()
imports = set(re.findall(r"^(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", src, re.M))
std_ok = {"argparse", "hashlib", "itertools", "json", "os", "re", "sqlite3", "statistics", "sys", "time", "datetime"}
check("imports are stdlib only (no ML libs, no pipeline modules)", imports <= std_ok, str(imports - std_ok))
check("no intelligence_store / orchestrator / analyst import", not re.search(r"intelligence_store|regulus_orchestrator|corpus_analyst|evidence_analyst|intelligence_editor", "\n".join(re.findall(r"^(?:import|from).*$", src, re.M))))

failed = [n for n, st in results if st == "FAIL"]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
