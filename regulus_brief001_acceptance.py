#!/usr/bin/env python3
"""
regulus_brief001_acceptance.py — the live, end-to-end acceptance runner
for REGULUS INTELLIGENCE BRIEF #001: the first full-cycle live run of
Corpus Analyst Pass #1 -> Evidence Analyst -> Intelligence Analyst
Pass #2 -> Intelligence Editor, over the golden Acceptance #3 corpus.

THIS SCRIPT MAKES MULTIPLE REAL, PAID ANTHROPIC API CALLS WHEN EXECUTED:
one Corpus Analyst call, one Evidence Analyst call per candidate story
(bounded web_search, max_uses=10 each), one Intelligence Analyst Pass #2
call per eligible story (no tools), and one Intelligence Editor call (no
tools). Per the explicit instruction for this task, this script is built
but is NOT executed as part of this implementation task. Running it
requires an explicit, separate authorization and a real ANTHROPIC_API_KEY.

WHAT THIS SCRIPT DOES NOT DO (scope control):
  - does NOT re-run corpus EXTRACTION from the production alerts table --
    the corpus is loaded verbatim from the already-committed golden
    fixture tests/fixtures/corpus_acceptance_3.json (the same 87-
    observation corpus every other acceptance run in this project has
    used), never from regulus_v3/dd_pipeline;
  - does NOT write to the production DD database -- regulus_orchestrator.
    py / intelligence_store.py never import regulus_v3 or open
    bis_watcher.db; this script's own persistence goes to a dedicated,
    timestamped SQLite file under brief_acceptance_runs/ (see
    INTELLIGENCE_DB_PATH override below), never the default
    regulus_intelligence.db a casual orchestrator run would use, and
    never bis_watcher.db;
  - does NOT send email;
  - does NOT modify any production file, table, schema, or the golden
    fixture itself (read-only);
  - does NOT merge or deploy anything;
  - does NOT hardcode any story's expected conclusion -- every stage uses
    its REAL default live caller (no stub), exactly as a casual
    orchestrator run would, UNLESS --corpus-analysis-artifact is passed
    explicitly, in which case (and ONLY in that case) the Corpus Analyst
    stage substitutes a previously-validated, already-accepted artifact
    instead of a live call -- see load_and_validate_corpus_analysis_
    artifact() / bootstrap_corpus_analysis_artifact() below. This is
    never an implicit fallback.

WHAT IT DOES:
  1. Load the real, unmodified Acceptance #3 corpus (87 observations) from
     tests/fixtures/corpus_acceptance_3.json.
  2. Call regulus_orchestrator.run_intelligence_cycle(run_id, corpus,
     reporting_period, api_key=...) with every call_* parameter left at
     its default (None) -- so every stage uses its own REAL live caller:
     corpus_analyst.call_anthropic_corpus_analyst,
     evidence_analyst.call_anthropic_evidence_analyst (per story),
     intelligence_analyst_pass2.call_anthropic_pass2 (per eligible
     story), intelligence_editor.call_anthropic_editor.
  3. Persist every stage's outcome via intelligence_store.py to a fresh,
     timestamped SQLite file under brief_acceptance_runs/ (never the
     default regulus_intelligence.db, never bis_watcher.db).
  4. Write a single diagnostic JSON summary (run metadata, per-story
     outcomes, the compiled Brief if produced) under the same directory.
  5. Print a concise run summary to stdout.

Run (NOT executed by this task):
    ANTHROPIC_API_KEY=... python3 regulus_brief001_acceptance.py \\
        --run-id brief001-acceptance-1 \\
        --reporting-period-start 2026-09-14 --reporting-period-end 2026-10-05
"""

import argparse
import json
import os
from datetime import datetime, timezone

from corpus_extractor import Corpus, CorpusObservation
import corpus_analyst
import corpus_analyst_schema
import evidence_analyst
import evidence_analyst_schema
import evidence_retrieval
import intelligence_analyst_pass2 as pass2
import intelligence_editor as editor
import intelligence_store as store
import regulus_orchestrator as orch

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixtures")
CORPUS_FIXTURE_PATH = os.path.join(FIXTURES_DIR, "corpus_acceptance_3.json")

DEFAULT_RUN_ID = "brief001-acceptance-1"
DEFAULT_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brief_acceptance_runs")

# ---------------------------------------------------------------------------
# CONSERVATIVE ACCEPTANCE BUDGET DEFAULTS -- added per the reliability/cost-
# control patch (see regulus_orchestrator.RunBudget / CircuitBreaker
# docstrings for exact semantics). These are deliberately TIGHT, not
# generous: by default this script cannot complete a full, fully-healthy
# Acceptance #3 run (11 candidate stories) in one invocation -- it will
# hit max_total_llm_calls=4 after roughly one story's worth of calls and
# fail closed (run_budget_exhausted), persisting whatever it got and
# stopping. This is intentional: these are ACCEPTANCE defaults for
# controlled, incremental, human-supervised runs after the live incident
# this patch responds to (an uncontrolled run silently burned ~$5 before
# it was manually aborted) -- not a throughput setting and not a global
# production limit (regulus_orchestrator.run_intelligence_cycle's own
# default remains unlimited; nothing here changes that). A full run is
# expected to require either several resumed invocations against the
# SAME --db-path (each one picking up where the last left off via the
# resume/reuse mechanism) or an explicit, wider budget passed via CLI
# flags. Nothing here silently raises the ceiling.
# ---------------------------------------------------------------------------
DEFAULT_MAX_TOTAL_LLM_CALLS = 4
DEFAULT_MAX_EVIDENCE_ANALYST_CALLS = 1
DEFAULT_MAX_EVIDENCE_ATTEMPTS_PER_STORY = 1
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 2


class CorpusAnalysisArtifactInvalidError(ValueError):
    """Raised by load_and_validate_corpus_analysis_artifact() when a
    --corpus-analysis-artifact fails validation, cannot be parsed, or
    does not exist. Always raised BEFORE bootstrap_corpus_analysis_
    artifact() persists anything and BEFORE any API call -- fail closed,
    never a silent partial acceptance of a bad artifact."""


class EvidenceAnalysisArtifactInvalidError(ValueError):
    """Raised by load_and_validate_evidence_analysis_artifact() when a
    --evidence-analysis-artifact fails validation, is incompatible with
    the candidate_story currently persisted for its story_id under the
    target run_id, or cannot be parsed. Always raised BEFORE bootstrap_
    evidence_analysis_artifact() persists anything and BEFORE any API
    call -- fail closed, never a silent partial acceptance. story_id
    alone is never sufficient to establish compatibility -- see the
    function's own docstring for the full compatibility chain."""


class UnknownStoryIdError(ValueError):
    """Raised by --story-id targeted-execution filtering when the
    requested story_id does not match any candidate_story in the
    CURRENTLY KNOWN Corpus Analyst output for this run_id (known from a
    --corpus-analysis-artifact bootstrapped THIS invocation, or from
    corpus_analyst-stage rows already persisted at --db-path from a
    prior invocation of the same --run-id).

    Raised BEFORE any API call whenever that output is already known --
    which, per this feature's scope, is the ONLY case --story-id is
    exercised against (the documented, immediate use: a bootstrapped
    Corpus Analyst artifact). story_id alone is matched against the
    EXACT SAME candidate_story.story_id values a live/bootstrapped
    Corpus Analyst Pass #1 would produce -- there is no separate/looser
    story-identity rule for this feature."""


def _known_candidate_stories(run_id: str, db_path: str) -> list:
    """Return every VALID candidate_story dict already persisted for
    `run_id` at the corpus_analyst stage of the intelligence store at
    `db_path`, or None if db_path does not exist yet or nothing is
    persisted there for this run_id -- i.e. Corpus Analyst output is NOT
    yet known, without making any API/network call either way. Used by
    --story-id targeted execution to validate the requested story_id,
    and to compute its own call-budget ceiling, using the SAME source of
    truth --dry-run reporting already uses (store.list_story_artifacts),
    never a parallel/bespoke lookup."""
    if not os.path.exists(db_path):
        return None
    conn = store.get_connection(db_path)
    try:
        persisted = store.list_story_artifacts(run_id, stage="corpus_analyst", conn=conn)
    finally:
        conn.close()
    if not persisted:
        return None
    return [rec.payload for rec in persisted if rec.is_valid]


def _require_known_story_id(candidate_stories: list, story_id: str) -> dict:
    """Return the single candidate_story dict in `candidate_stories`
    whose story_id matches `story_id`, or raise UnknownStoryIdError
    (fail closed) if none matches. story_id alone is matched exactly --
    no fuzzy/partial matching, no fallback to "process everything" on a
    miss."""
    for candidate_story in candidate_stories:
        if candidate_story.get("story_id") == story_id:
            return candidate_story
    raise UnknownStoryIdError(
        f"--story-id {story_id!r} does not match any candidate_story in the current, known "
        f"Corpus Analyst output for this run (known story_ids: "
        f"{[cs.get('story_id') for cs in candidate_stories]!r}) -- fails closed: no Evidence "
        f"Analyst, Pass #2, or Editor call will be made for any story this invocation."
    )


def build_targeted_call_corpus_analyst(underlying_caller, story_id: str):
    """Wrap `underlying_caller` (the real live Corpus Analyst caller, or
    a --corpus-analysis-artifact bootstrap substitution) so that, AFTER
    it returns its (unfiltered) raw result but BEFORE run_intelligence_
    cycle (regulus_orchestrator.py -- never edited) ever reads
    candidate_stories from it, that list is narrowed to the single
    candidate_story whose story_id matches `story_id`. Raises
    UnknownStoryIdError -- fail closed -- if no candidate_story matches;
    this is the ONLY place that check is still possible when Corpus
    Analyst output was not already known before the call (the early,
    zero-cost check in run_acceptance() covers every other case).
    Returned callable has the exact (corpus_payload, api_key) signature
    every call_corpus_analyst caller must have."""
    def _wrapped(corpus_payload, api_key):  # noqa: ARG001 -- signature match
        raw = underlying_caller(corpus_payload, api_key)
        if isinstance(raw, dict) and isinstance(raw.get("candidate_stories"), list):
            raw = dict(raw)
            raw["candidate_stories"] = [_require_known_story_id(raw["candidate_stories"], story_id)]
        return raw
    return _wrapped


def build_targeted_circuit_breaker(circuit_breaker: "orch.CircuitBreaker") -> "orch.CircuitBreaker":
    """Build a DEDICATED CircuitBreaker, already triggered, for --story-id
    targeted execution -- never the caller's own `circuit_breaker`
    instance/state. The Editor must NEVER run for a targeted
    partial-story invocation, regardless of how many new calls the
    selected story actually needed (even zero): run_intelligence_cycle
    (regulus_orchestrator.py -- never edited) already has an EXISTING,
    unmodified "stopped_reason -> skip the Editor entirely" path, taken
    whenever circuit_breaker.triggered is true after a story is
    processed -- so constructing it pre-triggered (same
    consecutive_failure_threshold, for reporting fidelity only; the
    threshold is otherwise moot with exactly one story in play) makes
    that path fire deterministically, with zero orchestrator changes."""
    return orch.CircuitBreaker(
        consecutive_failure_threshold=circuit_breaker.consecutive_failure_threshold,
        triggered=True,
    )


def compute_targeted_budget(budget: "orch.RunBudget", targeted_new_call_cap: int) -> "orch.RunBudget":
    """Build the EFFECTIVE run budget for a --story-id targeted
    invocation whose Corpus Analyst output is already known (so its own
    budget consumption is deterministically exactly one unit -- see
    run_acceptance()'s corpus-bootstrap docstring note): max_total_llm_
    calls is tightened to exactly `targeted_new_call_cap` (the number of
    NOT-yet-reusable Evidence Analyst/Pass #2 stages for the selected
    story) plus 1 reserved for that deterministic corpus unit -- unless
    the caller's OWN configured max_total_llm_calls is already tighter,
    in which case it is never loosened. max_evidence_analyst_calls and
    max_evidence_attempts_per_story are passed through completely
    unchanged -- this feature only ever tightens max_total_llm_calls."""
    internal_cap = 1 + targeted_new_call_cap
    capped_total = (
        internal_cap if budget.max_total_llm_calls is None
        else min(budget.max_total_llm_calls, internal_cap)
    )
    return orch.RunBudget(
        max_total_llm_calls=capped_total,
        max_evidence_analyst_calls=budget.max_evidence_analyst_calls,
        max_evidence_attempts_per_story=budget.max_evidence_attempts_per_story,
    )


def _resolve_story_reuse_state(run_id: str, candidate_story: dict, *, conn,
                                memory_sources: list = None, corpus: Corpus = None) -> dict:
    """Determine, for ONE candidate_story and WITHOUT making any
    API/network call, whether its Evidence Analyst and Pass #2 stages
    are already reusable under `run_id` -- via intelligence_store.
    find_reusable_story_artifact(), the orchestrator's OWN existing
    lookup, never a parallel/bespoke rule -- and how many NEW external
    LLM requests remain possible for it (0, 1, or 2: one per stage that
    is NOT yet reusable). Shared by both the general --dry-run report
    (every story) and --story-id targeted execution (one story only),
    so the two can never diverge on what "would_reuse"/"would_call"
    means for a given story.

    When `memory_sources` is supplied, a local miss is followed by the
    orchestrator's OWN read-only memory lookup (orch.find_memory_artifact,
    the same function the live path uses; `corpus` is then required), and
    states become would_reuse_local / would_reuse_from_memory / would_call.
    With no memory_sources the vocabulary is the original would_reuse /
    would_call, unchanged."""
    story_id = candidate_story.get("story_id", "UNKNOWN")
    use_memory = bool(memory_sources)
    local_label = "would_reuse_local" if use_memory else "would_reuse"
    evidence_fingerprint = store.compute_fingerprint(candidate_story)
    reusable_evidence = store.find_reusable_story_artifact(
        run_id, story_id, "evidence_analyst", evidence_fingerprint, conn=conn,
    )
    new_calls_needed = 0
    evidence_source = pass2_source = None
    if reusable_evidence is not None:
        evidence_state = local_label
        evidence_payload = reusable_evidence.payload
    else:
        hit = (orch.find_memory_artifact("evidence_analyst", candidate_story, evidence_fingerprint, corpus,
                                         memory_sources=memory_sources) if use_memory else None)
        if hit is not None:
            evidence_state = "would_reuse_from_memory"
            evidence_payload = hit[0].payload
            evidence_source = list(hit[1])
        else:
            evidence_state = "would_call"
            evidence_payload = None
            new_calls_needed += 1

    if evidence_payload is not None:
        pass2_fingerprint = store.compute_fingerprint(candidate_story, evidence_payload)
        reusable_pass2 = store.find_reusable_story_artifact(
            run_id, story_id, "pass2", pass2_fingerprint, conn=conn,
        )
        if reusable_pass2 is not None:
            pass2_state = local_label
        else:
            hit = (orch.find_memory_artifact("pass2", candidate_story, pass2_fingerprint, corpus,
                                             memory_sources=memory_sources,
                                             evidence_payload=evidence_payload) if use_memory else None)
            if hit is not None:
                pass2_state = "would_reuse_from_memory"
                pass2_source = list(hit[1])
            else:
                pass2_state = "would_call"
                new_calls_needed += 1
    else:
        pass2_state = "unknown_pending_evidence_call"
        new_calls_needed += 1  # conservative: assume it would run too

    return {
        "story_id": story_id,
        "evidence_analyst": evidence_state,
        "pass2": pass2_state,
        "evidence_memory_source": evidence_source,
        "pass2_memory_source": pass2_source,
        "new_calls_needed": new_calls_needed,
    }


def load_golden_corpus(path: str = CORPUS_FIXTURE_PATH) -> Corpus:
    """Load and reconstruct the REAL corpus_extractor.Corpus used for
    Acceptance #3 from tests/fixtures/corpus_acceptance_3.json. No
    extraction call is made here -- this is a read-only load of an
    already-committed fixture. Mirrors tests/_evidence_test_helpers.
    load_corpus_acceptance_3() exactly, reimplemented here so this script
    has no import dependency on the tests/ package."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    observations = [
        CorpusObservation(
            document_number=o["document_number"], publication_date=o["publication_date"],
            effective_date=o["effective_date"], title=o["title"], agency=o["agency"],
            score=o["score"], countries=o["countries"], entities=o["entities"], eccns=o["eccns"],
            change_type=o["change_type"], summary=o["summary"], primary_source_url=o["primary_source_url"],
            tier=o["tier"], due_diligence_ran=o["due_diligence_ran"],
        )
        for o in raw["observations"]
    ]
    return Corpus(
        start_date=raw["reporting_period"]["start"], end_date=raw["reporting_period"]["end"],
        observations=observations,
    ), raw["reporting_period"]


def load_and_validate_corpus_analysis_artifact(path: str, corpus: Corpus) -> dict:
    """Load a previously-produced, already-accepted Corpus Analyst Pass #1
    output from `path` (--corpus-analysis-artifact) and validate it using
    the EXACT SAME deterministic contract corpus_analyst.run_corpus_
    analysis() itself uses -- corpus_analyst_schema.validate_corpus_
    analysis() -- computed over the ACTUAL document numbers present in
    `corpus`, never a looser or different check. This function imports
    corpus_analyst_schema only (the schema module corpus_analyst.py
    itself already depends on); it never imports or modifies corpus_
    analyst.py's own call/retry logic, and makes no API call.

    Raises CorpusAnalysisArtifactInvalidError -- fail closed, before any
    further processing -- if the file does not exist, is not valid JSON,
    or fails schema validation (including a document-number reference
    outside the current corpus, which is exactly how a MISMATCHED
    artifact -- one produced against a different corpus -- is caught).
    """
    if not os.path.isfile(path):
        raise CorpusAnalysisArtifactInvalidError(
            f"--corpus-analysis-artifact path does not exist or is not a file: {path!r}"
        )
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise CorpusAnalysisArtifactInvalidError(
            f"--corpus-analysis-artifact at {path!r} could not be read/parsed as JSON: {e}"
        ) from e

    valid_document_numbers = {
        o.document_number for o in corpus.observations if o.document_number is not None
    }
    result = corpus_analyst_schema.validate_corpus_analysis(raw, valid_document_numbers)
    if not result.is_valid:
        raise CorpusAnalysisArtifactInvalidError(
            f"--corpus-analysis-artifact at {path!r} failed Corpus Analyst schema validation "
            f"against the current corpus fixture's document numbers "
            f"(a mismatched artifact -- e.g. produced against a different corpus -- fails here "
            f"via an out-of-set document-number reference): {result.validation_errors}"
        )
    return raw


def bootstrap_corpus_analysis_artifact(run_id: str, raw: dict, *, db_path: str) -> list:
    """Persist an externally-validated Corpus Analyst Pass #1 output
    (already passed through load_and_validate_corpus_analysis_artifact())
    into the intelligence store for `run_id`, exactly as a normal live
    run would persist it: one store.save_story_artifact(..., stage=
    "corpus_analyst", input_fingerprint=...) call per candidate_story,
    with its deterministic content fingerprint attached so later Evidence/
    Pass #2 reuse lookups (and this script's own --dry-run reporting) see
    exactly the same state a live Corpus Analyst call would have produced.

    Never called implicitly -- only when the operator passes
    --corpus-analysis-artifact explicitly (see run_acceptance()). Does
    not modify corpus_analyst.py. Makes no API call. Returns the list of
    candidate_story dicts persisted.

    Known limitation (documented, not engineered around, per the
    smallest-safe-change scope of this bootstrap capability): if `run_id`
    already has PERSISTED corpus_analyst rows for story_ids that are NOT
    present in `raw` (e.g. a prior artifact/live run covered more
    stories), those stale rows are left untouched rather than reconciled
    or deleted -- this bootstrap is intended for a fresh run_id/db_path,
    not for replacing a partially-complete run with a narrower artifact.
    """
    candidate_stories = raw.get("candidate_stories") or []
    conn = store.get_connection(db_path)
    try:
        for candidate_story in candidate_stories:
            story_id = candidate_story.get("story_id", "UNKNOWN")
            fingerprint = store.compute_fingerprint(candidate_story)
            store.save_story_artifact(
                run_id, story_id, "corpus_analyst", candidate_story,
                is_valid=True, input_fingerprint=fingerprint, conn=conn,
            )
    finally:
        conn.close()
    return candidate_stories


def load_and_validate_evidence_analysis_artifact(path: str, run_id: str, *, corpus: Corpus,
                                                   db_path: str) -> tuple:
    """Load a previously-produced, already-validated Evidence Analyst
    diagnostic artifact (the JSON file evidence_analyst_acceptance_cs01.py
    itself writes under evidence_acceptance_runs/) from `path`
    (--evidence-analysis-artifact) and establish that it is safe to reuse
    for `run_id`, making ZERO API/network requests.

    COMPATIBILITY CHAIN (story_id alone is never sufficient -- every one
    of these must hold, in order, or this raises
    EvidenceAnalysisArtifactInvalidError and fails closed before anything
    is persisted):

      1. The file must parse as JSON and have the diagnostic shape
         evidence_analyst_acceptance_cs01.py writes: run_metadata.
         story_id, outcome.is_valid, outcome.raw (a dict).
      2. outcome.is_valid must be True -- an artifact the live run itself
         recorded as invalid/failed is never a bootstrap candidate, for
         the same reason intelligence_store.py only ever attaches a
         fingerprint to a VALID artifact: an invalid result must never
         become silently reusable.
      3. run_metadata.story_id and outcome.raw["story_id"] must agree
         with each other (an internally inconsistent artifact is
         rejected outright).
      4. A candidate_story for that EXACT story_id must already be
         persisted, and VALID, as the corpus_analyst stage for `run_id`
         (i.e. Corpus Analyst Pass #1 -- live or bootstrapped via
         --corpus-analysis-artifact -- must already be in the store for
         this run_id). There is nothing to check compatibility against
         otherwise.
      5. The artifact's outcome.raw is RE-VALIDATED, right now, against
         THAT CURRENT candidate_story using the EXACT SAME contract
         run_live_evidence_analysis() itself uses on a live success path:
         evidence_analyst_schema.validate_evidence_analysis(raw,
         story=candidate_story, valid_document_numbers=<the current
         corpus's own document numbers>, retrieval_results=<reconstructed
         from the artifact's OWN already-persisted retrieval data -- no
         new retrieval call is made>), PLUS evidence_analyst._find_
         silently_dropped_documents(). This is the step that actually
         catches a MISMATCHED artifact beyond story_id: the schema
         enforces an exact 1:1 correspondence between raw.question_
         findings and candidate_story["research_questions"], so an
         artifact produced against a different (e.g. edited) version of
         this story_id's candidate_story fails here deterministically,
         not via a separate/weaker rule invented for this bootstrap path.
      6. The artifact itself carries no fingerprint of its own (it
         predates the resume/reuse fingerprinting mechanism entirely --
         it is the standalone CS-01 diagnostic a plain acceptance script
         writes). So the "exact Evidence input fingerprint" requirement
         is satisfied by COMPUTING it, right now, via intelligence_
         store.compute_fingerprint(candidate_story) -- the literal same
         call _process_one_story() makes before an Evidence Analyst call
         -- over the CURRENT persisted candidate_story (step 4). This is
         not a comparison against a stale value (none exists); it is the
         value that gets attached on persistence, so a LATER orchestrator
         lookup (find_reusable_story_artifact) recomputes the identical
         fingerprint from the same candidate_story and finds a match --
         and, symmetrically, if that story_id's candidate_story content
         ever changes afterward, the next computed fingerprint will
         differ and reuse will correctly be refused, with no special
         case needed here.

    IMPORTANT -- this is a LEGACY ARTIFACT MIGRATION, not normal
    fingerprint-verified reuse, and the distinction is load-bearing: a
    normal reused artifact's fingerprint was computed from the SAME input
    it was then later matched against (the orchestrator attached it from
    the actual candidate_story it analyzed, and a subsequent lookup
    recomputes from that same, unchanged candidate_story -- true history-
    equality). This artifact predates fingerprinting entirely, so there
    is no historical fingerprint to verify equality against -- step 6
    above COMPUTES a fingerprint now, from the CURRENT candidate_story,
    rather than confirming one that was already there. Compatibility with
    the current input is instead established by the full re-validation in
    step 5 (the same schema contract a live success path applies), not by
    fingerprint equality against history. This return value's `provenance`
    dict records that distinction explicitly and auditably (see module
    docstring note on intelligence_store.save_story_artifact's
    `provenance` parameter); it is store-level metadata only, never part
    of the substantive Evidence Analyst payload, and is NEVER consulted by
    find_reusable_story_artifact() -- reuse remains governed exclusively
    by is_valid + exact input_fingerprint equality, identically for
    legacy-migrated and normally-generated artifacts alike.

    Returns (story_id, raw, fingerprint, provenance) on success. Raises
    EvidenceAnalysisArtifactInvalidError, fail closed, otherwise.
    """
    if not os.path.isfile(path):
        raise EvidenceAnalysisArtifactInvalidError(
            f"--evidence-analysis-artifact path does not exist or is not a file: {path!r}"
        )
    try:
        with open(path, "r", encoding="utf-8") as f:
            artifact = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise EvidenceAnalysisArtifactInvalidError(
            f"--evidence-analysis-artifact at {path!r} could not be read/parsed as JSON: {e}"
        ) from e

    if not isinstance(artifact, dict):
        raise EvidenceAnalysisArtifactInvalidError(
            f"--evidence-analysis-artifact at {path!r}: root must be a JSON object"
        )

    run_metadata = artifact.get("run_metadata")
    outcome_dict = artifact.get("outcome")
    if not isinstance(run_metadata, dict) or not isinstance(outcome_dict, dict):
        raise EvidenceAnalysisArtifactInvalidError(
            f"--evidence-analysis-artifact at {path!r} is missing the expected "
            f"run_metadata/outcome diagnostic shape (not a structurally valid Evidence "
            f"Analyst acceptance artifact)"
        )

    if outcome_dict.get("is_valid") is not True:
        raise EvidenceAnalysisArtifactInvalidError(
            f"--evidence-analysis-artifact at {path!r} has outcome.is_valid="
            f"{outcome_dict.get('is_valid')!r} -- only an artifact the live run itself "
            f"recorded as VALID may be bootstrapped; failure_reason="
            f"{outcome_dict.get('failure_reason')!r}"
        )

    raw = outcome_dict.get("raw")
    if not isinstance(raw, dict):
        raise EvidenceAnalysisArtifactInvalidError(
            f"--evidence-analysis-artifact at {path!r}: outcome.raw must be an object, "
            f"got {type(raw).__name__}"
        )

    metadata_story_id = run_metadata.get("story_id")
    raw_story_id = raw.get("story_id")
    if not metadata_story_id or not raw_story_id:
        raise EvidenceAnalysisArtifactInvalidError(
            f"--evidence-analysis-artifact at {path!r}: run_metadata.story_id and/or "
            f"outcome.raw.story_id is missing"
        )
    if metadata_story_id != raw_story_id:
        raise EvidenceAnalysisArtifactInvalidError(
            f"--evidence-analysis-artifact at {path!r}: run_metadata.story_id "
            f"({metadata_story_id!r}) does not match outcome.raw.story_id ({raw_story_id!r}) -- "
            f"internally inconsistent artifact, story_id alone cannot establish compatibility"
        )
    story_id = raw_story_id

    conn = store.get_connection(db_path)
    try:
        corpus_record = store.load_story_artifact(run_id, story_id, "corpus_analyst", conn=conn)
    finally:
        conn.close()
    if corpus_record is None or not corpus_record.is_valid:
        raise EvidenceAnalysisArtifactInvalidError(
            f"no persisted, VALID Corpus Analyst candidate_story found for story_id={story_id!r} "
            f"under run_id={run_id!r} at {db_path!r} -- bootstrap/persist Corpus Analyst Pass #1 "
            f"for this run_id first (e.g. via --corpus-analysis-artifact); story_id alone cannot "
            f"establish compatibility with an input that isn't known yet"
        )
    candidate_story = corpus_record.payload
    if candidate_story.get("story_id") != story_id:
        raise EvidenceAnalysisArtifactInvalidError(
            f"persisted corpus_analyst artifact for story_id={story_id!r} under run_id={run_id!r} "
            f"carries a different story_id internally ({candidate_story.get('story_id')!r}) -- "
            f"refusing to bootstrap against inconsistent persisted state"
        )

    valid_document_numbers = {
        o.document_number for o in corpus.observations if o.document_number is not None
    }

    retrieval_dict = artifact.get("retrieval")
    documents = []
    if isinstance(retrieval_dict, dict) and isinstance(retrieval_dict.get("documents"), list):
        try:
            documents = [evidence_retrieval.RetrievedDocument(**d) for d in retrieval_dict["documents"]]
        except TypeError as e:
            raise EvidenceAnalysisArtifactInvalidError(
                f"--evidence-analysis-artifact at {path!r}: retrieval.documents entries do not "
                f"match the expected RetrievedDocument shape: {e}"
            ) from e
    retrieval_bundle = evidence_retrieval.EvidenceRetrievalBundle(
        story_id=(retrieval_dict.get("story_id", story_id) if isinstance(retrieval_dict, dict) else story_id),
        documents=documents,
    )

    # Re-validate RIGHT NOW against the CURRENT candidate_story, using the
    # exact same contract run_live_evidence_analysis() itself applies on
    # its own success path -- no separate/weaker rule. Makes no network
    # call: retrieval_bundle was reconstructed above from the artifact's
    # OWN already-persisted (already-paid-for) retrieval data.
    result = evidence_analyst_schema.validate_evidence_analysis(
        raw, story=candidate_story, valid_document_numbers=valid_document_numbers,
        retrieval_results=retrieval_bundle.by_document_number(),
    )
    dropped_errors = evidence_analyst._find_silently_dropped_documents(raw, retrieval_bundle)
    combined_errors = list(result.validation_errors) + dropped_errors
    if not result.is_valid or dropped_errors:
        raise EvidenceAnalysisArtifactInvalidError(
            f"--evidence-analysis-artifact at {path!r} for story_id={story_id!r} is NOT "
            f"compatible with the candidate_story currently persisted for run_id={run_id!r} "
            f"(re-validation against the current input failed): {combined_errors}"
        )

    # The artifact predates fingerprinting and carries none of its own --
    # this IS the exact computation _process_one_story() makes before an
    # Evidence Analyst call, over the CURRENT persisted candidate_story.
    # It is NOT a verification against a historical fingerprint (none
    # exists); it is a freshly-assigned value, so this bootstrap is a
    # LEGACY ARTIFACT MIGRATION, not normal fingerprint-verified reuse --
    # see the docstring above and the `provenance` dict below, which
    # records that distinction explicitly and auditably as store-level
    # metadata (never part of the substantive Evidence Analyst payload,
    # never consulted by find_reusable_story_artifact()).
    fingerprint = store.compute_fingerprint(candidate_story)
    provenance = {
        "kind": "legacy_artifact_migration",
        "source_artifact_path": path,
        "historical_input_fingerprint_verified": False,
        "compatibility_established_by": "revalidation_against_current_candidate_story",
        "migrated_at": datetime.now(timezone.utc).isoformat(),
    }
    return story_id, raw, fingerprint, provenance


def bootstrap_evidence_analysis_artifact(run_id: str, story_id: str, raw: dict, fingerprint: str, *,
                                          db_path: str, provenance: dict = None) -> None:
    """Persist an externally-validated Evidence Analyst output (already
    passed through load_and_validate_evidence_analysis_artifact()) into
    the intelligence store for `run_id`/`story_id` as the evidence_
    analyst stage, exactly as a successful live Evidence Analyst call
    would be persisted (is_valid=True, with its input_fingerprint
    attached) -- so the orchestrator's EXISTING find_reusable_story_
    artifact() lookup picks it up on the next run/dry-run with no
    special-casing. `raw` is persisted verbatim -- this function never
    alters its substantive analytical content. Never called implicitly
    -- only when the operator passes --evidence-analysis-artifact
    explicitly. Makes no API call.

    `provenance`, when given (as produced by load_and_validate_evidence_
    analysis_artifact()), is persisted alongside the artifact as store-
    level-only metadata (intelligence_store.StoryArtifactRecord.
    provenance) marking this record as a LEGACY ARTIFACT MIGRATION --
    i.e. one whose input_fingerprint could not be verified against any
    historical value (the source artifact predates fingerprinting) and
    whose compatibility with the current input was instead established
    by full re-validation. It is never folded into `raw` (the frozen
    Evidence Analyst payload/schema is untouched) and is never read by
    find_reusable_story_artifact() or any other reuse-eligibility
    decision -- reuse continues to require exact input_fingerprint
    equality, identically whether or not provenance is set. A normally-
    generated (live) Evidence Analyst artifact is saved with no
    provenance argument, so its persisted record's provenance is None,
    making legacy-migrated and normally-verified records explicitly
    distinguishable at the store level."""
    conn = store.get_connection(db_path)
    try:
        store.save_story_artifact(
            run_id, story_id, "evidence_analyst", raw,
            is_valid=True, input_fingerprint=fingerprint, provenance=provenance, conn=conn,
        )
    finally:
        conn.close()


def _outcome_to_diagnostic_dict(run_id: str, outcome: orch.OrchestrationOutcome, *,
                                 started_at: str, finished_at: str, db_path: str) -> dict:
    """Serialize a full OrchestrationOutcome (plus run metadata) into a
    JSON-safe diagnostic dict. Never includes an API key or any request
    header -- every nested *AttemptDiagnostics.to_dict() already carries
    no credential field, and this function adds none."""
    return {
        "run_metadata": {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "corpus_fixture": os.path.relpath(CORPUS_FIXTURE_PATH, os.path.dirname(os.path.abspath(__file__))),
            "intelligence_store_db_path": db_path,
        },
        "corpus_analysis_is_valid": outcome.corpus_analysis_is_valid,
        "corpus_analysis_failure_reason": outcome.corpus_analysis_failure_reason,
        "stories": [
            {
                "story_id": s.story_id,
                "included": s.included,
                "exclusion_reason": s.exclusion_reason,
                "evidence_is_valid": s.evidence_is_valid,
                "evidence_failure_reason": s.evidence_failure_reason,
                "evidence_error_category": s.evidence_error_category,
                "evidence_reused": s.evidence_reused,
                "evidence_reused_from_memory": s.evidence_reused_from_memory,
                "pass2_is_valid": s.pass2_is_valid,
                "pass2_failure_reason": s.pass2_failure_reason,
                "pass2_editor_eligibility": s.pass2_editor_eligibility,
                "pass2_reused": s.pass2_reused,
                "pass2_reused_from_memory": s.pass2_reused_from_memory,
                "exception": s.exception,
            }
            for s in outcome.stories
        ],
        "editor_inputs_count": outcome.editor_inputs_count,
        "brief_id": outcome.brief_id,
        "brief_skipped_reason": outcome.brief_skipped_reason,
        "budget_exhausted_reason": outcome.budget_exhausted_reason,
        "budget_summary": outcome.budget_summary,
        "circuit_breaker": outcome.circuit_breaker,
        "brief_outcome": (
            {
                "is_valid": outcome.brief_outcome.is_valid,
                "validation_status": outcome.brief_outcome.validation_status,
                "validation_errors": outcome.brief_outcome.validation_errors,
                "failure_reason": outcome.brief_outcome.failure_reason,
                "raw": outcome.brief_outcome.raw,
                "attempts": [a.to_dict() for a in outcome.brief_outcome.attempts],
            }
            if outcome.brief_outcome is not None else None
        ),
    }


def _print_summary(outcome: orch.OrchestrationOutcome, out_path: str) -> None:
    print(f"Regulus Intelligence Brief #001 live acceptance run -- run_id={outcome.run_id}")
    print(f"  corpus_analysis_is_valid : {outcome.corpus_analysis_is_valid}")
    print(f"  corpus_analysis_failure_reason: {outcome.corpus_analysis_failure_reason}")
    print(f"  stories processed        : {len(outcome.stories)}")
    print(f"  stories included         : {outcome.included_story_ids}")
    print(f"  stories excluded         : {outcome.excluded_story_ids}")
    for s in outcome.stories:
        if not s.included:
            print(f"    - {s.story_id} excluded: {s.exclusion_reason}")
    print(f"  editor_inputs_count      : {outcome.editor_inputs_count}")
    print(f"  brief_skipped_reason     : {outcome.brief_skipped_reason}")
    print(f"  budget_exhausted_reason  : {outcome.budget_exhausted_reason}")
    if outcome.budget_summary is not None:
        print(f"  budget_summary           : {outcome.budget_summary}")
    if outcome.circuit_breaker is not None:
        print(f"  circuit_breaker          : {outcome.circuit_breaker}")
    if outcome.brief_outcome is not None:
        print(f"  brief_id                 : {outcome.brief_id}")
        print(f"  brief validation_status  : {outcome.brief_outcome.validation_status}")
        print(f"  brief failure_reason     : {outcome.brief_outcome.failure_reason}")
    print(f"  diagnostic artifact      : {out_path}")


def _print_budget_plan(budget: orch.RunBudget, circuit_breaker: orch.CircuitBreaker, *, db_path: str) -> None:
    """Print the configured call budget BEFORE any execution (live or
    dry-run) -- required so a human watching stdout can abort before any
    request is made if the configured budget looks wrong."""
    print("Regulus Intelligence Brief #001 acceptance -- configured run budget (enforced BEFORE "
          "each request is initiated, not after):")
    print(f"  max_total_llm_calls              : {budget.max_total_llm_calls}")
    print(f"  max_evidence_analyst_calls        : {budget.max_evidence_analyst_calls}")
    print(f"  max_evidence_attempts_per_story    : {budget.max_evidence_attempts_per_story}")
    print(f"  circuit_breaker_consecutive_threshold: {circuit_breaker.consecutive_failure_threshold}")
    print(f"  intelligence_store_db_path        : {db_path}")


def _dry_run_report(run_id: str, *, db_path: str, budget: orch.RunBudget,
                     circuit_breaker: orch.CircuitBreaker, story_id: str = None,
                     memory_sources: list = None) -> dict:
    """Report what a real run WOULD do, making ZERO API requests.

    Loads the corpus (a local, read-only fixture load -- not a model
    call) and checks intelligence_store for any already-persisted,
    reusable state for this exact run_id at this exact db_path (only
    present if --db-path points at a db file from a PRIOR run of this
    same run_id -- the default fresh-timestamped db_path never has any).

    If no persisted Corpus Analyst output exists for this run_id, the
    candidate stories are genuinely unknown without a live call -- this
    function does not guess or fabricate them; it reports that plainly
    along with the worst-case call volume bound by the configured
    EVIDENCE_ANALYST/PASS2/EDITOR_MAX_ATTEMPTS constants and the budget.

    `story_id` (--story-id) is the SAME explicit, opt-in execution
    filter run_acceptance() applies to a live run -- see its docstring.
    Here it only changes what is REPORTED, never what is persisted:
      - when Corpus Analyst output is already known (the only case this
        feature targets), an unknown story_id raises UnknownStoryIdError
        -- fail closed, exactly as a live run would, and just as free of
        cost (a dry run never makes a call either way);
      - only story_id's OWN entry appears in report["stories"] (every
        other story is simply not reported on, not merely marked
        excluded -- a targeted run never considers them);
      - report["would_call_editor"] is always False, with
        report["editor_skipped_reason"] = "targeted_partial_story_run"
        (the Editor needs every eligible story's Pass #2 output, and a
        targeted run deliberately never runs any OTHER story's Pass #2)
        -- this does not depend on whether story_id's own stages are
        would_reuse or would_call;
      - report["max_new_external_llm_requests"] is the exact count of
        NOT-YET-reusable stages for story_id alone (0, 1, or 2) -- the
        number of genuinely NEW network/model calls this invocation
        could make, as opposed to max_possible_calls_worst_case's
        retries-exhausted bound (a different, intentionally more
        conservative metric, left unchanged here). run_acceptance()
        enforces this exact number as this invocation's own additional
        call-budget ceiling on a live run -- see its docstring -- so
        this reported figure is never merely descriptive.
    """
    print(f"\n[DRY RUN] run_id={run_id!r} -- no API request will be made."
          + (f" --story-id={story_id!r} (targeted partial-story run)" if story_id is not None else ""))
    _print_budget_plan(budget, circuit_breaker, db_path=db_path)

    corpus, _ = load_golden_corpus()  # local fixture load only -- no API call

    conn = None
    persisted_corpus_artifacts = []
    if os.path.exists(db_path):
        conn = store.get_connection(db_path)
        persisted_corpus_artifacts = store.list_story_artifacts(run_id, stage="corpus_analyst", conn=conn)

    report: dict = {
        "run_id": run_id,
        "memory_sources": [list(m) for m in (memory_sources or [])],
        "db_path": db_path,
        "db_path_exists": os.path.exists(db_path),
        "budget": budget.summary(),
        "circuit_breaker_consecutive_threshold": circuit_breaker.consecutive_failure_threshold,
        "corpus_analyst": None,
        "stories": [],
        "would_call_editor": None,
        "max_possible_calls_worst_case": None,
        "max_possible_calls_budget_ceiling": budget.max_total_llm_calls,
    }

    if not persisted_corpus_artifacts:
        print("  corpus_analyst   : no persisted output found for this run_id at this db_path -- "
              "candidate stories are unknown without a live call.")
        report["corpus_analyst"] = {
            "resolved": False,
            "note": "no persisted artifact -- a real run would call Corpus Analyst Pass #1 "
                    f"(up to {corpus_analyst.CORPUS_ANALYST_MAX_ATTEMPTS} attempts) before anything "
                    "else is knowable",
        }
        if story_id is not None:
            report["selected_story_ids"] = [story_id]
            print(f"  --story-id       : {story_id!r} cannot be resolved yet -- Corpus Analyst "
                  f"output is unknown without a live call; bootstrap it first (e.g. via "
                  f"--corpus-analysis-artifact) to validate --story-id at zero cost.")
        # Worst case with zero prior state: Corpus Analyst's own attempts,
        # plus nothing else is computable yet (candidate_stories unknown).
        worst_case = corpus_analyst.CORPUS_ANALYST_MAX_ATTEMPTS
        report["max_possible_calls_worst_case"] = worst_case
        report["max_possible_calls_actual"] = (
            min(worst_case, budget.max_total_llm_calls) if budget.max_total_llm_calls is not None else worst_case
        )
        print(f"  max possible calls (worst case, corpus unresolved): {worst_case} "
              f"(Corpus Analyst attempts only -- nothing further is knowable before that call)")
        print(f"  max calls actually initiable under the configured budget: "
              f"{report['max_possible_calls_actual']}")
        if conn is not None:
            conn.close()
        return report

    # A persisted corpus_analyst artifact exists -- use its candidate
    # stories (exactly as the real run would via intelligence_store) to
    # report per-story reuse eligibility, with zero model calls.
    candidate_stories = [rec.payload for rec in persisted_corpus_artifacts if rec.is_valid]
    report["corpus_analyst"] = {"resolved": True, "candidate_story_count": len(candidate_stories)}
    print(f"  corpus_analyst   : persisted, valid output found ({len(candidate_stories)} candidate stories)")

    if story_id is not None:
        # --story-id targeted reporting: validate (fail closed, zero
        # cost) and then report ONLY this one story -- every other
        # story is simply never considered, not merely marked excluded.
        target_candidate_story = _require_known_story_id(candidate_stories, story_id)
        stories_to_report = [target_candidate_story]
        report["selected_story_ids"] = [story_id]
        print(f"  selected stories : {story_id} only")
        print(f"  Corpus Analyst   : reused/bootstrap, zero calls")
    else:
        stories_to_report = candidate_stories
        report["selected_story_ids"] = [cs.get("story_id", "UNKNOWN") for cs in candidate_stories]

    worst_case = 0
    any_would_call_editor_input = False
    max_new_external_llm_requests = 0
    for candidate_story in stories_to_report:
        state = _resolve_story_reuse_state(run_id, candidate_story, conn=conn,
                                           memory_sources=memory_sources, corpus=corpus)
        story_id_i = state["story_id"]
        story_report = {"story_id": story_id_i, "evidence_analyst": state["evidence_analyst"],
                         "pass2": state["pass2"]}
        if memory_sources:
            story_report["evidence_memory_source"] = state["evidence_memory_source"]
            story_report["pass2_memory_source"] = state["pass2_memory_source"]
        max_new_external_llm_requests += state["new_calls_needed"]

        if state["evidence_analyst"] == "would_call":
            worst_case += evidence_analyst.EVIDENCE_ANALYST_MAX_ATTEMPTS
        if state["pass2"] in ("would_call", "unknown_pending_evidence_call"):
            worst_case += pass2.PASS2_MAX_ATTEMPTS
        if state["pass2"] != "unknown_pending_evidence_call":
            any_would_call_editor_input = True

        report["stories"].append(story_report)
        if story_id is not None:
            print(f"  {story_id_i} Evidence Analyst: {story_report['evidence_analyst']}, "
                  f"{'zero calls' if story_report['evidence_analyst'].startswith('would_reuse') else 'up to 1 new call'}")
            print(f"  {story_id_i} Pass #2        : {story_report['pass2']}")
        else:
            print(f"    - {story_id_i}: evidence_analyst={story_report['evidence_analyst']}, "
                  f"pass2={story_report['pass2']}")

    if story_id is not None:
        # The Editor is NEVER run for a targeted partial-story
        # invocation -- it needs every eligible story's persisted Pass
        # #2 output, and a targeted run deliberately never processes
        # any story other than story_id, regardless of whether
        # story_id's own stages were reused or newly called.
        report["would_call_editor"] = False
        report["editor_skipped_reason"] = "targeted_partial_story_run"
        print(f"  Editor           : skipped because targeted partial-story run")
        report["max_new_external_llm_requests"] = max_new_external_llm_requests
        print(f"  maximum NEW external LLM requests for this invocation: {max_new_external_llm_requests}")
        # For a targeted run, the enforced ceiling (see run_acceptance())
        # makes the retries-exhausted worst_case bound below inapplicable
        # -- replace it with the same, actually-enforced figure so these
        # fields never contradict the dedicated one above.
        worst_case = max_new_external_llm_requests
    elif any_would_call_editor_input:
        worst_case += editor.EDITOR_MAX_ATTEMPTS
        report["would_call_editor"] = True
        if memory_sources:
            report["max_new_external_llm_requests"] = max_new_external_llm_requests + 1  # +1 Editor call
            print(f"  maximum NEW external LLM requests (stage calls + 1 Editor call, "
                  f"before Editor retry): {report['max_new_external_llm_requests']}")
    else:
        report["would_call_editor"] = False

    report["max_possible_calls_worst_case"] = worst_case
    report["max_possible_calls_actual"] = (
        min(worst_case, budget.max_total_llm_calls) if budget.max_total_llm_calls is not None else worst_case
    )
    print(f"  max possible calls (worst case, all own-retries exhausted): {worst_case}")
    if budget.max_total_llm_calls is not None:
        print(f"  budget ceiling (max_total_llm_calls)                      : {budget.max_total_llm_calls}")
    print(f"  max calls actually initiable under the configured budget   : "
          f"{report['max_possible_calls_actual']}")

    if conn is not None:
        conn.close()
    return report


def run_acceptance(run_id: str = DEFAULT_RUN_ID, *,
                    reporting_period: dict = None,
                    output_dir: str = DEFAULT_OUTPUT_DIR,
                    api_key: str = None,
                    db_path: str = None,
                    budget: "orch.RunBudget" = None,
                    circuit_breaker: "orch.CircuitBreaker" = None,
                    dry_run: bool = False,
                    corpus_analysis_artifact_path: str = None,
                    evidence_analysis_artifact_path: str = None,
                    story_id: str = None,
                    memory_sources: list = None) -> str:
    """Run one full, LIVE Regulus intelligence cycle over the golden
    Acceptance #3 corpus, persist it to a fresh timestamped SQLite file
    under output_dir (or to `db_path`, when given, enabling resume across
    runs of the SAME run_id), write a diagnostic JSON artifact, and
    return the artifact's path.

    `budget` and `circuit_breaker` default to the conservative acceptance
    defaults (DEFAULT_* constants above) when not supplied -- NOT to
    unlimited -- so invoking this function (or the CLI) with no extra
    flags always runs under a bounded budget; raising any limit requires
    an explicit, separate argument.

    If `dry_run` is True, makes ZERO API requests: prints and returns the
    dry-run report from _dry_run_report() and does not call
    run_intelligence_cycle at all.

    `corpus_analysis_artifact_path` (--corpus-analysis-artifact) is the
    explicit, opt-in bootstrap: when supplied, the file is loaded and
    validated via load_and_validate_corpus_analysis_artifact() (fail
    closed, before anything else happens) and persisted via bootstrap_
    corpus_analysis_artifact() -- BEFORE the dry-run branch, so a dry run
    with this flag reports the bootstrapped candidate stories' reuse
    eligibility rather than "unresolved". On a LIVE run, the validated
    artifact is also substituted in place of a real Corpus Analyst call
    (via call_corpus_analyst), so Corpus Analyst Pass #1 is never
    re-invoked for this run_id -- exactly the "do not spend API calls
    rediscovering an already-accepted result" requirement this flag
    exists for. This is NEVER an implicit fallback: with the flag
    omitted, behavior is bit-for-bit what it was before this capability
    existed. NOTE: the orchestrator's budget wrapper still counts this
    substituted callable as ONE corpus_analyst-stage call against
    max_total_llm_calls on a live run (it has no way to distinguish a
    real network call from a bootstrap substitution without changing the
    shared budget-wrapping mechanism used by every other caller/test) --
    it makes zero actual requests, but conservatively still consumes one
    unit of the configured budget headroom.

    `evidence_analysis_artifact_path` (--evidence-analysis-artifact) is
    the analogous explicit, opt-in bootstrap for ONE already-validated
    Evidence Analyst artifact (e.g. the diagnostic JSON evidence_analyst_
    acceptance_cs01.py writes). See load_and_validate_evidence_analysis_
    artifact() for the full compatibility chain it enforces before
    persisting anything. Unlike the Corpus Analyst bootstrap, this needs
    NO call_evidence_analyst override on a live run: _process_one_story()
    already checks intelligence_store.find_reusable_story_artifact()
    for a valid, fingerprint-matching Evidence artifact before ever
    calling the Evidence Analyst, so persisting it here is sufficient on
    its own for that story to be reused with zero further cost. Also
    never an implicit fallback -- omitted, behavior is unchanged.

    Makes MULTIPLE real Anthropic API calls when NOT in dry-run mode (see
    module docstring) -- this is the live acceptance path, not a test.
    Never writes to bis_watcher.db / regulus_v3.DB_PATH, never sends
    email, never re-runs corpus extraction from the production alerts
    table. Never modifies corpus_analyst.py.

    `story_id` (--story-id) is an explicit, opt-in EXECUTION FILTER,
    narrowly scoped to selecting which ONE candidate_story is processed
    downstream of Corpus Analyst Pass #1 -- it changes nothing about
    intelligence logic, prompts, schemas, persistence semantics,
    fingerprints, provenance, or the budget/circuit-breaker VALUES the
    operator configured. Never an implicit fallback -- omitted,
    behavior is bit-for-bit unchanged.

    FILTERING SEMANTICS: Corpus Analyst Pass #1 output is loaded/reused
    exactly as it would be without --story-id (bootstrapped or live,
    unfiltered, and persisted in full). Only the set of candidate_
    stories handed to the orchestrator for Evidence Analyst/Pass #2
    processing is narrowed to the single matching story -- via a
    filtering wrapper around whatever call_corpus_analyst would
    otherwise be used (the bootstrap substitution above, or the real
    live caller), applied AFTER that callable returns its (unfiltered)
    raw result but BEFORE run_intelligence_cycle ever sees the
    candidate_stories list. No other story's Evidence Analyst or Pass #2
    is ever invoked; their already-persisted corpus_analyst rows (e.g.
    from a --corpus-analysis-artifact bootstrap covering the whole
    corpus) are left untouched.

    FAIL CLOSED: whenever Corpus Analyst output is already known without
    a call -- a --corpus-analysis-artifact bootstrapped THIS invocation,
    or corpus_analyst rows already persisted at db_path from a prior
    invocation of the same run_id -- an unknown story_id raises
    UnknownStoryIdError immediately, before the Evidence Analyst
    bootstrap, before --dry-run, before the API-key check, before
    anything else: zero API calls, nothing additional persisted. (If
    Corpus Analyst output genuinely is NOT yet known -- a brand-new
    run_id/db_path with no bootstrap -- this cannot be validated before
    the Corpus Analyst call itself, which is unavoidable cycle-level
    overhead independent of story_id; the SAME filtering wrapper still
    validates it immediately afterward, before any Evidence Analyst/
    Pass #2/Editor call is even considered.)

    STOPPING BEHAVIOR: the Intelligence Editor is NEVER invoked for a
    targeted partial-story run, regardless of whether story_id's own
    Evidence Analyst/Pass #2 stages were reused or newly called --
    enforced via a dedicated CircuitBreaker instance constructed
    already-triggered (same consecutive_failure_threshold, never the
    caller's own `circuit_breaker` object) so that run_intelligence_
    cycle's EXISTING, unmodified "stopped_reason -> skip Editor" path
    (regulus_orchestrator.py is never edited) always fires immediately
    after story_id's own processing, before reconstruct_editor_inputs
    is ever consulted. Separately, whenever Corpus Analyst output is
    already known (the bootstrapped case, where its OWN budget
    consumption is deterministically exactly one unit -- see the
    corpus-bootstrap note above), this invocation's OWN run-budget
    ceiling (max_total_llm_calls) is tightened to the EXACT number of
    stages for story_id that are not yet reusable, plus that one
    deterministic corpus unit -- never loosened, and the caller's own
    configured budget (default or explicit) still applies in full
    alongside it, via min(). This is what makes the dry-run report's
    "maximum NEW external LLM requests" figure an ENFORCED ceiling on
    the live run, not merely descriptive output: Pass #2 (if not
    already persisted) gets exactly one real attempt, never its own
    internal retry, once this invocation's tightened budget is spent.
    max_evidence_analyst_calls/max_evidence_attempts_per_story are never
    altered by this feature.
    """
    budget = budget if budget is not None else orch.RunBudget(
        max_total_llm_calls=DEFAULT_MAX_TOTAL_LLM_CALLS,
        max_evidence_analyst_calls=DEFAULT_MAX_EVIDENCE_ANALYST_CALLS,
        max_evidence_attempts_per_story=DEFAULT_MAX_EVIDENCE_ATTEMPTS_PER_STORY,
    )
    circuit_breaker = circuit_breaker if circuit_breaker is not None else orch.CircuitBreaker(
        consecutive_failure_threshold=DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
    )

    os.makedirs(output_dir, exist_ok=True)
    if db_path is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        db_path = os.path.join(output_dir, f"regulus_intelligence_acceptance_{run_id}_{timestamp}.db")

    corpus, fixture_reporting_period = load_golden_corpus()
    reporting_period = reporting_period or fixture_reporting_period

    bootstrapped_artifact_raw = None
    if corpus_analysis_artifact_path is not None:
        # Validate BEFORE persisting anything and BEFORE any API call --
        # an invalid/mismatched artifact raises here and nothing below runs.
        bootstrapped_artifact_raw = load_and_validate_corpus_analysis_artifact(
            corpus_analysis_artifact_path, corpus,
        )
        persisted = bootstrap_corpus_analysis_artifact(run_id, bootstrapped_artifact_raw, db_path=db_path)
        print(f"Bootstrapped Corpus Analyst Pass #1: {len(persisted)} candidate stories loaded from "
              f"{corpus_analysis_artifact_path!r}, validated, and persisted for run_id={run_id!r} "
              f"at {db_path!r} (corpus_analyst stage) -- no API call made.")

    # --story-id: fail closed NOW (zero API calls) whenever Corpus
    # Analyst output is already known -- either just bootstrapped above,
    # or already persisted at db_path from a prior invocation of this
    # same run_id. If it genuinely is not known yet, this is deferred to
    # the live-run filtering wrapper below (after the unavoidable Corpus
    # Analyst call, before any Evidence Analyst/Pass #2/Editor call).
    if story_id is not None:
        known_candidate_stories = _known_candidate_stories(run_id, db_path)
        if known_candidate_stories is not None:
            _require_known_story_id(known_candidate_stories, story_id)

    if evidence_analysis_artifact_path is not None:
        # Validate BEFORE persisting anything and BEFORE any API call --
        # an invalid/incompatible artifact raises here and nothing below runs.
        evidence_story_id, evidence_raw, evidence_fingerprint, evidence_provenance = (
            load_and_validate_evidence_analysis_artifact(
                evidence_analysis_artifact_path, run_id, corpus=corpus, db_path=db_path,
            )
        )
        bootstrap_evidence_analysis_artifact(
            run_id, evidence_story_id, evidence_raw, evidence_fingerprint,
            db_path=db_path, provenance=evidence_provenance,
        )
        print(f"Bootstrapped Evidence Analyst artifact for story_id={evidence_story_id!r} from "
              f"{evidence_analysis_artifact_path!r}, validated against the current candidate_story, "
              f"and persisted for run_id={run_id!r} at {db_path!r} (evidence_analyst stage, "
              f"provenance=legacy_artifact_migration) -- no API call made.")

    if dry_run:
        return _dry_run_report(run_id, db_path=db_path, budget=budget, circuit_breaker=circuit_breaker,
                                story_id=story_id, memory_sources=memory_sources)

    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is required to run the live Regulus Intelligence Brief #001 "
            "acceptance run (set the environment variable or pass api_key). This check runs "
            "BEFORE any network call -- nothing is contacted if this raises."
        )

    _print_budget_plan(budget, circuit_breaker, db_path=db_path)

    started_at = datetime.now(timezone.utc).isoformat()
    # Every call_* parameter is left at its default (None) -- each stage
    # uses its OWN real live caller -- UNLESS a bootstrap artifact was
    # supplied, in which case call_corpus_analyst is substituted with a
    # callable that returns the already-validated artifact verbatim,
    # making no request. No stub for any other stage; no hardcoded
    # expectation anywhere.
    call_corpus_analyst_override = None
    if bootstrapped_artifact_raw is not None:
        def call_corpus_analyst_override(corpus_payload, api_key):  # noqa: ARG001 -- signature match
            return bootstrapped_artifact_raw

    # --story-id targeted execution (live run). See this function's own
    # docstring for the full filtering/fail-closed/stopping semantics,
    # and build_targeted_call_corpus_analyst/build_targeted_circuit_
    # breaker/compute_targeted_budget's own docstrings for exactly what
    # each piece does. These are also independently tested against
    # orch.run_intelligence_cycle directly, with injected stage stubs --
    # see tests/test_regulus_orchestrator.py.
    effective_budget = budget
    effective_circuit_breaker = circuit_breaker
    call_corpus_analyst_final = call_corpus_analyst_override
    targeted_new_call_cap = None
    if story_id is not None:
        underlying_corpus_caller = call_corpus_analyst_override or corpus_analyst.call_anthropic_corpus_analyst
        call_corpus_analyst_final = build_targeted_call_corpus_analyst(underlying_corpus_caller, story_id)

        # Tighten this invocation's OWN max_total_llm_calls ceiling --
        # only when Corpus Analyst output is already known (bootstrapped
        # THIS invocation), the one case its own budget consumption is
        # deterministically exactly one unit (see the corpus-bootstrap
        # docstring note).
        if bootstrapped_artifact_raw is not None:
            known_candidate_stories = bootstrapped_artifact_raw.get("candidate_stories") or []
            target_candidate_story = _require_known_story_id(known_candidate_stories, story_id)
            conn = store.get_connection(db_path)
            try:
                reuse_state = _resolve_story_reuse_state(run_id, target_candidate_story, conn=conn,
                                                         memory_sources=memory_sources, corpus=corpus)
            finally:
                conn.close()
            targeted_new_call_cap = reuse_state["new_calls_needed"]
            effective_budget = compute_targeted_budget(budget, targeted_new_call_cap)
            print(f"[--story-id] selected story_id={story_id!r} only -- maximum NEW external LLM "
                  f"requests for this invocation: {targeted_new_call_cap} (this invocation's own "
                  f"max_total_llm_calls ceiling tightened to {effective_budget.max_total_llm_calls}: "
                  f"{targeted_new_call_cap} plus 1 reserved for the already-bootstrapped Corpus "
                  f"Analyst stage, per the existing budget-accounting convention -- never looser "
                  f"than the configured {budget.max_total_llm_calls!r})")

        effective_circuit_breaker = build_targeted_circuit_breaker(circuit_breaker)

    outcome = orch.run_intelligence_cycle(
        run_id, corpus, reporting_period, api_key=api_key, db_path=db_path,
        budget=effective_budget, circuit_breaker=effective_circuit_breaker,
        call_corpus_analyst=call_corpus_analyst_final, memory_sources=memory_sources,
    )
    finished_at = datetime.now(timezone.utc).isoformat()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(output_dir, f"brief001_acceptance_{run_id}_{timestamp}.json")
    diagnostic = _outcome_to_diagnostic_dict(
        run_id, outcome, started_at=started_at, finished_at=finished_at, db_path=db_path,
    )
    if story_id is not None:
        diagnostic["targeted_story_id"] = story_id
        diagnostic["editor_skipped_reason"] = "targeted_partial_story_run"
        if targeted_new_call_cap is not None:
            diagnostic["max_new_external_llm_requests"] = targeted_new_call_cap
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(diagnostic, f, indent=2)

    _print_summary(outcome, out_path)
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="Live, end-to-end REGULUS INTELLIGENCE BRIEF #001 acceptance runner. "
                    "Makes multiple real, paid Anthropic API calls -- run only when explicitly authorized."
    )
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID, help=f"run_id (default: {DEFAULT_RUN_ID})")
    parser.add_argument("--reporting-period-start", default=None,
                         help="override the corpus fixture's own reporting_period.start")
    parser.add_argument("--reporting-period-end", default=None,
                         help="override the corpus fixture's own reporting_period.end")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                         help="directory to write the SQLite store and diagnostic JSON artifact into")
    parser.add_argument("--db-path", default=None,
                         help="use this SQLite file instead of a fresh timestamped one -- required to "
                              "actually resume/reuse a PRIOR run of the same --run-id; the default "
                              "(omitted) always starts a brand-new db file, so resume has nothing to "
                              "reuse unless this points at the previous run's db file")
    parser.add_argument("--max-total-llm-calls", type=int, default=DEFAULT_MAX_TOTAL_LLM_CALLS,
                         help=f"run-level ceiling on total LLM calls initiated, across every stage "
                              f"(default: {DEFAULT_MAX_TOTAL_LLM_CALLS}, the conservative acceptance "
                              f"budget -- pass a higher value explicitly to allow more)")
    parser.add_argument("--max-evidence-analyst-calls", type=int, default=DEFAULT_MAX_EVIDENCE_ANALYST_CALLS,
                         help=f"ceiling on Evidence Analyst calls across the whole run "
                              f"(default: {DEFAULT_MAX_EVIDENCE_ANALYST_CALLS})")
    parser.add_argument("--max-evidence-attempts-per-story", type=int,
                         default=DEFAULT_MAX_EVIDENCE_ATTEMPTS_PER_STORY,
                         help=f"ceiling on Evidence Analyst call attempts for any ONE story "
                              f"(default: {DEFAULT_MAX_EVIDENCE_ATTEMPTS_PER_STORY})")
    parser.add_argument("--circuit-breaker-threshold", type=int, default=DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
                         help=f"number of consecutive, identical-failure-class Evidence Analyst "
                              f"failures across different stories that stops all further Evidence "
                              f"Analyst calls for the run (default: {DEFAULT_CIRCUIT_BREAKER_THRESHOLD})")
    parser.add_argument("--dry-run", action="store_true",
                         help="report what a real run would call or reuse, and the maximum possible "
                              "call volume under the configured budget -- makes ZERO API requests and "
                              "does not execute the cycle")
    parser.add_argument("--corpus-analysis-artifact", default=None,
                         help="path to a previously-validated Corpus Analyst Pass #1 output (e.g. "
                              "tests/fixtures/corpus_analyst_acceptance_3.json) to bootstrap into the "
                              "intelligence store for --run-id instead of making a live Corpus Analyst "
                              "call. Validated against the current corpus fixture's document numbers "
                              "before being accepted (fails closed, before any API call, on an invalid "
                              "or mismatched artifact). Never an implicit fallback -- only used when "
                              "this flag is passed explicitly.")
    parser.add_argument("--evidence-analysis-artifact", default=None,
                         help="path to a previously-validated Evidence Analyst diagnostic artifact "
                              "(e.g. evidence_acceptance_runs/evidence_analyst_acceptance_CS-01_"
                              "<timestamp>.json) to bootstrap into the intelligence store for --run-id "
                              "instead of making a live Evidence Analyst call for that story. "
                              "Re-validated against the candidate_story currently persisted for its "
                              "story_id under --run-id (which must already exist -- e.g. via "
                              "--corpus-analysis-artifact) -- fails closed, before any API call, on a "
                              "story-id mismatch, a content/fingerprint mismatch, or a malformed/"
                              "invalid artifact. Never an implicit fallback -- only used when this flag "
                              "is passed explicitly.")
    parser.add_argument("--story-id", default=None,
                         help="execution filter: process ONLY this one candidate_story downstream "
                              "of Corpus Analyst Pass #1 (Evidence Analyst reuse/call, then Pass #2) "
                              "-- no other story's Evidence Analyst or Pass #2 is ever invoked, and "
                              "the Intelligence Editor is never run for this targeted partial-story "
                              "invocation. Changes nothing about intelligence logic, prompts, "
                              "schemas, persistence semantics, fingerprints, provenance, or the "
                              "configured budget VALUES -- see run_acceptance()'s docstring for the "
                              "exact filtering/fail-closed/stopping semantics. Fails closed (before "
                              "any API call) on a story_id with no matching candidate_story, whenever "
                              "Corpus Analyst output is already known (e.g. via "
                              "--corpus-analysis-artifact). Never an implicit fallback -- omitted, "
                              "behavior is unchanged.")
    parser.add_argument("--memory-db", action="append", default=[],
                         help="path to a PRIOR intelligence store to consult (read-only) for reusable, "
                              "valid, fingerprint-matching Evidence Analyst / Pass #2 artifacts before "
                              "making a new call. Repeatable; paired by position with --memory-run-id; "
                              "searched in the order given, after the current run. Never modified.")
    parser.add_argument("--memory-run-id", action="append", default=[],
                         help="run_id inside the matching --memory-db. Repeatable.")
    args = parser.parse_args()
    if len(args.memory_db) != len(args.memory_run_id):
        parser.error("--memory-db and --memory-run-id must be supplied the same number of times")
    memory_sources = list(zip(args.memory_db, args.memory_run_id)) or None

    reporting_period = None
    if args.reporting_period_start or args.reporting_period_end:
        _, fixture_reporting_period = load_golden_corpus()
        reporting_period = {
            "start": args.reporting_period_start or fixture_reporting_period["start"],
            "end": args.reporting_period_end or fixture_reporting_period["end"],
        }

    budget = orch.RunBudget(
        max_total_llm_calls=args.max_total_llm_calls,
        max_evidence_analyst_calls=args.max_evidence_analyst_calls,
        max_evidence_attempts_per_story=args.max_evidence_attempts_per_story,
    )
    circuit_breaker = orch.CircuitBreaker(consecutive_failure_threshold=args.circuit_breaker_threshold)

    run_acceptance(
        args.run_id, reporting_period=reporting_period, output_dir=args.output_dir,
        db_path=args.db_path, budget=budget, circuit_breaker=circuit_breaker, dry_run=args.dry_run,
        corpus_analysis_artifact_path=args.corpus_analysis_artifact,
        evidence_analysis_artifact_path=args.evidence_analysis_artifact,
        story_id=args.story_id, memory_sources=memory_sources,
    )


if __name__ == "__main__":
    main()
