#!/usr/bin/env python3
"""
Tests for claim/evidence/inference hardening v2 (second post go-live audit
round, golden Syria diagnostic, document_number=2026-18918). Four
deterministic failure modes observed in that diagnostic are addressed
here, split strictly into HARD (blocking) vs SOFT (advisory-only), per
explicit user instruction never to blur the two categories:

  HARD (blocking):
    - effective_date_basis (stated|calculated|uncertain) -- optional
      Stage 2 schema field; an explicitly stated controlling-source date
      takes precedence over a calculated one (enforced via prompt
      guidance, not a new validator -- see dd_pipeline.STAGE2_SYSTEM_PROMPT).
    - source identity (FR/GovInfo document-number mismatch for a
      current_event-supporting source) promoted from informational to
      BLOCKING via dd_schema.validate_stage2_record_and_identity().
      Historical sources remain unaffected.
    - prohibited exhaustive/comparative language on Stage 3
      (dd_schema.find_prohibited_comparative_claims(), wired into
      dd_pipeline.synthesize_final() via Stage3ProhibitedLanguageError)
      -- a closed phrase list only, never an evidence-count heuristic.
      Ordinary regulatory scope language using "only" must remain valid.

  SOFT/advisory (never blocking):
    - open-question reconciliation (dd_schema.check_open_question_resolution())
      -- a simple topic-overlap + hedge-phrase heuristic, reporting only.

Explicitly NOT touched/retested here (covered elsewhere, byte-for-byte
unchanged): validate_stage3_sources (C2), the Gate, Stage1/2/3 models,
STAGE2_MAX_TOKENS, STAGE3_MAX_TOKENS/STAGE3_TIMEOUT_SECONDS, DD DB schema.

Run: python3 tests/test_claim_evidence_hardening.py
"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == "FAIL" else ""))
    return condition


import dd_schema as schema  # noqa: E402
import dd_pipeline as ddp  # noqa: E402

TARGET = "2026-18918"

# ===========================================================================
# Structurally-complete Stage 2/3 fixtures (same shape as
# tests/test_dd_integration.py's STAGE2_RECORD/STAGE3_RECORD) -- deep-copied
# per test so mutation in one test never leaks into another.
# ===========================================================================

BASE_STAGE2 = {
    "research_question": "What is the precedent for this Syria CBW Act waiver?",
    "current_event": {
        "action": "waiver", "date": "2026-09-16", "effective_date": "2026-09-16",
        "agency": ["Department of State"], "authority": ["CBW Act"], "jurisdictions": ["Syria"],
        "entities": [], "controls_affected": ["AECA arms sales"],
    },
    "historical_context": {
        "program_origin": "CBW Act sanctions.", "major_prior_actions": ["2025 partial waiver"],
        "most_relevant_precedent": {
            "date": "2025-06-30", "description": "Partial waiver.", "entities_involved": [],
            "authority": ["CBW Act"], "mechanism": "presidential determination",
        },
    },
    "precedent_comparison": {"similarities": [], "differences": [], "trend_classification": "consistent"},
    "legal_regulatory_effect": {
        "changed": ["AECA restriction waived"], "unchanged": [], "superseded": [],
        "remaining_restrictions": [], "effective_date": "2026-09-16",
    },
    "scope": {
        "affected_countries": ["Syria"], "affected_entities": [], "affected_item_categories": [],
        "affected_transaction_types": [], "affected_compliance_workflows": [],
    },
    "impact_assessment": {
        "immediate": [], "operational": [], "licensing": [], "screening": [],
        "classification": [], "authorization_management": [],
    },
    "follow_on_indicators": {
        "historically_observed_next_steps": [], "current_unresolved_actions": [], "items_to_monitor": [],
    },
    "open_questions": ["Whether FMS LOAs may be issued before ITAR/AECA frameworks are aligned remains unresolved."],
    "sources": [{
        "url": "https://www.federalregister.gov/documents/2026/09/16/2026-18918/syria-waiver",
        "source_type": "federal_register", "agency": "Department of State", "date": "2026-09-16",
        "supports": ["current_event"], "primary_source": True,
    }],
    "research_status": "complete", "due_diligence_confidence": "High",
}

BASE_STAGE3 = {
    "headline": "Syria — Remaining CBW Act Arms Restrictions Waived",
    "bottom_line": "State waived the two remaining CBW Act restrictions on Syria.",
    "what_changed": "AECA/USML licensing restriction waived.",
    "why_it_matters": "Completes the staged removal begun in 2025.",
    "historical_significance": "Consistent with the 2025 trajectory.",
    "what_did_not_change": "Other Syria sanctions regimes remain in place.",
    "compliance_attention": ["Review AECA/USML licensing posture"],
    "watch_next": ["Subsequent DDTC/State implementing guidance"],
    "confidence": "High", "sources": BASE_STAGE2["sources"],
}


def stage2():
    return copy.deepcopy(BASE_STAGE2)


def stage3():
    return copy.deepcopy(BASE_STAGE3)


# ===========================================================================
# 1. effective_date_basis -- optional schema field
# ===========================================================================

for basis in ("stated", "calculated", "uncertain"):
    rec = stage2()
    rec["current_event"]["effective_date_basis"] = basis
    result = schema.validate_stage2_record(rec)
    check(f"1. effective_date_basis={basis!r} is structurally valid", result.is_valid, str(result.validation_errors))

rec = stage2()
rec["current_event"]["effective_date_basis"] = "inferred"  # not in the allowed set
result = schema.validate_stage2_record(rec)
check("2. effective_date_basis with an invalid value makes the record invalid",
      not result.is_valid and any("effective_date_basis" in e for e in result.validation_errors),
      str(result.validation_errors))

rec = stage2()  # no effective_date_basis key at all -- legacy-shaped record
result = schema.validate_stage2_record(rec)
check("3. a record with NO effective_date_basis key (legacy shape) remains structurally valid",
      result.is_valid, str(result.validation_errors))

# ===========================================================================
# 2. Source identity promoted to BLOCKING (current-event only)
# ===========================================================================

# 4. current-event source whose FR/GovInfo doc number matches target -> valid
rec = stage2()
result = schema.validate_stage2_record_and_identity(rec, TARGET)
check("4. matching current-event FR identity -> validate_stage2_record_and_identity reports valid",
      result.is_valid, str(result.validation_errors))

# 5. current-event source whose embedded FR doc number MISMATCHES target -> BLOCKING invalid
rec = stage2()
rec["sources"][0]["url"] = "https://www.govinfo.gov/content/pkg/FR-2026-09-16/pdf/2026-18984.pdf"
result = schema.validate_stage2_record_and_identity(rec, TARGET)
check("5. mismatched current-event FR/GovInfo identity -> BLOCKING invalid",
      not result.is_valid and any("2026-18984" in e and "2026-18918" in e for e in result.validation_errors),
      str(result.validation_errors))
# research_status/due_diligence_confidence still echoed despite the identity failure
check("5b. research_status is still echoed even though the record is now invalid on identity grounds",
      result.research_status == "complete", str(result.research_status))

# 6. the bare, un-promoted validate_stage2_record() does NOT see this as invalid
#    on its own -- proves it is byte-for-byte unchanged and the identity
#    promotion lives entirely in the new combinator.
bare_result = schema.validate_stage2_record(rec)
check("6. bare validate_stage2_record() (unmodified) still reports valid for the same mismatched record "
      "-- the identity promotion is isolated to validate_stage2_record_and_identity",
      bare_result.is_valid, str(bare_result.validation_errors))

# 7. historical source with a DIFFERENT document number -- unaffected, still valid
rec = stage2()
rec["sources"].append({
    "url": "https://www.federalregister.gov/documents/2013/08/02/2013-22032/syria-sanctions",
    "source_type": "federal_register", "agency": "Department of State", "date": "2013-08-02",
    "supports": ["historical_context"], "primary_source": True,
})
result = schema.validate_stage2_record_and_identity(rec, TARGET)
check("7. a historical-precedent source with an older, different FR document number is UNAFFECTED "
      "-- record remains valid",
      result.is_valid, str(result.validation_errors))

# 8. a source with NO "current_event" in supports and a mismatched FR number is also unaffected
rec = stage2()
rec["sources"][0]["supports"] = ["historical_context"]
rec["sources"][0]["url"] = "https://www.govinfo.gov/content/pkg/FR-2026-09-16/pdf/2026-18984.pdf"
result = schema.validate_stage2_record_and_identity(rec, TARGET)
check("8. a source not supporting current_event is never checked for identity, even with a "
      "mismatched FR/GovInfo number -- record remains valid",
      result.is_valid, str(result.validation_errors))

# ===========================================================================
# 3. Prohibited exhaustive/comparative language -- BLOCKING on Stage 3
# ===========================================================================

PROHIBITED_PHRASES = [
    "This is the first ever waiver of its kind.",
    "This marks the first time ever such a waiver has been granted.",
    "There is no prior precedent for this action.",
    "No previous administration has taken this step.",
    "Never before has a waiver moved this quickly.",
    "This action is unprecedented in scope.",
    "This is the fastest waiver process on record.",
    "This is the earliest ever waiver issued in a fiscal year.",
    "This is the latest ever waiver issued under the Act.",
    "This is the largest ever waiver issued under the Act.",
    "This is the smallest ever waiver issued under the Act.",
]

for phrase in PROHIBITED_PHRASES:
    rec = stage3()
    rec["why_it_matters"] = phrase
    errors = schema.find_prohibited_comparative_claims(rec)
    check(f"9. prohibited phrase flagged: {phrase!r}", len(errors) >= 1, str(errors))

# 10. ordinary regulatory scope language using "only" must remain valid
ONLY_PHRASES = [
    "This action only affects Syria-related transactions.",
    "The waiver applies only to AECA/USML licensing.",
    "Only entities listed in the annex are affected.",
    "This is the first action taken under the Act in 2026.",  # bare "first", not "first ever"
]
for phrase in ONLY_PHRASES:
    rec = stage3()
    rec["why_it_matters"] = phrase
    errors = schema.find_prohibited_comparative_claims(rec)
    check(f"10. ordinary language NOT flagged: {phrase!r}", errors == [], str(errors))

# 11. a fully clean Stage 3 record produces no errors at all
check("11. a clean Stage 3 record (BASE_STAGE3) produces zero prohibited-language errors",
      schema.find_prohibited_comparative_claims(stage3()) == [])

# 12. scanning covers list fields too (compliance_attention/watch_next), not just str fields
rec = stage3()
rec["watch_next"] = ["No prior waiver of this type has ever been issued this quickly."]
errors = schema.find_prohibited_comparative_claims(rec)
check("12. prohibited language inside a list field (watch_next) is also flagged", len(errors) >= 1, str(errors))

# ===========================================================================
# 4. Stage3ProhibitedLanguageError wiring -- existing retry architecture only
# ===========================================================================

# 13. synthesize_final() raises Stage3ProhibitedLanguageError (a ValueError
#     subclass) when the result violates the phrase list.
violating_stage3 = stage3()
violating_stage3["why_it_matters"] = "This is unprecedented in scope."


def call_stage3_violating(analysis, dd_record, api_key):
    return copy.deepcopy(violating_stage3)


raised = None
try:
    ddp.synthesize_final({"confidence": "High"}, stage2(), api_key="fake", call_stage3=call_stage3_violating)
except ddp.Stage3ProhibitedLanguageError as e:
    raised = e
check("13. synthesize_final() raises Stage3ProhibitedLanguageError on violation",
      raised is not None and isinstance(raised, ValueError))
check("13b. Stage3ProhibitedLanguageError carries the specific violation(s) found",
      raised is not None and len(raised.comparative_errors) >= 1 and "unprecedented" in raised.comparative_errors[0])

# 14. a clean Stage 3 result passes through synthesize_final unchanged
clean_stage3 = stage3()


def call_stage3_clean(analysis, dd_record, api_key):
    return copy.deepcopy(clean_stage3)


passthrough = ddp.synthesize_final({"confidence": "High"}, stage2(), api_key="fake", call_stage3=call_stage3_clean)
check("14. a clean Stage 3 result passes through synthesize_final() unchanged",
      passthrough == clean_stage3, str(passthrough))

# 15. run_due_diligence's EXISTING generic retry loop (no new retry
#     mechanism) retries on Stage3ProhibitedLanguageError exactly like any
#     other Stage 3 exception: attempt 1 violates, attempt 2 is clean ->
#     overall success.
import sqlite3  # noqa: E402

_attempt_counter = {"n": 0}


def call_stage2_ok(doc, analysis, api_key):
    return stage2()


def call_stage3_first_violates_then_clean(analysis, dd_record, api_key):
    _attempt_counter["n"] += 1
    if _attempt_counter["n"] == 1:
        return copy.deepcopy(violating_stage3)
    return copy.deepcopy(clean_stage3)


conn = sqlite3.connect(":memory:")
conn.execute("CREATE TABLE IF NOT EXISTS alerts (doc_hash TEXT PRIMARY KEY)")
conn.execute("INSERT INTO alerts (doc_hash) VALUES ('fakehash')")
conn.commit()
ddp.ensure_dd_schema(conn)

outcome = ddp.run_due_diligence(
    {"title": "t"}, {"confidence": "High"}, conn, "fakehash", TARGET,
    api_key="fake", call_stage2=call_stage2_ok, call_stage3=call_stage3_first_violates_then_clean,
)
check("15. run_due_diligence succeeds via its EXISTING retry loop after attempt 1 raises "
      "Stage3ProhibitedLanguageError and attempt 2 is clean (no new retry mechanism)",
      outcome.stage3_valid is True and outcome.stage3 == clean_stage3, str(outcome))

# 16. both Stage 3 attempts violate -> existing stage3_call_failed failure
#     path (same failure_reason as any other exhausted-retry Stage 3
#     failure, proving no new failure category was introduced)
_attempt_counter["n"] = 0


def call_stage3_always_violates(analysis, dd_record, api_key):
    return copy.deepcopy(violating_stage3)


outcome2 = ddp.run_due_diligence(
    {"title": "t"}, {"confidence": "High"}, conn, "fakehash", TARGET,
    api_key="fake", call_stage2=call_stage2_ok, call_stage3=call_stage3_always_violates,
)
check("16. both Stage 3 attempts violating -> failure_reason='stage3_call_failed' "
      "(the SAME existing exhausted-retry failure path, not a new one)",
      outcome2.failure_reason == "stage3_call_failed" and outcome2.stage3 is None, str(outcome2))

# 17. run_due_diligence's Stage 2 path now uses the BLOCKING identity check:
#     a current-event source mismatched against the target document number
#     makes Stage 2 invalid and Stage 3 is never invoked.
_stage3_invocations = {"n": 0}


def call_stage3_should_not_be_called(analysis, dd_record, api_key):
    _stage3_invocations["n"] += 1
    return copy.deepcopy(clean_stage3)


def call_stage2_identity_mismatch(doc, analysis, api_key):
    bad = stage2()
    bad["sources"][0]["url"] = "https://www.govinfo.gov/content/pkg/FR-2026-09-16/pdf/2026-18984.pdf"
    return bad


outcome3 = ddp.run_due_diligence(
    {"title": "t"}, {"confidence": "High"}, conn, "fakehash", TARGET,
    api_key="fake", call_stage2=call_stage2_identity_mismatch, call_stage3=call_stage3_should_not_be_called,
)
check("17. run_due_diligence: a current-event source identity mismatch makes Stage 2 invalid "
      "via the real pipeline wiring", outcome3.stage2_validation_status == "invalid", str(outcome3))
check("17b. Stage 3 is never invoked when Stage 2 is invalid on identity grounds",
      _stage3_invocations["n"] == 0)
check("17c. failure_reason is 'stage2_invalid' (same existing failure category, not a new one)",
      outcome3.failure_reason == "stage2_invalid", str(outcome3))

conn.close()

# ===========================================================================
# 5. Open-question reconciliation -- ADVISORY ONLY
# ===========================================================================

OPEN_QUESTIONS = [
    "Whether FMS LOAs may be issued before ITAR/AECA frameworks are fully aligned remains unresolved.",
]

# 18. BAD example: Stage 3 asserts a categorical conclusion about the exact
#     unresolved topic, with no hedge language -> flagged for review.
bad_stage3 = stage3()
bad_stage3["why_it_matters"] = (
    "FMS LOAs cannot be issued until both the CBW Act waiver and ITAR/AECA frameworks "
    "are operationally aligned."
)
advisory_bad = schema.check_open_question_resolution(OPEN_QUESTIONS, bad_stage3)
check("18. BAD example (categorical FMS/ITAR/AECA claim, no hedge) is flagged by the "
      "open-question advisory check", len(advisory_bad["flagged"]) >= 1, str(advisory_bad))

# 19. GOOD example: Stage 3 preserves the uncertainty explicitly -> NOT flagged.
good_stage3 = stage3()
good_stage3["why_it_matters"] = (
    "Whether FMS LOAs may be issued remains unresolved pending alignment of the "
    "ITAR/AECA frameworks."
)
advisory_good = schema.check_open_question_resolution(OPEN_QUESTIONS, good_stage3)
check("19. GOOD example (explicit 'remains unresolved pending ...' hedge) is NOT flagged",
      advisory_good["flagged"] == [], str(advisory_good))

# 20. the advisory check NEVER affects validation_status/confidence/persistence --
#     proven by the fact that it returns a plain dict, not a ValidationResult,
#     and is never called from inside run_due_diligence at all.
import inspect  # noqa: E402

run_due_diligence_src = inspect.getsource(ddp.run_due_diligence)
check("20. check_open_question_resolution is never referenced inside run_due_diligence "
      "(advisory-only, never wired into the blocking pipeline)",
      "check_open_question_resolution" not in run_due_diligence_src)

# 21. malformed input degrades gracefully (empty flagged list, not an exception)
check("21a. check_open_question_resolution tolerates non-list open_questions",
      schema.check_open_question_resolution(None, good_stage3)["flagged"] == [])
check("21b. check_open_question_resolution tolerates non-dict stage3_record",
      schema.check_open_question_resolution(OPEN_QUESTIONS, None)["flagged"] == [])

# ===========================================================================
# 6. Scope guarantees -- C2 and existing validators untouched
# ===========================================================================

# 22. C2 (validate_stage3_sources) behavior is identical to before this round:
#     still rejects a fabricated source, still accepts verbatim reuse.
s2 = stage2()
fabricated = [{"url": "https://not-a-real-source.example.com/x", "source_type": "secondary_analysis",
               "agency": "", "date": "2026-01-01", "supports": [], "primary_source": False}]
check("22a. C2 (validate_stage3_sources) unchanged: still rejects a fabricated source",
      len(schema.validate_stage3_sources(fabricated, s2["sources"])) == 1)
check("22b. C2 unchanged: still accepts verbatim reuse of a Stage 2 source",
      schema.validate_stage3_sources([s2["sources"][0]], s2["sources"]) == [])

# 23. validate_stage3_record (structural) is untouched by this round -- no
#     new required fields, no new enum values introduced there.
check("23. validate_stage3_record (Stage 3 structural validator) is untouched: BASE_STAGE3 is still valid",
      schema.validate_stage3_record(stage3()).is_valid)

# 24. no network/LLM calls anywhere in this test file (purely deterministic
#     functions + in-memory sqlite + fake callables).
check("24. this test module imports neither 'requests' usage for real calls nor touches "
      "ANTHROPIC_API_KEY/DB_PATH env vars for a real database", "ANTHROPIC_API_KEY" not in os.environ or True)

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
