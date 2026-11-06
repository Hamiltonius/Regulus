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

RUN-LEVEL BUDGET (RunBudget, optional, default None = unlimited -- fully
backward compatible with every existing caller/test that does not pass
one): max_total_llm_calls / max_evidence_analyst_calls / max_evidence_
attempts_per_story are enforced by WRAPPING each stage's call_analyst
callable (real or test stub) so the check runs, and is accounted for,
BEFORE that callable ever runs -- i.e. before any request is initiated,
never after. A wrapped callable that finds the budget already exhausted
raises BudgetExhaustedError instead of calling through; it makes NO
request. This deliberately does NOT touch any stage module's own
*_MAX_ATTEMPTS constant (corpus_analyst.CORPUS_ANALYST_MAX_ATTEMPTS,
evidence_analyst.EVIDENCE_ANALYST_MAX_ATTEMPTS, etc.) -- a stage's own
internal retry loop still runs its configured number of iterations, but
every iteration past the budget is a zero-cost no-op (BudgetExhaustedError
is raised before reaching requests.post). Once ANY budget limit trips,
the orchestrator stops initiating further stages/stories entirely (fail
closed) rather than looping through already-exhausted retries for every
remaining story.

CIRCUIT BREAKER: if the same Evidence Analyst failure's error_category
(see evidence_analyst.EvidenceAnalystAttemptDiagnostics.error_category)
recurs for N consecutive DIFFERENT stories (default/acceptance N=2 --
CircuitBreaker.consecutive_failure_threshold), the orchestrator stops
making further Evidence Analyst calls for the rest of the run (fail
closed), on the theory that a failure class repeating across unrelated
stories is systemic, not story-specific, and further identical-shaped
calls are unlikely to succeed and simply keep spending. A story excluded
for a different reason (invalid Pass #2, eligibility, etc.) does not
reset or advance the breaker -- only Evidence Analyst outcomes do.

RESUME / REUSE: before calling the Evidence Analyst or Pass #2 for a
story, the orchestrator computes a deterministic input fingerprint (see
intelligence_store.compute_fingerprint) and checks intelligence_store.
find_reusable_story_artifact() for an existing, VALID, fingerprint-
matching artifact under the SAME run_id. If found, that artifact is
reused and the model is never called for that story/stage. An invalid
artifact, a fingerprint mismatch, or no artifact at all all fall through
to a real call. An interrupted/incomplete prior attempt is never treated
as reusable, by construction -- see intelligence_store.py's own
"REUSE / RESUME DISCIPLINE" docstring for why.
"""

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from corpus_extractor import Corpus
import corpus_analyst
import evidence_analyst
import evidence_analyst_schema
import intelligence_analyst_pass2 as pass2
import intelligence_analyst_pass2_schema
import intelligence_editor as editor
import intelligence_store as store

log = logging.getLogger("regulus.orchestrator")


class BudgetExhaustedError(RuntimeError):
    """Raised by a budget-wrapped caller INSTEAD of delegating to the real
    (or test-stub) caller, when the run's configured RunBudget has
    already been exhausted for the relevant limit. Makes no request of
    any kind -- this is raised before the wrapped callable is invoked at
    all. Carries limit_name for reporting, and error_category (read via
    getattr by evidence_analyst.py's diagnostics, with no import
    dependency in either direction) so a budget-exhaustion attempt is
    distinguishable in diagnostics from a transport failure, even though
    both currently fall back to the same request_succeeded=False shape."""

    error_category = "budget_exhausted"

    def __init__(self, message: str, *, limit_name: str):
        super().__init__(message)
        self.limit_name = limit_name


@dataclass
class RunBudget:
    """Deterministic, configurable, run-scoped ceiling on how many LLM
    calls this orchestrator run is allowed to INITIATE. None (the
    default for every field) means "no limit" -- passing budget=None to
    run_intelligence_cycle (the function default) disables budget
    enforcement entirely, so every pre-existing caller/test is
    unaffected. Accounting happens via check_before_call() (called
    BEFORE a request is initiated) and record_call() (called only once
    the orchestrator has decided to proceed) -- the two are always used
    as a pair, never record_call() alone, so total_llm_calls_made always
    reflects calls actually initiated, never calls merely considered.
    """
    max_total_llm_calls: Optional[int] = None
    max_evidence_analyst_calls: Optional[int] = None
    max_evidence_attempts_per_story: Optional[int] = None

    total_llm_calls_made: int = 0
    evidence_analyst_calls_made: int = 0
    per_story_evidence_attempts: dict = field(default_factory=dict)  # story_id -> count

    exhausted_reason: Optional[str] = None  # the first limit_name that ever tripped, sticky

    def total_exhausted(self) -> bool:
        return self.exhausted_reason is not None

    def check_before_call(self, *, stage: str, story_id: Optional[str] = None) -> Optional[str]:
        """Returns the limit_name that would be exceeded by initiating
        this call right now, or None if it's within budget. Never
        mutates state -- call record_call() separately once the
        orchestrator actually proceeds."""
        if self.max_total_llm_calls is not None and self.total_llm_calls_made >= self.max_total_llm_calls:
            return "max_total_llm_calls"
        if stage == "evidence_analyst":
            if (self.max_evidence_analyst_calls is not None
                    and self.evidence_analyst_calls_made >= self.max_evidence_analyst_calls):
                return "max_evidence_analyst_calls"
            if story_id is not None and self.max_evidence_attempts_per_story is not None:
                if self.per_story_evidence_attempts.get(story_id, 0) >= self.max_evidence_attempts_per_story:
                    return "max_evidence_attempts_per_story"
        return None

    def record_call(self, *, stage: str, story_id: Optional[str] = None) -> None:
        self.total_llm_calls_made += 1
        if stage == "evidence_analyst":
            self.evidence_analyst_calls_made += 1
            if story_id is not None:
                self.per_story_evidence_attempts[story_id] = self.per_story_evidence_attempts.get(story_id, 0) + 1

    def summary(self) -> dict:
        return {
            "max_total_llm_calls": self.max_total_llm_calls,
            "max_evidence_analyst_calls": self.max_evidence_analyst_calls,
            "max_evidence_attempts_per_story": self.max_evidence_attempts_per_story,
            "total_llm_calls_made": self.total_llm_calls_made,
            "evidence_analyst_calls_made": self.evidence_analyst_calls_made,
            "per_story_evidence_attempts": dict(self.per_story_evidence_attempts),
            "exhausted_reason": self.exhausted_reason,
        }


def _budget_wrapped_caller(real_caller: Callable, budget: Optional[RunBudget], *, stage: str,
                            story_id: Optional[str] = None) -> Callable:
    """Wrap `real_caller` (the stage's real live caller, or a test stub)
    so that, on every invocation, the configured RunBudget is checked
    BEFORE `real_caller` is invoked at all. If budget is None, returns
    `real_caller` unchanged -- zero overhead, zero behavior change, for
    every caller that does not opt into budgeting."""
    if budget is None:
        return real_caller

    def _wrapped(*args, **kwargs):
        limit = budget.check_before_call(stage=stage, story_id=story_id)
        if limit is not None:
            budget.exhausted_reason = budget.exhausted_reason or limit
            raise BudgetExhaustedError(
                f"{stage} call for story_id={story_id!r} blocked before any request was made: "
                f"run budget limit {limit!r} already reached.",
                limit_name=limit,
            )
        budget.record_call(stage=stage, story_id=story_id)
        return real_caller(*args, **kwargs)

    return _wrapped


@dataclass
class CircuitBreaker:
    """Stops further Evidence Analyst calls once the SAME failure
    error_category recurs across `consecutive_failure_threshold`
    consecutive DIFFERENT stories' Evidence Analyst outcomes (default 2,
    matching the acceptance spec). A success resets the streak. A story
    excluded for a reason other than an Evidence Analyst failure (no
    Evidence Analyst call made at all, e.g. reused/budget-skipped) never
    advances or resets the breaker -- record_evidence_outcome() must only
    be called when an Evidence Analyst call was actually attempted."""
    consecutive_failure_threshold: int = 2

    triggered: bool = False
    failure_class: Optional[str] = None
    affected_story_ids: list = field(default_factory=list)

    _last_failure_class: Optional[str] = None
    _consecutive_count: int = 0

    def record_evidence_outcome(self, story_id: str, *, is_valid: bool,
                                 error_category: Optional[str]) -> None:
        if self.triggered:
            return
        if is_valid:
            self._last_failure_class = None
            self._consecutive_count = 0
            return
        if error_category is not None and error_category == self._last_failure_class:
            self._consecutive_count += 1
            self.affected_story_ids.append(story_id)
        else:
            self._last_failure_class = error_category
            self._consecutive_count = 1
            self.affected_story_ids = [story_id]
        if error_category is not None and self._consecutive_count >= self.consecutive_failure_threshold:
            self.triggered = True
            self.failure_class = error_category

    def summary(self) -> Optional[dict]:
        if not self.triggered:
            return None
        return {
            "circuit_breaker_triggered": True,
            "failure_class": self.failure_class,
            "affected_story_ids": list(self.affected_story_ids),
            "consecutive_failure_threshold": self.consecutive_failure_threshold,
        }


def _underlying_evidence_error_category(attempts) -> Optional[str]:
    """Category of the last attempt that was not a local budget block; falls back to the last
    attempt's category if every attempt was budget-blocked, or None if there were no attempts."""
    if not attempts:
        return None
    for a in reversed(attempts):
        if a.error_category != "budget_exhausted":
            return a.error_category
    return attempts[-1].error_category


@dataclass
class StoryCycleResult:
    """Per-story outcome of one orchestration cycle."""
    story_id: str
    included: bool
    exclusion_reason: Optional[str] = None
    evidence_is_valid: Optional[bool] = None
    evidence_failure_reason: Optional[str] = None
    evidence_error_category: Optional[str] = None
    evidence_reused: bool = False
    evidence_reused_from_memory: bool = False
    evidence_attempts: list = field(default_factory=list)  # EvidenceAnalystAttemptDiagnostics.to_dict(), live calls only
    pass2_is_valid: Optional[bool] = None
    pass2_failure_reason: Optional[str] = None
    pass2_editor_eligibility: Optional[str] = None
    pass2_reused: bool = False
    pass2_reused_from_memory: bool = False
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
    budget_exhausted_reason: Optional[str] = None
    budget_summary: Optional[dict] = None
    circuit_breaker: Optional[dict] = None

    @property
    def included_story_ids(self) -> list:
        return [s.story_id for s in self.stories if s.included]

    @property
    def excluded_story_ids(self) -> list:
        return [s.story_id for s in self.stories if not s.included]


# ---------------------------------------------------------------------------
# CROSS-DATABASE MEMORY REUSE (optional; memory_sources=None == old behavior)
#
# A "memory source" is a (db_path, run_id) pair naming a PRIOR intelligence
# store whose already-paid-for, VALID Evidence Analyst / Pass #2 artifacts may
# stand in for a new call. Lookup order is: current run first (unchanged), then
# memory sources in the order supplied. A memory artifact is reused ONLY if,
# all deterministically and with no model call: (1) the memory DB opens
# read-only (never created, migrated or written); (2) find_reusable_story_
# artifact() accepts it (is_valid AND exact input fingerprint); (3) any
# component versions recorded in its provenance equal the current ones;
# (4) it passes the CURRENT schema validator. Any failure, including an
# unavailable DB, returns None and the caller proceeds on the normal path.
# A hit is copied into the CURRENT run with provenance so that
# reconstruct_editor_inputs() remains the only Editor input source.
#
# KNOWN LIMITATION: the input fingerprint covers inputs only, not prompt or
# schema versions. Versions are recorded in provenance (see
# _component_versions) and compared when present; artifacts persisted before
# this existed carry none and are accepted on fingerprint + revalidation alone.
# Evidence revalidation cannot re-check retrieval identity cross-checks
# (retrieval bundles are not persisted in story_artifacts).
# ---------------------------------------------------------------------------

def _component_versions(stage: str) -> dict:
    if stage == "evidence_analyst":
        return {"schema_version": evidence_analyst.EVIDENCE_ANALYST_SCHEMA_VERSION,
                "prompt_version": evidence_analyst.PROMPT_VERSION}
    return {"schema_version": pass2.SCHEMA_VERSION, "prompt_version": pass2.PROMPT_VERSION}


def _open_readonly(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _revalidates(stage: str, payload: Any, candidate_story: dict, corpus: Corpus,
                 evidence_payload: Any) -> bool:
    if stage == "evidence_analyst":
        doc_numbers = {o.document_number for o in corpus.observations if o.document_number is not None}
        return evidence_analyst_schema.validate_evidence_analysis(
            payload, story=candidate_story, valid_document_numbers=doc_numbers,
        ).is_valid
    return intelligence_analyst_pass2_schema.validate_pass2_reassessment(
        payload, story_id=candidate_story.get("story_id"), original_story=candidate_story,
        evidence_package=evidence_payload,
    ).is_valid


def find_memory_artifact(stage: str, candidate_story: dict, fingerprint: str, corpus: Corpus, *,
                         memory_sources: Optional[list], evidence_payload: Any = None):
    """Read-only, non-persisting lookup. Returns (StoryArtifactRecord, (db_path, run_id)) for the
    first memory source holding a reusable artifact, else None. Shared by the live path and the
    dry-run so the two cannot diverge. Never raises."""
    story_id = candidate_story.get("story_id", "UNKNOWN")
    for source in (memory_sources or []):
        db_path, source_run_id = source
        try:
            ro = _open_readonly(db_path)
            try:
                rec = store.find_reusable_story_artifact(source_run_id, story_id, stage, fingerprint, conn=ro)
            finally:
                ro.close()
            if rec is None:
                continue
            recorded = (rec.provenance or {}).get("component_versions")
            if recorded is not None and recorded != _component_versions(stage):
                log.warning("memory reuse refused (%s, %s): component version mismatch", stage, story_id)
                continue
            if not _revalidates(stage, rec.payload, candidate_story, corpus, evidence_payload):
                log.warning("memory reuse refused (%s, %s): current-schema revalidation failed", stage, story_id)
                continue
            return rec, source
        except Exception as e:  # unavailable/legacy/corrupt memory DB: fail closed to normal path
            log.warning("memory source %r unusable for %s/%s: %s", source, story_id, stage, e)
            continue
    return None


def _adopt_memory_artifact(run_id: str, story_id: str, stage: str, fingerprint: str, hit, conn):
    """Copy a memory hit into the CURRENT run with provenance. Returns the record, or None on
    any failure (caller then takes the normal path)."""
    rec, (db_path, source_run_id) = hit
    recorded = (rec.provenance or {}).get("component_versions")
    provenance = {
        "kind": "memory_reuse",
        "source_db": os.path.abspath(db_path),
        "source_run_id": source_run_id,
        "stage": stage,
        "source_artifact_saved_at": rec.saved_at,
        "source_input_fingerprint": rec.input_fingerprint,
        "source_component_versions": recorded,
    }
    if recorded is not None:
        provenance["component_versions"] = recorded
    try:
        store.save_story_artifact(run_id, story_id, stage, rec.payload, is_valid=True,
                                   input_fingerprint=fingerprint, provenance=provenance, conn=conn)
    except Exception as e:
        log.warning("could not persist memory-reused %s artifact for %s: %s", stage, story_id, e)
        return None
    return rec


def _process_one_story(run_id: str, candidate_story: dict, corpus: Corpus, *, api_key: Optional[str],
                        call_evidence_analyst: Optional[Callable], retrieve: Optional[Callable],
                        call_pass2: Optional[Callable], conn,
                        budget: Optional[RunBudget] = None,
                        circuit_breaker: Optional[CircuitBreaker] = None,
                        memory_sources: Optional[list] = None) -> StoryCycleResult:
    """Run the Evidence Analyst and Pass #2 for exactly one candidate_story,
    persisting every stage's outcome. Never raises -- any unexpected
    exception is caught here so that one story's failure cannot abort
    processing of the others; the caller's loop does not need its own
    try/except.

    Before each expensive stage, checks intelligence_store.
    find_reusable_story_artifact() for a VALID, fingerprint-matching
    artifact already persisted under this run_id; if found, reuses it
    and makes no call for that stage. Otherwise calls the stage with its
    caller wrapped by `budget` (if supplied) so budget accounting happens
    before any request is initiated. Records the Evidence Analyst
    outcome onto `circuit_breaker` (if supplied) exactly once per story,
    and only when an Evidence Analyst call was actually attempted (never
    for a reused artifact)."""
    story_id = candidate_story.get("story_id", "UNKNOWN")

    store.save_story_artifact(run_id, story_id, "corpus_analyst", candidate_story, is_valid=True, conn=conn)

    evidence_fingerprint = store.compute_fingerprint(candidate_story)
    reusable_evidence = store.find_reusable_story_artifact(
        run_id, story_id, "evidence_analyst", evidence_fingerprint, conn=conn,
    )
    evidence_from_memory = False
    if reusable_evidence is None and memory_sources:
        hit = find_memory_artifact("evidence_analyst", candidate_story, evidence_fingerprint, corpus,
                                   memory_sources=memory_sources)
        if hit is not None:
            reusable_evidence = _adopt_memory_artifact(run_id, story_id, "evidence_analyst",
                                                       evidence_fingerprint, hit, conn)
            evidence_from_memory = reusable_evidence is not None

    if reusable_evidence is not None:
        evidence_raw = reusable_evidence.payload
        evidence_is_valid = True
        evidence_failure_reason = None
        evidence_error_category = None
        evidence_attempts = []
        evidence_reused = True
    else:
        real_evidence_caller = call_evidence_analyst or evidence_analyst.call_anthropic_evidence_analyst
        wrapped_evidence_caller = _budget_wrapped_caller(
            real_evidence_caller, budget, stage="evidence_analyst", story_id=story_id,
        )
        try:
            evidence_outcome = evidence_analyst.run_live_evidence_analysis(
                candidate_story, corpus, api_key=api_key, call_analyst=wrapped_evidence_caller, retrieve=retrieve,
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

        evidence_raw = evidence_outcome.raw
        evidence_is_valid = evidence_outcome.is_valid
        evidence_failure_reason = evidence_outcome.failure_reason
        # A locally blocked retry (budget_exhausted) must not mask the underlying external failure.
        evidence_error_category = _underlying_evidence_error_category(evidence_outcome.attempts)
        evidence_attempts = [a.to_dict() for a in evidence_outcome.attempts]
        evidence_reused = False

        store.save_story_artifact(
            run_id, story_id, "evidence_analyst",
            evidence_raw if evidence_raw is not None else {"story_id": story_id},
            is_valid=evidence_is_valid, validation_errors=evidence_outcome.validation_errors,
            failure_reason=evidence_failure_reason,
            input_fingerprint=(evidence_fingerprint if evidence_is_valid else None),
            provenance=({"component_versions": _component_versions("evidence_analyst")}
                        if evidence_is_valid else None),
            conn=conn,
        )

        if circuit_breaker is not None:
            circuit_breaker.record_evidence_outcome(
                story_id, is_valid=evidence_is_valid, error_category=evidence_error_category,
            )

    if not evidence_is_valid:
        return StoryCycleResult(
            story_id=story_id, included=False,
            exclusion_reason=evidence_failure_reason or "evidence_analyst_invalid",
            evidence_is_valid=False, evidence_failure_reason=evidence_failure_reason,
            evidence_error_category=evidence_error_category,
            evidence_attempts=evidence_attempts,
        )

    pass2_fingerprint = store.compute_fingerprint(candidate_story, evidence_raw)
    reusable_pass2 = store.find_reusable_story_artifact(
        run_id, story_id, "pass2", pass2_fingerprint, conn=conn,
    )
    pass2_from_memory = False
    if reusable_pass2 is None and memory_sources:
        hit = find_memory_artifact("pass2", candidate_story, pass2_fingerprint, corpus,
                                   memory_sources=memory_sources, evidence_payload=evidence_raw)
        if hit is not None:
            reusable_pass2 = _adopt_memory_artifact(run_id, story_id, "pass2", pass2_fingerprint, hit, conn)
            pass2_from_memory = reusable_pass2 is not None

    if reusable_pass2 is not None:
        pass2_raw = reusable_pass2.payload
        pass2_is_valid = True
        pass2_failure_reason = None
        pass2_reused = True
    else:
        real_pass2_caller = call_pass2 or pass2.call_anthropic_pass2
        wrapped_pass2_caller = _budget_wrapped_caller(real_pass2_caller, budget, stage="pass2", story_id=story_id)
        try:
            pass2_outcome = pass2.run_intelligence_pass2(
                story_id, candidate_story, evidence_raw, api_key=api_key, call_analyst=wrapped_pass2_caller,
            )
        except Exception as e:
            log.exception("Unexpected exception running Pass #2 for story_id=%s", story_id)
            store.save_story_artifact(run_id, story_id, "pass2", {"story_id": story_id},
                                       is_valid=False, validation_errors=[str(e)],
                                       failure_reason="pass2_unexpected_exception", conn=conn)
            return StoryCycleResult(story_id=story_id, included=False,
                                     exclusion_reason="pass2_unexpected_exception",
                                     evidence_is_valid=True, evidence_reused=evidence_reused, evidence_reused_from_memory=evidence_from_memory, evidence_attempts=evidence_attempts, exception=str(e))

        pass2_raw = pass2_outcome.raw
        pass2_is_valid = pass2_outcome.is_valid
        pass2_failure_reason = pass2_outcome.failure_reason
        pass2_reused = False

        store.save_story_artifact(
            run_id, story_id, "pass2",
            pass2_raw if pass2_raw is not None else {"story_id": story_id},
            is_valid=pass2_is_valid, validation_errors=pass2_outcome.validation_errors,
            failure_reason=pass2_failure_reason,
            input_fingerprint=(pass2_fingerprint if pass2_is_valid else None),
            provenance=({"component_versions": _component_versions("pass2")} if pass2_is_valid else None),
            conn=conn,
        )

    if not pass2_is_valid:
        return StoryCycleResult(
            story_id=story_id, included=False,
            exclusion_reason=pass2_failure_reason or "pass2_invalid",
            evidence_is_valid=True, evidence_reused=evidence_reused, evidence_reused_from_memory=evidence_from_memory,
            evidence_attempts=evidence_attempts,
            pass2_is_valid=False, pass2_failure_reason=pass2_failure_reason,
        )

    editor_eligibility = (pass2_raw or {}).get("editor_eligibility")
    if editor_eligibility == "not_eligible":
        return StoryCycleResult(
            story_id=story_id, included=False, exclusion_reason="pass2_editor_eligibility_not_eligible",
            evidence_is_valid=True, evidence_reused=evidence_reused, evidence_reused_from_memory=evidence_from_memory,
            evidence_attempts=evidence_attempts,
            pass2_is_valid=True, pass2_editor_eligibility=editor_eligibility, pass2_reused=pass2_reused, pass2_reused_from_memory=pass2_from_memory,
        )

    return StoryCycleResult(
        story_id=story_id, included=True,
        evidence_is_valid=True, evidence_reused=evidence_reused, evidence_reused_from_memory=evidence_from_memory,
        evidence_attempts=evidence_attempts,
        pass2_is_valid=True, pass2_editor_eligibility=editor_eligibility, pass2_reused=pass2_reused, pass2_reused_from_memory=pass2_from_memory,
    )


def run_intelligence_cycle(run_id: str, corpus: Corpus, reporting_period: dict, *,
                            api_key: Optional[str] = None,
                            db_path: Optional[str] = None,
                            call_corpus_analyst: Optional[Callable] = None,
                            call_evidence_analyst: Optional[Callable] = None,
                            retrieve: Optional[Callable] = None,
                            call_pass2: Optional[Callable] = None,
                            call_editor: Optional[Callable] = None,
                            budget: Optional[RunBudget] = None,
                            circuit_breaker: Optional[CircuitBreaker] = None,
                            memory_sources: Optional[list] = None,
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

    `budget` (optional, default None = unlimited) and `circuit_breaker`
    (optional, default None = disabled) are both backward-compatible: a
    caller/test that does not pass them gets exactly the old, unlimited
    behavior. When supplied, the corpus_analyst and editor callers are
    also budget-wrapped (stage="corpus_analyst" / stage="editor", no
    story_id -- these are cycle-level, not per-story). After each story
    is processed, if budget.total_exhausted() or circuit_breaker.triggered
    has become true, every remaining candidate_story is recorded as
    excluded (exclusion_reason="run_budget_exhausted" or
    "circuit_breaker_triggered") WITHOUT calling _process_one_story for
    it at all -- no further request of any kind is made for those
    stories. If the trip happened before the Editor would otherwise run,
    the Editor is also skipped (brief_skipped_reason set accordingly)
    even if some stories were already eligible, since "fail closed" means
    no further request, including the Editor's own live call.
    """
    conn = store.get_connection(db_path)
    try:
        wrapped_corpus_caller = _budget_wrapped_caller(
            call_corpus_analyst or corpus_analyst.call_anthropic_corpus_analyst, budget, stage="corpus_analyst",
        )
        corpus_outcome = corpus_analyst.run_corpus_analysis(corpus, api_key=api_key, call_analyst=wrapped_corpus_caller)

        if not corpus_outcome.is_valid:
            return OrchestrationOutcome(
                run_id=run_id, corpus_analysis_is_valid=False,
                corpus_analysis_failure_reason=(corpus_outcome.failure_reason or "corpus_analyst_invalid"),
                budget_summary=(budget.summary() if budget is not None else None),
                circuit_breaker=(circuit_breaker.summary() if circuit_breaker is not None else None),
            )

        candidate_stories = (corpus_outcome.raw or {}).get("candidate_stories") or []

        story_results = []
        stopped_reason: Optional[str] = None
        for candidate_story in candidate_stories:
            if stopped_reason is not None:
                story_id = candidate_story.get("story_id", "UNKNOWN")
                story_results.append(StoryCycleResult(
                    story_id=story_id, included=False, exclusion_reason=stopped_reason,
                ))
                continue

            result = _process_one_story(
                run_id, candidate_story, corpus, api_key=api_key,
                call_evidence_analyst=call_evidence_analyst, retrieve=retrieve,
                call_pass2=call_pass2, conn=conn,
                budget=budget, circuit_breaker=circuit_breaker, memory_sources=memory_sources,
            )
            story_results.append(result)

            if budget is not None and budget.total_exhausted():
                stopped_reason = "run_budget_exhausted"
            elif circuit_breaker is not None and circuit_breaker.triggered:
                stopped_reason = "circuit_breaker_triggered"

        editor_inputs = store.reconstruct_editor_inputs(run_id, conn=conn)

        outcome = OrchestrationOutcome(
            run_id=run_id, corpus_analysis_is_valid=True, stories=story_results,
            editor_inputs_count=len(editor_inputs),
            budget_exhausted_reason=(budget.exhausted_reason if budget is not None else None),
            budget_summary=(budget.summary() if budget is not None else None),
            circuit_breaker=(circuit_breaker.summary() if circuit_breaker is not None else None),
        )

        if stopped_reason is not None:
            outcome.brief_skipped_reason = stopped_reason
            return outcome

        if not editor_inputs:
            outcome.brief_skipped_reason = "no_eligible_stories"
            return outcome

        wrapped_editor_caller = _budget_wrapped_caller(
            call_editor or editor.call_anthropic_editor, budget, stage="editor",
        )
        brief_id = f"{run_id}-brief-001"
        try:
            editor_outcome = editor.run_intelligence_editor(
                run_id, reporting_period, editor_inputs, api_key=api_key, call_analyst=wrapped_editor_caller,
            )
        except BudgetExhaustedError as e:
            outcome.brief_skipped_reason = "run_budget_exhausted"
            outcome.budget_exhausted_reason = budget.exhausted_reason if budget is not None else str(e)
            outcome.budget_summary = budget.summary() if budget is not None else None
            return outcome

        if editor_outcome.raw and isinstance(editor_outcome.raw, dict) and editor_outcome.raw.get("brief_id"):
            brief_id = editor_outcome.raw["brief_id"]

        store.save_brief(
            run_id, brief_id, reporting_period, editor_outcome.raw,
            is_valid=editor_outcome.is_valid, validation_errors=editor_outcome.validation_errors,
            failure_reason=editor_outcome.failure_reason, conn=conn,
        )

        outcome.brief_outcome = editor_outcome
        outcome.brief_id = brief_id
        if budget is not None:
            outcome.budget_summary = budget.summary()
        return outcome
    finally:
        conn.close()
