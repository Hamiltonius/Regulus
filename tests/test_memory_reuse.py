#!/usr/bin/env python3
"""
Tests for cross-database `memory_sources` reuse in regulus_orchestrator.py and
its dry-run mirror in regulus_brief001_acceptance.py.

No LLM/network call anywhere: every stage is a counting stub over the real
golden fixtures; requests.post is patched to raise. Throwaway SQLite files only.

Run: python3 tests/test_memory_reuse.py
"""
import copy
import hashlib
import json
import os
import shutil
import sqlite3
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


import requests


def _no_network(*a, **k):
    raise AssertionError("memory reuse tests must never make a network call")


requests.post = _no_network

import regulus_orchestrator as orch
import intelligence_store as store
import regulus_brief001_acceptance as acc
from _evidence_test_helpers import load_acceptance_3, get_story, load_corpus_acceptance_3

import evidence_retrieval as er

ACCEPTANCE_3 = load_acceptance_3()
CORPUS = load_corpus_acceptance_3()
PERIOD = {"start": "2026-09-14", "end": "2026-10-05"}
IDS = ("CS-01", "CS-02")
TMP = tempfile.mkdtemp(prefix="regulus_memreuse_")


def dbp(n):
    return os.path.join(TMP, n)


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


# ---- stubs (structural stand-ins; same shapes the orchestrator suite uses) ----
def corpus_stub(ids):
    stories = [get_story(ACCEPTANCE_3, s) for s in ids]

    def _s(payload, api_key):
        raw = copy.deepcopy(ACCEPTANCE_3)
        raw["candidate_stories"] = copy.deepcopy(stories)
        return raw
    return _s


def fake_retrieve(story, corpus, **kw):
    docs = [er.RetrievedDocument(document_number=d, status="retrieved", identity_status="verified",
                                 source_url=f"https://www.federalregister.gov/documents/x/{d}",
                                 primary_source=True, text="Synthetic.", text_source="pdf_full",
                                 retrieved_at="2026-10-06T00:00:00+00:00")
            for d in story["supporting_document_numbers"]]
    return er.EvidenceRetrievalBundle(story_id=story["story_id"], documents=docs)


def evidence_payload(story, tag="a"):
    recs = [{"evidence_id": f"EV-{i + 1}", "document_number": d, "source_title": "T",
             "source_url": f"https://www.federalregister.gov/documents/x/{d}", "source_type": "primary",
             "primary_source": True, "retrieved_at": "2026-10-06T00:00:00+00:00",
             "source_identity_status": "verified", "publication_date": "2026-09-16",
             "effective_date": "2026-09-16", "relevant_excerpt": "x", "supports": [], "contradicts": [],
             "limitations": ""} for i, d in enumerate(story["supporting_document_numbers"])]
    return {"story_id": story["story_id"], "research_status": "partial", "hypothesis_assessment": "unresolved",
            "question_findings": [{"question": q, "status": "unanswered", "finding": "", "evidence_ids": [],
                                  "confidence": "low"} for q in story["research_questions"]],
            "evidence_records": recs, "contradictions": [], "remaining_gaps": [],
            "disconfirming_evidence_found": [], "overall_assessment": f"Synthetic overall {tag}.",
            "confidence": "low"}


def pass2_payload(sid, story, ev):
    return {"story_id": sid, "assessment_disposition": "confirmed_with_modification",
            "original_hypothesis": story.get("preliminary_hypothesis", "S."),
            "revised_hypothesis": "Synthetic revised.", "material_changes": [],
            "supported_findings": ["Synthetic supported finding."], "weakened_or_rejected_findings": [],
            "remaining_uncertainties": [],
            "alternative_hypotheses_assessment": [
                {"hypothesis": h, "disposition": "unresolved", "explanation": "S.", "evidence_ids": []}
                for h in story.get("alternative_hypotheses") or []],
            "intelligence_assessment": "Synthetic.", "confidence": "medium",
            "editor_eligibility": "eligible", "editor_caveats": []}


def editor_stub(run_id, period, inputs, api_key):
    sids = [e["story_id"] for e in inputs]
    items = [{"text": f"Dev {s}.", "story_ids": [s], "evidence_ids": list(e["evidence_ids"])}
             for s, e in zip(sids, inputs)]
    ev_ids = sorted({x for e in inputs for x in e["evidence_ids"]})
    sec = lambda items_: {"summary": "S.", "items": items_}
    return {"brief_id": f"{run_id}-brief-001", "reporting_period": period, "executive_assessment": "S.",
            "regulatory_tempo": sec([]), "targeting_and_policy_direction": sec([]),
            "key_developments": sec(items), "cross_agency_signals": sec([]),
            "emerging_patterns": sec([]), "watchlist": sec([]),
            "methodology_and_sources": {"summary": "S.", "stories_included": sids, "stories_excluded": [],
                                        "evidence_ids_referenced": ev_ids},
            "confidence": "medium", "stories_included": sids, "stories_excluded": []}


class Counters:
    def __init__(self, tag="a"):
        self.evidence = self.pass2 = self.editor = 0
        self.tag = tag

    def ev(self, story, bundle, corpus, api_key):
        self.evidence += 1
        return evidence_payload(story, self.tag)

    def p2(self, sid, story, ev, api_key):
        self.pass2 += 1
        return pass2_payload(sid, story, ev)

    def ed(self, run_id, period, inputs, api_key):
        self.editor += 1
        return editor_stub(run_id, period, inputs, api_key)


def run(run_id, db, *, memory=None, c=None, ids=IDS, budget=None, cb=None):
    c = c or Counters()
    out = orch.run_intelligence_cycle(
        run_id, CORPUS, PERIOD, api_key="fake", db_path=db, call_corpus_analyst=corpus_stub(ids),
        call_evidence_analyst=c.ev, retrieve=fake_retrieve, call_pass2=c.p2, call_editor=c.ed,
        budget=budget, circuit_breaker=cb, memory_sources=memory)
    return out, c


def art(db, run_id, sid, stage):
    return store.load_story_artifact(run_id, sid, stage, db_path=db)


def sql(db, q, args=()):
    c = sqlite3.connect(db)
    try:
        c.execute(q, args)
        c.commit()
    finally:
        c.close()


# ===== seed a "memory" DB with a normal, no-memory run =====
MEM = dbp("mem.db")
seed_out, seed_c = run("mem-run", MEM)
check("S1. seed run (no memory) made real stub calls: 2 evidence + 2 pass2",
      (seed_c.evidence, seed_c.pass2) == (2, 2), f"{seed_c.evidence},{seed_c.pass2}")
check("S2. no-memory run: nothing reused from memory, all included",
      all(not s.evidence_reused_from_memory and not s.pass2_reused_from_memory for s in seed_out.stories)
      and len(seed_out.included_story_ids) == 2)
check("S3. fresh valid saves now stamp component versions in provenance",
      art(MEM, "mem-run", "CS-01", "evidence_analyst").provenance["component_versions"]
      == orch._component_versions("evidence_analyst")
      and art(MEM, "mem-run", "CS-01", "pass2").provenance["component_versions"]
      == orch._component_versions("pass2"))
mem_hash_before = sha(MEM)

# ===== 1. exact Evidence + Pass #2 memory hits => zero caller invocations =====
NEW = dbp("new1.db")
budget = orch.RunBudget(max_total_llm_calls=2)  # corpus unit + Editor only: any stage call would trip it
cb = orch.CircuitBreaker(consecutive_failure_threshold=1)
out, c = run("new-run", NEW, memory=[(MEM, "mem-run")], budget=budget, cb=cb)
check("1. exact Evidence memory hit => zero Evidence caller invocations", c.evidence == 0, str(c.evidence))
check("2. exact Pass #2 memory hit => zero Pass #2 caller invocations", c.pass2 == 0, str(c.pass2))
check("3. result flags report memory reuse for both stages and both stories",
      all(s.evidence_reused and s.evidence_reused_from_memory and s.pass2_reused
          and s.pass2_reused_from_memory for s in out.stories) and len(out.stories) == 2)
check("4. budget unchanged on reuse: no evidence calls counted, only corpus+editor units",
      budget.evidence_analyst_calls_made == 0 and budget.total_llm_calls_made == 2
      and budget.exhausted_reason is None, str(budget.summary()))
check("5. circuit breaker unchanged on reuse (never triggered, no streak recorded)",
      not cb.triggered and cb._consecutive_count == 0 and cb.summary() is None)
check("6. Editor ran once on the reused stories", c.editor == 1 and out.brief_outcome is not None
      and out.brief_outcome.is_valid, str(getattr(out.brief_outcome, "validation_errors", "")))

# ===== 2. persisted into current run with provenance; Editor input source preserved =====
src = art(MEM, "mem-run", "CS-01", "evidence_analyst")
cp = art(NEW, "new-run", "CS-01", "evidence_analyst")
p = cp.provenance or {}
check("7. copied into current run, valid, same payload and fingerprint",
      cp.is_valid and cp.payload == src.payload and cp.input_fingerprint == src.input_fingerprint)
check("8. provenance names source DB, source run, stage and source timestamp",
      p.get("kind") == "memory_reuse" and p.get("source_db") == os.path.abspath(MEM)
      and p.get("source_run_id") == "mem-run" and p.get("stage") == "evidence_analyst"
      and p.get("source_artifact_saved_at") == src.saved_at, str(p))
p2prov = (art(NEW, "new-run", "CS-02", "pass2").provenance or {})
check("9. pass2 copy carries provenance too", p2prov.get("stage") == "pass2"
      and p2prov.get("source_run_id") == "mem-run")
inputs = store.reconstruct_editor_inputs("new-run", db_path=NEW)
check("10. reconstruct_editor_inputs() on the current run sees both reused stories",
      sorted(i["story_id"] for i in inputs) == ["CS-01", "CS-02"] if isinstance(inputs, list)
      else sorted(i["story_id"] for i in getattr(inputs, "editor_inputs", inputs)) == ["CS-01", "CS-02"])

# ===== 3. memory DB not modified =====
check("11. memory DB byte-identical after retrieval (read-only, no migration)", sha(MEM) == mem_hash_before)
legacy = dbp("legacy_noprov.db")
shutil.copy(MEM, legacy)
sql(legacy, "DROP TABLE IF EXISTS briefs")  # perturb so we can prove untouched below
leg_hash = sha(legacy)
run("legacy-run", dbp("new_leg.db"), memory=[(legacy, "mem-run")])
check("12. second memory DB also untouched", sha(legacy) == leg_hash)

# ===== 4. fail-closed cases (each: caller IS invoked, i.e. normal path) =====
def fresh_copy(name):
    d = dbp(name)
    shutil.copy(MEM, d)
    return d


def expect_no_reuse(label, memdb, expect_ev=2):
    out, c = run("x-" + label, dbp(f"x_{label}.db"), memory=[(memdb, "mem-run")])
    ok = c.evidence == expect_ev and not any(s.evidence_reused_from_memory for s in out.stories)
    return check(label, ok, f"evidence calls={c.evidence} flags={[s.evidence_reused_from_memory for s in out.stories]}")


d = fresh_copy("inv.db")
sql(d, "UPDATE story_artifacts SET is_valid = 0 WHERE stage = 'evidence_analyst'")
expect_no_reuse("13. invalid memory artifact => no reuse", d)

d = fresh_copy("fp.db")
sql(d, "UPDATE story_artifacts SET input_fingerprint = 'deadbeef' WHERE stage = 'evidence_analyst'")
expect_no_reuse("14. fingerprint mismatch => no reuse", d)

d = fresh_copy("reval.db")
bad = art(MEM, "mem-run", "CS-01", "evidence_analyst").payload
bad = copy.deepcopy(bad)
del bad["overall_assessment"]
sql(d, "UPDATE story_artifacts SET payload_json = ? WHERE story_id='CS-01' AND stage='evidence_analyst'",
    (json.dumps(bad),))
out, c = run("x-reval", dbp("x_reval.db"), memory=[(d, "mem-run")])
cs01 = [s for s in out.stories if s.story_id == "CS-01"][0]
check("15. schema-revalidation failure => no reuse (CS-01 re-called, CS-02 still reused)",
      c.evidence == 1 and not cs01.evidence_reused_from_memory
      and [s for s in out.stories if s.story_id == "CS-02"][0].evidence_reused_from_memory,
      f"calls={c.evidence}")

d = fresh_copy("ver.db")
sql(d, "UPDATE story_artifacts SET provenance_json = ? WHERE stage='evidence_analyst'",
    (json.dumps({"component_versions": {"schema_version": "0.0", "prompt_version": "0.0"}}),))
expect_no_reuse("16. recorded component-version mismatch => no reuse", d)

missing = dbp("does_not_exist.db")
out, c = run("x-missing", dbp("x_missing.db"), memory=[(missing, "mem-run")])
check("17. unavailable memory DB => normal execution, and the file is NOT created",
      c.evidence == 2 and not os.path.exists(missing))

d = dbp("garbage.db")
open(d, "wb").write(b"not a sqlite database at all")
out, c = run("x-garbage", dbp("x_garbage.db"), memory=[(d, "mem-run")])
check("18. corrupt memory DB => normal execution", c.evidence == 2 and len(out.included_story_ids) == 2)

# legacy artifact without provenance (pre-existing node02-style) is accepted on fingerprint+revalidation
d = fresh_copy("legacyprov.db")
sql(d, "UPDATE story_artifacts SET provenance_json = NULL")
out, c = run("x-legacyprov", dbp("x_legacyprov.db"), memory=[(d, "mem-run")])
check("19. legacy artifact (no recorded versions) accepted; copy records versions as unrecorded",
      c.evidence == 0 and c.pass2 == 0
      and (art(dbp("x_legacyprov.db"), "x-legacyprov", "CS-01", "evidence_analyst").provenance
           .get("source_component_versions") is None))

# ===== 5. current-run precedence =====
CUR = dbp("cur.db")
run("cur-run", CUR, c=Counters(tag="LOCAL"))   # local run with distinguishable payloads
out, c = run("cur-run", CUR, memory=[(MEM, "mem-run")])
local_payload = art(CUR, "cur-run", "CS-01", "evidence_analyst").payload
check("20. current-run hit takes precedence over memory",
      c.evidence == 0 and all(s.evidence_reused and not s.evidence_reused_from_memory for s in out.stories)
      and "LOCAL" in local_payload["overall_assessment"])

# ===== 6. deterministic ordering of memory sources =====
MEM_B = dbp("mem_b.db")
run("mem-b-run", MEM_B, c=Counters(tag="B"))
tagA = lambda db, rid: art(db, rid, "CS-01", "evidence_analyst").payload["overall_assessment"]
o1 = dbp("ord1.db"); run("o1", o1, memory=[(MEM, "mem-run"), (MEM_B, "mem-b-run")])
o2 = dbp("ord2.db"); run("o2", o2, memory=[(MEM_B, "mem-b-run"), (MEM, "mem-run")])
check("21. memory order is honored: [A,B] picks A, [B,A] picks B",
      tagA(o1, "o1") == tagA(MEM, "mem-run") and tagA(o2, "o2") == tagA(MEM_B, "mem-b-run")
      and tagA(MEM, "mem-run") != tagA(MEM_B, "mem-b-run"))
o3 = dbp("ord3.db")
dead = fresh_copy("dead.db"); sql(dead, "UPDATE story_artifacts SET is_valid=0")
_, c3 = run("o3", o3, memory=[(dead, "mem-run"), (MEM_B, "mem-b-run")])
check("22. first source misses => next source used (zero calls)", c3.evidence == 0 and c3.pass2 == 0)
check("23. repeated runs choose identically (deterministic)",
      all(art(dbp(f"rep{i}.db"), f"rep{i}", "CS-02", "pass2").payload == art(MEM, "mem-run", "CS-02", "pass2").payload
          for i in range(2) if run(f"rep{i}", dbp(f"rep{i}.db"), memory=[(MEM, "mem-run")])))

# ===== 7. Pass #2 keyed to exact candidate + evidence fingerprint =====
# Current run holds a DIFFERENT local evidence payload for CS-01, so the memory Pass #2
# (computed over the memory evidence) must not match.
P2DB = dbp("p2key.db")
story01 = get_story(ACCEPTANCE_3, "CS-01")
store.save_story_artifact("p2-run", "CS-01", "corpus_analyst", story01, is_valid=True, db_path=P2DB)
store.save_story_artifact("p2-run", "CS-01", "evidence_analyst", evidence_payload(story01, "DIFFERENT"),
                          is_valid=True, input_fingerprint=store.compute_fingerprint(story01), db_path=P2DB)
out, c = run("p2-run", P2DB, memory=[(MEM, "mem-run")], ids=("CS-01",))
check("24. Pass #2 not reused from memory when the current evidence payload differs (1 Pass #2 call)",
      c.evidence == 0 and c.pass2 == 1 and not out.stories[0].pass2_reused_from_memory,
      f"ev={c.evidence} p2={c.pass2}")
# Evidence local + identical to memory => Pass #2 alone comes from memory.
P2B = dbp("p2only.db")
mem_ev = art(MEM, "mem-run", "CS-01", "evidence_analyst")
store.save_story_artifact("p2b-run", "CS-01", "corpus_analyst", story01, is_valid=True, db_path=P2B)
store.save_story_artifact("p2b-run", "CS-01", "evidence_analyst", mem_ev.payload, is_valid=True,
                          input_fingerprint=mem_ev.input_fingerprint, db_path=P2B)
out, c = run("p2b-run", P2B, memory=[(MEM, "mem-run")], ids=("CS-01",))
check("25. local evidence + exact Pass #2 in memory => zero calls, Pass #2 flagged from memory",
      c.evidence == 0 and c.pass2 == 0 and out.stories[0].pass2_reused_from_memory
      and not out.stories[0].evidence_reused_from_memory)

# ===== 8. ordinary no-memory execution unchanged =====
out0, c0 = run("plain", dbp("plain.db"))
check("26. memory_sources omitted: full calls, no memory flags, Editor runs",
      (c0.evidence, c0.pass2, c0.editor) == (2, 2, 1)
      and not any(s.evidence_reused_from_memory or s.pass2_reused_from_memory for s in out0.stories))
out0b, c0b = run("plain", dbp("plain.db"))
check("27. same-run local reuse still works exactly as before",
      c0b.evidence == 0 and c0b.pass2 == 0 and all(s.evidence_reused for s in out0b.stories))
out0c, c0c = run("plain2", dbp("plain2.db"), memory=[])
check("28. empty memory_sources behaves as no memory", (c0c.evidence, c0c.pass2) == (2, 2))

# ===== 9. dry-run =====
fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "corpus_analyst_acceptance_3.json")
DRY = dbp("dry.db")
rep = acc.run_acceptance("dry-run-id", db_path=DRY, output_dir=dbp("out"), dry_run=True,
                         corpus_analysis_artifact_path=fixture,
                         budget=orch.RunBudget(max_total_llm_calls=100),
                         circuit_breaker=orch.CircuitBreaker(),
                         memory_sources=[(MEM, "mem-run")])
st_ = {s["story_id"]: s for s in rep["stories"]}
check("29. dry-run: CS-01/CS-02 report would_reuse_from_memory for Evidence and Pass #2",
      all(st_[i]["evidence_analyst"] == "would_reuse_from_memory" and st_[i]["pass2"] == "would_reuse_from_memory"
          for i in IDS), str({i: st_[i] for i in IDS}))
check("30. dry-run: the 9 other stories report would_call",
      all(st_[i]["evidence_analyst"] == "would_call" for i in st_ if i not in IDS) and len(st_) == 11)
check("31. dry-run: memory source recorded per story",
      st_["CS-01"]["evidence_memory_source"] == [MEM, "mem-run"])
check("32. dry-run max NEW requests = 9 stories x 2 stages + 1 Editor = 19",
      rep["max_new_external_llm_requests"] == 19, str(rep.get("max_new_external_llm_requests")))
check("33. dry-run did not modify the memory DB", sha(MEM) == mem_hash_before)

# would_reuse_local when the local run already holds it
LOC = dbp("loc.db")
run("loc-run", LOC, ids=IDS)
store_conn = store.get_connection(LOC)
try:
    s1 = acc._resolve_story_reuse_state("loc-run", story01, conn=store_conn, memory_sources=[(MEM, "mem-run")],
                                        corpus=CORPUS)
    s_plain = acc._resolve_story_reuse_state("loc-run", story01, conn=store_conn)
finally:
    store_conn.close()
check("34. dry-run state: local hit => would_reuse_local when memory configured",
      s1["evidence_analyst"] == "would_reuse_local" and s1["pass2"] == "would_reuse_local")
check("35. dry-run state without memory keeps the original vocabulary (would_reuse)",
      s_plain["evidence_analyst"] == "would_reuse" and s_plain["pass2"] == "would_reuse")
rep0 = acc.run_acceptance("dry-run-nomem", db_path=dbp("dry0.db"), output_dir=dbp("out"), dry_run=True,
                          corpus_analysis_artifact_path=fixture, budget=orch.RunBudget(max_total_llm_calls=100),
                          circuit_breaker=orch.CircuitBreaker())
check("36. dry-run without memory: every story would_call, no memory fields",
      all(s["evidence_analyst"] == "would_call" for s in rep0["stories"])
      and "evidence_memory_source" not in rep0["stories"][0])

check("37. memory DB hash unchanged after the whole file", sha(MEM) == mem_hash_before)

failed = [r for r in results if r[1] == "FAIL"]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
if failed:
    for n, _, d_ in failed:
        print(f"  - {n}: {d_}")
    sys.exit(1)
