#!/usr/bin/env python3
"""
pattern_variance.py -- Pattern/Variance v1: cheap, deterministic, offline
quantitative observations about a set of corpus records.

Purpose: preserve clean numeric/categorical measurements that later
analysis (including ML) can use. This module does NOT do ML, makes no
model/API/network call, reads no database except an optional history of
its own prior observations, and is isolated from the live pipeline (no
orchestrator, analyst, editor or intelligence_store dependency).

Observations are MECHANICAL: set sizes, overlaps, date differences, text
token overlap, explicit identifier matches, baseline percentiles. This
module never states or implies coordination, causation, intent, motive or
policy purpose; the report template uses only measurement wording.

Input: the Corpus.to_dict()-shaped dict (observations with document_number,
publication_date, effective_date, title, agency, score, countries,
entities, eccns, summary, tier ...). A "subject" is the whole corpus or a
set of document numbers (e.g. a candidate story's supporting documents).

Output record (one per subject):
    observation_id, run_id, subject_kind, subject_id, algorithm_name,
    algorithm_version, computed_at, input_fingerprint,
    metrics_json (nested dict, flexible), telemetry (dict)

Usage:
    python3 pattern_variance.py --corpus-json corpus.json [--stories-json analysis.json]
        [--run-id R] [--db PATH] [--report-dir DIR]
"""
import argparse
import hashlib
import itertools
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from datetime import date, datetime, timezone

ALGORITHM_NAME = "pattern_variance"
ALGORITHM_VERSION = "1.0"
MIN_BASELINE_PAIRS = 30       # corpus pair baseline needs at least this many pairs
MIN_HISTORY_OBS = 10          # historical baseline needs at least this many prior observations
TOP_PAIRS = 5
TOP_SHARED = 10

_FR_DOC = re.compile(r"\b(20\d{2}-\d{4,5})\b")
_CFR = re.compile(r"\b(\d{1,2}) CFR (?:Parts? |§+ ?)?(\d+(?:\.\d+)?)", re.I)
_EO = re.compile(r"\b(?:E\.O\.|EO|Executive Order)\s*(\d{4,5})\b", re.I)
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset("the of and to in for on a an by or with as at is are be from that this its it "
                  "under any has have not no all may their such other which into than also".split())

HISTORY_METRICS = ("counts.n_docs", "temporal.span_days", "text.summary_jaccard_mean",
                   "text.title_jaccard_mean", "scores.mean")


# ------------------------------------------------------------------ helpers

def _canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _pdate(s):
    try:
        y, m, d = (int(x) for x in str(s)[:10].split("-"))
        return date(y, m, d)
    except Exception:
        return None


def _tokens(text):
    return [t for t in _TOKEN.findall((text or "").lower()) if len(t) >= 3 and t not in _STOP]


def _jaccard(a: set, b: set):
    if not a and not b:
        return None
    return len(a & b) / len(a | b)


def _stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0, "mean": None, "std": None, "min": None, "max": None, "median": None}
    return {"n": len(vals), "mean": statistics.fmean(vals),
            "std": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
            "min": min(vals), "max": max(vals), "median": statistics.median(vals)}


def _percentile(values, x):
    """Percent of baseline values <= x (None if no values)."""
    if not values or x is None:
        return None
    return 100.0 * sum(1 for v in values if v <= x) / len(values)


def _agency_names(agency):
    """Return (leaf_names, parent_ids) from the Federal Register agency list
    (list of dicts) or a plain string/list of strings."""
    names, parents = [], set()
    if isinstance(agency, str):
        return [agency.strip().casefold()] if agency.strip() else [], parents
    for a in agency or []:
        if isinstance(a, dict):
            n = (a.get("name") or a.get("raw_name") or "").strip().casefold()
            if n:
                names.append(n)
            if a.get("parent_id") is not None:
                parents.add(a["parent_id"])
        elif isinstance(a, str) and a.strip():
            names.append(a.strip().casefold())
    return names, parents


def _norm_list(x):
    if not x:
        return []
    if isinstance(x, str):
        x = [x]
    return [str(i).strip() for i in x if str(i).strip()]


def _prep(o):
    """Per-document derived fields. Returns (doc dict, None) or (None, skip_reason)."""
    num = o.get("document_number")
    if not num:
        return None, "missing document_number"
    pub = _pdate(o.get("publication_date"))
    if pub is None:
        return None, "missing/unparsable publication_date"
    eff = _pdate(o.get("effective_date"))
    names, _ = _agency_names(o.get("agency"))
    text = f"{o.get('title') or ''} {o.get('summary') or ''}"
    title_t = set(_tokens(o.get("title")))
    summ_words = _TOKEN.findall((o.get("summary") or "").lower())
    return {
        "num": num, "pub": pub, "eff": eff, "score": o.get("score"), "tier": o.get("tier"),
        "agencies": set(names),
        "countries": {c.casefold(): c for c in _norm_list(o.get("countries"))},
        "entities": {c.casefold(): c for c in _norm_list(o.get("entities"))},
        "eccns": {c.casefold(): c for c in _norm_list(o.get("eccns"))},
        "title_tok": title_t, "summary_tok": set(_tokens(o.get("summary"))),
        "trigrams": set(zip(summ_words, summ_words[1:], summ_words[2:])),
        "fr_cited": set(_FR_DOC.findall(text)) - {num},
        "cfr": {f"{a} CFR {b}" for a, b in _CFR.findall(text)},
        "eo": {f"EO {n}" for n in _EO.findall(text)},
    }, None


def _pair_metrics(a, b):
    return {
        "doc_a": a["num"], "doc_b": b["num"],
        "days_apart": abs((a["pub"] - b["pub"]).days),
        "title_jaccard": _jaccard(a["title_tok"], b["title_tok"]),
        "summary_jaccard": _jaccard(a["summary_tok"], b["summary_tok"]),
        "summary_trigram_jaccard": _jaccard(a["trigrams"], b["trigrams"]),
        "shared_agencies": len(a["agencies"] & b["agencies"]),
        "shared_countries": len(set(a["countries"]) & set(b["countries"])),
        "shared_entities": len(set(a["entities"]) & set(b["entities"])),
        "shared_eccns": len(set(a["eccns"]) & set(b["eccns"])),
        "shared_cfr": len(a["cfr"] & b["cfr"]),
        "shared_eo": len(a["eo"] & b["eo"]),
        "score_diff": (abs(a["score"] - b["score"]) if isinstance(a["score"], (int, float))
                       and isinstance(b["score"], (int, float)) else None),
    }


def _shared_items(docs, key):
    """Items present in >=2 documents: [{item, n_docs}], top N, plus summary numbers."""
    counts, display = {}, {}
    for d in docs:
        items = d[key]
        for k in (items if isinstance(items, (set, frozenset)) else items.keys()):
            counts[k] = counts.get(k, 0) + 1
            display.setdefault(k, items[k] if isinstance(items, dict) else k)
    shared = sorted(((display[k], n) for k, n in counts.items() if n >= 2), key=lambda t: (-t[1], str(t[0])))
    n_with = sum(1 for d in docs if any(counts[k] >= 2 for k in (d[key] if isinstance(d[key], (set, frozenset)) else d[key].keys())))
    return {"n_distinct": len(counts), "n_shared_by_2plus_docs": len(shared),
            "pct_docs_with_a_shared_item": (100.0 * n_with / len(docs)) if docs else None,
            "top": [{"item": i, "n_docs": n} for i, n in shared[:TOP_SHARED]]}


# ------------------------------------------------------------------ core

def corpus_pair_baseline(docs):
    """Pair-level distributions over a whole corpus, used as the in-corpus baseline."""
    vals = {"summary_jaccard": [], "title_jaccard": [], "days_apart": []}
    for a, b in itertools.combinations(docs, 2):
        pm = _pair_metrics(a, b)
        for k in vals:
            if pm[k] is not None:
                vals[k].append(pm[k])
    return vals


def compute_observation(corpus: dict, *, subject_kind="corpus", subject_id="corpus",
                        doc_numbers=None, run_id=None, history=None, _baseline_vals=None) -> dict:
    """Compute one observation record. `history` = list of prior records (same
    algorithm/subject_kind) for the historical baseline; may be None/empty."""
    t0 = time.perf_counter()
    all_obs = corpus.get("observations", [])
    errors, skipped = [], []
    prepped_all = {}
    for o in all_obs:
        d, why = _prep(o)
        if d is None:
            skipped.append({"document_number": o.get("document_number"), "reason": why})
        else:
            prepped_all[d["num"]] = d
    if doc_numbers is None:
        scope = list(prepped_all.values())
        in_scope = len(all_obs)
    else:
        scope = []
        in_scope = len(doc_numbers)
        for n in doc_numbers:
            if n in prepped_all:
                scope.append(prepped_all[n])
            elif not any(o.get("document_number") == n for o in all_obs):
                errors.append(f"document {n} not present in corpus")
    scope.sort(key=lambda d: (d["pub"], d["num"]))
    n = len(scope)

    m = {}
    # counts
    leaf = set().union(*[d["agencies"] for d in scope]) if scope else set()
    m["counts"] = {
        "n_docs": n,
        "n_agencies_distinct": len(leaf),
        "n_countries_distinct": len(set().union(*[set(d["countries"]) for d in scope])) if scope else 0,
        "n_entities_distinct": len(set().union(*[set(d["entities"]) for d in scope])) if scope else 0,
        "n_eccns_distinct": len(set().union(*[set(d["eccns"]) for d in scope])) if scope else 0,
        "n_with_effective_date": sum(1 for d in scope if d["eff"]),
        "n_analyzed_tier": sum(1 for d in scope if d["tier"] == "analyzed"),
        "agencies": sorted(leaf),
    }
    # temporal
    dates = [d["pub"] for d in scope]
    uniq = sorted(set(dates))
    gaps = [(b - a).days for a, b in zip(uniq, uniq[1:])]
    lags = [(d["eff"] - d["pub"]).days for d in scope if d["eff"]]
    m["temporal"] = {
        "pub_date_min": uniq[0].isoformat() if uniq else None,
        "pub_date_max": uniq[-1].isoformat() if uniq else None,
        "span_days": (uniq[-1] - uniq[0]).days if uniq else None,
        "n_distinct_pub_dates": len(uniq),
        "gap_days": _stats(gaps),
        "effective_minus_publication_days": _stats(lags),
    }
    # shared items
    m["shared"] = {k: _shared_items(scope, k) for k in ("agencies", "countries", "entities", "eccns", "cfr", "eo")}
    # explicit identifiers
    nums = {d["num"] for d in scope}
    edges = [(d["num"], c) for d in scope for c in sorted(d["fr_cited"] & nums)]
    cited_elsewhere = Counter_like([c for _, c in edges])
    m["identifiers"] = {
        "intra_set_fr_doc_citations": len(edges),
        "docs_citing_another_doc_in_set": len({a for a, _ in edges}),
        "docs_cited_by_another_doc_in_set": len(cited_elsewhere),
        "edges": [{"from": a, "to": b} for a, b in edges[:20]],
        "fr_doc_numbers_cited_by_2plus_docs": sorted(k for k, v in Counter_like(
            [x for d in scope for x in d["fr_cited"]]).items() if v >= 2)[:TOP_SHARED],
    }
    # pairs / text similarity
    pairs = [_pair_metrics(a, b) for a, b in itertools.combinations(scope, 2)]
    sj = [p["summary_jaccard"] for p in pairs if p["summary_jaccard"] is not None]
    tj = [p["title_jaccard"] for p in pairs if p["title_jaccard"] is not None]
    gj = [p["summary_trigram_jaccard"] for p in pairs if p["summary_trigram_jaccard"] is not None]
    da = [p["days_apart"] for p in pairs]
    m["text"] = {
        "n_pairs": len(pairs),
        "summary_jaccard_mean": _stats(sj)["mean"], "summary_jaccard_max": _stats(sj)["max"],
        "title_jaccard_mean": _stats(tj)["mean"], "title_jaccard_max": _stats(tj)["max"],
        "summary_trigram_jaccard_mean": _stats(gj)["mean"], "summary_trigram_jaccard_max": _stats(gj)["max"],
        "pairwise_days_apart": _stats(da),
    }
    top = sorted(pairs, key=lambda p: (-(p["summary_jaccard"] or 0), p["doc_a"], p["doc_b"]))[:TOP_PAIRS]
    m["top_pairs_by_summary_jaccard"] = top
    # scores
    sc = _stats([d["score"] for d in scope if isinstance(d["score"], (int, float))])
    m["scores"] = {"n": sc["n"], "mean": sc["mean"], "std": sc["std"], "min": sc["min"], "max": sc["max"]}

    # in-corpus baseline (only meaningful for a strict subset)
    base = {"available": False, "reason": None}
    if subject_kind != "corpus" and n >= 2:
        vals = _baseline_vals if _baseline_vals is not None else corpus_pair_baseline(list(prepped_all.values()))
        if len(vals["summary_jaccard"]) >= MIN_BASELINE_PAIRS:
            def blk(name, x):
                v = vals[name]
                s = _stats(v)
                return {"subject_mean": x, "baseline_pair_mean": s["mean"], "baseline_pair_std": s["std"],
                        "baseline_n_pairs": s["n"], "percentile_of_subject_mean": _percentile(v, x)}
            base = {"available": True,
                    "summary_jaccard": blk("summary_jaccard", m["text"]["summary_jaccard_mean"]),
                    "title_jaccard": blk("title_jaccard", m["text"]["title_jaccard_mean"]),
                    "days_apart": blk("days_apart", m["text"]["pairwise_days_apart"]["mean"])}
        else:
            base["reason"] = f"fewer than {MIN_BASELINE_PAIRS} corpus pairs"
    else:
        base["reason"] = "whole-corpus subject or fewer than 2 documents"
    m["baseline_corpus"] = base

    # historical baseline from prior records of the same kind
    hist = {"available": False, "n_prior": len(history or []), "metrics": {}}
    if history and len(history) >= MIN_HISTORY_OBS:
        hist["available"] = True
        for key in HISTORY_METRICS:
            cur = _get_path(m, key)
            prior = [v for v in (_get_path(h.get("metrics_json", {}), key) for h in history) if isinstance(v, (int, float))]
            if isinstance(cur, (int, float)) and len(prior) >= MIN_HISTORY_OBS:
                s = _stats(prior)
                hist["metrics"][key] = {"current": cur, "prior_n": s["n"], "prior_mean": s["mean"],
                                        "prior_std": s["std"], "percentile": _percentile(prior, cur),
                                        "z": ((cur - s["mean"]) / s["std"]) if s["std"] else None}
    else:
        hist["reason"] = f"fewer than {MIN_HISTORY_OBS} prior observations"
    m["baseline_history"] = hist

    fingerprint = _sha(_canon({
        "subject_kind": subject_kind, "subject_id": subject_id,
        "docs": [[o.get("document_number"), o.get("publication_date"), o.get("effective_date"),
                  o.get("title"), o.get("summary"), o.get("score"), o.get("countries"),
                  o.get("entities"), o.get("eccns"), _agency_names(o.get("agency"))[0], o.get("tier")]
                 for o in all_obs if o.get("document_number") in nums],
        "period": corpus.get("reporting_period")}))
    telemetry = {
        "runtime_ms": round((time.perf_counter() - t0) * 1000, 3),
        "documents_in_scope": in_scope, "documents_processed": n,
        "documents_skipped": len(skipped), "skipped": skipped[:20],
        "errors": errors, "n_errors": len(errors),
        "pairs_computed": len(pairs),
    }
    return {
        "observation_id": _sha(_canon([ALGORITHM_NAME, ALGORITHM_VERSION, subject_kind, subject_id, fingerprint])),
        "run_id": run_id, "subject_kind": subject_kind, "subject_id": subject_id,
        "algorithm_name": ALGORITHM_NAME, "algorithm_version": ALGORITHM_VERSION,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "input_fingerprint": fingerprint, "metrics_json": m, "telemetry": telemetry,
    }


def Counter_like(items):
    d = {}
    for i in items:
        d[i] = d.get(i, 0) + 1
    return d


def _get_path(d, dotted):
    cur = d
    for k in dotted.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def flatten_metrics(metrics: dict, prefix="") -> dict:
    """Dotted-key view of the scalar leaves of metrics_json (numbers/str/bool/None),
    for later tabular use. Lists are skipped."""
    out = {}
    for k, v in metrics.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten_metrics(v, key + "."))
        elif isinstance(v, (int, float, str, bool)) or v is None:
            out[key] = v
    return out


def compute_all(corpus: dict, stories=None, *, run_id=None, history_by_kind=None) -> list:
    """One record for the whole corpus plus one per story (stories =
    [{"story_id", "supporting_document_numbers"}])."""
    history_by_kind = history_by_kind or {}
    recs = [compute_observation(corpus, subject_kind="corpus", subject_id="corpus", run_id=run_id,
                                history=history_by_kind.get("corpus"))]
    docs = [d for d, _ in (_prep(o) for o in corpus.get("observations", [])) if d]
    base_vals = corpus_pair_baseline(docs)
    for st in stories or []:
        recs.append(compute_observation(corpus, subject_kind="story", subject_id=st["story_id"],
                                        doc_numbers=st.get("supporting_document_numbers", []),
                                        run_id=run_id, history=history_by_kind.get("story"),
                                        _baseline_vals=base_vals))
    return recs


# ------------------------------------------------------------------ storage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pattern_observations (
    observation_id TEXT PRIMARY KEY,
    run_id TEXT,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    algorithm_name TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    telemetry_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pattern_obs_subject ON pattern_observations(subject_kind, subject_id);
"""


def save_observation(db_path: str, rec: dict) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO pattern_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rec["observation_id"], rec["run_id"], rec["subject_kind"], rec["subject_id"],
             rec["algorithm_name"], rec["algorithm_version"], rec["computed_at"],
             rec["input_fingerprint"], _canon(rec["metrics_json"]), _canon(rec["telemetry"])))
        conn.commit()
    finally:
        conn.close()


def list_observations(db_path: str, subject_kind=None, exclude_observation_id=None) -> list:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        q, args = "SELECT * FROM pattern_observations", []
        if subject_kind:
            q += " WHERE subject_kind=?"
            args.append(subject_kind)
        rows = conn.execute(q + " ORDER BY computed_at, observation_id", args).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        if r[0] == exclude_observation_id:
            continue
        out.append({"observation_id": r[0], "run_id": r[1], "subject_kind": r[2], "subject_id": r[3],
                    "algorithm_name": r[4], "algorithm_version": r[5], "computed_at": r[6],
                    "input_fingerprint": r[7], "metrics_json": json.loads(r[8]), "telemetry": json.loads(r[9])})
    return out


# ------------------------------------------------------------------ report

def _f(x, nd=3):
    if x is None:
        return "n/a"
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def render_report(rec: dict) -> str:
    """Human-readable Pattern/Variance Report, rendered ONLY from the record."""
    m, t = rec["metrics_json"], rec["telemetry"]
    L = []
    L.append(f"PATTERN/VARIANCE REPORT  --  {rec['subject_kind']} {rec['subject_id']}")
    L.append(f"algorithm {rec['algorithm_name']} v{rec['algorithm_version']} | computed {rec['computed_at']} | run {rec['run_id'] or 'n/a'}")
    L.append(f"observation_id {rec['observation_id'][:16]} | input_fingerprint {rec['input_fingerprint'][:16]}")
    L.append("This report lists mechanical measurements of the supplied records only.")
    L.append("")
    L.append("TELEMETRY")
    L.append(f"  runtime_ms={_f(t['runtime_ms'])}  documents_in_scope={t['documents_in_scope']}  processed={t['documents_processed']}  "
             f"skipped={t['documents_skipped']}  errors={t['n_errors']}  pairs={t['pairs_computed']}")
    for s in t.get("skipped", [])[:5]:
        L.append(f"  skipped {s['document_number']}: {s['reason']}")
    for e in t.get("errors", [])[:5]:
        L.append(f"  error: {e}")
    c = m["counts"]
    L.append("")
    L.append("COUNTS")
    L.append(f"  documents={c['n_docs']}  agencies={c['n_agencies_distinct']}  countries={c['n_countries_distinct']}  "
             f"entities={c['n_entities_distinct']}  eccns={c['n_eccns_distinct']}  with_effective_date={c['n_with_effective_date']}  analyzed_tier={c['n_analyzed_tier']}")
    tm = m["temporal"]
    L.append("")
    L.append("TEMPORAL")
    L.append(f"  publication dates {tm['pub_date_min']} .. {tm['pub_date_max']}  span_days={tm['span_days']}  distinct_dates={tm['n_distinct_pub_dates']}")
    g = tm["gap_days"]
    L.append(f"  gap between consecutive distinct dates (days): n={g['n']} mean={_f(g['mean'],2)} median={_f(g['median'],1)} max={_f(g['max'],0)}")
    e = tm["effective_minus_publication_days"]
    L.append(f"  effective minus publication (days): n={e['n']} mean={_f(e['mean'],2)} min={_f(e['min'],0)} max={_f(e['max'],0)}")
    L.append("")
    L.append("SHARED EXPLICIT ITEMS (present in 2+ documents)")
    for k in ("agencies", "countries", "entities", "eccns", "cfr", "eo"):
        v = m["shared"][k]
        top = ", ".join(f"{x['item']} ({x['n_docs']})" for x in v["top"][:5]) or "none"
        L.append(f"  {k}: distinct={v['n_distinct']} shared={v['n_shared_by_2plus_docs']} "
                 f"docs_with_shared={_f(v['pct_docs_with_a_shared_item'],1)}%  top: {top}")
    i = m["identifiers"]
    L.append(f"  FR document numbers cited inside the set: citations={i['intra_set_fr_doc_citations']} "
             f"citing_docs={i['docs_citing_another_doc_in_set']} cited_docs={i['docs_cited_by_another_doc_in_set']}")
    x = m["text"]
    L.append("")
    L.append("TEXT OVERLAP (token / trigram Jaccard, pairwise)")
    L.append(f"  pairs={x['n_pairs']}  title mean={_f(x['title_jaccard_mean'])} max={_f(x['title_jaccard_max'])}  "
             f"summary mean={_f(x['summary_jaccard_mean'])} max={_f(x['summary_jaccard_max'])}  "
             f"summary-trigram mean={_f(x['summary_trigram_jaccard_mean'])} max={_f(x['summary_trigram_jaccard_max'])}")
    d = x["pairwise_days_apart"]
    L.append(f"  pairwise days apart: mean={_f(d['mean'],2)} std={_f(d['std'],2)} min={_f(d['min'],0)} max={_f(d['max'],0)}")
    if m["top_pairs_by_summary_jaccard"]:
        L.append("")
        L.append("TOP PAIRS BY SUMMARY OVERLAP")
        for p in m["top_pairs_by_summary_jaccard"]:
            L.append(f"  {p['doc_a']} / {p['doc_b']}: days_apart={p['days_apart']} summary_jaccard={_f(p['summary_jaccard'])} "
                     f"title_jaccard={_f(p['title_jaccard'])} shared(agency/country/entity/eccn)="
                     f"{p['shared_agencies']}/{p['shared_countries']}/{p['shared_entities']}/{p['shared_eccns']} "
                     f"score_diff={_f(p['score_diff'],0)}")
    s = m["scores"]
    L.append("")
    L.append(f"STORED SCORE FIELD: n={s['n']} mean={_f(s['mean'],2)} std={_f(s['std'],2)} min={_f(s['min'],0)} max={_f(s['max'],0)}")
    L.append("")
    L.append("BASELINES")
    b = m["baseline_corpus"]
    if b["available"]:
        for k in ("summary_jaccard", "title_jaccard", "days_apart"):
            v = b[k]
            L.append(f"  in-corpus {k}: subject_mean={_f(v['subject_mean'])} baseline_pair_mean={_f(v['baseline_pair_mean'])} "
                     f"std={_f(v['baseline_pair_std'])} n_pairs={v['baseline_n_pairs']} percentile={_f(v['percentile_of_subject_mean'],1)}")
    else:
        L.append(f"  in-corpus baseline not available: {b['reason']}")
    h = m["baseline_history"]
    if h["available"] and h["metrics"]:
        for k, v in h["metrics"].items():
            L.append(f"  history {k}: current={_f(v['current'])} prior_mean={_f(v['prior_mean'])} prior_std={_f(v['prior_std'])} "
                     f"percentile={_f(v['percentile'],1)} z={_f(v['z'],2)} (n_prior={v['prior_n']})")
    else:
        L.append(f"  historical baseline not available: n_prior={h['n_prior']} ({h.get('reason', 'no comparable metrics')})")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus-json", required=True, help="Corpus.to_dict()-shaped JSON file")
    ap.add_argument("--stories-json", help="Corpus Analyst output JSON (candidate_stories[].supporting_document_numbers)")
    ap.add_argument("--run-id")
    ap.add_argument("--db", help="SQLite file for pattern_observations (own table; created if absent). Omit to skip storage.")
    ap.add_argument("--report-dir", help="Write one .txt report per observation here; otherwise print to stdout")
    a = ap.parse_args(argv)

    with open(a.corpus_json, encoding="utf-8") as fh:
        corpus = json.load(fh)
    stories = []
    if a.stories_json:
        with open(a.stories_json, encoding="utf-8") as fh:
            stories = [{"story_id": s["story_id"], "supporting_document_numbers": s.get("supporting_document_numbers", [])}
                       for s in json.load(fh).get("candidate_stories", [])]
    hist = {}
    if a.db:
        for kind in ("corpus", "story"):
            hist[kind] = list_observations(a.db, kind)
    recs = compute_all(corpus, stories, run_id=a.run_id, history_by_kind=hist)
    for r in recs:
        if a.db:
            save_observation(a.db, r)
        text = render_report(r)
        if a.report_dir:
            os.makedirs(a.report_dir, exist_ok=True)
            with open(os.path.join(a.report_dir, f"pattern_variance_{r['subject_kind']}_{r['subject_id']}.txt"), "w", encoding="utf-8") as fh:
                fh.write(text)
        else:
            print(text)
    print(f"{len(recs)} observation(s) computed" + (f", stored in {a.db}" if a.db else ", not stored"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
