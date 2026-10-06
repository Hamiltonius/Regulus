#!/usr/bin/env python3
"""
regulus_orchestrator.py — connects the four intelligence-product roles
into one cycle and persists their state:

    Corpus Analyst Pass #1 (corpus_analyst.run_corpus_analysis)
            -> Evidence Analyst (evidence_analyst.run_live_evidence_analysis), per story
            -> Intelligence Analyst Pass #2 (intelligence_analyst_pass2.run_intelligence_pass2), per story
            -> Intelligence Editor (intelligence_editor.run_intelligence_editor)
            -> REGULUS INTELLIGENCE BRIEF #001

This is the ONE module in Regulus allowed to import all four role modules
plus intelligence_store -- every other module in this pipeline is
deliberately isolated from its neighbors (see each module's own
"Isolation discipline" docstring). The orchestrator's job is wiring and
failure isolation, not analysis: it does not reinterpret, repair, or
second-guess any stage's own validated output.

PER-STORY FAILURE ISOLATION: one story's Evidence Analyst or Pass #2
failure/invalidity must never abort the cycle for other stories. Each
story is processed inside its own try/except; a failure excludes that
story (with a machine-readable reason) and processing continues with the
next story. A failure in Corpus Analyst Pass #1 itself (there are no
stories yet) IS cycle-level -- the whole cycle aborts.

RETRY DISCIPLINE: every stage already retries internally, bounded by its
own *_MAX_ATTEMPTS constant (CORPUS_ANALYST_MAX_ATTEMPTS,
EVIDENCE_ANALYST_MAX_ATTEMPTS, PASS2_MAX_ATTEMPTS, EDITOR_MAX_ATTEMPTS).
The orchestrator calls each stage's run_* entry point exactly ONCE per
story per stage -- it never wraps an additional retry loop around a
stage, which would silently double the effective attempt budget.

PERSISTENCE: every stage's outcome (success or failure) is persisted via
intelligence_store, keyed by run_id, before the orchestrator moves on --
so a crash mid-cycle still leaves a reconstructable partial record, and
Editor inputs are always rebuilt from store.reconstruct_editor_inputs()
rather than carried by hand from the per-story loop (proving the
persistence/reconstruction path is actually exercised by the normal
cycle, not just available).

NO NEW PAID LIVE API CALLS ARE MADE BY IMPORTING OR RUNNING THIS MODULE'S
TESTS -- every call into a stage accepts (and, in tests, is always given)
an injected call_analyst/retrieve stub; nothing here changes any stage's
own no-API-key-raises-RuntimeError discipline (that check lives in each
stage's own acceptance runner, never in the stage module or here).
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from corpus_extractor import Corpus
import corpus_analyst
import evidence_analyst
import intelligence_analyst_pass2 as pass2
import intelligence_editor as editor
import intelligence_store as store

log = logging.getLogger("regulus.orchestrator")


@dataclass
class StoryCycleResult:
    """Per-story outcome of one orchestration cycle."""
    story_id: str
    included: bool
    exclusion_reason: Optional[str] = None
    evidence_is_valid: Optional[bool] = None
    evidence_failure_reason: Optional[str] = None
    pass2_is_valid: Optional[bool] = None
    pass2_failure_reason: Optional[str] = None
    pass2_editor_eligibility: Optional[str] = None
    exception: Optional[str] = None  # set only if an unexpected exception was caught


@dataclass
class OrchestrationOutcome:
    """Result of run_intelligence_cycle()."""
    run_id: str
    corpus_analysis_is_valid: bool = False
    corpus_analysis_failure_reason: Optional[str] = None
    stories: list = field(default_factory=list)          # list[StoryCycleResult]
    editor_inputs_count: int = 0
    brief_outcome: Optional[Any] = None                    # editor.EditorOutcome, or None if skipped
    brief_skipped_reason: Optional[str] = None
    brief_id: Optional[str] = None

    @property
    def included_story_ids(self) -> list:
        return [s.story_id for s in self.stories if s.included]

    @property
    def excluded_story_ids(self) -> list:
        return [s.story_id for s in self.stories if not s.included]


def _process_one_story(run_id: str, candidate_story: dict, corpus: Corpus, *, api_key: Optional[str],
                        call_evidence_analyst: Optional[Callable], retrieve: Optional[Callable],
                        call_pass2: Optional[Callable], conn) -> StoryCycleResult:
    """Run the Evidence Analyst and Pass #2 for exactly one candidate_story,
    persisting every stage's outcome. Never raises -- any unexpected
    exception is caught here so that one story's failure cannot abort
    processing of the others; the caller's loop does not need its own
    try/except."""
    story_id = candidate_story.get("story_id", "UNKNOWN")

    store.save_story_artifact(run_id, story_id, "corpus_analyst", candidate_story, is_valid=True, conn=conn)

    try:
        evidence_outcome = evidence_analyst.run_live_evidence_analysis(
            candidate_story, corpus, api_key=api_key, call_analyst=call_evidence_analyst, retrieve=retrieve,
        )
    except Exception as e:  # defensive: run_live_evidence_analysis itself never raises on model/
        # network/parse failure, but an unexpected bug here must not abort other stories.
        log.exception("Unexpected exception running Evidence Analyst for story_id=%s", story_id)
        store.save_story_artifact(run_id, story_id, "evidence_analyst", {"story_id": story_id},
                                   is_valid=False, validation_errors=[str(e)],
                                   failure_reason="evidence_analyst_unexpected_exception", conn=conn)
        return StoryCycleResult(story_id=story_id, included=False,
                                 exclusion_reason="evidence_analyst_unexpected_exception",
                                 exception=str(e))

    store.save_story_artifact(
        run_id, story_id, "evidence_analyst",
        evidence_outcome.raw if evidence_outcome.raw is not None else {"story_id": story_id},
        is_valid=evidence_outcome.is_valid, validation_errors=evidence_outcome.validation_errors,
        failure_reason=evidence_outcome.failure_reason, conn=conn,
    )

    if not evidence_outcome.is_valid:
        return StoryCycleResult(
            story_id=story_id, included=False,
            exclusion_reason=evidence_outcome.failure_reason or "evidence_analyst_invalid",
            evidence_is_valid=False, evidence_failure_reason=evidence_outcome.failure_reason,
        )

    try:
        pass2_outcome = pass2.run_intelligence_pass2(
            story_id, candidate_story, evidence_outcome.raw, api_key=api_key, call_analyst=call_pass2,
        )
    except Exception as e:
        log.exception("Unexpected exception running Pass #2 for story_id=%s", story_id)
        store.save_story_artifact(run_id, story_id, "pass2", {"story_id": story_id},
                                   is_valid=False, validation_errors=[str(e)],
                                   failure_reason="pass2_unexpected_exception", conn=conn)
        return StoryCycleResult(story_id=story_id, included=False,
                                 exclusion_reason="pass2_unexpected_exception",
                                 evidence_is_valid=True, exception=str(e))

    store.save_story_artifact(
        run_id, story_id, "pass2",
        pass2_outcome.raw if pass2_outcome.raw is not None else {"story_id": story_id},
        is_valid=pass2_outcome.is_valid, validation_errors=pass2_outcome.validation_errors,
        failure_reason=pass2_outcome.failure_reason, conn=conn,
    )

    if not pass2_outcome.is_valid:
        return StoryCycleResult(
            story_id=story_id, included=False,
            exclusion_reason=pass2_outcome.failure_reason or "pass2_invalid",
            evidence_is_valid=True, pass2_is_valid=False, pass2_failure_reason=pass2_outcome.failure_reason,
        )

    editor_eligibility = (pass2_outcome.raw or {}).get("editor_eligibility")
    if editor_eligibility == "not_eligible":
        return StoryCycleResult(
            story_id=story_id, included=False, exclusion_reason="pass2_editor_eligibility_not_eligible",
            evidence_is_valid=True, pass2_is_valid=True, pass2_editor_eligibility=editor_eligibility,
        )

    return StoryCycleResult(
        story_id=story_id, included=True,
        evidence_is_valid=True, pass2_is_valid=True, pass2_editor_eligibility=editor_eligibility,
    )


def run_intelligence_cycle(run_id: str, corpus: Corpus, reporting_period: dict, *,
                            api_key: Optional[str] = None,
                            db_path: Optional[str] = None,
                            call_corpus_analyst: Optional[Callable] = None,
                            call_evidence_analyst: Optional[Callable] = None,
                            retrieve: Optional[Callable] = None,
                            call_pass2: Optional[Callable] = None,
                            call_editor: Optional[Callable] = None,
                            ) -> OrchestrationOutcome:
    """Run one full Regulus intelligence cycle over `corpus`:

      1. Corpus Analyst Pass #1 (once, over the whole corpus). A call/
         parse/invalidity failure here is cycle-level -- there are no
         candidate stories yet, so the cycle aborts and is reported via
         OrchestrationOutcome.corpus_analysis_failure_reason.
      2. For each candidate_story in the Pass #1 output: Evidence Analyst
         -> Intelligence Analyst Pass #2, each persisted, with per-story
         failure isolation (one story's failure never aborts the others).
      3. Editor inputs are rebuilt from persisted state via
         intelligence_store.reconstruct_editor_inputs() (never carried by
         hand from the loop above), applying the Pass #2 -> Editor
         eligibility rule (is_valid AND editor_eligibility != "not_eligible").
      4. If no story is eligible, the Editor is not called at all
         (brief_outcome stays None, brief_skipped_reason="no_eligible_stories").
         Otherwise the Intelligence Editor is run once and persisted.

    Every call_* parameter is forwarded verbatim to the corresponding
    stage's own run_* entry point as its call_analyst (and `retrieve` as
    its retrieve) -- this function makes no live API call of its own and
    introduces no additional retry loop around any stage.
    """
    conn = store.get_connection(db_path)
    try:
        corpus_outcome = corpus_analyst.run_corpus_analysis(corpus, api_key=api_key, call_analyst=call_corpus_analyst)

        if not corpus_outcome.is_valid:
            return OrchestrationOutcome(
                run_id=run_id, corpus_analysis_is_valid=False,
                corpus_analysis_failure_reason=(corpus_outcome.failure_reason or "corpus_analyst_invalid"),
            )

        candidate_stories = (corpus_outcome.raw or {}).get("candidate_stories") or []

        story_results = []
        for candidate_story in candidate_stories:
            result = _process_one_story(
                run_id, candidate_story, corpus, api_key=api_key,
                call_evidence_analyst=call_evidence_analyst, retrieve=retrieve,
                call_pass2=call_pass2, conn=conn,
            )
            story_results.append(result)

        editor_inputs = store.reconstruct_editor_inputs(run_id, conn=conn)

        outcome = OrchestrationOutcome(
            run_id=run_id, corpus_analysis_is_valid=True, stories=story_results,
            editor_inputs_count=len(editor_inputs),
        )

        if not editor_inputs:
            outcome.brief_skipped_reason = "no_eligible_stories"
            return outcome

        brief_id = f"{run_id}-brief-001"
        editor_outcome = editor.run_intelligence_editor(
            run_id, reporting_period, editor_inputs, api_key=api_key, call_analyst=call_editor,
        )
        if editor_outcome.raw and isinstance(editor_outcome.raw, dict) and editor_outcome.raw.get("brief_id"):
            brief_id = editor_outcome.raw["brief_id"]

        store.save_brief(
            run_id, brief_id, reporting_period, editor_outcome.raw,
            is_valid=editor_outcome.is_valid, validation_errors=editor_outcome.validation_errors,
            failure_reason=editor_outcome.failure_reason, conn=conn,
        )

        outcome.brief_outcome = editor_outcome
        outcome.brief_id = brief_id
        return outcome
    finally:
        conn.close()
