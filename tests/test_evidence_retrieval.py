#!/usr/bin/env python3
"""
Tests for evidence_retrieval.py — deterministic, read-only primary-
document retrieval preparation.

No live network call is made anywhere in this file: fetch_metadata,
download_pdf, extract_text, and is_valid_pdf_url are always injected test
doubles. No sqlite3 import, no database connection, no production DB
write of any kind.

Run: python3 tests/test_evidence_retrieval.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == "FAIL" else ""))
    return condition


import evidence_retrieval as er
from _evidence_test_helpers import load_acceptance_3, get_story, load_corpus_acceptance_3

ACCEPTANCE_3 = load_acceptance_3()
CS01_STORY = get_story(ACCEPTANCE_3, "CS-01")
CS01_DOCS = CS01_STORY["supporting_document_numbers"]
DEV_CORPUS = load_corpus_acceptance_3()

# ===========================================================================
# A. story/document linkage: resolve_story_documents
# ===========================================================================
resolved = er.resolve_story_documents(CS01_STORY, DEV_CORPUS)
check("A1. resolve_story_documents returns one entry per supporting_document_number",
      set(resolved.keys()) == set(CS01_DOCS), str(resolved.keys()))
check("A2. every CS-01 document resolves to a real CorpusObservation",
      all(obs is not None for obs in resolved.values()))
check("A3. resolved observation document_number matches its key",
      all(doc_num == obs.document_number for doc_num, obs in resolved.items()))

story_with_bad_doc = dict(CS01_STORY)
story_with_bad_doc["supporting_document_numbers"] = CS01_DOCS + ["9999-99999"]
resolved_bad = er.resolve_story_documents(story_with_bad_doc, DEV_CORPUS)
check("A4. a document_number not in the corpus resolves to None, not an exception",
      resolved_bad["9999-99999"] is None)

# ===========================================================================
# B. Mock fakes shared by the retrieval tests below
# ===========================================================================


class _FakeResp:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise RuntimeError("simulated HTTP error")

    def json(self):
        return self._payload


def _make_fetch_metadata(doc_num, *, html_url=None, pdf_url=None, mismatched_number=None):
    returned_number = mismatched_number if mismatched_number else doc_num
    html = html_url or f"https://www.federalregister.gov/documents/2026/09/16/{doc_num}"

    def _fetch(document_number, timeout=30, http_get=None):
        return {
            "document_number": returned_number,
            "html_url": html,
            "pdf_url": pdf_url,
            "title": f"Title for {doc_num}",
        }
    return _fetch


def _always_raises_metadata(document_number, timeout=30, http_get=None):
    raise RuntimeError("simulated metadata fetch failure")


def _make_download_pdf(local_path_or_none):
    def _download(pdf_url, dest_dir, doc_id):
        return local_path_or_none
    return _download


def _make_extract_text(text):
    def _extract(local_path):
        return text
    return _extract


def _true(*a, **kw):
    return True


# ===========================================================================
# C. Successful retrieval, one document, with real PDF-extraction mocks
# ===========================================================================
doc_num = CS01_DOCS[0]
observation = resolved[doc_num]
result = er.retrieve_document(
    doc_num, observation,
    fetch_metadata=_make_fetch_metadata(doc_num, pdf_url="https://www.federalregister.gov/x.pdf"),
    download_pdf=_make_download_pdf("/tmp/fake.pdf"),
    extract_text=_make_extract_text("Full primary document text for " + doc_num),
    is_valid_pdf_url=_true,
)
check("C1. successful retrieval has status='retrieved'", result.status == "retrieved", result.failure_detail)
check("C2. successful retrieval has identity_status='verified'", result.identity_status == "verified")
check("C3. successful retrieval's primary_source is True (federalregister.gov domain)",
      result.primary_source is True)
check("C4. successful retrieval's text matches the mocked extraction", "Full primary document text" in result.text)
check("C5. successful retrieval is not truncated (short text)", result.truncated is False)
check("C6. successful retrieval's publication/effective dates come from the corpus observation",
      result.publication_date == observation.publication_date
      and result.effective_date == observation.effective_date)

# ===========================================================================
# D. Retrieval failure: document not in corpus
# ===========================================================================
result = er.retrieve_document("9999-99999", None)
check("D1. document not in corpus: status='failed'", result.status == "failed")
check("D2. document not in corpus: failure_reason is machine-readable",
      result.failure_reason == "document_not_in_corpus")

# ===========================================================================
# E. Retrieval failure: metadata fetch fails
# ===========================================================================
result = er.retrieve_document(
    doc_num, observation, fetch_metadata=_always_raises_metadata,
)
check("E1. metadata fetch failure: status='failed'", result.status == "failed")
check("E2. metadata fetch failure: failure_reason is machine-readable",
      result.failure_reason == "metadata_fetch_failed")
check("E3. metadata fetch failure: does not raise past retrieve_document", True)  # reaching here proves it

# ===========================================================================
# F. Retrieval failure: identity mismatch (document-number mismatch detection)
# ===========================================================================
result = er.retrieve_document(
    doc_num, observation,
    fetch_metadata=_make_fetch_metadata(doc_num, mismatched_number="2026-00000"),
)
check("F1. identity mismatch: status='failed'", result.status == "failed")
check("F2. identity mismatch: identity_status='mismatch'", result.identity_status == "mismatch")
check("F3. identity mismatch: failure_reason is machine-readable",
      result.failure_reason == "identity_mismatch_fatal")

# Mismatch via the html_url's own encoded FR document number disagreeing
# with what was requested (not just the metadata's bare document_number field).
result = er.retrieve_document(
    doc_num, observation,
    fetch_metadata=_make_fetch_metadata(
        doc_num, html_url="https://www.federalregister.gov/documents/2026/01/01/2026-00000"
    ),
)
check("F4. identity mismatch via html_url's encoded document number also fails",
      result.status == "failed" and result.identity_status == "mismatch")

# ===========================================================================
# G. Retrieval failure: no usable primary text available (PDF download fails)
# — never substitutes the corpus observation's Stage-1 summary as if it
# were primary source text.
# ===========================================================================
result = er.retrieve_document(
    doc_num, observation,
    fetch_metadata=_make_fetch_metadata(doc_num, pdf_url="https://www.federalregister.gov/x.pdf"),
    download_pdf=_make_download_pdf(None),  # download "fails"
    is_valid_pdf_url=_true,
)
check("G1. PDF download failure: status='failed'", result.status == "failed")
check("G2. PDF download failure: failure_reason is machine-readable",
      result.failure_reason == "primary_text_unavailable")
check("G3. PDF download failure: text is None, never silently filled from the corpus summary",
      result.text is None)
check("G4. PDF download failure: identity_status is still reported (identity was checked before text)",
      result.identity_status == "verified")

# No pdf_url at all
result = er.retrieve_document(
    doc_num, observation,
    fetch_metadata=_make_fetch_metadata(doc_num, pdf_url=None),
)
check("G5. no pdf_url available: status='failed' with primary_text_unavailable",
      result.status == "failed" and result.failure_reason == "primary_text_unavailable")

# extract_text raises
result = er.retrieve_document(
    doc_num, observation,
    fetch_metadata=_make_fetch_metadata(doc_num, pdf_url="https://www.federalregister.gov/x.pdf"),
    download_pdf=_make_download_pdf("/tmp/fake.pdf"),
    is_valid_pdf_url=_true,
    extract_text=lambda path: (_ for _ in ()).throw(RuntimeError("simulated extraction crash")),
)
check("G6. extract_text raising an exception is caught, not propagated",
      result.status == "failed" and result.failure_reason == "primary_text_unavailable")

# ===========================================================================
# H. Bounded long-document extraction (mock long-document extraction)
# ===========================================================================
long_text = "X" * 50000
result = er.retrieve_document(
    doc_num, observation,
    fetch_metadata=_make_fetch_metadata(doc_num, pdf_url="https://www.federalregister.gov/x.pdf"),
    download_pdf=_make_download_pdf("/tmp/fake.pdf"),
    extract_text=_make_extract_text(long_text),
    is_valid_pdf_url=_true,
    max_excerpt_chars=1000,
)
check("H1. long document is bounded to max_excerpt_chars", len(result.text) == 1000)
check("H2. long document result is marked truncated=True", result.truncated is True)
check("H3. long document text_source is 'pdf_excerpt' (not pretending full review)",
      result.text_source == "pdf_excerpt")

short_result = er.retrieve_document(
    doc_num, observation,
    fetch_metadata=_make_fetch_metadata(doc_num, pdf_url="https://www.federalregister.gov/x.pdf"),
    download_pdf=_make_download_pdf("/tmp/fake.pdf"),
    extract_text=_make_extract_text("short text"),
    is_valid_pdf_url=_true,
    max_excerpt_chars=1000,
)
check("H4. short document is NOT truncated and text_source='pdf_full'",
      short_result.truncated is False and short_result.text_source == "pdf_full")

# ===========================================================================
# I. Multiple primary documents: one failure must not corrupt the others
# ===========================================================================


def _mixed_fetch_metadata(document_number, timeout=30, http_get=None):
    if document_number == CS01_DOCS[1]:
        raise RuntimeError("simulated failure for the second document only")
    return {
        "document_number": document_number,
        "html_url": f"https://www.federalregister.gov/documents/2026/09/16/{document_number}",
        "pdf_url": "https://www.federalregister.gov/x.pdf",
    }


bundle = er.build_evidence_source_material(
    CS01_STORY, DEV_CORPUS,
    fetch_metadata=_mixed_fetch_metadata,
    download_pdf=_make_download_pdf("/tmp/fake.pdf"),
    extract_text=_make_extract_text("Some primary text."),
    is_valid_pdf_url=_true,
)
check("I1. build_evidence_source_material returns one result per supporting_document_number",
      len(bundle.documents) == len(CS01_DOCS))
check("I2. the deliberately-failing document is reported as failed",
      bundle.by_document_number()[CS01_DOCS[1]].status == "failed")
check("I3. every OTHER document in the same story still retrieved successfully",
      all(bundle.by_document_number()[d].status == "retrieved"
          for d in CS01_DOCS if d != CS01_DOCS[1]))
check("I4. retrieved_documents/failed_documents properties partition correctly",
      len(bundle.retrieved_documents) + len(bundle.failed_documents) == len(bundle.documents)
      and len(bundle.failed_documents) == 1)

# A test double that raises something retrieve_document itself wouldn't
# normally catch (e.g. raises inside is_valid_pdf_url) must still not
# corrupt the bundle.
def _misbehaving_is_valid(*a, **kw):
    raise RuntimeError("a badly-written test double blows up")


bundle2 = er.build_evidence_source_material(
    CS01_STORY, DEV_CORPUS,
    fetch_metadata=_make_fetch_metadata(CS01_DOCS[0], pdf_url="https://www.federalregister.gov/x.pdf"),
    is_valid_pdf_url=_misbehaving_is_valid,
)
check("I5. an unexpected exception from an injected double is isolated per-document, never propagated",
      len(bundle2.documents) == len(CS01_DOCS)
      and all(d.status == "failed" for d in bundle2.documents))

# ===========================================================================
# J. No production database writes anywhere in this module
# ===========================================================================
check("J1. evidence_retrieval.py does not import sqlite3", not hasattr(er, "sqlite3"))
import inspect
_source = inspect.getsource(er)
check("J2. no INSERT/UPDATE/DELETE/ALTER/CREATE SQL keyword appears in evidence_retrieval.py source",
      not any(kw in _source for kw in ["INSERT INTO", "UPDATE ", "DELETE FROM", "ALTER TABLE", "CREATE TABLE"]))

# ===========================================================================
# K. Isolation: frozen DD/regulus files reused but never modified by import
# ===========================================================================
check("K1. evidence_retrieval imports regulus_v3 (AS-IS reuse of download_source_pdf/extract_pdf_text)",
      hasattr(er, "regulus_v3"))
check("K2. evidence_retrieval imports dd_schema (AS-IS reuse of classify_primary_source/"
      "extract_federal_register_document_number)", hasattr(er, "dd_schema"))
check("K3. evidence_retrieval does NOT import dd_pipeline", not hasattr(er, "dd_pipeline"))
check("K4. evidence_retrieval does NOT import corpus_analyst/corpus_analyst_schema",
      not hasattr(er, "corpus_analyst") and not hasattr(er, "corpus_analyst_schema"))

# ===========================================================================
# L. is_valid_pdf_url / classify_primary_source reuse sanity (AS-IS)
# ===========================================================================
check("L1. a non-federalregister.gov URL is NOT classified primary (dd_schema reuse works)",
      er.dd_schema.classify_primary_source("https://example.com/doc.pdf") is False)
check("L2. a federalregister.gov URL IS classified primary (dd_schema reuse works)",
      er.dd_schema.classify_primary_source("https://www.federalregister.gov/documents/x") is True)

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
