#!/usr/bin/env python3
"""
corpus_analyst.py — Step 2 of the corpus-intelligence workflow: the
first-pass Corpus Analyst (intelligence REQUIREMENTS GENERATION, not
final intelligence production).

    corpus_extractor.get_corpus()
            -> CORPUS ANALYST — FIRST PASS (this module)
            -> structured candidate stories / collection requirements
            -> STOP

This module makes exactly one kind of LLM call (no web_search, no other
tool) and performs no persistence, no email, no PDF generation, and no
second-pass hypothesis testing. It does not call Stage 2 or Stage 3, does
not import dd_pipeline or dd_schema, and does not modify any existing
Regulus file.

The analyst receives the COMPLETE reporting-window Corpus produced by
corpus_extractor.get_corpus() — no server-side filtering by score, tier,
or due_diligence_ran. Its system prompt explicitly tells it that score,
tier, and due_diligence_ran are signals, not ground truth about
significance (see CORPUS_ANALYST_SYSTEM_PROMPT).

Output is validated deterministically by corpus_analyst_schema.py before
any caller may treat it as usable: every document number the model cites
must exist in the corpus it was actually given, candidate story IDs must
be unique, and the required non-empty fields (research_questions,
intelligence_gaps, evidence_needed, disconfirming_evidence_needed,
supporting_document_numbers) must be present and non-empty. Whether a
hypothesis is CORRECT is never decided here — only whether the output is
structurally sound and traceable to real input.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests

import corpus_analyst_schema as schema
from corpus_extractor import Corpus

log = logging.getLogger("regulus.corpus_analyst")

SCHEMA_VERSION = "1.0"
PROMPT_VERSION = "1.0"

# Same model family already used for Stage 1/2/3 elsewhere in Regulus —
# no new model introduced.
CORPUS_ANALYST_MODEL = "claude-sonnet-4-6"

# Sizing rationale (first-principles estimate; no live-run data exists yet
# for this role, unlike Stage 2/3's token limits, which were raised in
# response to observed truncations on real acceptance runs -- see
# dd_pipeline.py's STAGE2_MAX_TOKENS/STAGE3_MAX_TOKENS comments for that
# history). For an 87-observation corpus: a generous upper bound is on the
# order of 15-20 candidate stories, each with ~7 narrative/list fields
# (hypothesis, alternative hypotheses, observational basis, gaps,
# questions, evidence needed, disconfirming evidence needed) at roughly
# 500-600 tokens per story once JSON structure overhead is included, plus
# the administrative-activity list, unclustered-observations list, and
# corpus-level gaps. That puts a reasonable worst case around 9,000-10,000
# output tokens. 12000 gives real headroom above that estimate without
# being arbitrarily large. This has NOT been validated against a live run
# yet (Step 2 explicitly defers the live acceptance test) -- expect this
# constant may need the same kind of revision Stage 2/3's did once real
# output is observed.
CORPUS_ANALYST_MAX_TOKENS = 12000

# No web_search tool is used here (single shot, no server-side search
# rounds to wait on, unlike Stage 2's 600s). The output is larger than
# Stage 3's (which uses 180s at a 4000-token cap), so this scales that
# budget up for the larger token ceiling above rather than reusing either
# existing constant as-is.
CORPUS_ANALYST_TIMEOUT_SECONDS = 300

# Mirrors the existing STAGE2_MAX_ATTEMPTS/STAGE3_MAX_ATTEMPTS pattern in
# dd_pipeline.py -- one retry on any call/parse failure, no autonomous
# loop, no new retry subsystem.
CORPUS_ANALYST_MAX_ATTEMPTS = 2


CORPUS_ANALYST_SYSTEM_PROMPT = """You are a corpus intelligence analyst performing FIRST-PASS DISCOVERY over
a reporting-period corpus of regulatory observations. Your job is
intelligence REQUIREMENTS GENERATION, not final intelligence production.

WHAT YOU ARE GIVEN: a deterministic set of observations extracted from a
database of Federal Register documents. Each observation may carry a
"tier" of "analyzed" (an earlier automated step produced structured
fields for it) or "metadata_only" (only title/agency/date/score survive
-- no structured analysis was performed). Every observation also carries
a numeric "score", a "due_diligence_ran" flag, and -- for "analyzed"
observations only -- fields such as countries/entities/eccns/change_type/
summary.

THESE ARE SIGNALS, NOT GROUND TRUTH ABOUT SIGNIFICANCE. A high score does
not mean a document is materially important. A low score does not mean it
is unimportant. due_diligence_ran reflects an earlier, narrower automated
screening decision, not a verified importance judgment. A "metadata_only"
observation has not been read at the same depth as an "analyzed" one --
treat its absence of countries/entities/eccns as UNKNOWN, never as
evidence that none exist.

EPISTEMIC LIMITATION -- state this to yourself before every judgment: you
have NOT read the underlying primary government documents. You are
analyzing observations ABOUT documents -- titles, abstracts, and (for
"analyzed" rows) an earlier automated summary -- not the documents
themselves. You must never imply you have read a primary source. Every
output you produce is a CANDIDATE: a candidate story, a preliminary
hypothesis, an intelligence gap, a collection requirement. None of it is
an established policy conclusion, a verified government intent, a
verified instance of coordination, or a final legal/regulatory
interpretation. Say "preliminary" or "candidate" rather than stating
things as settled.

ANALYTICAL DISCIPLINE -- you must actively apply every rule below, not
merely avoid contradicting it:
  A. Document count is not policy intensity. A day with many
     publications is not necessarily a day of intense policy activity.
  B. Temporal proximity is not coordination. Multiple agencies acting in
     the same window does not by itself establish they coordinated.
  C. Multiple Federal Register publications that implement one upstream
     decision are not multiple independent policy signals -- they may be
     one decision surfacing several times.
  D. Administrative publication (formalizing, codifying, or correcting
     something already in effect) is not the same as a substantive
     policy change.
  E. A proposed action is not a final action. Keep these explicitly
     distinct wherever the observation indicates which one it is.
  F. Publication date is not effective date. Do not conflate them.
  G. The absence of a country/entity/ECCN in a metadata_only record is
     not evidence that none exists -- it means that record was never
     analyzed at that depth.
  H. Any structured field on an "analyzed" observation (countries,
     entities, change_type, summary, etc.) is an earlier automated
     system's output, not a verified fact. Treat it as a claim, not
     ground truth.
  I. Score is not materiality.
  J. due_diligence_ran is not importance.
  K. The same country appearing in several observations does not make
     them the same policy story.
  L. The same agency appearing in several observations does not make
     them the same policy story.
  M. A government document's own characterization of its action is not
     the same as your analytic assessment of that action's significance.
  N. "Unknown" is not "false." When you don't know something, say so
     explicitly -- never substitute an assumption for an unresolved
     question.

CANDIDATE STORIES: a candidate story can be supported by multiple
observations OR by a single observation. Do NOT require multiple
documents to form a story -- actively look for high-consequence
SINGLETON actions: a single proposed rule with broad scope, a single
major final rule, a new enforcement mechanism, a major licensing change,
a significant jurisdiction change, an action touching an important
technology or sector, or an action with unusual cross-regime
implications. A singleton candidate story is exactly as legitimate as a
multi-document one when the evidence supports investigating it -- again,
this is a candidate judgment requiring evidence, not a verified fact.

ADMINISTRATIVE ACTIVITY: observations that APPEAR administrative,
procedural, corrective, codifying, or duplicative of something already
public belong in potential_administrative_activity, not in
candidate_stories -- but do not discard them. Give them LOW research
priority UNLESS they materially change legal effect, reveal a broader
implementation pattern, provide context for another candidate story,
contradict another observation, or contain an independently important
change buried inside otherwise-administrative text -- in which case give
them the appropriate higher priority and say why. This classification is
preliminary, pending evidence review, not a final disposition.

HYPOTHESIS DISCIPLINE: for every candidate story, you must provide:
  - a preliminary_hypothesis (your leading interpretation);
  - alternative_hypotheses: at least one reasonable competing explanation
    wherever one plausibly exists (an empty list is only acceptable when
    you can state why no reasonable alternative exists);
  - intelligence_gaps: what you do not yet know;
  - research_questions: CONCRETE, ANSWERABLE questions that could drive
    actual document retrieval or research. "We need more research" or
    any equivalent non-answer is NOT an acceptable research question.
    BAD: "Research whether these actions are related."
    BETTER: "Do the three agency actions cite the same Presidential
    determination, Executive Order, statutory authority, or earlier
    agency action?"
  - evidence_needed: what would have to be true, and be found, to
    SUPPORT the leading hypothesis;
  - disconfirming_evidence_needed: what would WEAKEN OR FALSIFY the
    leading hypothesis. You must actively try to identify this -- do not
    treat your leading hypothesis as safe by default.

OUTPUT: return ONLY valid JSON, no prose, no markdown fences, matching
EXACTLY this shape (empty arrays are fine where you genuinely have
nothing to report; omitting a required key is not):

{
  "reporting_period": {"start": "", "end": ""},
  "corpus_assessment": {
    "observation_count": 0,
    "overall_activity_characterization": "",
    "important_caveats": []
  },
  "candidate_stories": [
    {
      "story_id": "",
      "title": "",
      "supporting_document_numbers": [],
      "candidate_materiality": "high|medium|low",
      "why_it_deserves_investigation": "",
      "preliminary_hypothesis": "",
      "alternative_hypotheses": [],
      "observational_basis": [
        {"document_number": "", "observation": ""}
      ],
      "intelligence_gaps": [],
      "research_questions": [],
      "evidence_needed": [],
      "disconfirming_evidence_needed": [],
      "research_priority": "high|medium|low",
      "preliminary_confidence": "high|medium|low"
    }
  ],
  "potential_administrative_activity": [
    {"document_numbers": [], "reason": "", "research_priority": "low|medium|high"}
  ],
  "unclustered_observations_of_interest": [
    {"document_number": "", "reason": ""}
  ],
  "corpus_level_gaps": []
}

Every document_number you cite anywhere in your output MUST be a
document_number that actually appears in the corpus you were given. Do
not invent, guess, or paraphrase a document number. If you are uncertain
whether an observation belongs to a story, it is better to omit it than
to cite a document number you are not certain is correct.
"""


def _extract_json_text(content_blocks):
    """Pull the final JSON text out of a Messages API response's content
    blocks. Mirrors dd_pipeline._extract_json_text's behavior exactly
    (same markdown-fence stripping), reimplemented locally rather than
    imported so this module has no dependency on dd_pipeline at all."""
    text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
    text = text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return text


class CorpusAnalystJSONDecodeError(ValueError):
    """Raised by call_anthropic_corpus_analyst when the extracted text is
    not valid JSON. Mirrors dd_pipeline.Stage2JSONDecodeError/
    Stage3JSONDecodeError exactly -- DIAGNOSTIC-ONLY, str(this) is
    IDENTICAL to str() of the underlying json.JSONDecodeError, so any
    `except Exception as e: ...str(e)...` caller sees the same message it
    would have seen without this class. response-side only; no secrets.
    """

    def __init__(self, json_error: json.JSONDecodeError, *, raw_text: str,
                 content_blocks: list, response_meta: dict):
        super().__init__(str(json_error))
        self.raw_text = raw_text
        self.content_blocks = content_blocks
        self.response_meta = response_meta


def call_anthropic_corpus_analyst(corpus_payload: dict, api_key: str) -> dict:
    """Real Corpus Analyst call. No tools -- this role gets no web_search
    and no other tool; it sees only the supplied corpus payload."""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CORPUS_ANALYST_MODEL,
            "max_tokens": CORPUS_ANALYST_MAX_TOKENS,
            "system": CORPUS_ANALYST_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": json.dumps(corpus_payload, indent=2)}],
        },
        timeout=CORPUS_ANALYST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    response_json = resp.json()
    content = response_json["content"]
    text = _extract_json_text(content)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        response_meta = {
            "stop_reason": response_json.get("stop_reason"),
            "model": response_json.get("model"),
            "usage": response_json.get("usage"),
            "content_block_count": len(content) if isinstance(content, list) else None,
            "content_block_types": (
                [b.get("type") for b in content] if isinstance(content, list) else None
            ),
        }
        raise CorpusAnalystJSONDecodeError(
            e, raw_text=text, content_blocks=content, response_meta=response_meta
        ) from e


@dataclass
class CorpusAnalysisOutcome:
    """Result of run_corpus_analysis(). Carries the raw model output (if
    any), the deterministic validation result, and a failure_reason set
    only on a pipeline-level (non-analytical) failure -- never on a
    structurally-valid-but-"wrong" hypothesis, which this layer has no
    way to detect and does not attempt to."""
    raw: Optional[dict] = None
    validation_status: str = "invalid"   # "valid" | "invalid"
    validation_errors: list = field(default_factory=list)
    failure_reason: Optional[str] = None  # set on call/parse failure only

    @property
    def is_valid(self) -> bool:
        return self.validation_status == "valid"


def run_corpus_analysis(corpus: Corpus, *, api_key: Optional[str] = None,
                         call_analyst: Optional[Callable[[dict, str], dict]] = None
                         ) -> CorpusAnalysisOutcome:
    """Run the first-pass Corpus Analyst over a Corpus produced by
    corpus_extractor.get_corpus(), and deterministically validate the
    result.

    This function:
      - sends the COMPLETE corpus (corpus.to_dict()) to the model --
        never filters by score/tier/due_diligence_ran before sending;
      - makes no Stage 2/Stage 3 call, and does not import dd_pipeline;
      - retries once (CORPUS_ANALYST_MAX_ATTEMPTS) on call/parse failure,
        mirroring the existing Stage 2/3 retry pattern -- no autonomous
        loop;
      - validates the result with corpus_analyst_schema.validate_
        corpus_analysis against the exact document-number set present in
        `corpus`, so a model-invented document number is always caught
        regardless of how plausible it looks.

    Never raises on a model/network/parse failure -- those are caught,
    logged, and reported via the returned CorpusAnalysisOutcome.
    failure_reason, matching dd_pipeline.run_due_diligence's failure-
    handling convention.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    caller = call_analyst or call_anthropic_corpus_analyst
    corpus_payload = corpus.to_dict()

    raw = None
    last_error = None
    for attempt in range(1, CORPUS_ANALYST_MAX_ATTEMPTS + 1):
        try:
            raw = caller(corpus_payload, api_key)
            last_error = None
            break
        except Exception as e:  # network error, HTTP error, JSON parse error
            last_error = e
            log.warning("Corpus Analyst attempt %d/%d failed: %s",
                        attempt, CORPUS_ANALYST_MAX_ATTEMPTS, e)

    if last_error is not None:
        return CorpusAnalysisOutcome(
            raw=None, validation_status="invalid",
            validation_errors=[f"corpus_analyst_call_failed: {last_error}"],
            failure_reason="corpus_analyst_call_failed",
        )

    valid_document_numbers = {
        o.document_number for o in corpus.observations if o.document_number is not None
    }
    result = schema.validate_corpus_analysis(raw, valid_document_numbers)
    return CorpusAnalysisOutcome(
        raw=raw,
        validation_status="valid" if result.is_valid else "invalid",
        validation_errors=result.validation_errors,
    )
