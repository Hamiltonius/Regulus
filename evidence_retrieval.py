#!/usr/bin/env python3
"""
evidence_retrieval.py — deterministic, read-only primary-document
retrieval PREPARATION for the future Evidence Analyst role.

FOUNDATION ONLY (Evidence Analyst Step 0): this module implements the
pipeline described in the task spec section 5 —

    candidate_story.supporting_document_numbers
            -> resolve corresponding corpus observations
            -> identify authoritative source URL/document
            -> retrieve primary document
            -> extract usable text
            -> verify document identity
            -> produce deterministic source material suitable for
               Evidence Analyst input

It does NOT call any LLM, does NOT write to any database, and is NOT a
broad crawler — it only ever fetches the single Federal Register
document(s) a candidate_story already names via supporting_document_numbers.

Isolation / reuse discipline:
  - Reuses regulus_v3.download_source_pdf / extract_pdf_text /
    is_valid_pdf_url AS-IS (imported, not reimplemented) -- these are
    already small, defensive, side-effect-bounded functions (HEAD check,
    size cap, streaming download, never raise) and reusing them verbatim
    means this module can never drift from the download/extraction
    behavior already running in production. Importing them does not
    modify regulus_v3.py.
  - Reuses dd_schema.classify_primary_source and
    dd_schema.extract_federal_register_document_number AS-IS -- these are
    pure, deterministic, network-free functions with no side effects.
    Importing dd_schema.py does not modify it, does not touch the DD
    Gate, Stage 2/3 prompts, due_diligence_records, or any DD validator.
  - Does NOT import dd_pipeline (not needed -- nothing here touches
    Stage 1/2/3 call functions, the Gate, or due_diligence_records).
  - Does NOT import corpus_analyst or corpus_analyst_schema (no coupling
    to the Corpus Analyst's own validation).
  - Imports corpus_extractor only for type hints (Corpus/CorpusObservation)
    -- this module reads Corpus/CorpusObservation objects, it does not
    call get_corpus() or touch any database connection itself.
  - Performs NO INSERT/UPDATE/DELETE/ALTER anywhere. No sqlite3 import.

Failure handling: every retrieval step that can fail (document not in
corpus, FR metadata lookup, PDF download, PDF text extraction) is caught
and converted into an explicit, machine-readable RetrievedDocument with
status="failed" and a failure_reason code -- never raised past
retrieve_document()/build_evidence_source_material(), and never silently
degraded into a fabricated success. One failed document's exception
cannot propagate and corrupt the retrieval of any other document in the
same story (see build_evidence_source_material's per-document try/except).

Never substitutes a secondary source for a missing primary source: if the
primary document's text cannot be retrieved (PDF unavailable, download
failed, extraction produced nothing), the result is an explicit failure
-- this module never falls back to the corpus observation's own Stage-1
LLM-derived `summary` field and presents it as if it were primary source
text.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests

import dd_schema
import regulus_v3
from corpus_extractor import Corpus, CorpusObservation


# Federal Register single-document metadata endpoint. A read-only GET by
# document_number -- never a bulk/date-range query (that would be a
# crawler; this module only ever asks for documents a candidate_story
# already names).
FR_DOCUMENT_API = "https://www.federalregister.gov/api/v1/documents/{document_number}.json"
FR_DOCUMENT_FIELDS = [
    "document_number", "title", "html_url", "pdf_url", "type",
    "publication_date", "effective_on", "citation", "agencies",
]

# Mirrors the existing STAGE2_MAX_ATTEMPTS/CORPUS_ANALYST_MAX_ATTEMPTS
# pattern -- one retry on a metadata-fetch failure, no autonomous loop.
RETRIEVAL_METADATA_TIMEOUT_SECONDS = 30
RETRIEVAL_METADATA_MAX_ATTEMPTS = 2

# Bounded relevant-section extraction for long documents (spec section 5:
# "Long documents must support bounded relevant-section extraction
# without pretending unread sections were reviewed"). This is a hard
# character cap on what is handed to the Evidence Analyst as "read" text
# -- truncated=True on the result makes clear that anything beyond this
# point was NOT reviewed, rather than silently omitted.
DEFAULT_MAX_EXCERPT_CHARS = 20000

PDF_DOWNLOAD_DEST_DIR = os.environ.get("EVIDENCE_PDF_DIR", "evidence_pdfs")

_FAILURE_REASONS = {
    "document_not_in_corpus",
    "metadata_fetch_failed",
    "identity_mismatch_fatal",
    "primary_text_unavailable",
}


@dataclass
class RetrievedDocument:
    """Result of retrieving ONE document for Evidence Analyst input.
    Always has a `status` of "retrieved" or "failed" -- never raises, and
    never fabricates text or identity: a field that could not be
    deterministically established is None, not guessed.

    identity_status mirrors evidence_analyst_schema.SOURCE_IDENTITY_STATUS_VALUES
    ("verified"/"mismatch"/"unverified"/"not_applicable") -- this module
    and that schema module share the vocabulary but neither imports the
    other, so the cross-check in evidence_analyst_schema.validate_
    evidence_analysis()'s retrieval_results parameter is done via
    getattr(..., "identity_status", None), not an isinstance/import
    dependency.
    """
    document_number: str
    status: str  # "retrieved" | "failed"
    identity_status: Optional[str] = None
    source_url: Optional[str] = None
    pdf_url: Optional[str] = None
    primary_source: Optional[bool] = None
    text: Optional[str] = None
    text_source: Optional[str] = None  # "pdf_full" | "pdf_excerpt" | None
    truncated: bool = False
    publication_date: Optional[str] = None
    effective_date: Optional[str] = None
    retrieved_at: Optional[str] = None
    failure_reason: Optional[str] = None
    failure_detail: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "document_number": self.document_number,
            "status": self.status,
            "identity_status": self.identity_status,
            "source_url": self.source_url,
            "pdf_url": self.pdf_url,
            "primary_source": self.primary_source,
            "text": self.text,
            "text_source": self.text_source,
            "truncated": self.truncated,
            "publication_date": self.publication_date,
            "effective_date": self.effective_date,
            "retrieved_at": self.retrieved_at,
            "failure_reason": self.failure_reason,
            "failure_detail": self.failure_detail,
        }


@dataclass
class EvidenceRetrievalBundle:
    """Result of build_evidence_source_material() for one candidate_story:
    one RetrievedDocument per supporting_document_number, in the same
    order as the story listed them. A document_number the story cites
    that does not exist in the supplied corpus is still represented here
    (status="failed", failure_reason="document_not_in_corpus") rather
    than silently dropped."""
    story_id: str
    documents: list = field(default_factory=list)  # list[RetrievedDocument]

    @property
    def retrieved_documents(self) -> list:
        return [d for d in self.documents if d.status == "retrieved"]

    @property
    def failed_documents(self) -> list:
        return [d for d in self.documents if d.status == "failed"]

    def by_document_number(self) -> dict:
        return {d.document_number: d for d in self.documents}

    def to_dict(self) -> dict:
        return {"story_id": self.story_id, "documents": [d.to_dict() for d in self.documents]}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_story_documents(candidate_story: dict, corpus: Corpus) -> dict:
    """Resolve candidate_story["supporting_document_numbers"] against the
    corpus's observations. Returns {document_number: CorpusObservation or
    None} -- None means the story cites a document_number that does not
    exist in the supplied corpus (a data-integrity issue the schema
    validator should already have caught upstream, but this function
    checks again rather than assuming it was)."""
    by_doc_number = {o.document_number: o for o in corpus.observations}
    supporting = candidate_story.get("supporting_document_numbers") or []
    return {doc_num: by_doc_number.get(doc_num) for doc_num in supporting}


def fetch_document_metadata(document_number: str, *,
                             timeout: int = RETRIEVAL_METADATA_TIMEOUT_SECONDS,
                             http_get: Optional[Callable] = None) -> dict:
    """Read-only lookup of ONE Federal Register document's metadata by its
    document_number -- never a bulk/date-range query. Raises
    requests.RequestException (or a ValueError on a malformed/non-200
    response) on any failure; callers (retrieve_document) catch this and
    convert it into an explicit failed RetrievedDocument.

    http_get is injectable for tests -- it must have the same signature
    as requests.get(url, params=..., timeout=...) and return an object
    with .raise_for_status() and .json(). No live network call is made
    when a test supplies its own http_get.
    """
    get = http_get or requests.get
    url = FR_DOCUMENT_API.format(document_number=document_number)
    resp = get(url, params={"fields[]": FR_DOCUMENT_FIELDS}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError(f"Federal Register API returned non-object for {document_number!r}")
    return data


def _check_identity(document_number: str, metadata: dict, observation: Optional[CorpusObservation]):
    """Deterministically decide identity_status for a retrieved document.
    Cross-checks THREE independent signals, none of which is trusted
    alone:
      1. the metadata response's own "document_number" field must equal
         what was requested (the FR API should never disagree with
         itself, but this is checked rather than assumed);
      2. if the metadata's html_url encodes an FR document number (via
         dd_schema.extract_federal_register_document_number), it must
         agree with the requested document_number;
      3. if the corpus observation already has a primary_source_url whose
         encoded document number disagrees with the requested
         document_number, that is also a mismatch -- it would mean the
         database's own stored source URL does not match the document
         actually being retrieved.
    Returns one of "verified", "mismatch", "unverified".
    """
    returned_number = metadata.get("document_number")
    if returned_number is not None and returned_number != document_number:
        return "mismatch"

    html_url = metadata.get("html_url")
    encoded_from_api = dd_schema.extract_federal_register_document_number(html_url)
    if encoded_from_api is not None and encoded_from_api != document_number:
        return "mismatch"

    if observation is not None and observation.primary_source_url:
        encoded_from_corpus = dd_schema.extract_federal_register_document_number(
            observation.primary_source_url
        )
        if encoded_from_corpus is not None and encoded_from_corpus != document_number:
            return "mismatch"

    if encoded_from_api is not None:
        return "verified"
    # No FR-shaped number could be extracted from the html_url at all
    # (e.g. the API returned a non-federalregister.gov URL, or no URL) --
    # identity is not disproven, but it is not independently confirmed
    # either.
    return "unverified"


def retrieve_document(document_number: str, observation: Optional[CorpusObservation], *,
                       fetch_metadata: Optional[Callable] = None,
                       download_pdf: Optional[Callable] = None,
                       extract_text: Optional[Callable] = None,
                       is_valid_pdf_url: Optional[Callable] = None,
                       max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
                       pdf_dest_dir: str = PDF_DOWNLOAD_DEST_DIR) -> RetrievedDocument:
    """Retrieve ONE document's primary source material. Never raises --
    every failure mode is caught and returned as a RetrievedDocument with
    status="failed".

    fetch_metadata/download_pdf/extract_text/is_valid_pdf_url are
    injectable (mockable) for tests; when omitted, the real
    fetch_document_metadata() and regulus_v3.download_source_pdf() /
    regulus_v3.extract_pdf_text() / regulus_v3.is_valid_pdf_url() are
    used -- network-dependent retrieval is mockable exactly like
    corpus_analyst.py's call_analyst injection.
    """
    fetch_metadata = fetch_metadata or fetch_document_metadata
    download_pdf = download_pdf or regulus_v3.download_source_pdf
    extract_text = extract_text or regulus_v3.extract_pdf_text
    is_valid_pdf_url = is_valid_pdf_url or regulus_v3.is_valid_pdf_url

    if observation is None:
        return RetrievedDocument(
            document_number=document_number, status="failed",
            failure_reason="document_not_in_corpus",
            failure_detail=f"{document_number!r} is not present in the supplied corpus",
            retrieved_at=_now_iso(),
        )

    try:
        metadata = fetch_metadata(document_number)
    except Exception as e:
        return RetrievedDocument(
            document_number=document_number, status="failed",
            failure_reason="metadata_fetch_failed",
            failure_detail=str(e),
            publication_date=observation.publication_date,
            effective_date=observation.effective_date,
            retrieved_at=_now_iso(),
        )

    identity_status = _check_identity(document_number, metadata, observation)
    retrieved_at = _now_iso()

    if identity_status == "mismatch":
        # Fatal: never hand the Evidence Analyst material whose own
        # identity cannot be trusted, even if a PDF/text happens to be
        # downloadable. The three-way cross-check in _check_identity is
        # exactly the "detect document-number mismatch" requirement.
        return RetrievedDocument(
            document_number=document_number, status="failed",
            identity_status="mismatch",
            failure_reason="identity_mismatch_fatal",
            failure_detail=(
                f"document identity could not be confirmed for {document_number!r} "
                f"(FR metadata document_number={metadata.get('document_number')!r}, "
                f"html_url={metadata.get('html_url')!r})"
            ),
            source_url=metadata.get("html_url"),
            pdf_url=metadata.get("pdf_url"),
            publication_date=observation.publication_date,
            effective_date=observation.effective_date,
            retrieved_at=retrieved_at,
        )

    source_url = metadata.get("html_url") or observation.primary_source_url
    pdf_url = metadata.get("pdf_url")
    primary_source = dd_schema.classify_primary_source(source_url) if source_url else False

    text = None
    text_source = None
    truncated = False
    extraction_detail = None

    if pdf_url and is_valid_pdf_url(pdf_url):
        try:
            local_path = download_pdf(pdf_url, pdf_dest_dir, document_number)
        except Exception as e:
            local_path = None
            extraction_detail = f"download_source_pdf raised: {e}"
        if local_path:
            try:
                extracted = extract_text(local_path)
            except Exception as e:
                extracted = ""
                extraction_detail = f"extract_pdf_text raised: {e}"
            if extracted:
                if len(extracted) > max_excerpt_chars:
                    text = extracted[:max_excerpt_chars]
                    text_source = "pdf_excerpt"
                    truncated = True
                else:
                    text = extracted
                    text_source = "pdf_full"
            elif extraction_detail is None:
                extraction_detail = "extract_pdf_text returned no text"
        elif extraction_detail is None:
            extraction_detail = "download_source_pdf did not return a local file (see regulus_v3 logs)"
    else:
        extraction_detail = f"no usable pdf_url for {document_number!r} (pdf_url={pdf_url!r})"

    if text is None:
        # Never substitute the corpus observation's Stage-1 LLM summary
        # as if it were primary source text -- an explicit, machine-
        # readable failure instead.
        return RetrievedDocument(
            document_number=document_number, status="failed",
            identity_status=identity_status,
            failure_reason="primary_text_unavailable",
            failure_detail=extraction_detail,
            source_url=source_url, pdf_url=pdf_url, primary_source=primary_source,
            publication_date=observation.publication_date,
            effective_date=observation.effective_date,
            retrieved_at=retrieved_at,
        )

    return RetrievedDocument(
        document_number=document_number, status="retrieved",
        identity_status=identity_status,
        source_url=source_url, pdf_url=pdf_url, primary_source=primary_source,
        text=text, text_source=text_source, truncated=truncated,
        publication_date=observation.publication_date,
        effective_date=observation.effective_date,
        retrieved_at=retrieved_at,
    )


def build_evidence_source_material(candidate_story: dict, corpus: Corpus, **retrieval_kwargs
                                    ) -> EvidenceRetrievalBundle:
    """Top-level entry point: resolve every document the candidate_story
    cites and retrieve each one's primary source material. One document's
    failure (of any kind, including an unexpected exception from an
    injected test double) is isolated and reported in that document's own
    RetrievedDocument -- it never aborts or corrupts retrieval of the
    other documents in the same story.

    retrieval_kwargs are passed through to retrieve_document() for every
    document (fetch_metadata/download_pdf/extract_text/is_valid_pdf_url/
    max_excerpt_chars/pdf_dest_dir) -- the same injected test doubles
    apply uniformly across the whole story.
    """
    story_id = candidate_story.get("story_id")
    resolved = resolve_story_documents(candidate_story, corpus)

    documents = []
    for doc_num, observation in resolved.items():
        try:
            result = retrieve_document(doc_num, observation, **retrieval_kwargs)
        except Exception as e:
            # Defensive backstop: retrieve_document is already designed to
            # never raise, but a misbehaving injected test double must
            # still not be able to corrupt the rest of the bundle.
            result = RetrievedDocument(
                document_number=doc_num, status="failed",
                failure_reason="metadata_fetch_failed",
                failure_detail=f"unexpected exception during retrieval: {e}",
                retrieved_at=_now_iso(),
            )
        documents.append(result)

    return EvidenceRetrievalBundle(story_id=story_id, documents=documents)
