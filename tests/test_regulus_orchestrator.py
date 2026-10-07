#!/usr/bin/env python3
"""
Tests for regulus_orchestrator.py — the end-to-end wiring of Corpus
Analyst Pass #1 -> Evidence Analyst -> Intelligence Analyst Pass #2 ->
Intelligence Editor, with persistence and per-story failure isolation.

No REAL Anthropic call is made anywhere in this file. Every stage's
call_analyst (and evidence retrieval) is an injected stub operating over
the REAL golden fixtures (tests/fixtures/corpus_acceptance_3.json +
corpus_analyst_acceptance_3.json). requests.post is monkeypatched to
raise if ever actually invoked, as an additional, stronger guarantee.
Each test uses its own throwaway SQLite file inside a tempdir.

Run: python3 tests/test_regulus_orchestrator.py
"""
import copy
import json
import os
import shutil
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


import regulus_orchestrator as orch
import evidence_retrieval as er
import intelligence_store as store
import regulus_brief001_acceptance as brief_acc
from _evidence_test_helpers import load_acceptance_3, get_story, load_corpus_acceptance_3

ACCEPTANCE_3 = load_acceptance_3()          # real 11-candidate-story Corpus Analyst output
DEV_CORPUS = load_corpus_acceptance_3()      # real 87-observation corpus
REPORTING_PERIOD = {"start": "2026-09-14", "end": "2026-10-05"}

# ===========================================================================
# Guard against any REAL network call for the whole file.
# ===========================================================================
import requests as _requests_module

_real_requests_post = _requests_module.post
_network_call_attempted = {"flag": False}


def _guard_requests_post(*args, **kwargs):
    _network_call_attempted["flag"] = True
    raise AssertionError(
        "test_regulus_orchestrator.py attempted a REAL network POST -- "
        "every test must supply its own stage stubs"
    )


_requests_module.post = _guard_requests_post

_TMPDIR = tempfile.mkdtemp(prefix="regulus_orchestrator_test_")


def _fresh_db_path(name):
    return os.path.join(_TMPDIR, name)


# ===========================================================================
# Stage stubs, generic over any candidate_story (never hardcoded to a
# specific Syria/CS-01 outcome -- these are structural stand-ins, not
# analytical judgments).
# ===========================================================================


def _corpus_analyst_stub_all():
    """Returns the REAL, unmodified 11-story Corpus Analyst output."""
    def _stub(corpus_payload, api_key):
        return copy.deepcopy(ACCEPTANCE_3)
    return _stub


def _corpus_analyst_stub_subset(story_ids):
    stories = [get_story(ACCEPTANCE_3, sid) for sid in story_ids]

    def _stub(corpus_payload, api_key):
        raw = copy.deepcopy(ACCEPTANCE_3)
        raw["candidate_stories"] = stories
        return raw
    return _stub


def _fake_retrieve(candidate_story, corpus, **kwargs):
    docs = []
    for doc_num in candidate_story["supporting_document_numbers"]:
        docs.append(er.RetrievedDocument(
            document_number=doc_num, status="retrieved", identity_status="verified",
            source_url=f"https://www.federalregister.gov/documents/x/{doc_num}",
            primary_source=True, text="Synthetic retrieved text.", text_source="pdf_full",
            retrieved_at="2026-10-06T00:00:00+00:00",
        ))
    return er.EvidenceRetrievalBundle(story_id=candidate_story["story_id"], documents=docs)


def _valid_evidence_output(candidate_story, retrieval_bundle, corpus, api_key=None):
    docs = candidate_story["supporting_document_numbers"]
    questions = candidate_story["research_questions"]
    # One evidence_record per supplied/retrieved primary document -- the
    # live Evidence Analyst's own additive check (_find_silently_dropped_
    # documents) rejects an output that successfully retrieves a document
    # but never cites it, so a stand-in output must cite every one too.
    evidence_records = [
        {
            "evidence_id": f"EV-{i + 1}",
            "document_number": doc_num,
            "source_title": "Synthetic title",
            "source_url": f"https://www.federalregister.gov/documents/x/{doc_num}",
            "source_type": "primary",
            "primary_source": True,
            "retrieved_at": "2026-10-06T00:00:00+00:00",
            "source_identity_status": "verified",
            "publication_date": "2026-09-16",
            "effective_date": "2026-09-16",
            "relevant_excerpt": "Synthetic excerpt.",
            "supports": [],
            "contradicts": [],
            "limitations": "",
        }
        for i, doc_num in enumerate(docs)
    ]
    return {
        "story_id": candidate_story["story_id"],
        "research_status": "partial",
        "hypothesis_assessment": "unresolved",
        "question_findings": [
            {"question": q, "status": "unanswered", "finding": "", "evidence_ids": [], "confidence": "low"}
            for q in questions
        ],
        "evidence_records": evidence_records,
        "contradictions": [],
        "remaining_gaps": [],
        "disconfirming_evidence_found": [],
        "overall_assessment": "Synthetic overall assessment.",
        "confidence": "low",
    }


def _evidence_call_stub(candidate_story, retrieval_bundle, corpus, api_key):
    return _valid_evidence_output(candidate_story, retrieval_bundle, corpus, api_key)


def _valid_pass2_output(story_id, original_story, evidence_package, api_key=None,
                         editor_eligibility="eligible", disposition="confirmed_with_modification"):
    alt_hypotheses = original_story.get("alternative_hypotheses") or []
    alt_assessments = [
        {"hypothesis": h, "disposition": "unresolved", "explanation": "Synthetic.", "evidence_ids": []}
        for h in alt_hypotheses
    ]
    return {
        "story_id": story_id,
        "assessment_disposition": disposition,
        "original_hypothesis": original_story.get("preliminary_hypothesis", "Synthetic."),
        "revised_hypothesis": "Synthetic revised hypothesis.",
        "material_changes": [],
        "supported_findings": ["Synthetic supported finding."],
        "weakened_or_rejected_findings": [],
        "remaining_uncertainties": [],
        "alternative_hypotheses_assessment": alt_assessments,
        "intelligence_assessment": "Synthetic intelligence assessment.",
        "confidence": "medium",
        "editor_eligibility": editor_eligibility,
        "editor_caveats": [],
    }


def _pass2_call_stub(story_id, original_story, evidence_package, api_key):
    return _valid_pass2_output(story_id, original_story, evidence_package, api_key)


def _editor_call_stub(run_id, reporting_period, editor_inputs, api_key):
    story_ids = [e["story_id"] for e in editor_inputs]
    items = [
        {"text": f"Development for {sid}.", "story_ids": [sid], "evidence_ids": list(e["evidence_ids"])}
        for sid, e in zip(story_ids, editor_inputs)
    ]
    all_evidence_ids = sorted({eid for e in editor_inputs for eid in e["evidence_ids"]})
    return {
        "brief_id": f"{run_id}-brief-001",
        "reporting_period": reporting_period,
        "executive_assessment": "Synthetic executive assessment across all included stories.",
        "regulatory_tempo": {"summary": "Synthetic tempo summary.", "items": []},
        "targeting_and_policy_direction": {"summary": "Synthetic targeting summary.", "items": []},
        "key_developments": {"summary": "Synthetic key developments summary.", "items": items},
        "cross_agency_signals": {"summary": "Synthetic cross-agency summary.", "items": []},
        "emerging_patterns": {"summary": "Synthetic emerging patterns summary.", "items": []},
        "watchlist": {"summary": "Synthetic watchlist summary.", "items": []},
        "methodology_and_sources": {
            "summary": "Synthetic methodology summary.",
            "stories_included": story_ids,
            "stories_excluded": [],
            "evidence_ids_referenced": all_evidence_ids,
        },
        "confidence": "medium",
        "stories_included": story_ids,
        "stories_excluded": [],
    }


# ===========================================================================
# A. Full end-to-end happy path over ALL 11 real CS-01..CS-11 stories
# ===========================================================================
db_path = _fresh_db_path("a.db")
outcome = orch.run_intelligence_cycle(
    "run-a", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=_corpus_analyst_stub_all(),
    call_evidence_analyst=_evidence_call_stub, retrieve=_fake_retrieve,
    call_pass2=_pass2_call_stub, call_editor=_editor_call_stub,
)
check("A1. corpus_analysis_is_valid is True over the real 11-story fixture",
      outcome.corpus_analysis_is_valid, outcome.corpus_analysis_failure_reason or "")
check("A2. all 11 stories are processed", len(outcome.stories) == len(ACCEPTANCE_3["candidate_stories"]))
check("A3. all 11 stories are included (eligible)", len(outcome.included_story_ids) == 11,
      str([s.exclusion_reason for s in outcome.stories if not s.included]))
check("A4. editor_inputs_count matches the number of included stories",
      outcome.editor_inputs_count == len(outcome.included_story_ids))
check("A5. a Brief outcome was produced and is valid", outcome.brief_outcome is not None and outcome.brief_outcome.is_valid,
      str(outcome.brief_outcome.validation_errors) if outcome.brief_outcome else "no brief_outcome")
check("A6. brief_id was set", outcome.brief_id == "run-a-brief-001")

# Persistence is actually exercised: the brief and every stage can be reloaded.
persisted_brief = store.load_brief("run-a", "run-a-brief-001", db_path=db_path)
check("A7. the Brief was actually persisted and reloads", persisted_brief is not None and persisted_brief.is_valid)
persisted_pass2 = store.list_story_artifacts("run-a", stage="pass2", db_path=db_path)
check("A8. all 11 stories have a persisted pass2 artifact", len(persisted_pass2) == 11)

# ===========================================================================
# B. Cycle-level failure: Corpus Analyst Pass #1 itself invalid -> no
# stories processed at all, Editor never called
# ===========================================================================
db_path = _fresh_db_path("b.db")
_editor_called = {"flag": False}


def _editor_should_never_be_called(run_id, reporting_period, editor_inputs, api_key):
    _editor_called["flag"] = True
    return _editor_call_stub(run_id, reporting_period, editor_inputs, api_key)


def _corpus_analyst_bad_stub(corpus_payload, api_key):
    return {"candidate_stories": [{"story_id": "CS-INVALID-SHAPE"}]}  # missing required fields


outcome = orch.run_intelligence_cycle(
    "run-b", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=_corpus_analyst_bad_stub,
    call_evidence_analyst=_evidence_call_stub, retrieve=_fake_retrieve,
    call_pass2=_pass2_call_stub, call_editor=_editor_should_never_be_called,
)
check("B1. an invalid Corpus Analyst output makes the cycle-level corpus_analysis_is_valid False",
      not outcome.corpus_analysis_is_valid)
check("B2. corpus_analysis_failure_reason is set", outcome.corpus_analysis_failure_reason is not None)
check("B3. no stories are processed when Pass #1 itself is invalid", outcome.stories == [])
check("B4. the Editor is never called when there are no candidate stories", not _editor_called["flag"])

# ===========================================================================
# C. Per-story failure isolation: one story's Evidence Analyst raises an
# unexpected exception, one story's Pass #2 is schema-invalid, one story
# succeeds fully -- all three are processed independently and the
# successful one still reaches the Brief
# ===========================================================================
db_path = _fresh_db_path("c.db")
THREE_STORY_IDS = [s["story_id"] for s in ACCEPTANCE_3["candidate_stories"][:3]]
BROKEN_EVIDENCE_STORY, BROKEN_PASS2_STORY, HEALTHY_STORY = THREE_STORY_IDS


def _evidence_stub_one_breaks(candidate_story, retrieval_bundle, corpus, api_key):
    if candidate_story["story_id"] == BROKEN_EVIDENCE_STORY:
        raise RuntimeError("simulated unexpected Evidence Analyst failure")
    return _valid_evidence_output(candidate_story, retrieval_bundle, corpus, api_key)


def _pass2_stub_one_invalid(story_id, original_story, evidence_package, api_key):
    if story_id == BROKEN_PASS2_STORY:
        raw = _valid_pass2_output(story_id, original_story, evidence_package, api_key)
        raw["assessment_disposition"] = "bogus_enum_value"  # schema-invalid
        return raw
    return _valid_pass2_output(story_id, original_story, evidence_package, api_key)


outcome = orch.run_intelligence_cycle(
    "run-c", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=_corpus_analyst_stub_subset(THREE_STORY_IDS),
    call_evidence_analyst=_evidence_stub_one_breaks, retrieve=_fake_retrieve,
    call_pass2=_pass2_stub_one_invalid, call_editor=_editor_call_stub,
)
check("C1. all three stories are still processed (one failure does not abort the loop)",
      len(outcome.stories) == 3)
results_by_id = {s.story_id: s for s in outcome.stories}
check("C2. the story whose Evidence Analyst call raised is excluded with a machine-readable reason "
      "(run_live_evidence_analysis itself catches the call_analyst exception and reports it via "
      "failure_reason -- it never propagates up to the orchestrator's own defensive try/except)",
      not results_by_id[BROKEN_EVIDENCE_STORY].included
      and results_by_id[BROKEN_EVIDENCE_STORY].exclusion_reason == "evidence_analyst_call_failed")
check("C3. the story whose Pass #2 output was schema-invalid is excluded, evidence stage recorded valid",
      not results_by_id[BROKEN_PASS2_STORY].included
      and results_by_id[BROKEN_PASS2_STORY].evidence_is_valid is True
      and results_by_id[BROKEN_PASS2_STORY].pass2_is_valid is False)
check("C4. the healthy story is included", results_by_id[HEALTHY_STORY].included)
check("C5. the Brief is still produced and valid, over only the one healthy story",
      outcome.brief_outcome is not None and outcome.brief_outcome.is_valid, )
check("C6. editor_inputs_count reflects only the surviving story", outcome.editor_inputs_count == 1)

# A broken story's failure is itself persisted (inspectable later), not silently dropped.
broken_evidence_artifact = store.load_story_artifact("run-c", BROKEN_EVIDENCE_STORY, "evidence_analyst", db_path=db_path)
check("C7. the broken-evidence story's failure is persisted with is_valid=False",
      broken_evidence_artifact is not None and broken_evidence_artifact.is_valid is False)

# ===========================================================================
# D. Pass #2 -> Editor eligibility filtering end-to-end: editor_eligibility
# = "not_eligible" excludes a story that is otherwise schema-VALID
# ===========================================================================
db_path = _fresh_db_path("d.db")
TWO_STORY_IDS = [s["story_id"] for s in ACCEPTANCE_3["candidate_stories"][:2]]
NOT_ELIGIBLE_STORY, ELIGIBLE_STORY = TWO_STORY_IDS


def _pass2_stub_one_not_eligible(story_id, original_story, evidence_package, api_key):
    eligibility = "not_eligible" if story_id == NOT_ELIGIBLE_STORY else "eligible"
    disposition = "contradicted" if story_id == NOT_ELIGIBLE_STORY else "confirmed_with_modification"
    return _valid_pass2_output(story_id, original_story, evidence_package, api_key,
                                editor_eligibility=eligibility, disposition=disposition)


outcome = orch.run_intelligence_cycle(
    "run-d", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=_corpus_analyst_stub_subset(TWO_STORY_IDS),
    call_evidence_analyst=_evidence_call_stub, retrieve=_fake_retrieve,
    call_pass2=_pass2_stub_one_not_eligible, call_editor=_editor_call_stub,
)
results_by_id = {s.story_id: s for s in outcome.stories}
check("D1. a schema-valid story with editor_eligibility='not_eligible' is excluded "
      "(eligibility filtering, not a validity failure)",
      not results_by_id[NOT_ELIGIBLE_STORY].included
      and results_by_id[NOT_ELIGIBLE_STORY].pass2_is_valid is True
      and results_by_id[NOT_ELIGIBLE_STORY].exclusion_reason == "pass2_editor_eligibility_not_eligible")
check("D2. the eligible story is included", results_by_id[ELIGIBLE_STORY].included)
check("D3. only the eligible story reaches the Editor", outcome.editor_inputs_count == 1)

# ===========================================================================
# E. Incomplete/insufficient story: assessment_disposition="insufficient_
# evidence" with editor_eligibility="eligible_with_caveats" is still
# INCLUDED (Pass #2 schema allows this shape; it's an editorial choice
# where to place it in the Brief, not a validity failure)
# ===========================================================================
db_path = _fresh_db_path("e.db")
ONE_STORY_ID = ACCEPTANCE_3["candidate_stories"][0]["story_id"]


def _pass2_stub_insufficient(story_id, original_story, evidence_package, api_key):
    return _valid_pass2_output(story_id, original_story, evidence_package, api_key,
                                editor_eligibility="eligible_with_caveats",
                                disposition="insufficient_evidence")


outcome = orch.run_intelligence_cycle(
    "run-e", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=_corpus_analyst_stub_subset([ONE_STORY_ID]),
    call_evidence_analyst=_evidence_call_stub, retrieve=_fake_retrieve,
    call_pass2=_pass2_stub_insufficient, call_editor=_editor_call_stub,
)
check("E1. an insufficient_evidence/eligible_with_caveats story is included, not auto-excluded",
      outcome.stories[0].included, str(outcome.stories[0].exclusion_reason))

# ===========================================================================
# F. No eligible stories at all -> the Editor is never called, brief_outcome
# stays None
# ===========================================================================
db_path = _fresh_db_path("f.db")
_editor_called_f = {"flag": False}


def _editor_should_never_be_called_f(run_id, reporting_period, editor_inputs, api_key):
    _editor_called_f["flag"] = True
    return _editor_call_stub(run_id, reporting_period, editor_inputs, api_key)


def _pass2_stub_all_not_eligible(story_id, original_story, evidence_package, api_key):
    return _valid_pass2_output(story_id, original_story, evidence_package, api_key,
                                editor_eligibility="not_eligible", disposition="contradicted")


outcome = orch.run_intelligence_cycle(
    "run-f", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=_corpus_analyst_stub_subset([ONE_STORY_ID]),
    call_evidence_analyst=_evidence_call_stub, retrieve=_fake_retrieve,
    call_pass2=_pass2_stub_all_not_eligible, call_editor=_editor_should_never_be_called_f,
)
check("F1. brief_outcome stays None when no story is eligible", outcome.brief_outcome is None)
check("F2. brief_skipped_reason is set", outcome.brief_skipped_reason == "no_eligible_stories")
check("F3. the Editor is never called when editor_inputs is empty", not _editor_called_f["flag"])

# ===========================================================================
# G. Bounded retries: the orchestrator calls each stage's run_* entry
# point exactly ONCE per story -- it does not wrap its own retry loop
# around a stage (which would double that stage's own *_MAX_ATTEMPTS
# budget). A stage-internal transient failure followed by success (that
# stage's OWN retry) must still show exactly one orchestrator-level call
# to run_live_evidence_analysis per story.
# ===========================================================================
db_path = _fresh_db_path("g.db")
_evidence_stage_entry_calls = {"n": 0}
_real_run_live_evidence_analysis = __import__("evidence_analyst").run_live_evidence_analysis


def _counting_run_live_evidence_analysis(*args, **kwargs):
    _evidence_stage_entry_calls["n"] += 1
    return _real_run_live_evidence_analysis(*args, **kwargs)


_retry_internal_calls = {"n": 0}


def _evidence_call_transient_then_ok(candidate_story, retrieval_bundle, corpus, api_key):
    _retry_internal_calls["n"] += 1
    if _retry_internal_calls["n"] == 1:
        raise ConnectionError("simulated transient Evidence Analyst connection failure")
    return _valid_evidence_output(candidate_story, retrieval_bundle, corpus, api_key)


orch.evidence_analyst.run_live_evidence_analysis = _counting_run_live_evidence_analysis
try:
    outcome = orch.run_intelligence_cycle(
        "run-g", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
        call_corpus_analyst=_corpus_analyst_stub_subset([ONE_STORY_ID]),
        call_evidence_analyst=_evidence_call_transient_then_ok, retrieve=_fake_retrieve,
        call_pass2=_pass2_call_stub, call_editor=_editor_call_stub,
    )
finally:
    orch.evidence_analyst.run_live_evidence_analysis = _real_run_live_evidence_analysis

check("G1. the stage succeeded after its OWN internal retry (2 internal attempts)",
      outcome.stories[0].included and _retry_internal_calls["n"] == 2)
check("G2. the orchestrator called the stage's run_* entry point exactly ONCE for this one story "
      "(no additional retry loop wrapped around the stage)",
      _evidence_stage_entry_calls["n"] == 1)

# ===========================================================================
# H. Reconstruction: editor_inputs used for the Brief in a full run can be
# independently rebuilt from the store after the fact
# ===========================================================================
db_path = _fresh_db_path("h.db")
outcome = orch.run_intelligence_cycle(
    "run-h", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=_corpus_analyst_stub_subset(TWO_STORY_IDS),
    call_evidence_analyst=_evidence_call_stub, retrieve=_fake_retrieve,
    call_pass2=_pass2_call_stub, call_editor=_editor_call_stub,
)
reconstructed = store.reconstruct_editor_inputs("run-h", db_path=db_path)
check("H1. reconstruct_editor_inputs independently rebuilds the same set of eligible story_ids "
      "the orchestrator used for this run",
      {e["story_id"] for e in reconstructed} == set(TWO_STORY_IDS))

# ===========================================================================
# J. Run-level budget: checked BEFORE a request is initiated; exhaustion
# stops further requests entirely (fail closed) rather than silently
# continuing to spend, and remaining stories are excluded without any
# stage call being attempted for them at all.
# ===========================================================================
db_path = _fresh_db_path("j.db")
_evidence_calls_j = {"n": 0}


def _evidence_call_counting(candidate_story, retrieval_bundle, corpus, api_key):
    _evidence_calls_j["n"] += 1
    return _valid_evidence_output(candidate_story, retrieval_bundle, corpus, api_key)


budget_j = orch.RunBudget(max_evidence_analyst_calls=1)
outcome = orch.run_intelligence_cycle(
    "run-j", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=_corpus_analyst_stub_subset(THREE_STORY_IDS),
    call_evidence_analyst=_evidence_call_counting, retrieve=_fake_retrieve,
    call_pass2=_pass2_call_stub, call_editor=_editor_call_stub,
    budget=budget_j,
)
check("J1. only ONE Evidence Analyst call is actually made before the budget trips "
      "(max_evidence_analyst_calls=1 enforced BEFORE the request, not after -- the second "
      "story's own retry loop re-checks the budget on every attempt but makes zero further "
      "requests, and the third story is skipped without ever reaching the stage at all)",
      _evidence_calls_j["n"] == 1, f"actual calls={_evidence_calls_j['n']}")
check("J2. the first story is still processed normally (included)", outcome.stories[0].included)
check("J2b. the second story's Evidence Analyst call is blocked before any request and the "
      "story is excluded via the stage's own failure reporting (evidence_analyst_call_failed), "
      "never silently treated as included",
      not outcome.stories[1].included
      and outcome.stories[1].evidence_failure_reason == "evidence_analyst_call_failed")
check("J3. the third story is skipped entirely once the run is already known to be budget-"
      "exhausted -- exclusion_reason='run_budget_exhausted', with NO stage call attempted "
      "for it at all (not even a blocked one)",
      outcome.stories[2].exclusion_reason == "run_budget_exhausted")
check("J4. the Editor is skipped once the budget has tripped (fail closed covers the Editor too)",
      outcome.brief_outcome is None and outcome.brief_skipped_reason == "run_budget_exhausted")
check("J5. OrchestrationOutcome.budget_exhausted_reason reports the tripped limit",
      outcome.budget_exhausted_reason == "max_evidence_analyst_calls")
check("J6. OrchestrationOutcome.budget_summary reflects exactly 1 evidence_analyst_calls_made",
      outcome.budget_summary is not None and outcome.budget_summary["evidence_analyst_calls_made"] == 1,
      str(outcome.budget_summary))

# Per-story attempt cap: max_evidence_attempts_per_story=0 blocks the very
# first call for every story, so zero real calls are ever made.
db_path = _fresh_db_path("j2.db")
_evidence_calls_j2 = {"n": 0}


def _evidence_call_counting_j2(candidate_story, retrieval_bundle, corpus, api_key):
    _evidence_calls_j2["n"] += 1
    return _valid_evidence_output(candidate_story, retrieval_bundle, corpus, api_key)


# max_total_llm_calls=1 is consumed entirely by the (budget-wrapped)
# Corpus Analyst call itself, so EVERY Evidence Analyst call for every
# story is blocked before any request is made.
budget_j2 = orch.RunBudget(max_total_llm_calls=1)
outcome = orch.run_intelligence_cycle(
    "run-j2", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=_corpus_analyst_stub_subset(THREE_STORY_IDS),
    call_evidence_analyst=_evidence_call_counting_j2, retrieve=_fake_retrieve,
    call_pass2=_pass2_call_stub, call_editor=_editor_call_stub,
    budget=budget_j2,
)
check("J7. max_total_llm_calls=1 (consumed by the budget-wrapped Corpus Analyst call itself) "
      "blocks every single Evidence Analyst call across all stories "
      "(budget accounting happens before ANY request, including the first)",
      _evidence_calls_j2["n"] == 0)
check("J8. the first story's own call is blocked before any request and excluded via the "
      "stage's own failure reporting; the two stories after it are skipped entirely with "
      "exclusion_reason='run_budget_exhausted'",
      outcome.stories[0].evidence_failure_reason == "evidence_analyst_call_failed"
      and all(s.exclusion_reason == "run_budget_exhausted" for s in outcome.stories[1:]))
check("J9. budget_exhausted_reason is 'max_total_llm_calls'",
      outcome.budget_exhausted_reason == "max_total_llm_calls")

# ===========================================================================
# K. Resume / reuse: a VALID, fingerprint-matching artifact persisted
# under the SAME run_id is reused with NO model call; an INVALID artifact,
# or one whose fingerprint does not match the current input, is never
# reused and falls through to a real call.
# ===========================================================================
db_path = _fresh_db_path("k.db")

# First pass: a normal run persists valid Evidence + Pass #2 artifacts.
outcome_first = orch.run_intelligence_cycle(
    "run-k", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=_corpus_analyst_stub_subset(TWO_STORY_IDS),
    call_evidence_analyst=_evidence_call_stub, retrieve=_fake_retrieve,
    call_pass2=_pass2_call_stub, call_editor=_editor_call_stub,
)
check("K1. first pass over run-k succeeds normally (sets up state to reuse)",
      outcome_first.brief_outcome is not None and outcome_first.brief_outcome.is_valid)


def _evidence_call_must_not_be_called(candidate_story, retrieval_bundle, corpus, api_key):
    raise AssertionError("Evidence Analyst must not be called -- a valid reusable artifact exists")


def _pass2_call_must_not_be_called(story_id, original_story, evidence_package, api_key):
    raise AssertionError("Pass #2 must not be called -- a valid reusable artifact exists")


# Second pass: SAME run_id, SAME candidate stories (so fingerprints match) --
# both stages must be reused and NEITHER stub above may ever be invoked.
outcome_second = orch.run_intelligence_cycle(
    "run-k", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=_corpus_analyst_stub_subset(TWO_STORY_IDS),
    call_evidence_analyst=_evidence_call_must_not_be_called, retrieve=_fake_retrieve,
    call_pass2=_pass2_call_must_not_be_called, call_editor=_editor_call_stub,
)
check("K2. second pass over the SAME run_id/stories reuses both stages for both stories "
      "(evidence_reused and pass2_reused are both True, and no AssertionError was raised)",
      all(s.evidence_reused and s.pass2_reused for s in outcome_second.stories),
      str([(s.story_id, s.evidence_reused, s.pass2_reused) for s in outcome_second.stories]))
check("K3. the reused run still produces a valid Brief", outcome_second.brief_outcome is not None
      and outcome_second.brief_outcome.is_valid)

# An INVALID persisted artifact is never reused, even under the same run_id/story.
db_path = _fresh_db_path("k2.db")
conn_k2 = store.get_connection(db_path)
story_k2 = get_story(ACCEPTANCE_3, ONE_STORY_ID)
store.save_story_artifact("run-k2", ONE_STORY_ID, "corpus_analyst", story_k2, is_valid=True, conn=conn_k2)
store.save_story_artifact("run-k2", ONE_STORY_ID, "evidence_analyst", {"story_id": ONE_STORY_ID},
                           is_valid=False, failure_reason="simulated_prior_failure", conn=conn_k2)
conn_k2.close()
_evidence_calls_k2 = {"n": 0}


def _evidence_call_counting_k2(candidate_story, retrieval_bundle, corpus, api_key):
    _evidence_calls_k2["n"] += 1
    return _valid_evidence_output(candidate_story, retrieval_bundle, corpus, api_key)


outcome = orch.run_intelligence_cycle(
    "run-k2", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=_corpus_analyst_stub_subset([ONE_STORY_ID]),
    call_evidence_analyst=_evidence_call_counting_k2, retrieve=_fake_retrieve,
    call_pass2=_pass2_call_stub, call_editor=_editor_call_stub,
)
check("K4. a previously INVALID Evidence Analyst artifact is never reused -- a real call is made",
      _evidence_calls_k2["n"] == 1 and outcome.stories[0].evidence_reused is False)

# A VALID artifact whose fingerprint does NOT match the current input is
# never reused either (simulates the underlying story content changing).
db_path = _fresh_db_path("k3.db")
conn_k3 = store.get_connection(db_path)
story_k3 = get_story(ACCEPTANCE_3, ONE_STORY_ID)
store.save_story_artifact("run-k3", ONE_STORY_ID, "corpus_analyst", story_k3, is_valid=True, conn=conn_k3)
store.save_story_artifact("run-k3", ONE_STORY_ID, "evidence_analyst",
                           _valid_evidence_output(story_k3, None, None),
                           is_valid=True, input_fingerprint="deliberately-wrong-fingerprint", conn=conn_k3)
conn_k3.close()
_evidence_calls_k3 = {"n": 0}


def _evidence_call_counting_k3(candidate_story, retrieval_bundle, corpus, api_key):
    _evidence_calls_k3["n"] += 1
    return _valid_evidence_output(candidate_story, retrieval_bundle, corpus, api_key)


outcome = orch.run_intelligence_cycle(
    "run-k3", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=_corpus_analyst_stub_subset([ONE_STORY_ID]),
    call_evidence_analyst=_evidence_call_counting_k3, retrieve=_fake_retrieve,
    call_pass2=_pass2_call_stub, call_editor=_editor_call_stub,
)
check("K5. a VALID artifact with a MISMATCHED input fingerprint is never reused -- a real call is made",
      _evidence_calls_k3["n"] == 1 and outcome.stories[0].evidence_reused is False)

# ===========================================================================
# L. Failure circuit breaker: the SAME Evidence Analyst failure class
# recurring across consecutive_failure_threshold (default 2) different
# stories stops ALL further Evidence Analyst calls for the rest of the run.
# ===========================================================================
db_path = _fresh_db_path("l.db")
_evidence_calls_l = {"n": 0}


def _evidence_call_always_transport_error(candidate_story, retrieval_bundle, corpus, api_key):
    _evidence_calls_l["n"] += 1
    raise ConnectionError("simulated systemic transport failure")  # -> error_category="transport_error"


circuit_breaker_l = orch.CircuitBreaker(consecutive_failure_threshold=2)
outcome = orch.run_intelligence_cycle(
    "run-l", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=_corpus_analyst_stub_subset(THREE_STORY_IDS),
    call_evidence_analyst=_evidence_call_always_transport_error, retrieve=_fake_retrieve,
    call_pass2=_pass2_call_stub, call_editor=_editor_call_stub,
    circuit_breaker=circuit_breaker_l,
)
check("L1. the breaker trips after the first two stories each exhaust their own 2 internal "
      "retry attempts with the SAME failure class (2 stories x 2 attempts = 4 real calls), "
      "then makes ZERO further Evidence Analyst calls for the third story",
      _evidence_calls_l["n"] == 4, f"actual calls={_evidence_calls_l['n']}")
check("L2. the third story is excluded with exclusion_reason='circuit_breaker_triggered' "
      "and no further Evidence Analyst call is attempted for it (skipped before "
      "_process_one_story is even invoked for it)",
      outcome.stories[2].exclusion_reason == "circuit_breaker_triggered")
check("L3. OrchestrationOutcome.circuit_breaker reports circuit_breaker_triggered=True "
      "with the correct failure_class and affected story ids",
      outcome.circuit_breaker is not None
      and outcome.circuit_breaker["circuit_breaker_triggered"] is True
      and outcome.circuit_breaker["failure_class"] == "transport_error"
      and outcome.circuit_breaker["affected_story_ids"] == THREE_STORY_IDS[:2],
      str(outcome.circuit_breaker))
check("L4. the Editor is skipped once the breaker has tripped",
      outcome.brief_outcome is None and outcome.brief_skipped_reason == "circuit_breaker_triggered")

# A single Evidence Analyst SUCCESS resets the breaker's streak -- an
# isolated failure followed by a success followed by another isolated
# failure of the SAME class must NOT trip the breaker (no two CONSECUTIVE
# failures of the same class).
db_path = _fresh_db_path("l2.db")


def _evidence_call_fail_ok_fail(candidate_story, retrieval_bundle, corpus, api_key):
    sid = candidate_story["story_id"]
    if sid == THREE_STORY_IDS[1]:
        return _valid_evidence_output(candidate_story, retrieval_bundle, corpus, api_key)
    raise ConnectionError("simulated isolated transport failure")


circuit_breaker_l2 = orch.CircuitBreaker(consecutive_failure_threshold=2)
outcome = orch.run_intelligence_cycle(
    "run-l2", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=_corpus_analyst_stub_subset(THREE_STORY_IDS),
    call_evidence_analyst=_evidence_call_fail_ok_fail, retrieve=_fake_retrieve,
    call_pass2=_pass2_call_stub, call_editor=_editor_call_stub,
    circuit_breaker=circuit_breaker_l2,
)
check("L5. a success between two isolated same-class failures resets the streak -- "
      "the breaker never trips, and all three stories are still processed",
      circuit_breaker_l2.triggered is False and len(outcome.stories) == 3)

# ===========================================================================
# T. --story-id targeted execution (regulus_brief001_acceptance.py):
# build_targeted_call_corpus_analyst / build_targeted_circuit_breaker /
# compute_targeted_budget, exercised directly against
# orch.run_intelligence_cycle (frozen, never edited by this feature)
# with injected, CALL-COUNTING stage stubs -- proving the acceptance
# layer's --story-id filtering/stopping/budget-capping composes
# correctly with the real orchestrator wiring, at zero cost (no real
# Anthropic call anywhere in this file -- same network guard as above).
# ===========================================================================
TARGET_STORY_ID = "CS-01"


def _counting_evidence_stub():
    calls = []

    def _stub(candidate_story, retrieval_bundle, corpus, api_key):
        calls.append(candidate_story["story_id"])
        return _valid_evidence_output(candidate_story, retrieval_bundle, corpus, api_key)
    return _stub, calls


def _counting_pass2_stub():
    calls = []

    def _stub(story_id, original_story, evidence_package, api_key):
        calls.append(story_id)
        return _valid_pass2_output(story_id, original_story, evidence_package, api_key)
    return _stub, calls


def _counting_editor_stub():
    calls = []

    def _stub(run_id, reporting_period, editor_inputs, api_key):
        calls.append(run_id)
        return _editor_call_stub(run_id, reporting_period, editor_inputs, api_key)
    return _stub, calls


# T1-T7: the EXACT documented scenario -- CS-01's Evidence Analyst
# artifact is ALREADY reusable (pre-seeded, as a --evidence-analysis-
# artifact bootstrap would leave it), Corpus Analyst output is already
# known (the stub simulates a --corpus-analysis-artifact bootstrap
# returning the full, real 11-story fixture, UNFILTERED), and only
# CS-01's Pass #2 is not yet persisted -- so targeted_new_call_cap == 1
# (exactly "maximum NEW external LLM requests for this invocation: 1").
db_path = _fresh_db_path("t1.db")
cs01_story = get_story(ACCEPTANCE_3, TARGET_STORY_ID)
cs01_evidence_raw = _valid_evidence_output(cs01_story, None, None)
cs01_evidence_fp = store.compute_fingerprint(cs01_story)
store.save_story_artifact("run-t1", TARGET_STORY_ID, "evidence_analyst", cs01_evidence_raw,
                           is_valid=True, input_fingerprint=cs01_evidence_fp, db_path=db_path)

reuse_state_t1 = brief_acc._resolve_story_reuse_state(
    "run-t1", cs01_story, conn=store.get_connection(db_path),
)
check("T1. the shared reuse-state helper confirms CS-01 Evidence is already reusable "
      "and only Pass #2 needs a new call (new_calls_needed == 1)",
      reuse_state_t1["evidence_analyst"] == "would_reuse"
      and reuse_state_t1["pass2"] == "would_call"
      and reuse_state_t1["new_calls_needed"] == 1)

targeted_cap_t1 = reuse_state_t1["new_calls_needed"]
targeted_budget_t1 = brief_acc.compute_targeted_budget(orch.RunBudget(max_total_llm_calls=4), targeted_cap_t1)
check("T2. compute_targeted_budget tightens max_total_llm_calls to exactly 2 "
      "(1 new external call + 1 reserved for the corpus-bootstrap unit), never looser "
      "than the configured ceiling of 4",
      targeted_budget_t1.max_total_llm_calls == 2)

targeted_corpus_caller_t1 = brief_acc.build_targeted_call_corpus_analyst(
    _corpus_analyst_stub_all(), TARGET_STORY_ID,
)
targeted_cb_t1 = brief_acc.build_targeted_circuit_breaker(orch.CircuitBreaker(consecutive_failure_threshold=2))
evidence_stub_t1, evidence_calls_t1 = _counting_evidence_stub()
pass2_stub_t1, pass2_calls_t1 = _counting_pass2_stub()
editor_stub_t1, editor_calls_t1 = _counting_editor_stub()

outcome_t1 = orch.run_intelligence_cycle(
    "run-t1", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=targeted_corpus_caller_t1,
    call_evidence_analyst=evidence_stub_t1, retrieve=_fake_retrieve,
    call_pass2=pass2_stub_t1, call_editor=editor_stub_t1,
    budget=targeted_budget_t1, circuit_breaker=targeted_cb_t1,
)
check("T3. --story-id targeting processes ONLY the selected story (CS-01), "
      "even though the Corpus Analyst stub returned the full 11-story fixture",
      [s.story_id for s in outcome_t1.stories] == [TARGET_STORY_ID])
check("T4. CS-02..CS-11's Evidence Analyst caller is never invoked at all -- the "
      "Evidence Analyst caller is invoked zero times total (CS-01's own artifact was reused)",
      evidence_calls_t1 == [])
check("T5. CS-01's Pass #2 caller IS invoked exactly once -- the only new call this "
      "invocation makes -- and no other story's Pass #2 caller is ever invoked",
      pass2_calls_t1 == [TARGET_STORY_ID])
check("T6. the Intelligence Editor is NEVER invoked in targeted mode, even though "
      "CS-01's Pass #2 succeeded and is eligible for it",
      editor_calls_t1 == [])
check("T7. outcome.brief_outcome is None (Editor never ran) because the stopping "
      "mechanism fired", outcome_t1.brief_outcome is None and outcome_t1.brief_skipped_reason is not None)

pass2_record_t1 = store.load_story_artifact("run-t1", TARGET_STORY_ID, "pass2", db_path=db_path)
check("T8. CS-01's successful Pass #2 result is persisted as valid, exactly as a normal "
      "(non-targeted) run would persist it",
      pass2_record_t1 is not None and pass2_record_t1.is_valid
      and pass2_record_t1.input_fingerprint is not None)
check("T9. that persisted Pass #2 result is reusable under its own recorded "
      "input_fingerprint -- so a LATER invocation (targeted or not) can reuse it with "
      "zero further cost",
      store.find_reusable_story_artifact(
          "run-t1", TARGET_STORY_ID, "pass2", pass2_record_t1.input_fingerprint, db_path=db_path,
      ) is not None)

# T10: build_targeted_call_corpus_analyst's own filtering logic fails
# closed -- called directly (the same (corpus_payload, api_key) call
# shape every call_corpus_analyst caller has), an unknown story_id
# raises UnknownStoryIdError immediately, with the underlying caller's
# result already in hand and zero further stage calls made.
targeted_corpus_caller_t10 = brief_acc.build_targeted_call_corpus_analyst(
    _corpus_analyst_stub_all(), "CS-99-DOES-NOT-EXIST",
)
raised_t10 = None
try:
    targeted_corpus_caller_t10({"some": "corpus_payload"}, "fake-key-not-real")
except brief_acc.UnknownStoryIdError as e:
    raised_t10 = e
check("T10. an unknown --story-id raises UnknownStoryIdError, fails closed, directly "
      "from the targeted call_corpus_analyst wrapper", raised_t10 is not None)

# T11-T12: that SAME unknown story_id, run through the real
# run_intelligence_cycle (frozen, never edited), still makes zero
# Evidence Analyst/Pass #2/Editor calls -- corpus_analyst.py (also
# frozen) catches ANY exception its caller raises, including
# UnknownStoryIdError, as a failed Corpus Analyst attempt (retried up to
# CORPUS_ANALYST_MAX_ATTEMPTS, then reported as corpus_analysis_is_valid
# =False) rather than letting it propagate -- so the OBSERVABLE
# signal one level up is an invalid corpus analysis, not a raised
# UnknownStoryIdError, but the fail-closed GUARANTEE (no per-story stage
# is ever reached for an unresolvable story_id) still holds exactly the
# same, since run_intelligence_cycle returns immediately whenever
# corpus_outcome.is_valid is False.
db_path = _fresh_db_path("t10.db")
evidence_stub_t10, evidence_calls_t10 = _counting_evidence_stub()
pass2_stub_t10, pass2_calls_t10 = _counting_pass2_stub()
editor_stub_t10, editor_calls_t10 = _counting_editor_stub()
outcome_t10 = orch.run_intelligence_cycle(
    "run-t10", DEV_CORPUS, REPORTING_PERIOD, api_key="fake-key-not-real", db_path=db_path,
    call_corpus_analyst=targeted_corpus_caller_t10,
    call_evidence_analyst=evidence_stub_t10, retrieve=_fake_retrieve,
    call_pass2=pass2_stub_t10, call_editor=editor_stub_t10,
)
check("T11. the unresolvable story_id surfaces as an invalid Corpus Analysis "
      "(corpus_analyst.py's own frozen retry/exception handling, never bypassed/"
      "special-cased by this feature) -- not a crash, not silent full-corpus processing",
      outcome_t10.corpus_analysis_is_valid is False)
check("T12. no Evidence Analyst, Pass #2, or Editor caller was EVER invoked for the "
      "unknown-story-id case, for either story_id or any other -- the failure happens "
      "before any per-story stage is even considered",
      evidence_calls_t10 == [] and pass2_calls_t10 == [] and editor_calls_t10 == [])

# T13: a REAL (unmodified) acceptance-layer run_acceptance() call, using
# regulus_brief001_acceptance.py's own dry-run path, against a fresh
# run_id/db_path with --story-id but NO corpus bootstrap yet (Corpus
# Analyst output genuinely unknown) -- confirms the early, zero-cost
# fail-closed check is simply deferred (not skipped/bypassed) rather
# than silently processing every story, by checking the returned report
# never resolves a selected story under that condition.
out_dir_t13 = tempfile.mkdtemp(prefix="regulus_brief_acceptance_targeted_test_", dir=_TMPDIR)
report_t13 = brief_acc.run_acceptance(
    "dryrun-t13", output_dir=out_dir_t13, dry_run=True, story_id=TARGET_STORY_ID,
)
check("T13. --story-id on a dry run with no known Corpus Analyst output yet does NOT "
      "raise and does NOT silently resolve/select the story -- it is reported as unresolved",
      report_t13["corpus_analyst"]["resolved"] is False
      and report_t13.get("selected_story_ids") == [TARGET_STORY_ID])

# ===========================================================================
# I. Isolation / no real network call anywhere in this file
# ===========================================================================
check("I1. no real network POST was attempted anywhere in this test file",
      not _network_call_attempted["flag"])
_requests_module.post = _real_requests_post

# ===========================================================================
# Summary
# ===========================================================================
shutil.rmtree(_TMPDIR, ignore_errors=True)

failed = [r for r in results if r[1] == "FAIL"]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
if failed:
    print("FAILURES:")
    for name, status, detail in failed:
        print(f"  - {name}: {detail}")
    sys.exit(1)
sys.exit(0)
