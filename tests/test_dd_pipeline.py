#!/usr/bin/env python3
"""
Tests for dd_pipeline.py: the Gate, orchestration, persistence, retry/
failure behavior, and the six frozen acceptance scenarios.

Uses ONLY temporary SQLite databases (via regulus_v3.get_db() pointed at a
throwaway DB_PATH). Never touches production bis_watcher.db. All LLM calls
are injected stubs — no real network/API calls are made anywhere in this
file. That is a deliberate, reported scope limitation (see the
implementation report): this sandbox has no ANTHROPIC_API_KEY, so the six
acceptance scenarios are run against the deterministic gate for real, and
against the full validate -> persist -> synthesize -> validate ->
source-reuse-check orchestration using realistic canned Stage 2/Stage 3
payloads standing in for the model, not live model-generated research.

Run: python3 tests/test_dd_pipeline.py
"""
import copy
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
    fd, path = tempfile.mkstemp(suffix=".db", prefix="regulus_dd_test_")
    os.close(fd)
    os.remove(path)
    return path


TMP_DB = fresh_db_path()
os.environ["DB_PATH"] = TMP_DB
import importlib
import regulus_v3 as rv
importlib.reload(rv)
import dd_pipeline as ddp
import dd_schema as schema

conn = rv.get_db()


def make_alert_row(conn, doc_hash, document_number, title, score=15):
    """Minimal alerts row so due_diligence_records' FK reference and the
    UPDATE ... WHERE doc_hash=? calls in main()'s DD integration have
    something to point at — mirrors what main() would have already
    inserted before running DD."""
    conn.execute(
        "INSERT INTO alerts (doc_hash, document_number, title, score, fetched_at) VALUES (?, ?, ?, ?, ?)",
        (doc_hash, document_number, title, score, "2026-09-30T00:00:00+00:00"),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Fixtures — six acceptance scenarios (doc, analysis) pairs
# ---------------------------------------------------------------------------

def syria_case():
    doc = {
        "document_number": "2026-19001", "title": "Waiver of Sanctions on Syria Under the CBW Act",
        "abstract": "State waives the two remaining CBW Act restrictions on Syria.",
        "agencies": [{"name": "Department of State"}], "html_url": "https://www.federalregister.gov/d/2026-19001",
    }
    analysis = {
        "confidence": "High", "change_type": "sanctions_waiver", "countries": ["Syria"],
        "unresolved_questions": [],
    }
    return doc, analysis


def entity_list_case():
    doc = {
        "document_number": "2026-19002", "title": "Addition of Entities to the Entity List",
        "abstract": "BIS adds several entities to the Entity List for diversion risk.",
        "agencies": [{"name": "Bureau of Industry and Security"}],
        "html_url": "https://www.federalregister.gov/d/2026-19002",
    }
    analysis = {
        "confidence": "High", "change_type": "entity_list_addition", "countries": ["China"],
        "unresolved_questions": [],
    }
    return doc, analysis


def ofac_derivative_case():
    doc = {
        "document_number": "2026-19003", "title": "Notice of OFAC Sanctions Action",
        "abstract": "OFAC designates entities owned or controlled by a previously designated party.",
        "agencies": [{"name": "Office of Foreign Assets Control"}],
        "html_url": "https://www.federalregister.gov/d/2026-19003",
    }
    analysis = {
        # Confidence Medium (not High) — Stage 1 flagged genuine uncertainty
        # about the ownership chain — is itself sufficient to escalate.
        "confidence": "Medium", "change_type": "sdn_designation", "countries": [],
        "unresolved_questions": ["Is the derivative designation basis fully documented?"],
    }
    return doc, analysis


def ccl_eccn_case():
    doc = {
        "document_number": "2026-19004", "title": "Amendment to ECCN 3A001 Controls",
        "abstract": "BIS amends the Commerce Control List entry for 3A001.",
        "agencies": [{"name": "Bureau of Industry and Security"}],
        "html_url": "https://www.federalregister.gov/d/2026-19004",
    }
    analysis = {
        "confidence": "High", "change_type": "ccl_amendment", "countries": [],
        "unresolved_questions": [],
    }
    return doc, analysis


def correction_notice_case():
    """Negative control — must NOT escalate."""
    doc = {
        "document_number": "2026-19005", "title": "Correction: Notice of Technical Amendment",
        "abstract": "This document corrects a citation error in a previously published rule.",
        "agencies": [{"name": "Bureau of Industry and Security"}],
        "html_url": "https://www.federalregister.gov/d/2026-19005",
    }
    analysis = {
        "confidence": "High", "change_type": "correction", "countries": [],
        "unresolved_questions": [],
    }
    return doc, analysis


def no_precedent_case():
    doc = {
        "document_number": "2026-19006", "title": "Addition of Novel-Program Entities to the Entity List",
        "abstract": "BIS adds entities under a newly established control program with no prior precedent.",
        "agencies": [{"name": "Bureau of Industry and Security"}],
        "html_url": "https://www.federalregister.gov/d/2026-19006",
    }
    analysis = {
        "confidence": "High", "change_type": "entity_list_addition", "countries": [],
        "unresolved_questions": [],
    }
    return doc, analysis


# ---------------------------------------------------------------------------
# Stage 2/3 canned-response builders
# ---------------------------------------------------------------------------

def canned_stage2(research_question, trend, research_status="complete", confidence="High",
                   sources=None):
    sources = sources if sources is not None else [{
        "url": "https://www.federalregister.gov/d/2025-00001", "source_type": "federal_register",
        "agency": "Department of State", "date": "2025-06-30", "supports": ["historical_context"],
        "primary_source": True,
    }]
    return {
        "research_question": research_question,
        "current_event": {
            "action": "action", "date": "2026-09-16", "effective_date": "2026-09-16",
            "agency": ["Department of State"], "authority": ["Statute"], "jurisdictions": ["Syria"],
            "entities": [], "controls_affected": [],
        },
        "historical_context": {
            "program_origin": "Program origin.", "major_prior_actions": ["Prior action"],
            "most_relevant_precedent": {
                "date": "2025-06-30" if research_status != "insufficient_data" else None,
                "description": "" if research_status == "insufficient_data" else "Precedent description.",
                "entities_involved": [], "authority": [], "mechanism": "",
            },
        },
        "precedent_comparison": {
            "similarities": [], "differences": [], "trend_classification": trend,
        },
        "legal_regulatory_effect": {
            "changed": ["Something changed"], "unchanged": [], "superseded": [],
            "remaining_restrictions": [], "effective_date": "2026-09-16",
        },
        "scope": {
            "affected_countries": [], "affected_entities": [], "affected_item_categories": [],
            "affected_transaction_types": [], "affected_compliance_workflows": [],
        },
        "impact_assessment": {
            "immediate": [], "operational": [], "licensing": [], "screening": [],
            "classification": [], "authorization_management": [],
        },
        "follow_on_indicators": {
            "historically_observed_next_steps": [], "current_unresolved_actions": [],
            "items_to_monitor": [],
        },
        "open_questions": [],
        "sources": sources,
        "research_status": research_status,
        "due_diligence_confidence": confidence,
    }


def canned_stage3_from(stage2_record, headline):
    return {
        "headline": headline, "bottom_line": "Bottom line.", "what_changed": "What changed.",
        "why_it_matters": "Why it matters.", "historical_significance": "Historical significance.",
        "what_did_not_change": "What did not change.", "compliance_attention": ["Review licensing"],
        "watch_next": ["Watch this"], "confidence": stage2_record["due_diligence_confidence"],
        "sources": stage2_record["sources"],  # verbatim reuse, per C2
    }


# ---------------------------------------------------------------------------
# 1-4, 6: Gate correctly escalates the five significant cases
# ---------------------------------------------------------------------------
print("=== Gate: 5/5 significant cases escalate ===")
for name, case_fn in [
    ("Syria waiver", syria_case), ("Entity List addition", entity_list_case),
    ("OFAC derivative designation", ofac_derivative_case), ("CCL/ECCN amendment", ccl_eccn_case),
    ("No precedent found", no_precedent_case),
]:
    doc, analysis = case_fn()
    escalated = ddp.needs_due_diligence(analysis, doc)
    check(f"Gate escalates: {name}", escalated is True)

# ---------------------------------------------------------------------------
# 5: Negative control — correction notice must NOT escalate
# ---------------------------------------------------------------------------
print("\n=== Gate: negative control ===")
doc, analysis = correction_notice_case()
escalated = ddp.needs_due_diligence(analysis, doc)
check("Gate does NOT escalate correction notice", escalated is False)

# ---------------------------------------------------------------------------
# Full orchestration — Syria waiver (happy path)
# ---------------------------------------------------------------------------
print("\n=== Orchestration: Syria waiver end-to-end ===")
doc, analysis = syria_case()
doc_hash = "hash_syria_0001"
make_alert_row(conn, doc_hash, doc["document_number"], doc["title"])

stage2_calls = []
stage3_calls = []


def stub_stage2_syria(doc_, analysis_, api_key):
    stage2_calls.append((doc_, analysis_))
    return canned_stage2("What is the precedent for this Syria CBW Act waiver?", "consistent")


def stub_stage3_reuse(analysis_, dd_record, api_key):
    stage3_calls.append((analysis_, dd_record))
    return canned_stage3_from(dd_record, "Syria — Remaining CBW Act Arms Restrictions Waived")


outcome = ddp.run_due_diligence(doc, analysis, conn, doc_hash, doc["document_number"],
                                 call_stage2=stub_stage2_syria, call_stage3=stub_stage3_reuse)
check("Stage 2 called exactly once", len(stage2_calls) == 1)
check("Stage 3 called exactly once", len(stage3_calls) == 1)
check("stage2_validation_status == valid", outcome.stage2_validation_status == "valid",
      str(outcome.stage2_validation_errors))
check("research_status == complete", outcome.research_status == "complete")
check("stage3_valid == True", outcome.stage3_valid is True, str(outcome.stage3_validation_errors))
check("stage3 headline present", outcome.stage3.get("headline") == "Syria — Remaining CBW Act Arms Restrictions Waived")
check("dd_id assigned", isinstance(outcome.dd_id, int))

dd_row = conn.execute(
    "SELECT document_number, doc_hash, validation_status, research_status, confidence, model, "
    "schema_version, prompt_version FROM due_diligence_records WHERE dd_id = ?",
    (outcome.dd_id,),
).fetchone()
check("due_diligence_records row persisted with correct doc linkage",
      dd_row[0] == doc["document_number"] and dd_row[1] == doc_hash, str(dd_row))
check("persisted validation_status == valid", dd_row[2] == "valid")
check("persisted research_status == complete", dd_row[3] == "complete")
check("model/schema/prompt versioning persisted",
      dd_row[5] == ddp.STAGE2_MODEL and dd_row[6] == ddp.SCHEMA_VERSION and dd_row[7] == ddp.PROMPT_VERSION,
      str(dd_row))

# ---------------------------------------------------------------------------
# Full orchestration — no precedent found -> insufficient_data, still valid
# ---------------------------------------------------------------------------
print("\n=== Orchestration: no precedent found -> insufficient_data ===")
doc6, analysis6 = no_precedent_case()
doc_hash6 = "hash_noprec_0001"
make_alert_row(conn, doc_hash6, doc6["document_number"], doc6["title"])


def stub_stage2_insufficient(doc_, analysis_, api_key):
    return canned_stage2("Is there precedent for this novel program?", "novel",
                          research_status="insufficient_data")


def stub_stage3_insufficient(analysis_, dd_record, api_key):
    out = canned_stage3_from(dd_record, "Novel Entity List Program — No Established Precedent")
    out["confidence"] = "Low"
    out["historical_significance"] = "No comparable prior action was found; this appears to be a novel program."
    return out


outcome6 = ddp.run_due_diligence(doc6, analysis6, conn, doc_hash6, doc6["document_number"],
                                  call_stage2=stub_stage2_insufficient, call_stage3=stub_stage3_insufficient)
check("insufficient_data record is still validation_status=valid", outcome6.stage2_validation_status == "valid",
      str(outcome6.stage2_validation_errors))
check("research_status == insufficient_data", outcome6.research_status == "insufficient_data")
check("Stage 3 still runs on a valid insufficient_data record", outcome6.stage3_valid is True,
      str(outcome6.stage3_validation_errors))
check("Stage 3 does not manufacture false certainty", outcome6.stage3["confidence"] == "Low")

# ---------------------------------------------------------------------------
# Malformed Stage 2 output -> invalid, Stage 3 never called
# ---------------------------------------------------------------------------
print("\n=== Malformed Stage 2 output: Stage 3 never invoked ===")
doc_bad, analysis_bad = entity_list_case()
doc_bad["document_number"] = "2026-19007"
doc_hash_bad = "hash_malformed_0001"
make_alert_row(conn, doc_hash_bad, doc_bad["document_number"], doc_bad["title"])

stage3_should_not_be_called = []


def stub_stage2_malformed(doc_, analysis_, api_key):
    bad = canned_stage2("q", "consistent")
    del bad["legal_regulatory_effect"]  # missing required field
    return bad


def stub_stage3_should_not_run(analysis_, dd_record, api_key):
    stage3_should_not_be_called.append(1)
    return canned_stage3_from(dd_record, "should never happen")


outcome_bad = ddp.run_due_diligence(doc_bad, analysis_bad, conn, doc_hash_bad, doc_bad["document_number"],
                                     call_stage2=stub_stage2_malformed, call_stage3=stub_stage3_should_not_run)
check("malformed Stage 2 -> validation_status invalid", outcome_bad.stage2_validation_status == "invalid")
check("Stage 3 never called on invalid Stage 2 output", len(stage3_should_not_be_called) == 0)
check("failure_reason == stage2_invalid", outcome_bad.failure_reason == "stage2_invalid")
check("malformed DD record still persisted for auditability", isinstance(outcome_bad.dd_id, int))
dd_row_bad = conn.execute("SELECT validation_status FROM due_diligence_records WHERE dd_id = ?",
                           (outcome_bad.dd_id,)).fetchone()
check("persisted invalid record has validation_status=invalid in DB", dd_row_bad[0] == "invalid")

# ---------------------------------------------------------------------------
# Stage 2 call fails entirely (network/API error) -> retried, then recorded
# as a pipeline failure, never crashes
# ---------------------------------------------------------------------------
print("\n=== Stage 2 permanent failure: retried, recorded, no crash ===")
doc_fail, analysis_fail = ccl_eccn_case()
doc_fail["document_number"] = "2026-19008"
doc_hash_fail = "hash_fail_0001"
make_alert_row(conn, doc_hash_fail, doc_fail["document_number"], doc_fail["title"])

attempt_count = []


def stub_stage2_always_fails(doc_, analysis_, api_key):
    attempt_count.append(1)
    raise RuntimeError("simulated network failure")


try:
    outcome_fail = ddp.run_due_diligence(doc_fail, analysis_fail, conn, doc_hash_fail, doc_fail["document_number"],
                                          call_stage2=stub_stage2_always_fails)
    raised = False
except Exception:
    raised = True
    outcome_fail = None

check("run_due_diligence does not raise on permanent Stage 2 failure", not raised)
check(f"Stage 2 retried up to STAGE2_MAX_ATTEMPTS ({ddp.STAGE2_MAX_ATTEMPTS})",
      len(attempt_count) == ddp.STAGE2_MAX_ATTEMPTS, f"attempts={len(attempt_count)}")
if outcome_fail:
    check("failure_reason == stage2_call_failed", outcome_fail.failure_reason == "stage2_call_failed")
    check("research_status uses RESEARCH_STATUS_ERROR sentinel, not a model value",
          outcome_fail.stage2_validation_status == "invalid")
    dd_row_fail = conn.execute("SELECT research_status, validation_status FROM due_diligence_records WHERE dd_id = ?",
                                (outcome_fail.dd_id,)).fetchone()
    check("persisted failure row uses error sentinel for research_status",
          dd_row_fail[0] == ddp.RESEARCH_STATUS_ERROR, str(dd_row_fail))
    check("persisted failure row has validation_status=invalid", dd_row_fail[1] == "invalid")

# ---------------------------------------------------------------------------
# Stage 2 succeeds on 2nd attempt (transient failure) -> proceeds normally
# ---------------------------------------------------------------------------
print("\n=== Stage 2 transient failure: succeeds on retry ===")
doc_retry, analysis_retry = entity_list_case()
doc_retry["document_number"] = "2026-19009"
doc_hash_retry = "hash_retry_0001"
make_alert_row(conn, doc_hash_retry, doc_retry["document_number"], doc_retry["title"])

retry_state = {"n": 0}


def stub_stage2_transient(doc_, analysis_, api_key):
    retry_state["n"] += 1
    if retry_state["n"] < 2:
        raise RuntimeError("transient error")
    return canned_stage2("q", "consistent")


outcome_retry = ddp.run_due_diligence(doc_retry, analysis_retry, conn, doc_hash_retry, doc_retry["document_number"],
                                       call_stage2=stub_stage2_transient, call_stage3=stub_stage3_reuse)
check("transient failure recovered on retry", outcome_retry.stage2_validation_status == "valid",
      str(outcome_retry.stage2_validation_errors))
check("stub was called exactly twice", retry_state["n"] == 2)

# ---------------------------------------------------------------------------
# Stage 3 violates C2 (introduces a source not in Stage 2 evidence)
# ---------------------------------------------------------------------------
print("\n=== Stage 3 source-reuse violation (C2) is caught ===")
doc_c2, analysis_c2 = ccl_eccn_case()
doc_c2["document_number"] = "2026-19010"
doc_hash_c2 = "hash_c2_0001"
make_alert_row(conn, doc_hash_c2, doc_c2["document_number"], doc_c2["title"])


def stub_stage2_c2(doc_, analysis_, api_key):
    return canned_stage2("q", "consistent")


def stub_stage3_violates_c2(analysis_, dd_record, api_key):
    out = canned_stage3_from(dd_record, "Bad Stage 3")
    out["sources"] = [{
        "url": "https://example.com/fabricated", "source_type": "secondary",
        "agency": "n/a", "date": "2026-01-01", "supports": [], "primary_source": False,
    }]
    return out


outcome_c2 = ddp.run_due_diligence(doc_c2, analysis_c2, conn, doc_hash_c2, doc_c2["document_number"],
                                    call_stage2=stub_stage2_c2, call_stage3=stub_stage3_violates_c2)
check("Stage 3 with fabricated source is rejected", outcome_c2.stage3_valid is False)
check("failure_reason == stage3_invalid", outcome_c2.failure_reason == "stage3_invalid")
check("C2 violation error mentions the fabricated source",
      any("fabricated" in e for e in outcome_c2.stage3_validation_errors),
      str(outcome_c2.stage3_validation_errors))
check("outcome.stage3 is None when C2 is violated (no silent pass-through)", outcome_c2.stage3 is None)

# ---------------------------------------------------------------------------
# due_diligence_ran / dd_id versioning survives multiple runs for the same
# document (never overwrites — new dd_id each time)
# ---------------------------------------------------------------------------
print("\n=== Re-running DD for the same document creates a new dd_id, not an overwrite ===")
outcome_again = ddp.run_due_diligence(doc, analysis, conn, doc_hash, doc["document_number"],
                                       call_stage2=stub_stage2_syria, call_stage3=stub_stage3_reuse)
check("second run gets a different dd_id than the first", outcome_again.dd_id != outcome.dd_id)
count_for_doc = conn.execute("SELECT COUNT(*) FROM due_diligence_records WHERE document_number = ?",
                              (doc["document_number"],)).fetchone()[0]
check("both DD runs for this document are retained", count_for_doc == 2, f"count={count_for_doc}")

conn.close()
try:
    os.remove(TMP_DB)
except OSError:
    pass

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("\n=== SUMMARY ===")
failed = [r for r in results if r[1] == "FAIL"]
for name, status, detail in results:
    print(f"{status}: {name}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
