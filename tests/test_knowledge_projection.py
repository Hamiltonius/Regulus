#!/usr/bin/env python3
"""
Tests for knowledge_projection.py + the propositions / knowledge_links /
research_telemetry tables in intelligence_store.py (Knowledge Layer v2).

Throwaway SQLite files only; network patched to raise.
Run: python3 tests/test_knowledge_projection.py
"""
import copy
import os
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
    raise AssertionError("knowledge tests must never make a network call")


requests.post = _no_network

import intelligence_store as st
import knowledge_projection as kp
import epistemic_scorer as scorer
from epistemic_calibration_cards import CARDS

_TMP = tempfile.mkdtemp(prefix="regulus_kl2_")


def dbp(name):
    return os.path.join(_TMP, name)


def raises(fn, exc=ValueError):
    try:
        fn()
    except exc:
        return True
    return False


EVIDENCE = {
    "story_id": "CS-01",
    "question_findings": [
        {"question": "Was the waiver executed?", "status": "answered",
         "finding": "DOCUMENTED FACT: the waiver postdates the rescission.",
         "evidence_ids": ["E-001"], "confidence": "high"},
        {"question": "Is there coordination?", "status": "partially_answered",
         "finding": "Partial support for coordination.", "evidence_ids": ["E-004"], "confidence": "medium"},
        {"question": "Was an NPRM issued?", "status": "unanswered",
         "finding": "No source located.", "evidence_ids": [], "confidence": "low"},
    ],
    "evidence_records": [
        {"evidence_id": "E-001", "document_number": "2026-1", "source_type": "federal_register",
         "primary_source": True, "source_identity_status": "verified",
         "limitations": "pdf_excerpt only"},
        {"evidence_id": "E-004", "document_number": None, "source_type": "press",
         "primary_source": False, "source_identity_status": "unverified"},
    ],
    "contradictions": [{"description": "Dates conflict between E-001 and E-004.",
                         "evidence_ids": ["E-001", "E-004"], "significance": "material"}],
    "disconfirming_evidence_found": ["A 2025 notice predates the claimed initiative."],
    "remaining_gaps": ["No agency statement on motive."],
}
PASS2 = {
    "supported_findings": ["Waiver published 2026-09-16."],
    "weakened_or_rejected_findings": ["The initiative is new."],
    "remaining_uncertainties": ["Motive unknown."],
    "material_changes": [
        {"original_claim": "Waiver preceded rescission.", "disposition": "modified",
         "revised_claim": "Waiver followed rescission.", "reason": "Date check.", "evidence_ids": ["E-001"]},
        {"original_claim": "Syria drove timing.", "disposition": "removed",
         "revised_claim": "", "reason": "No support.", "evidence_ids": []},
        {"original_claim": "ITAR change is real.", "disposition": "retained",
         "revised_claim": "", "reason": "Confirmed.", "evidence_ids": ["E-001"]},
        {"original_claim": "Cluster is coordinated.", "disposition": "unresolved",
         "revised_claim": "", "reason": "Open.", "evidence_ids": []},
    ],
    "alternative_hypotheses_assessment": [
        {"hypothesis": "H-supported", "disposition": "supported", "explanation": "x", "evidence_ids": []},
        {"hypothesis": "H-partial", "disposition": "partially_supported", "explanation": "x", "evidence_ids": []},
        {"hypothesis": "H-weak", "disposition": "weakened", "explanation": "x", "evidence_ids": []},
        {"hypothesis": "H-contra", "disposition": "contradicted", "explanation": "x", "evidence_ids": []},
        {"hypothesis": "H-open", "disposition": "unresolved", "explanation": "x", "evidence_ids": []},
    ],
}


def seed(path, run="run-1", story="CS-01", ev=EVIDENCE, p2=PASS2):
    if ev is not None:
        st.save_story_artifact(run, story, "evidence_analyst", ev, is_valid=True, db_path=path)
    if p2 is not None:
        st.save_story_artifact(run, story, "pass2", p2, is_valid=True, db_path=path)


# A. schema
p = dbp("a.db")
c = st.get_connection(p)
tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
c.close()
check("A1. propositions/knowledge_links/research_telemetry exist, epistemic_facts gone",
      {"propositions", "knowledge_links", "research_telemetry"} <= tables and "epistemic_facts" not in tables)
check("A2. no fact_type vocabulary remains", not hasattr(st, "FACT_TYPE_VALUES"))
check("A3. unknown role rejected", raises(lambda: st.save_proposition(st.PropositionRecord(
    "p", "r", "s", "pass2", "f", 0, "documented_fact", "t", "x"), db_path=p)))
check("A4. unknown status rejected", raises(lambda: st.save_proposition(st.PropositionRecord(
    "p", "r", "s", "pass2", "f", 0, "evidence_finding", "t", "x", status="rejected_claim"), db_path=p)))
check("A5. superseded_by requires historical status", raises(lambda: st.save_proposition(st.PropositionRecord(
    "p", "r", "s", "pass2", "f", 0, "original_claim", "t", "x", superseded_by="q"), db_path=p)))
check("A6. unknown predicate rejected", raises(lambda: st.save_knowledge_link(st.KnowledgeLinkRecord(
    "l", "CS-01", "story", "bad", "D", "document", "r"), db_path=p)))

# B. projection of roles and verbatim dispositions
p = dbp("b.db")
seed(p)
s = kp.project_story("run-1", "CS-01", db_path=p)
props = st.list_propositions(run_id="run-1", db_path=p)
by = lambda role: [x for x in props if x.statement_role == role]
check("B1. all three question statuses preserved verbatim (incl. unanswered)",
      [x.upstream_disposition for x in by("evidence_finding")] == ["answered", "partially_answered", "unanswered"])
check("B2. upstream_confidence copied verbatim",
      [x.upstream_confidence for x in by("evidence_finding")] == ["high", "medium", "low"])
check("B3. question preserved in context",
      by("evidence_finding")[2].context.get("question") == "Was an NPRM issued?")
check("B4. contradiction / disconfirming / gap preserved with roles",
      len(by("contradiction")) == 1 and len(by("disconfirming_evidence")) == 1
      and any(x.source_field == "remaining_gaps" for x in by("uncertainty")))
check("B5. contradiction significance + evidence ids kept",
      by("contradiction")[0].context.get("significance") == "material"
      and by("contradiction")[0].evidence_ids == ["E-001", "E-004"])
check("B6. alt-hypothesis dispositions NOT remapped",
      [x.upstream_disposition for x in by("alternative_hypothesis")] ==
      ["supported", "partially_supported", "weakened", "contradicted", "unresolved"])
check("B7. alt-hypothesis weakened/contradicted contested; others active",
      [x.status for x in by("alternative_hypothesis")] == ["active", "active", "contested", "contested", "active"])
check("B8. supported_finding / reviewed_finding / pass2 uncertainty roles",
      len(by("supported_finding")) == 1 and len(by("reviewed_finding")) == 1
      and any(x.source_stage == "pass2" for x in by("uncertainty")))
check("B9. weakened_or_rejected kept (contested), no disposition invented",
      by("reviewed_finding")[0].status == "contested" and by("reviewed_finding")[0].upstream_disposition is None)
check("B10. evidence identity status carried into context",
      by("evidence_finding")[0].context["evidence"]["E-001"]["source_identity_status"] == "verified"
      and by("evidence_finding")[1].context["evidence"]["E-004"]["source_identity_status"] == "unverified")
check("B11. pass2 material_change evidence context joined from evidence artifact",
      by("original_claim")[0].context["evidence"]["E-001"]["source_identity_status"] == "verified")
check("B12. provenance complete on every row",
      all(x.source_run_id and x.source_story_id and x.source_stage and x.source_field
          and x.source_index is not None and x.extraction_method and x.source_saved_at for x in props))
check("B13. no role/disposition literal 'documented_fact' or 'rejected_claim' anywhere",
      not any(x.statement_role in ("documented_fact", "rejected_claim") or
              x.upstream_disposition in ("documented_fact", "rejected_claim") for x in props))

# C. supersession
orig = {x.statement_text: x for x in by("original_claim")}
rev = {x.statement_text: x for x in by("revised_claim")}
check("C1. revised_claim projected (only where text exists)", list(rev) == ["Waiver followed rescission."])
mod = orig["Waiver preceded rescission."]
check("C2. modified original -> historical, superseded_by == revised id",
      mod.status == "historical" and mod.superseded_by == rev["Waiver followed rescission."].proposition_id)
check("C3. revised proposition active, unsuperseded", rev["Waiver followed rescission."].status == "active"
      and rev["Waiver followed rescission."].superseded_by is None)
rem = orig["Syria drove timing."]
check("C4. removed without successor preserved as contested, no link",
      rem.status == "contested" and rem.superseded_by is None and rem.upstream_disposition == "removed")
check("C5. retained/unresolved unlinked and active",
      orig["ITAR change is real."].status == "active" and orig["ITAR change is real."].superseded_by is None
      and orig["Cluster is coordinated."].status == "active" and orig["Cluster is coordinated."].superseded_by is None)
check("C6. reason preserved in context", mod.context.get("reason") == "Date check.")

# D. no prose parsing / scorecard-free
doc = by("evidence_finding")[0]
check("D1. 'DOCUMENTED FACT' prose changes nothing: text verbatim, role generic, unscored",
      doc.statement_text.startswith("DOCUMENTED FACT") and doc.statement_role == "evidence_finding"
      and doc.epistemic_rating is None and doc.scorecard_id is None and doc.usage_class is None)
check("D2. every projected proposition is unscored",
      all(x.scorecard_id is None and x.epistemic_rating is None and x.usage_class is None for x in props))
ev2 = copy.deepcopy(EVIDENCE)
ev2["question_findings"][0]["finding"] = "Plain text, no keywords."
p2db = dbp("d2.db")
seed(p2db, ev=ev2)
kp.project_story("run-1", "CS-01", db_path=p2db)
a = [(x.statement_role, x.upstream_disposition, x.status) for x in st.list_propositions(run_id="run-1", db_path=p2db)]
b = [(x.statement_role, x.upstream_disposition, x.status) for x in props]
check("D3. changing prose keywords alters no structural field", a == b)

# E. scorecards
card = scorer.score_card(CARDS[1])
txt = CARDS[1]["proposition"]["text"]
p = dbp("e.db")
st.save_proposition(st.PropositionRecord("px", "r", "CS-01", "pass2", "supported_findings", 0,
                                          "supported_finding", txt, "structural_projection"), db_path=p)
st.attach_scorecard("px", card, db_path=p)
row = st.list_propositions(run_id="r", db_path=p)[0]
check("E1. attach stores scorecard_id/rating/usage from the real scorer",
      row.scorecard_id == card["scorecard_id"] and row.epistemic_rating == card["computed"]["final_rating"]
      and row.usage_class == card["computed"]["usage_class"])
check("E2. attach refuses unknown proposition", raises(lambda: st.attach_scorecard("nope", card, db_path=p)))
check("E3. attach refuses mismatched text", raises(lambda: st.attach_scorecard(
    "px", scorer.score_card(CARDS[0]), db_path=p)))
check("E4. attach refuses invalid/empty scorecard", raises(lambda: st.attach_scorecard("px", {"status": "invalid"}, db_path=p)))
bad = copy.deepcopy(card); bad["computed"]["final_rating"] = "E9"
check("E5. attach refuses unknown rating", raises(lambda: st.attach_scorecard("px", bad, db_path=p)))
bad = copy.deepcopy(card); bad["computed"]["usage_class"] = "documented_fact"
check("E6. attach refuses unknown usage class", raises(lambda: st.attach_scorecard("px", bad, db_path=p)))
rec = st.list_propositions(run_id="r", db_path=p)[0]
st.save_proposition(rec, db_path=p)
check("E7. re-save with same text keeps scorecard",
      st.list_propositions(run_id="r", db_path=p)[0].scorecard_id == card["scorecard_id"])
rec.statement_text = "changed text"
st.save_proposition(rec, db_path=p)
row = st.list_propositions(run_id="r", db_path=p)[0]
check("E8. changed text clears stale scorecard", row.scorecard_id is None and row.epistemic_rating is None)

# survives re-projection
p = dbp("e2.db")
seed(p)
kp.project_story("run-1", "CS-01", db_path=p)
tgt = [x for x in st.list_propositions(run_id="run-1", db_path=p) if x.statement_text == txt]
check("E9. (precondition) fixture text differs from scored text", tgt == [])
sp = dict(PASS2); sp = copy.deepcopy(PASS2); sp["supported_findings"] = [txt]
p = dbp("e3.db"); seed(p, p2=sp)
kp.project_story("run-1", "CS-01", db_path=p)
tid = [x for x in st.list_propositions(run_id="run-1", db_path=p) if x.statement_text == txt][0].proposition_id
st.attach_scorecard(tid, card, db_path=p)
kp.project_story("run-1", "CS-01", db_path=p)
check("E10. scorecard survives re-projection",
      [x for x in st.list_propositions(run_id="run-1", db_path=p) if x.proposition_id == tid][0].scorecard_id
      == card["scorecard_id"])

# F. idempotency + invalid artifacts + created_at
p = dbp("f.db"); seed(p)
kp.project_run("run-1", db_path=p)
def snap(path):
    c = sqlite3.connect(path)
    r = (c.execute("SELECT * FROM propositions ORDER BY proposition_id").fetchall(),
         c.execute("SELECT * FROM knowledge_links ORDER BY link_id").fetchall())
    c.close(); return r
s1 = snap(p)
kp.project_run("run-1", db_path=p)
check("F1. re-projection is byte-identical (rows and created_at)", snap(p) == s1)
check("F2. non-empty", len(s1[0]) > 0 and len(s1[1]) > 0)
p = dbp("f2.db")
st.save_story_artifact("r", "CS-02", "evidence_analyst", EVIDENCE, is_valid=False, db_path=p)
st.save_story_artifact("r", "CS-02", "pass2", PASS2, is_valid=False, db_path=p)
kp.project_run("r", db_path=p)
check("F3. invalid artifacts contribute nothing", st.list_propositions(run_id="r", db_path=p) == []
      and st.list_knowledge_links(run_id="r", db_path=p) == [])
p = dbp("f3.db")
st.save_story_artifact("r", "CS-02", "pass2", {"supported_findings": [None, "", 5, "ok"],
                                              "material_changes": ["x", {"original_claim": ""}]},
                       is_valid=True, db_path=p)
kp.project_run("r", db_path=p)
check("F4. malformed entries skipped, valid kept",
      [x.statement_text for x in st.list_propositions(run_id="r", db_path=p)] == ["ok"])

# G. links + brief + telemetry + multi-story
p = dbp("g.db"); seed(p)
st.save_story_artifact("run-1", "CS-01", "corpus_analyst",
                       {"supporting_document_numbers": ["D-1"], "observational_basis": [{"document_number": "D-2"}]},
                       is_valid=True, db_path=p)
st.save_brief("run-1", "B-1", {"start": "a", "end": "b"},
              {"stories_included": ["CS-01"], "stories_excluded": [{"story_id": "CS-02", "reason": "r"}]},
              is_valid=True, db_path=p)
sm = kp.project_run("run-1", db_path=p)
preds = sorted({l.predicate for l in st.list_knowledge_links(run_id="run-1", db_path=p)})
check("G1. structural predicates only, all five present", preds == sorted(st.PREDICATE_VALUES) or
      set(preds) == {"cites_document", "evidence_for_story", "evidence_cites_document",
                     "story_included_in_brief", "story_excluded_from_brief"})
check("G2. brief links counted", sm["brief_links_written"] == 2)
st.save_research_telemetry(st.ResearchTelemetryRecord(
    telemetry_id="t1", run_id="run-i", story_id="CS-01", stage="evidence_analyst", attempt_number=1,
    input_tokens=100, output_tokens=50, duration_seconds=1.5, request_succeeded=True, cache_hit=False,
    avoided_call_count=2, escalation_reason=None), db_path=p)
t = st.list_research_telemetry(run_id="run-i", db_path=p)
check("G3. telemetry round-trips", len(t) == 1 and t[0].input_tokens == 100 and t[0].cache_hit is False)
p = dbp("g2.db")
for sid in ("CS-08", "CS-09"):
    st.save_story_artifact("run-j", sid, "evidence_analyst",
                           {"question_findings": [{"question": "Q", "status": "answered", "finding": f"F-{sid}",
                                                   "evidence_ids": [], "confidence": "high"}], "evidence_records": []},
                           is_valid=True, db_path=p)
sm = kp.project_run("run-j", db_path=p)
check("G4. multi-story run", sm["story_ids"] == ["CS-08", "CS-09"] and sm["total_propositions_written"] == 2)

failed = [r for r in results if r[1] == "FAIL"]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
if failed:
    for n, _, d in failed:
        print(f"  - {n}: {d}")
    sys.exit(1)
