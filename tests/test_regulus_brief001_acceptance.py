#!/usr/bin/env python3
"""
Tests for regulus_brief001_acceptance.py's reliability/cost-control
surface added in the budget/resume/circuit-breaker patch:
  - --dry-run makes ZERO network calls and reports the configured budget,
    resumable/reusable state (when any is persisted), and a worst-case
    call-volume bound.
  - run_acceptance() defaults to the CONSERVATIVE acceptance budget (not
    unlimited) when budget/circuit_breaker are not explicitly supplied.
  - the budget plan is printed before any execution.

requests.post is monkeypatched to raise if ever actually invoked, as an
additional, stronger guarantee beyond "dry_run never calls
run_intelligence_cycle".

Run: python3 tests/test_regulus_brief001_acceptance.py
"""
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


import requests as _requests_module

_real_requests_post = _requests_module.post
_network_call_attempted = {"flag": False}


def _guard_requests_post(*args, **kwargs):
    _network_call_attempted["flag"] = True
    raise AssertionError(
        "test_regulus_brief001_acceptance.py attempted a REAL network POST -- "
        "dry-run (and this test file) must never make one"
    )


_requests_module.post = _guard_requests_post

import regulus_brief001_acceptance as acc
import regulus_orchestrator as orch
import intelligence_store as store

_TMPDIR = tempfile.mkdtemp(prefix="regulus_acceptance_test_")


def _fresh_dir(name):
    path = os.path.join(_TMPDIR, name)
    os.makedirs(path, exist_ok=True)
    return path


# ===========================================================================
# A. --dry-run with no prior persisted state: makes zero calls, reports the
# configured budget, and reports the Corpus-Analyst-only worst case since
# candidate stories are genuinely unknown without a live call.
# ===========================================================================
out_dir = _fresh_dir("a")
report = acc.run_acceptance("dryrun-a", output_dir=out_dir, dry_run=True)
check("A1. dry_run returns a dict report (not a file path, since nothing was executed)",
      isinstance(report, dict))
check("A2. report includes the configured budget summary",
      report["budget"]["max_total_llm_calls"] == acc.DEFAULT_MAX_TOTAL_LLM_CALLS
      and report["budget"]["max_evidence_analyst_calls"] == acc.DEFAULT_MAX_EVIDENCE_ANALYST_CALLS
      and report["budget"]["max_evidence_attempts_per_story"] == acc.DEFAULT_MAX_EVIDENCE_ATTEMPTS_PER_STORY)
check("A3. with no persisted corpus_analyst artifact, corpus_analyst.resolved is False",
      report["corpus_analyst"]["resolved"] is False)
check("A4. worst-case call volume with corpus unresolved is exactly CORPUS_ANALYST_MAX_ATTEMPTS",
      report["max_possible_calls_worst_case"] == acc.corpus_analyst.CORPUS_ANALYST_MAX_ATTEMPTS)
check("A5. no story-level entries are reported (nothing is knowable yet)",
      report["stories"] == [])
check("A6. a db file is NOT created by a dry run with no prior state "
      "(dry-run never calls store.get_connection unless a db file already exists)",
      not os.path.exists(report["db_path"]))

# ===========================================================================
# B. --dry-run with persisted, reusable prior state (simulating a resumed
# run): reports per-story reuse eligibility with zero calls, and a tighter
# worst-case bound than a cold start.
# ===========================================================================
out_dir = _fresh_dir("b")
db_path = os.path.join(out_dir, "resume.db")
acc3_path = os.path.join(acc.FIXTURES_DIR, "corpus_analyst_acceptance_3.json")
with open(acc3_path, "r", encoding="utf-8") as f:
    acc3 = json.load(f)
story0 = acc3["candidate_stories"][0]
story0_id = story0["story_id"]

conn = store.get_connection(db_path)
store.save_story_artifact("dryrun-b", story0_id, "corpus_analyst", story0, is_valid=True, conn=conn)
fp = store.compute_fingerprint(story0)
store.save_story_artifact("dryrun-b", story0_id, "evidence_analyst", {"story_id": story0_id, "synthetic": True},
                           is_valid=True, input_fingerprint=fp, conn=conn)
conn.close()

report = acc.run_acceptance("dryrun-b", output_dir=out_dir, db_path=db_path, dry_run=True)
check("B1. corpus_analyst.resolved is True once a persisted artifact exists",
      report["corpus_analyst"]["resolved"] is True
      and report["corpus_analyst"]["candidate_story_count"] == 1)
story_reports = {s["story_id"]: s for s in report["stories"]}
check("B2. the story with a valid, fingerprint-matching persisted Evidence artifact reports "
      "evidence_analyst='would_reuse' (no call needed)",
      story_reports[story0_id]["evidence_analyst"] == "would_reuse")
check("B3. that same story's Pass #2 (never persisted) reports pass2='would_call'",
      story_reports[story0_id]["pass2"] == "would_call")
check("B4. worst-case call volume accounts for only Pass #2 + Editor attempts "
      "(Evidence Analyst is reused, so excluded from the worst case)",
      report["max_possible_calls_worst_case"] == acc.pass2.PASS2_MAX_ATTEMPTS + acc.editor.EDITOR_MAX_ATTEMPTS)
check("B5. would_call_editor is True (at least one story has evidence)",
      report["would_call_editor"] is True)

# ===========================================================================
# C. run_acceptance() defaults to the CONSERVATIVE acceptance budget (never
# unlimited) when budget/circuit_breaker are not supplied -- verified via
# the dry-run report, which reflects exactly what a live run would use.
# ===========================================================================
out_dir = _fresh_dir("c")
report = acc.run_acceptance("dryrun-c", output_dir=out_dir, dry_run=True)
check("C1. omitting budget/circuit_breaker entirely still yields the conservative defaults, "
      "never an unbounded (None) run budget",
      report["budget"]["max_total_llm_calls"] is not None
      and report["budget"]["max_evidence_analyst_calls"] is not None
      and report["budget"]["max_evidence_attempts_per_story"] is not None)
check("C2. the circuit breaker threshold defaults to DEFAULT_CIRCUIT_BREAKER_THRESHOLD",
      report["circuit_breaker_consecutive_threshold"] == acc.DEFAULT_CIRCUIT_BREAKER_THRESHOLD)

# A caller CAN explicitly raise the budget above the conservative default --
# proving the default is a default, not a hard ceiling baked into the code.
out_dir = _fresh_dir("c2")
custom_budget = orch.RunBudget(max_total_llm_calls=999, max_evidence_analyst_calls=999,
                                max_evidence_attempts_per_story=999)
report = acc.run_acceptance("dryrun-c2", output_dir=out_dir, dry_run=True, budget=custom_budget)
check("C3. an explicitly-supplied budget overrides the conservative default",
      report["budget"]["max_total_llm_calls"] == 999)

# ===========================================================================
# D. ANTHROPIC_API_KEY is never required for a dry run (it makes no call) --
# the live path still requires it (unchanged pre-existing behavior), but
# dry-run must work with no key configured at all.
# ===========================================================================
_had_key = os.environ.pop("ANTHROPIC_API_KEY", None)
try:
    out_dir = _fresh_dir("d")
    report = acc.run_acceptance("dryrun-d", output_dir=out_dir, dry_run=True, api_key=None)
    check("D1. dry-run succeeds with no ANTHROPIC_API_KEY configured at all", isinstance(report, dict))
finally:
    if _had_key is not None:
        os.environ["ANTHROPIC_API_KEY"] = _had_key

# ===========================================================================
# F. --corpus-analysis-artifact bootstrap: validate + persist an already-
# accepted Corpus Analyst Pass #1 output, with ZERO API calls, so a
# dry-run (and later a live run) never rediscovers a frozen result.
# ===========================================================================
ACC3_CORPUS_ANALYST_FIXTURE = os.path.join(acc.FIXTURES_DIR, "corpus_analyst_acceptance_3.json")
with open(ACC3_CORPUS_ANALYST_FIXTURE, "r", encoding="utf-8") as f:
    ACC3_CORPUS_ANALYST_RAW = json.load(f)
ACC3_STORY_IDS = [s["story_id"] for s in ACC3_CORPUS_ANALYST_RAW["candidate_stories"]]

# F1-F3: the real frozen fixture, loaded fresh, validates and bootstraps
# cleanly, and the dry-run report reflects it -- using the EXACT command
# the user asked for: --corpus-analysis-artifact tests/fixtures/
# corpus_analyst_acceptance_3.json against run_id=brief001-acceptance-2.
out_dir = _fresh_dir("f")
report = acc.run_acceptance(
    "brief001-acceptance-2", output_dir=out_dir, dry_run=True,
    corpus_analysis_artifact_path=ACC3_CORPUS_ANALYST_FIXTURE,
)
check("F1. bootstrapping the real frozen Acceptance #3 Corpus Analyst artifact succeeds "
      "and the dry-run report resolves corpus_analyst from it (no 'unknown without a live call')",
      report["corpus_analyst"]["resolved"] is True
      and report["corpus_analyst"]["candidate_story_count"] == len(ACC3_STORY_IDS))
check("F2. every bootstrapped story appears in the dry-run report, each needing a live call "
      "for Evidence Analyst (nothing was persisted for Evidence/Pass #2 yet)",
      {s["story_id"] for s in report["stories"]} == set(ACC3_STORY_IDS)
      and all(s["evidence_analyst"] == "would_call" for s in report["stories"]))
check("F3. the max calls actually initiable is capped at the configured budget "
      "(max_total_llm_calls), not the much larger uncapped worst case",
      report["max_possible_calls_actual"] == report["budget"]["max_total_llm_calls"]
      and report["max_possible_calls_worst_case"] > report["max_possible_calls_actual"])

# F4: the artifact is actually persisted into the store (not just read into
# memory for the report) -- a second, independent dry-run against the SAME
# db_path/run_id (without re-passing --corpus-analysis-artifact) still sees
# the bootstrapped candidate stories.
db_path_f = report["db_path"]
report_again = acc.run_acceptance("brief001-acceptance-2", output_dir=out_dir, db_path=db_path_f, dry_run=True)
check("F4. the bootstrapped corpus_analyst state persists across a SEPARATE dry-run "
      "invocation against the same db_path/run_id, with no artifact flag needed the second time",
      report_again["corpus_analyst"]["resolved"] is True
      and report_again["corpus_analyst"]["candidate_story_count"] == len(ACC3_STORY_IDS))

# F5: each persisted corpus_analyst row carries a fingerprint (required for
# safe resume semantics) -- verified directly against the store.
conn_f = store.get_connection(db_path_f)
persisted_f = store.list_story_artifacts("brief001-acceptance-2", stage="corpus_analyst", conn=conn_f)
conn_f.close()
check("F5. every persisted corpus_analyst artifact carries a non-None input_fingerprint",
      len(persisted_f) == len(ACC3_STORY_IDS) and all(r.input_fingerprint is not None for r in persisted_f))

# F6: omitting --corpus-analysis-artifact is a complete no-op with respect
# to this capability -- never an implicit fallback.
out_dir = _fresh_dir("f6")
report = acc.run_acceptance("dryrun-f6", output_dir=out_dir, dry_run=True)
check("F6. omitting --corpus-analysis-artifact entirely leaves corpus_analyst unresolved, "
      "exactly as before this capability existed -- it is never an implicit fallback",
      report["corpus_analyst"]["resolved"] is False)

# F7: a nonexistent artifact path fails closed with a clear, typed error --
# before any persistence and before any API call -- rather than silently
# falling through to a live call or crashing with an unrelated exception.
out_dir = _fresh_dir("f7")
raised = None
try:
    acc.run_acceptance("dryrun-f7", output_dir=out_dir, dry_run=True,
                        corpus_analysis_artifact_path="/tmp/this_file_does_not_exist_regulus.json")
except acc.CorpusAnalysisArtifactInvalidError as e:
    raised = e
check("F7. a nonexistent --corpus-analysis-artifact path raises CorpusAnalysisArtifactInvalidError, "
      "fails closed before any persistence or API call", raised is not None)
check("F7b. nothing is written to disk for the nonexistent-artifact case",
      not os.listdir(out_dir))

# F8: an artifact that references a document number NOT present in the
# current corpus (i.e. produced against a different corpus -- the
# "mismatched artifact" case) fails closed via schema validation, never
# silently accepted.
out_dir = _fresh_dir("f8")
mismatched_raw = {
    "reporting_period": {"start": "2026-09-14", "end": "2026-10-05"},
    "corpus_assessment": "synthetic",
    "candidate_stories": [{
        "story_id": "CS-MISMATCHED",
        "headline": "synthetic",
        "preliminary_hypothesis": "synthetic",
        "supporting_document_numbers": ["2099-99999999"],  # not in the real corpus fixture
        "research_questions": ["synthetic question"],
        "alternative_hypotheses": [],
    }],
    "potential_administrative_activity": [],
    "unclustered_observations_of_interest": [],
    "corpus_level_gaps": [],
}
mismatched_path = os.path.join(out_dir, "mismatched_corpus_analyst.json")
with open(mismatched_path, "w", encoding="utf-8") as f:
    json.dump(mismatched_raw, f)

raised = None
try:
    acc.run_acceptance("dryrun-f8", output_dir=out_dir, dry_run=True,
                        corpus_analysis_artifact_path=mismatched_path)
except acc.CorpusAnalysisArtifactInvalidError as e:
    raised = e
check("F8. an artifact referencing a document number outside the current corpus "
      "(a mismatched artifact) raises CorpusAnalysisArtifactInvalidError, fails closed",
      raised is not None)
# Nothing should have been persisted for this run_id as a result of the failed bootstrap.
db_candidates_f8 = [p for p in os.listdir(out_dir) if p.endswith(".db")]
check("F8b. no db file is created/populated for the mismatched-artifact case "
      "(validation happens before any persistence)",
      db_candidates_f8 == [])

# F9: a syntactically-invalid (not valid JSON) artifact file also fails
# closed with the same typed error, not a raw JSONDecodeError leaking out.
out_dir = _fresh_dir("f9")
bad_json_path = os.path.join(out_dir, "not_json.json")
with open(bad_json_path, "w", encoding="utf-8") as f:
    f.write("{not valid json")
raised = None
try:
    acc.run_acceptance("dryrun-f9", output_dir=out_dir, dry_run=True,
                        corpus_analysis_artifact_path=bad_json_path)
except acc.CorpusAnalysisArtifactInvalidError as e:
    raised = e
check("F9. an unparseable (non-JSON) artifact file raises CorpusAnalysisArtifactInvalidError "
      "(not a raw json.JSONDecodeError)", raised is not None)

# F10: load_and_validate_corpus_analysis_artifact() uses the SAME schema
# contract corpus_analyst.run_corpus_analysis() itself uses -- proven by
# calling corpus_analyst_schema.validate_corpus_analysis() directly over
# the same fixture and the same document-number set, and getting the same
# verdict as the function under test.
corpus_f10, _ = acc.load_golden_corpus()
valid_doc_numbers_f10 = {o.document_number for o in corpus_f10.observations if o.document_number is not None}
direct_result = acc.corpus_analyst_schema.validate_corpus_analysis(ACC3_CORPUS_ANALYST_RAW, valid_doc_numbers_f10)
check("F10. the bootstrap validator agrees with a direct call to corpus_analyst_schema."
      "validate_corpus_analysis() over the same artifact/corpus -- no separate, looser check",
      direct_result.is_valid is True)

# ===========================================================================
# G. --evidence-analysis-artifact bootstrap: validate + persist an
# already-successful Evidence Analyst result for ONE story, with ZERO API
# calls, so a dry-run (and later a live run) never re-pays for a story
# that already has a validated Evidence Analyst outcome.
# ===========================================================================
EVIDENCE_CS01_FIXTURE = os.path.join(
    acc.FIXTURES_DIR, "evidence_analyst_acceptance_CS-01_synthetic.json",
)
with open(EVIDENCE_CS01_FIXTURE, "r", encoding="utf-8") as f:
    EVIDENCE_CS01_RAW_ARTIFACT = json.load(f)


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    return path


# G1-G3: the full, realistic flow -- bootstrap Corpus Analyst first (as a
# prerequisite, exactly like the real --corpus-analysis-artifact flow),
# then bootstrap the CS-01 Evidence artifact on top of it, using the
# EXACT command shape the user asked for.
out_dir = _fresh_dir("g")
report = acc.run_acceptance(
    "brief001-acceptance-2", output_dir=out_dir, dry_run=True,
    corpus_analysis_artifact_path=ACC3_CORPUS_ANALYST_FIXTURE,
    evidence_analysis_artifact_path=EVIDENCE_CS01_FIXTURE,
)
story_reports_g = {s["story_id"]: s for s in report["stories"]}
check("G1. after bootstrapping both Corpus Analyst and the CS-01 Evidence artifact, "
      "CS-01 reports evidence_analyst='would_reuse' (not would_call)",
      story_reports_g["CS-01"]["evidence_analyst"] == "would_reuse")
check("G2. CS-01's Pass #2 is reported according to its own (unset) persisted state -- "
      "'would_call', since no Pass #2 artifact was bootstrapped",
      story_reports_g["CS-01"]["pass2"] == "would_call")
check("G3. every OTHER story is unaffected -- still 'would_call' for Evidence Analyst, "
      "exactly as a corpus-only bootstrap would report",
      all(story_reports_g[sid]["evidence_analyst"] == "would_call"
          for sid in ACC3_STORY_IDS if sid != "CS-01"))

db_path_g = report["db_path"]

# G4: the persisted Evidence artifact is reusable under the orchestrator's
# OWN existing lookup (find_reusable_story_artifact), not a parallel/
# bespoke reuse path -- proven by calling it directly with the fingerprint
# the orchestrator itself would compute for CS-01's candidate_story.
conn_g = store.get_connection(db_path_g)
cs01_candidate_story = store.load_story_artifact("brief001-acceptance-2", "CS-01", "corpus_analyst", conn=conn_g).payload
expected_fingerprint_g = store.compute_fingerprint(cs01_candidate_story)
reusable_g = store.find_reusable_story_artifact(
    "brief001-acceptance-2", "CS-01", "evidence_analyst", expected_fingerprint_g, conn=conn_g,
)
conn_g.close()
check("G5. intelligence_store.find_reusable_story_artifact() -- the orchestrator's own "
      "existing lookup, not a bespoke check -- finds the bootstrapped artifact reusable",
      reusable_g is not None)

# G6: the persisted content is BIT-FOR-BIT the artifact's own outcome.raw --
# bootstrapping never alters substantive Evidence Analyst content.
check("G6. the persisted Evidence Analyst payload is byte-for-byte identical to the "
      "artifact file's own outcome.raw -- no substantive content alteration",
      reusable_g.payload == EVIDENCE_CS01_RAW_ARTIFACT["outcome"]["raw"])

# G7: omitting --evidence-analysis-artifact entirely preserves the exact
# prior (corpus-only) behavior -- never an implicit fallback.
out_dir = _fresh_dir("g7")
report_g7 = acc.run_acceptance(
    "dryrun-g7", output_dir=out_dir, dry_run=True,
    corpus_analysis_artifact_path=ACC3_CORPUS_ANALYST_FIXTURE,
)
check("G7. omitting --evidence-analysis-artifact leaves EVERY story at "
      "evidence_analyst='would_call' -- identical to corpus-only bootstrap behavior",
      all(s["evidence_analyst"] == "would_call" for s in report_g7["stories"]))

# G8: STORY MISMATCH fails closed -- an artifact whose own run_metadata.
# story_id disagrees with its outcome.raw.story_id is internally
# inconsistent and must never be accepted on story_id (or any single
# field) alone.
out_dir = _fresh_dir("g8")
inconsistent = json.loads(json.dumps(EVIDENCE_CS01_RAW_ARTIFACT))  # deep copy
inconsistent["run_metadata"]["story_id"] = "CS-02"  # disagrees with outcome.raw.story_id == "CS-01"
inconsistent_path = _write_json(os.path.join(out_dir, "inconsistent.json"), inconsistent)
raised = None
try:
    acc.run_acceptance(
        "dryrun-g8", output_dir=out_dir, dry_run=True,
        corpus_analysis_artifact_path=ACC3_CORPUS_ANALYST_FIXTURE,
        evidence_analysis_artifact_path=inconsistent_path,
    )
except acc.EvidenceAnalysisArtifactInvalidError as e:
    raised = e
check("G8. an artifact whose run_metadata.story_id disagrees with outcome.raw.story_id "
      "raises EvidenceAnalysisArtifactInvalidError (story_id alone never establishes "
      "compatibility -- here the two 'story_id' sources don't even agree with each other)",
      raised is not None)

# G9: a story_id with NO persisted/bootstrapped Corpus Analyst candidate_story
# under this run_id fails closed -- nothing to check compatibility against.
out_dir = _fresh_dir("g9")
no_corpus_story_id = json.loads(json.dumps(EVIDENCE_CS01_RAW_ARTIFACT))
no_corpus_story_id["run_metadata"]["story_id"] = "CS-99"
no_corpus_story_id["outcome"]["raw"]["story_id"] = "CS-99"
no_corpus_path = _write_json(os.path.join(out_dir, "no_corpus.json"), no_corpus_story_id)
raised = None
try:
    acc.run_acceptance(
        "dryrun-g9", output_dir=out_dir, dry_run=True,
        corpus_analysis_artifact_path=ACC3_CORPUS_ANALYST_FIXTURE,  # has no CS-99
        evidence_analysis_artifact_path=no_corpus_path,
    )
except acc.EvidenceAnalysisArtifactInvalidError as e:
    raised = e
check("G9. a story_id with no persisted, valid Corpus Analyst candidate_story under this "
      "run_id raises EvidenceAnalysisArtifactInvalidError, fails closed", raised is not None)

# G10: FINGERPRINT/CONTENT MISMATCH fails closed -- the artifact's
# question_findings were produced against a DIFFERENT version of CS-01's
# research_questions than what is currently persisted (simulating the
# candidate_story having been edited/replaced since the artifact was
# generated). The compatibility re-validation must catch this
# deterministically, via the same schema contract, not story_id alone.
out_dir = _fresh_dir("g10")
edited_corpus_analyst = json.loads(json.dumps(ACC3_CORPUS_ANALYST_RAW))
for story in edited_corpus_analyst["candidate_stories"]:
    if story["story_id"] == "CS-01":
        story["research_questions"] = ["A completely different, unrelated research question."]
edited_corpus_path = _write_json(os.path.join(out_dir, "edited_corpus_analyst.json"), edited_corpus_analyst)
raised = None
try:
    acc.run_acceptance(
        "dryrun-g10", output_dir=out_dir, dry_run=True,
        corpus_analysis_artifact_path=edited_corpus_path,
        evidence_analysis_artifact_path=EVIDENCE_CS01_FIXTURE,  # built against the ORIGINAL research_questions
    )
except acc.EvidenceAnalysisArtifactInvalidError as e:
    raised = e
check("G10. an Evidence artifact whose question_findings no longer correspond to the "
      "CURRENT candidate_story's research_questions (content changed since the artifact "
      "was produced) raises EvidenceAnalysisArtifactInvalidError -- a genuine content/"
      "fingerprint mismatch, not merely a story_id check", raised is not None)
db_files_g10 = [p for p in os.listdir(out_dir) if p.endswith(".db")]
check("G10b. no persisted evidence_analyst row for CS-01 exists after the failed, "
      "mismatched bootstrap (the Corpus Analyst bootstrap that preceded it is unaffected "
      "and legitimately persisted -- only the Evidence bootstrap failed closed)",
      len(db_files_g10) == 1
      and store.load_story_artifact("dryrun-g10", "CS-01", "evidence_analyst",
                                     db_path=os.path.join(out_dir, db_files_g10[0])) is None)

# G11: a malformed/invalid artifact (outcome.is_valid=False -- i.e. the
# live run itself recorded this as a FAILED attempt) fails closed and is
# never a bootstrap candidate.
out_dir = _fresh_dir("g11")
failed_artifact = json.loads(json.dumps(EVIDENCE_CS01_RAW_ARTIFACT))
failed_artifact["outcome"]["is_valid"] = False
failed_artifact["outcome"]["failure_reason"] = "simulated_prior_failure"
failed_path = _write_json(os.path.join(out_dir, "failed.json"), failed_artifact)
raised = None
try:
    acc.run_acceptance(
        "dryrun-g11", output_dir=out_dir, dry_run=True,
        corpus_analysis_artifact_path=ACC3_CORPUS_ANALYST_FIXTURE,
        evidence_analysis_artifact_path=failed_path,
    )
except acc.EvidenceAnalysisArtifactInvalidError as e:
    raised = e
check("G11. an artifact with outcome.is_valid=False raises EvidenceAnalysisArtifactInvalidError "
      "-- only a VALID live outcome may ever be bootstrapped", raised is not None)

# G12: an unparseable (non-JSON) Evidence artifact file also fails closed
# with the typed error, not a raw json.JSONDecodeError leaking out.
out_dir = _fresh_dir("g12")
bad_json_path_g = os.path.join(out_dir, "not_json.json")
with open(bad_json_path_g, "w", encoding="utf-8") as f:
    f.write("{not valid json")
raised = None
try:
    acc.run_acceptance(
        "dryrun-g12", output_dir=out_dir, dry_run=True,
        corpus_analysis_artifact_path=ACC3_CORPUS_ANALYST_FIXTURE,
        evidence_analysis_artifact_path=bad_json_path_g,
    )
except acc.EvidenceAnalysisArtifactInvalidError as e:
    raised = e
check("G12. an unparseable (non-JSON) Evidence artifact raises "
      "EvidenceAnalysisArtifactInvalidError (not a raw json.JSONDecodeError)",
      raised is not None)

# G13: a nonexistent Evidence artifact path fails closed.
out_dir = _fresh_dir("g13")
raised = None
try:
    acc.run_acceptance(
        "dryrun-g13", output_dir=out_dir, dry_run=True,
        corpus_analysis_artifact_path=ACC3_CORPUS_ANALYST_FIXTURE,
        evidence_analysis_artifact_path="/tmp/this_evidence_artifact_does_not_exist.json",
    )
except acc.EvidenceAnalysisArtifactInvalidError as e:
    raised = e
check("G13. a nonexistent --evidence-analysis-artifact path raises "
      "EvidenceAnalysisArtifactInvalidError, fails closed", raised is not None)

# ===========================================================================
# H. Legacy-artifact migration provenance: the CS-01 Evidence artifact
# bootstrapped above (G1-G6) predates input fingerprinting, so its
# compatibility with the current candidate_story was established by full
# re-validation, not by verifying a historical fingerprint. That must be
# explicit and auditable as store-level metadata, distinguishable from a
# normal, freshly-generated, fingerprint-verified artifact, WITHOUT
# changing reuse-eligibility semantics (still exactly is_valid +
# fingerprint equality) or the substantive Evidence Analyst payload.
# ===========================================================================

# H1: the persisted legacy-migrated record carries explicit, auditable
# provenance identifying it as a legacy_artifact_migration -- not merely
# "reusable" with no trace of HOW compatibility was established.
check("H1. the bootstrapped (legacy) CS-01 Evidence artifact's persisted "
      "record carries provenance marking it as a legacy_artifact_migration",
      isinstance(reusable_g.provenance, dict)
      and reusable_g.provenance.get("kind") == "legacy_artifact_migration")
check("H1b. the provenance explicitly records that NO historical input "
      "fingerprint was verified (none existed on the source artifact)",
      reusable_g.provenance.get("historical_input_fingerprint_verified") is False)
check("H1c. the provenance explicitly records HOW compatibility was "
      "established instead -- revalidation against the current candidate_story",
      reusable_g.provenance.get("compatibility_established_by")
      == "revalidation_against_current_candidate_story")
check("H1d. the provenance records the source artifact path it was migrated from",
      reusable_g.provenance.get("source_artifact_path") == EVIDENCE_CS01_FIXTURE)

# H2: the substantive Evidence Analyst payload is unaffected by adding
# provenance -- it is store-level metadata alongside the record, never
# injected into the frozen analytical content (reaffirms G6 explicitly in
# terms of the provenance feature).
check("H2. provenance metadata is NOT folded into the substantive Evidence "
      "Analyst payload -- the persisted payload remains byte-for-byte "
      "identical to the artifact file's own outcome.raw, with no added keys",
      reusable_g.payload == EVIDENCE_CS01_RAW_ARTIFACT["outcome"]["raw"]
      and set(reusable_g.payload.keys()) == set(EVIDENCE_CS01_RAW_ARTIFACT["outcome"]["raw"].keys()))

# H3: a NORMAL (non-legacy) artifact persisted without a provenance
# argument -- exactly how a real, live-generated Evidence Analyst result
# would be saved -- has provenance=None, explicitly distinguishing
# "legacy migration" from "normal fingerprint-verified" persistence.
out_dir_h3 = _fresh_dir("h3")
db_path_h3 = os.path.join(out_dir_h3, "normal.db")
conn_h3 = store.get_connection(db_path_h3)
store.save_story_artifact(
    "run-h3", "CS-NORMAL", "evidence_analyst", {"story_id": "CS-NORMAL", "question_findings": []},
    is_valid=True, input_fingerprint="some-fingerprint-value", conn=conn_h3,
)
normal_record_h3 = store.load_story_artifact("run-h3", "CS-NORMAL", "evidence_analyst", conn=conn_h3)
conn_h3.close()
check("H3. a normally-persisted (live-style) Evidence Analyst artifact saved with no "
      "provenance argument has provenance=None -- distinguishable from a legacy migration",
      normal_record_h3 is not None and normal_record_h3.provenance is None)

# H4: find_reusable_story_artifact()'s reuse decision is completely
# unaffected by provenance -- a legacy-migrated artifact (provenance set)
# and a normal artifact (provenance=None) are both reusable under the
# EXACT SAME rule (is_valid + exact fingerprint equality), proving
# provenance is purely informational and never a parallel reuse gate.
conn_h4 = store.get_connection(db_path_h3)
reusable_h4 = store.find_reusable_story_artifact(
    "run-h3", "CS-NORMAL", "evidence_analyst", "some-fingerprint-value", conn=conn_h4,
)
conn_h4.close()
check("H4. a normal (provenance=None) artifact is reusable under the identical "
      "is_valid + fingerprint-equality rule used for the legacy-migrated one -- "
      "provenance never special-cased by the reuse decision",
      reusable_h4 is not None and reusable_h4.provenance is None)
check("H4b. the legacy-migrated CS-01 artifact (provenance set) is ALSO reusable under "
      "that same unmodified rule -- both paths produce a reusable record identically",
      reusable_g is not None and reusable_g.provenance is not None)

# H5: later orchestrator reuse still requires the assigned EXACT CURRENT
# fingerprint -- migration grants no permanent/special exemption from
# fingerprint discipline. Re-bootstrap CS-01's Corpus Analyst candidate
# story under the SAME run_id with DIFFERENT content (simulating the
# input changing after the legacy migration), then confirm a fresh lookup
# using the newly-computed fingerprint no longer finds the stale
# migrated Evidence artifact reusable.
out_dir_h5 = _fresh_dir("h5")
report_h5 = acc.run_acceptance(
    "run-h5", output_dir=out_dir_h5, dry_run=True,
    corpus_analysis_artifact_path=ACC3_CORPUS_ANALYST_FIXTURE,
    evidence_analysis_artifact_path=EVIDENCE_CS01_FIXTURE,
)
db_path_h5 = report_h5["db_path"]
conn_h5 = store.get_connection(db_path_h5)
original_candidate_h5 = store.load_story_artifact("run-h5", "CS-01", "corpus_analyst", conn=conn_h5).payload
original_fingerprint_h5 = store.compute_fingerprint(original_candidate_h5)
reusable_before_h5 = store.find_reusable_story_artifact(
    "run-h5", "CS-01", "evidence_analyst", original_fingerprint_h5, conn=conn_h5,
)
check("H5a. before any post-migration change, the legacy-migrated CS-01 Evidence "
      "artifact is reusable under its assigned (current-at-migration-time) fingerprint",
      reusable_before_h5 is not None)

# Re-persist CS-01's candidate_story with different content under the
# SAME run_id/db_path, exactly simulating the input changing after
# migration (e.g. a re-run of Corpus Analyst Pass #1 producing a revised
# story).
changed_candidate_h5 = json.loads(json.dumps(original_candidate_h5))
changed_candidate_h5["research_questions"] = ["A deliberately different research question, post-migration."]
store.save_story_artifact(
    "run-h5", "CS-01", "corpus_analyst", changed_candidate_h5, is_valid=True, conn=conn_h5,
)
new_fingerprint_h5 = store.compute_fingerprint(changed_candidate_h5)
check("H5b. the input_fingerprint computed from the CHANGED candidate_story differs from "
      "the one assigned to the legacy-migrated Evidence artifact at migration time",
      new_fingerprint_h5 != original_fingerprint_h5)
reusable_after_h5 = store.find_reusable_story_artifact(
    "run-h5", "CS-01", "evidence_analyst", new_fingerprint_h5, conn=conn_h5,
)
check("H5c. after the candidate_story changes, a fresh reuse lookup using the NEW "
      "fingerprint correctly finds the stale legacy-migrated artifact NOT reusable -- "
      "migration grants no exemption from ordinary fingerprint discipline",
      reusable_after_h5 is None)
# The old artifact is still present and still carries its legacy
# provenance (migration does not silently invalidate/delete history) --
# it is simply no longer the fingerprint-matched candidate for reuse.
stale_record_h5 = store.load_story_artifact("run-h5", "CS-01", "evidence_analyst", conn=conn_h5)
check("H5d. the stale legacy-migrated record itself is untouched by the candidate_story "
      "change -- still present, still is_valid, still carrying its original provenance "
      "and its original (now-stale) fingerprint",
      stale_record_h5 is not None and stale_record_h5.is_valid
      and stale_record_h5.provenance is not None
      and stale_record_h5.provenance.get("kind") == "legacy_artifact_migration"
      and stale_record_h5.input_fingerprint == original_fingerprint_h5)
conn_h5.close()

# ===========================================================================
# E. Isolation / no real network call anywhere in this file
# ===========================================================================
check("E1. no real network POST was attempted anywhere in this test file",
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
