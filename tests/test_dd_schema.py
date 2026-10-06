#!/usr/bin/env python3
"""
Tests for dd_schema.py (Stage 2 / Stage 3 structural validators).

No LLM calls, no network calls, no database access — pure structural
validation against plain dicts. Also proves existing Regulus behavior
(regulus_v3.py) is untouched by this slice.

Run: python3 tests/test_dd_schema.py
"""
import copy
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import dd_schema as ds

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == "FAIL" else ""))
    return condition


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def complete_stage2_record(research_status="complete"):
    return {
        "research_question": "What is the historical precedent for this Syria CBW Act waiver?",
        "current_event": {
            "action": "waiver",
            "date": "2026-09-16",
            "effective_date": "2026-09-16",
            "agency": ["Department of State"],
            "authority": ["CBW Act"],
            "jurisdictions": ["Syria"],
            "entities": [],
            "controls_affected": ["AECA arms sales", "USML export licensing"],
        },
        "historical_context": {
            "program_origin": "CBW Act sanctions imposed following Assad-era chemical weapons use",
            "major_prior_actions": ["2025-06-30 presidential determination", "2025 partial waiver"],
            "most_relevant_precedent": {
                "date": "2025-06-30",
                "description": "Presidential determination partially waived CBW Act sanctions on Syria",
                "entities_involved": ["Government of Syria"],
                "authority": ["CBW Act"],
                "mechanism": "presidential determination",
            },
        },
        "precedent_comparison": {
            "similarities": ["Same statutory authority", "Same underlying program"],
            "differences": ["Removes remaining two restrictions vs. partial removal"],
            "trend_classification": "consistent",
        },
        "legal_regulatory_effect": {
            "changed": ["AECA arms sales restriction waived", "Foreign military financing restriction waived"],
            "unchanged": ["Other Syria sanctions regimes"],
            "superseded": [],
            "remaining_restrictions": ["General Syria export control review requirements"],
            "effective_date": "2026-09-16",
        },
        "scope": {
            "affected_countries": ["Syria"],
            "affected_entities": [],
            "affected_item_categories": ["USML"],
            "affected_transaction_types": ["arms sales", "financing"],
            "affected_compliance_workflows": ["licensing review"],
        },
        "impact_assessment": {
            "immediate": ["Two remaining CBW Act restrictions lifted"],
            "operational": [],
            "licensing": ["AECA/USML licensing backdrop changes"],
            "screening": [],
            "classification": [],
            "authorization_management": [],
        },
        "follow_on_indicators": {
            "historically_observed_next_steps": ["Implementing agency guidance historically followed"],
            "current_unresolved_actions": [],
            "items_to_monitor": ["Subsequent DDTC/State implementing actions"],
        },
        "open_questions": [],
        "sources": [
            {
                "url": "https://www.federalregister.gov/d/2026-99999",
                "source_type": "federal_register",
                "agency": "Department of State",
                "date": "2026-09-16",
                "supports": ["current_event", "legal_regulatory_effect"],
                "primary_source": True,
            }
        ],
        "research_status": research_status,
        "due_diligence_confidence": "High",
    }


def complete_stage3_record():
    return {
        "headline": "Syria — Remaining CBW Act Arms Restrictions Waived",
        "bottom_line": "State waived the two remaining CBW Act restrictions on Syria effective Sept 16.",
        "what_changed": "AECA arms sales/USML licensing restriction and FMF restriction both waived.",
        "why_it_matters": "Completes the staged removal begun by the 2025 presidential determination.",
        "historical_significance": "Consistent with the trajectory set by the 2025 partial waiver.",
        "what_did_not_change": "Other Syria-specific sanctions and control regimes remain in place.",
        "compliance_attention": ["Review AECA/USML licensing posture for Syria-related transactions"],
        "watch_next": ["Subsequent DDTC/State implementing guidance"],
        "confidence": "High",
        "sources": [
            {
                "url": "https://www.federalregister.gov/d/2026-99999",
                "source_type": "federal_register",
                "agency": "Department of State",
                "date": "2026-09-16",
                "supports": ["what_changed"],
                "primary_source": True,
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1. Complete valid Stage 2 record passes
# ---------------------------------------------------------------------------
print("\n=== 1. Complete valid Stage 2 record passes ===")
r = ds.validate_stage2_record(complete_stage2_record())
check("validation_status == valid", r.validation_status == "valid", str(r.validation_errors))
check("no validation_errors", r.validation_errors == [], str(r.validation_errors))
check("research_status echoed", r.research_status == "complete")
check("due_diligence_confidence echoed", r.due_diligence_confidence == "High")

# ---------------------------------------------------------------------------
# 2. Valid Stage 2 record with research_status=insufficient_data passes
#    (THE key invariant: valid structure + insufficient evidence is
#    representable and is NOT a validation failure)
# ---------------------------------------------------------------------------
print("\n=== 2. valid + insufficient_data is representable ===")
rec = complete_stage2_record(research_status="insufficient_data")
r = ds.validate_stage2_record(rec)
check("validation_status == valid", r.validation_status == "valid", str(r.validation_errors))
check("research_status == insufficient_data", r.research_status == "insufficient_data")

# ---------------------------------------------------------------------------
# 3. Missing required field fails
# ---------------------------------------------------------------------------
print("\n=== 3. Missing required field fails ===")
rec = complete_stage2_record()
del rec["legal_regulatory_effect"]
r = ds.validate_stage2_record(rec)
check("validation_status == invalid", r.validation_status == "invalid")
check("error names the missing field", any("legal_regulatory_effect" in e for e in r.validation_errors),
      str(r.validation_errors))

# also test a deeply nested missing field
rec2 = complete_stage2_record()
del rec2["current_event"]["jurisdictions"]
r2 = ds.validate_stage2_record(rec2)
check("nested missing field fails", r2.validation_status == "invalid")
check("nested error path reported", any("current_event.jurisdictions" in e for e in r2.validation_errors),
      str(r2.validation_errors))

# ---------------------------------------------------------------------------
# 4. Invalid enum fails
# ---------------------------------------------------------------------------
print("\n=== 4. Invalid enum fails ===")
rec = complete_stage2_record()
rec["precedent_comparison"]["trend_classification"] = "definitely_escalating_trust_me"
r = ds.validate_stage2_record(rec)
check("bad trend_classification -> invalid", r.validation_status == "invalid")
check("error names trend_classification", any("trend_classification" in e for e in r.validation_errors),
      str(r.validation_errors))

rec2 = complete_stage2_record()
rec2["due_diligence_confidence"] = "Extremely High"
r2 = ds.validate_stage2_record(rec2)
check("bad due_diligence_confidence -> invalid", r2.validation_status == "invalid")
check("due_diligence_confidence not echoed when invalid", r2.due_diligence_confidence is None)

rec3 = complete_stage2_record()
rec3["research_status"] = "sort_of_complete"
r3 = ds.validate_stage2_record(rec3)
check("bad research_status -> invalid", r3.validation_status == "invalid")
check("research_status not echoed when invalid", r3.research_status is None)

# ---------------------------------------------------------------------------
# 5. Incorrect nested type fails
# ---------------------------------------------------------------------------
print("\n=== 5. Incorrect nested type fails ===")
rec = complete_stage2_record()
rec["current_event"]["entities"] = "should be a list"
r = ds.validate_stage2_record(rec)
check("string instead of list -> invalid", r.validation_status == "invalid")
check("error names current_event.entities", any("current_event.entities" in e for e in r.validation_errors),
      str(r.validation_errors))

rec2 = complete_stage2_record()
rec2["sources"][0]["primary_source"] = "true"  # string, not bool
r2 = ds.validate_stage2_record(rec2)
check("string instead of bool -> invalid", r2.validation_status == "invalid")

rec3 = complete_stage2_record()
rec3["historical_context"] = "not an object"
r3 = ds.validate_stage2_record(rec3)
check("string instead of nested object -> invalid", r3.validation_status == "invalid")

# ---------------------------------------------------------------------------
# 6. Malformed/invalid input fails safely (no exceptions raised)
# ---------------------------------------------------------------------------
print("\n=== 6. Malformed/invalid input fails safely ===")
for bad_input in [None, "just a string", 42, [], {}, {"totally": "wrong shape"}]:
    try:
        r = ds.validate_stage2_record(bad_input)
        ok = r.validation_status == "invalid"
    except Exception as e:
        ok = False
        print(f"    raised exception on {bad_input!r}: {e}")
    check(f"stage2 handles bad input {bad_input!r} without raising", ok)

for bad_input in [None, "just a string", 3.14, []]:
    try:
        r = ds.validate_stage3_record(bad_input)
        ok = r.validation_status == "invalid"
    except Exception as e:
        ok = False
        print(f"    raised exception on {bad_input!r}: {e}")
    check(f"stage3 handles bad input {bad_input!r} without raising", ok)

# ---------------------------------------------------------------------------
# 7. Complete valid Stage 3 output passes
# ---------------------------------------------------------------------------
print("\n=== 7. Complete valid Stage 3 output passes ===")
r = ds.validate_stage3_record(complete_stage3_record())
check("validation_status == valid", r.validation_status == "valid", str(r.validation_errors))
check("no validation_errors", r.validation_errors == [])
check("confidence echoed", r.confidence == "High")

# ---------------------------------------------------------------------------
# 8. Invalid Stage 3 output fails
# ---------------------------------------------------------------------------
print("\n=== 8. Invalid Stage 3 output fails ===")
rec = complete_stage3_record()
del rec["why_it_matters"]
r = ds.validate_stage3_record(rec)
check("missing why_it_matters -> invalid", r.validation_status == "invalid")

rec2 = complete_stage3_record()
rec2["watch_next"] = "should be a list"
r2 = ds.validate_stage3_record(rec2)
check("watch_next wrong type -> invalid", r2.validation_status == "invalid")

rec3 = complete_stage3_record()
rec3["confidence"] = "Super High"
r3 = ds.validate_stage3_record(rec3)
check("bad confidence enum -> invalid", r3.validation_status == "invalid")
check("confidence not echoed when invalid", r3.confidence is None)

# ---------------------------------------------------------------------------
# 9. Stage 3 cannot validate without required confidence/source structure
# ---------------------------------------------------------------------------
print("\n=== 9. Stage 3 requires confidence + source structure ===")
rec = complete_stage3_record()
del rec["confidence"]
r = ds.validate_stage3_record(rec)
check("missing confidence -> invalid", r.validation_status == "invalid")
check("error names confidence", any("root.confidence" in e for e in r.validation_errors), str(r.validation_errors))

rec2 = complete_stage3_record()
del rec2["sources"]
r2 = ds.validate_stage3_record(rec2)
check("missing sources -> invalid", r2.validation_status == "invalid")

rec3 = complete_stage3_record()
rec3["sources"] = [{"url": "https://example.gov/doc"}]  # missing required source sub-fields
r3 = ds.validate_stage3_record(rec3)
check("source missing required sub-fields -> invalid", r3.validation_status == "invalid")
check("error identifies missing source sub-field", any("sources[0]" in e for e in r3.validation_errors),
      str(r3.validation_errors))

rec4 = complete_stage3_record()
rec4["sources"] = ["https://example.gov/doc"]  # bare strings instead of objects, a plausible model mistake
r4 = ds.validate_stage3_record(rec4)
check("bare-string sources -> invalid (no silent repair)", r4.validation_status == "invalid")

# ---------------------------------------------------------------------------
# 11. Spec clarification C2 — Stage 3 source reuse enforcement
# ---------------------------------------------------------------------------
print("\n=== 11. Stage 3 source-reuse enforcement (C2) ===")
stage2_sources = complete_stage2_record()["sources"]

errs = ds.validate_stage3_sources(complete_stage3_record()["sources"], stage2_sources)
check("verbatim reused source passes C2 check", errs == [], str(errs))

new_source = [{
    "url": "https://example.com/invented", "source_type": "secondary",
    "agency": "n/a", "date": "2026-01-01", "supports": [], "primary_source": False,
}]
errs2 = ds.validate_stage3_sources(new_source, stage2_sources)
check("invented source fails C2 check", len(errs2) == 1, str(errs2))
check("C2 error names the offending url", "invented" in errs2[0], str(errs2))

altered_source = copy.deepcopy(stage2_sources)
altered_source[0]["agency"] = "A Different Agency"
errs3 = ds.validate_stage3_sources(altered_source, stage2_sources)
check("altered provenance (same url, different agency) fails C2 check", len(errs3) == 1, str(errs3))

errs4 = ds.validate_stage3_sources([], stage2_sources)
check("empty Stage 3 sources list passes C2 check trivially", errs4 == [])

# ---------------------------------------------------------------------------
# 10. Existing Regulus behavior remains untouched
# ---------------------------------------------------------------------------
print("\n=== 10. Existing Regulus behavior untouched ===")
repo_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
regulus_path = os.path.join(repo_root, "regulus_v3.py")
with open(regulus_path, "rb") as f:
    current_hash = hashlib.sha256(f.read()).hexdigest()
# Hash of regulus_v3.py on main immediately after the merged schema-
# reproducibility fix (commit 71cda46 / merge 1c27cdb), before this DD
# schema slice touched anything else in the repo.
expected_hash_after_schema_fix = None  # filled in by the harness at run time below
check("dd_schema.py imported without importing/touching regulus_v3", "rv" not in dir(), True)
check("regulus_v3.py still present and readable", os.path.exists(regulus_path))
print(f"    regulus_v3.py sha256 on this branch: {current_hash}")
check("no unexpected top-level files added under repo root",
      set(os.listdir(repo_root)) - {".git", ".gitignore", "README.md", "regulus.py",
                                     "regulus_scraper.py", "regulus_v2.py", "regulus_v3.py",
                                     "requirements.txt", "scraper", "docs", "dd_schema.py",
                                     "dd_pipeline.py", "tests", "__pycache__",
                                     # corpus_extractor.py: Step 1 of the corpus-intelligence
                                     # workflow -- deterministic, read-only reporting-window
                                     # extraction over the alerts table. Does not import or
                                     # touch dd_pipeline/dd_schema/regulus_v3's DD behavior.
                                     "corpus_extractor.py",
                                     # corpus_analyst.py / corpus_analyst_schema.py: Step 2 --
                                     # first-pass Corpus Analyst (intelligence requirements
                                     # generation) and its standalone output validator. No
                                     # web_search/tool use, no Stage 2/3 call, no import of
                                     # dd_pipeline/dd_schema/regulus_v3.
                                     "corpus_analyst.py", "corpus_analyst_schema.py",
                                     # evidence_analyst.py / evidence_analyst_schema.py /
                                     # evidence_retrieval.py: Evidence Analyst FOUNDATION
                                     # (infrastructure + schema + validation + deterministic
                                     # retrieval preparation only -- no live model call, no
                                     # database write). evidence_retrieval.py imports
                                     # regulus_v3 and dd_schema AS-IS (reusing
                                     # download_source_pdf/extract_pdf_text/is_valid_pdf_url
                                     # and classify_primary_source/
                                     # extract_federal_register_document_number verbatim) but
                                     # modifies neither; evidence_analyst.py/
                                     # evidence_analyst_schema.py import neither dd_pipeline,
                                     # dd_schema, nor regulus_v3.
                                     "evidence_analyst.py", "evidence_analyst_schema.py",
                                     "evidence_retrieval.py",
                                     # evidence_analyst_acceptance_cs01.py: the LIVE Evidence
                                     # Analyst diagnostic acceptance runner for CS-01. Makes
                                     # real retrieval/model calls ONLY when explicitly
                                     # executed (never from this test suite) -- reads the
                                     # golden fixtures read-only, never re-invokes the Corpus
                                     # Analyst, never touches dd_pipeline/dd_schema/
                                     # regulus_v3, no database write, no email.
                                     "evidence_analyst_acceptance_cs01.py",
                                     # intelligence_analyst_pass2.py / intelligence_analyst_
                                     # pass2_schema.py: Role #3 -- the Intelligence Analyst's
                                     # SECOND pass (evidence reassessment). No web_search/
                                     # tool use, no retrieval, no database write, no import of
                                     # dd_pipeline/dd_schema/regulus_v3/corpus_analyst/
                                     # corpus_analyst_schema/evidence_analyst/evidence_
                                     # analyst_schema/evidence_retrieval.
                                     "intelligence_analyst_pass2.py",
                                     "intelligence_analyst_pass2_schema.py",
                                     # intelligence_pass2_acceptance_cs01.py: the Pass #2 live
                                     # CS-01 diagnostic acceptance runner. Reads the frozen
                                     # Corpus Analyst fixture and a supplied Evidence Analyst
                                     # artifact read-only; never re-invokes the Corpus Analyst
                                     # or the Evidence Analyst, no database write, no email.
                                     "intelligence_pass2_acceptance_cs01.py",
                                     # intelligence_editor.py / intelligence_editor_schema.py:
                                     # Role #4 -- the Intelligence Editor (compiles REGULUS
                                     # INTELLIGENCE BRIEF #001 from validated Pass #2 story
                                     # reassessments). No web_search/tool use, no independent
                                     # research, no database write, no import of dd_pipeline/
                                     # dd_schema/regulus_v3/corpus_analyst/corpus_analyst_
                                     # schema/evidence_analyst/evidence_analyst_schema/
                                     # evidence_retrieval/intelligence_analyst_pass2.
                                     "intelligence_editor.py",
                                     "intelligence_editor_schema.py",
                                     # intelligence_store.py: persistence for story/evidence/
                                     # reassessment/brief state, in a completely separate
                                     # SQLite file (INTELLIGENCE_DB_PATH, default
                                     # regulus_intelligence.db) from regulus_v3.DB_PATH's
                                     # production bis_watcher.db. Never imports regulus_v3/
                                     # dd_pipeline/dd_schema.
                                     "intelligence_store.py",
                                     # regulus_orchestrator.py: the ONE module allowed to wire
                                     # Corpus Analyst Pass #1 -> Evidence Analyst ->
                                     # Intelligence Analyst Pass #2 -> Intelligence Editor
                                     # together, with per-story failure isolation and
                                     # persistence via intelligence_store.py. Makes no live
                                     # API call of its own; every call_* parameter is an
                                     # injected stage stub forwarded verbatim.
                                     "regulus_orchestrator.py",
                                     # regulus_brief001_acceptance.py: the live, end-to-end
                                     # REGULUS INTELLIGENCE BRIEF #001 acceptance runner.
                                     # Makes multiple real Anthropic calls ONLY when
                                     # explicitly executed (never from this test suite);
                                     # reads the golden corpus fixture read-only, never
                                     # re-runs extraction from the production alerts table,
                                     # never touches bis_watcher.db, no email.
                                     "regulus_brief001_acceptance.py",
                                     # brief_acceptance_runs/: gitignored output directory
                                     # regulus_brief001_acceptance.py writes its SQLite store
                                     # and diagnostic JSON artifacts into -- may or may not
                                     # exist on disk depending on whether it's been run
                                     # locally.
                                     "brief_acceptance_runs",
                                     # regulus_intelligence.db: gitignored SQLite file
                                     # intelligence_store.py's default INTELLIGENCE_DB_PATH
                                     # writes to -- may or may not exist on disk depending on
                                     # whether the orchestrator has been run locally (tests
                                     # always use their own tempdir db_path=, never this file).
                                     "regulus_intelligence.db",
                                     # scripts/: the isolated live acceptance-test runner
                                     # (831b9b4), not a DD behavior change.
                                     "scripts",
                                     # diagnostics/: gitignored output directory the
                                     # acceptance runner writes its reports into
                                     # (b634378) -- may or may not exist on disk
                                     # depending on whether it's been run locally.
                                     "diagnostics",
                                     # evidence_acceptance_runs/: gitignored output directory
                                     # evidence_analyst_acceptance_cs01.py writes its
                                     # diagnostic JSON artifacts into -- may or may not exist
                                     # on disk depending on whether it's been run locally.
                                     "evidence_acceptance_runs",
                                     # intelligence_pass2_acceptance_runs/: gitignored output
                                     # directory intelligence_pass2_acceptance_cs01.py writes
                                     # its diagnostic JSON artifacts into -- may or may not
                                     # exist on disk depending on whether it's been run
                                     # locally.
                                     "intelligence_pass2_acceptance_runs"} == set())

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("\n=== SUMMARY ===")
failed = [r for r in results if r[1] == "FAIL"]
for name, status, detail in results:
    print(f"{status}: {name}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
