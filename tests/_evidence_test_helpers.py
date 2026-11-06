#!/usr/bin/env python3
"""
Shared test helpers for the Evidence Analyst foundation tests
(test_evidence_analyst_schema.py, test_evidence_retrieval.py,
test_evidence_analyst.py).

GOLDEN FIXTURE PAIR (both real, both unmodified, both uploaded by the
user -- see tests/fixtures/):

  tests/fixtures/corpus_acceptance_3.json
      The REAL corpus_extractor.get_corpus() output used for Acceptance
      #3 -- 87 observations, reporting period 2026-09-14..2026-10-05,
      first document 2026-18641, last document 2026-20375. Independently
      verified by the user before upload. Every field is real.

  tests/fixtures/corpus_analyst_acceptance_3.json
      The REAL, successful blind Corpus Analyst output over that same
      corpus -- 87 observations analyzed -> 11 candidate stories. Every
      story_id, title, supporting_document_numbers, hypothesis,
      research_question, etc. is real.

Neither file is ever edited, repaired, or replaced by this module -- it
only loads and reconstructs typed objects from them. There is no
synthetic/placeholder Corpus construction left in this file: every
CorpusObservation field used by the Evidence Analyst foundation tests
now comes from the real corpus_acceptance_3.json fixture.

Referential integrity between the two fixtures (every supporting_
document_number in every candidate_story resolves to a real observation
in corpus_acceptance_3.json) was verified before this module was written
-- 28 total supporting-document references across 11 stories, 25 unique
document numbers, 0 unresolved. See the delivery report for this task
for the full verification output.
"""

import json
import os

from corpus_extractor import Corpus, CorpusObservation

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
ACCEPTANCE_3_PATH = os.path.join(FIXTURES_DIR, "corpus_analyst_acceptance_3.json")
CORPUS_ACCEPTANCE_3_PATH = os.path.join(FIXTURES_DIR, "corpus_acceptance_3.json")


def load_acceptance_3() -> dict:
    """Load the real, unmodified Corpus Analyst Acceptance #3 output."""
    with open(ACCEPTANCE_3_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_story(acceptance_output: dict, story_id: str) -> dict:
    for story in acceptance_output["candidate_stories"]:
        if story["story_id"] == story_id:
            return story
    raise KeyError(f"story_id {story_id!r} not found in fixture")


def load_corpus_acceptance_3() -> Corpus:
    """Load and reconstruct the REAL corpus_extractor.Corpus used for
    Acceptance #3 from tests/fixtures/corpus_acceptance_3.json.

    The fixture's top-level shape ({"reporting_period": {"start","end"},
    "observation_count", "observations": [...]}) and each observation's
    field set are exactly corpus_extractor.Corpus.to_dict()'s own output
    shape, so this is a faithful reconstruction, not a reinterpretation
    -- every CorpusObservation field is read directly from the fixture,
    none are invented or defaulted.
    """
    with open(CORPUS_ACCEPTANCE_3_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    observations = [
        CorpusObservation(
            document_number=o["document_number"],
            publication_date=o["publication_date"],
            effective_date=o["effective_date"],
            title=o["title"],
            agency=o["agency"],
            score=o["score"],
            countries=o["countries"],
            entities=o["entities"],
            eccns=o["eccns"],
            change_type=o["change_type"],
            summary=o["summary"],
            primary_source_url=o["primary_source_url"],
            tier=o["tier"],
            due_diligence_ran=o["due_diligence_ran"],
        )
        for o in raw["observations"]
    ]

    return Corpus(
        start_date=raw["reporting_period"]["start"],
        end_date=raw["reporting_period"]["end"],
        observations=observations,
    )
