#!/usr/bin/env python3
"""
regulus_v3.py — export-control regulatory watcher.

Formerly "bis_watcher.py". Renamed to consolidate under the Regulus project
name per the git-history merge documented in the repo's README and commit
log. All original functionality is preserved unchanged; this version adds
one new, isolated capability (see "PDF full-text + regex ECCN extraction"
below), ported from regulus_v2.py as an additive test path.

Pulls new Federal Register documents from a configured set of agencies
(BIS, State, OFAC/Treasury), scores them deterministically, sends the
material ones to an LLM for structured analysis, persists everything,
saves a legible PDF to disk, and emails only the material,
not-yet-sent alerts.

Deploy: same box/timer as before (node02). Cron/systemd runs this
once a day (or hourly); it is fully idempotent — safe to rerun.

Env vars required:
  ANTHROPIC_API_KEY      - for the analysis step
  GMAIL_USER             - sender/recipient gmail address
  GMAIL_APP_PASSWORD     - Gmail app password (not your login password)
  ALERT_TO               - recipient address (defaults to GMAIL_USER)
  DB_PATH                - path to sqlite db (defaults ./bis_watcher.db —
                            kept as the historical default; production sets
                            this explicitly via env var, so nothing on
                            node02 needs to change)
  LOOKBACK_DAYS          - how many days back to query on first run (default 3)
  PDF_DIR                - path to save PDF archives (defaults ./pdfs)
  ECCN_TEST_DIR           - path for regex-based ECCN extraction test output
                            (defaults ./eccn_test)
  MAX_PDF_SIZE_MB         - size cap for source PDF downloads (default 25)
"""

import os
import re
import sys
import json
import glob as _glob
import hashlib
import sqlite3
import smtplib
import logging
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from fpdf import FPDF

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("regulus")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "bis_watcher.db")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "3"))
PDF_DIR = os.environ.get("PDF_DIR", "pdfs")
ECCN_TEST_DIR = os.environ.get("ECCN_TEST_DIR", "eccn_test")
MAX_PDF_SIZE_MB = int(os.environ.get("MAX_PDF_SIZE_MB", "25"))

# Federal Register agency slugs. Verify slugs against
# https://www.federalregister.gov/api/v1/agencies before relying on this —
# slugs occasionally change. These four cover BIS, State, and OFAC, which is
# where your Turkey/Arrow/Syria/Cyprus/denial-order alerts actually came
# from — none of those were BIS-only, so a BIS-only feed would miss most.
AGENCIES = [
    "industry-and-security-bureau",
    "state-department",
    "foreign-assets-control-office",
    "treasury-department",
]

FR_API = "https://www.federalregister.gov/api/v1/documents.json"
FR_FIELDS = [
    "document_number", "title", "agencies", "type", "publication_date",
    "effective_on", "html_url", "pdf_url", "abstract", "excerpts", "citation",
]

# Deterministic relevance weights. Tune these as you see false positives/negatives.
KEYWORD_WEIGHTS = {
    # high priority — direct regulatory mechanism changes
    "entity list": 10, "commerce control list": 10,
    "export administration regulations": 9, "eccn": 10,
    "license requirement": 9, "license exception": 8,
    "presumption of denial": 9, "military end user": 8, "military end use": 8,
    "600 series": 8, "9x515": 8, "country group": 7,
    "denied persons list": 9, "unverified list": 7, "temporary denial order": 8,
    "denial of export privileges": 9,
    "specially designed": 4, "deemed export": 6, "foreign national": 3,
    "end-user": 4, "end user": 4, "diversion": 5,
    # defense / aerospace signal
    "defense article": 8, "defense service": 8, "arms embargo": 8,
    "aircraft": 5, "missile": 6, "unmanned aircraft": 6,
    "satellite": 5, "spacecraft": 5, "usml": 8, "itar": 7,
    "international traffic in arms regulations": 7,
    "hypersonic": 8,
    # emerging tech / dual-use
    "artificial intelligence": 6, "ai model weights": 9,
    "semiconductor": 7, "advanced computing": 8, "integrated circuit": 5,
    "quantum computing": 6, "quantum": 4, "biotechnology": 6,
    "gene synthesis": 6, "supercomputer": 6, "lithography": 6,
    "foundry": 4, "graphics processing unit": 5, "gpu": 4,
    "gain-of-function": 6, "synthetic biology": 5,
    # sanctions / enforcement
    "sanction": 6, "civil penalty": 5, "settlement": 4, "denial order": 8,
    "ofac": 5, "specially designated national": 7,
    # named countries / regions of concern
    "china": 6, "hong kong": 4, "macau": 3,
    "russia": 6, "iran": 5, "syria": 4, "north korea": 5, "belarus": 4,
    "cuba": 3, "venezuela": 3, "myanmar": 3, "burma": 3, "sudan": 3,
    "pakistan": 3, "cyprus": 2,
    # weak signal — don't let these alone trigger an alert
    "press release": 1, "notice of inquiry": 2,
    "section 232": 7, "national security tariff": 6, "import restriction": 5, "stockpiling": 4, "polysilicon": 5, "minimum price": 4,
}

SCORE_ARCHIVE_MAX = 6      # store only, no email
SCORE_DIGEST_MAX = 8       # store, low-priority (not emailed by this script)
# above SCORE_DIGEST_MAX -> LLM analysis + immediate alert email + PDF

# Regex ECCN pattern: e.g. 3A001, 9E515.a, 0Y521 — one digit, one letter,
# three digits, optional dotted sub-paragraph.
ECCN_PATTERN = re.compile(r'\b[0-9][A-Z][0-9]{3}(?:\.[a-z0-9]+)?\b')

ANALYSIS_SCHEMA_PROMPT = """You are a U.S. export-controls regulatory analyst supporting a
defense/aerospace compliance organization. Analyze the supplied government document.

Rules:
- Do not infer any regulatory requirement not directly supported by the source text.
- Clearly separate final/effective requirements from proposed rules, guidance, and corrections.
- Never invent an ECCN, CFR citation, effective date, entity name, or licensing requirement.
- If information isn't in the source, use null or an empty list — do not guess.
- Every list field (authority, countries, entities, eccns, ear_sections,
  recommended_actions) must contain plain strings only — never objects/dicts.

Return ONLY valid JSON, no prose, no markdown fences, matching exactly this shape:

{
  "title": "",
  "agency": [],
  "publication_date": "",
  "effective_date": "",
  "authority": [],
  "countries": [],
  "entities": [],
  "eccns": [],
  "ear_sections": [],
  "change_type": "",
  "summary": "",
  "licensing_impact": "",
  "defense_impact": "",
  "remaining_controls": "",
  "recommended_actions": [],
  "primary_sources": [],
  "confidence": "High | Medium | Low"
}
"""

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY,
            doc_hash TEXT UNIQUE,
            document_number TEXT,
            title TEXT,
            agency TEXT,
            pub_date TEXT,
            effective_date TEXT,
            authority TEXT,
            countries TEXT,
            entities TEXT,
            eccns TEXT,
            ear_sections TEXT,
            change_type TEXT,
            summary TEXT,
            licensing_impact TEXT,
            defense_impact TEXT,
            remaining_controls TEXT,
            recommended_actions TEXT,
            primary_source_url TEXT,
            confidence TEXT,
            score INTEGER,
            raw_excerpt TEXT,
            fetched_at TEXT,
            emailed_at TEXT
        )
    """)
    conn.commit()
    _ensure_column(conn, "alerts", "eccn_regex_matches", "TEXT")
    _ensure_column(conn, "alerts", "eccn_regex_source", "TEXT")
    conn.commit()
    return conn


def _ensure_column(conn, table, column, coltype):
    """Additive schema migration: add a column if it doesn't already exist.
    Never touches or drops existing columns/data."""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        log.info("Migrated schema: added column %s.%s", table, column)


def already_seen(conn, doc_hash):
    row = conn.execute("SELECT 1 FROM alerts WHERE doc_hash = ?", (doc_hash,)).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_documents(since_date):
    """Fetch Federal Register docs from configured agencies since since_date (date obj)."""
    all_docs = []
    for agency in AGENCIES:
        p = {
            "per_page": 100,
            "order": "newest",
            "conditions[publication_date][gte]": since_date.isoformat(),
            "conditions[agencies][]": agency,
            "fields[]": FR_FIELDS,
        }
        try:
            resp = requests.get(FR_API, params=p, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            docs = data.get("results", [])
            log.info("Fetched %d docs for agency=%s", len(docs), agency)
            all_docs.extend(docs)
        except requests.RequestException as e:
            log.warning("Fetch failed for agency=%s: %s", agency, e)

    # Dedup across agency queries (a doc can list multiple agencies)
    seen_numbers = set()
    unique = []
    for d in all_docs:
        num = d.get("document_number")
        if num and num not in seen_numbers:
            seen_numbers.add(num)
            unique.append(d)
    return unique


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------

def score_document(doc):
    text = " ".join(filter(None, [
        doc.get("title", ""),
        doc.get("abstract", "") or "",
        " ".join(doc.get("excerpts", []) or []) if isinstance(doc.get("excerpts"), list) else str(doc.get("excerpts", "") or ""),
    ])).lower()
    score = 0
    hits = []
    for kw, weight in KEYWORD_WEIGHTS.items():
        if kw in text:
            score += weight
            hits.append(kw)
    return score, hits


# ---------------------------------------------------------------------------
# LLM analysis
# ---------------------------------------------------------------------------

def analyze_with_llm(doc):
    api_key = os.environ["ANTHROPIC_API_KEY"]
    source_text = json.dumps({
        "title": doc.get("title"),
        "agencies": doc.get("agencies"),
        "type": doc.get("type"),
        "publication_date": doc.get("publication_date"),
        "effective_on": doc.get("effective_on"),
        "citation": doc.get("citation"),
        "html_url": doc.get("html_url"),
        "abstract": doc.get("abstract"),
        "excerpts": doc.get("excerpts"),
    }, indent=2)

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 1500,
            "system": ANALYSIS_SCHEMA_PROMPT,
            "messages": [{"role": "user", "content": source_text}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["content"]
    text = "".join(b["text"] for b in content if b.get("type") == "text")
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _safe_join(items):
    """Coerce list items to strings before joining — LLM output isn't guaranteed
    to give plain strings for entities/authority/etc; sometimes it's a list of dicts."""
    if not items:
        return ""
    out = []
    for i in items:
        if isinstance(i, dict):
            out.append(i.get("name") or i.get("entity") or json.dumps(i, ensure_ascii=False))
        else:
            out.append(str(i))
    return ", ".join(out)


def _safe_actions(items):
    """Same coercion, but returns a list (for iterating), not a joined string."""
    if not items:
        return []
    out = []
    for i in items:
        if isinstance(i, dict):
            out.append(i.get("action") or i.get("name") or json.dumps(i, ensure_ascii=False))
        else:
            out.append(str(i))
    return out


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def format_email(analysis, doc_url, score):
    lines = [
        f"{analysis.get('title', doc_url)}",
        f"Change type: {analysis.get('change_type', 'n/a')}   |   Confidence: {analysis.get('confidence', 'n/a')}   |   Score: {score}",
        "",
        analysis.get("summary") or "",
        "",
        f"Effective date: {analysis.get('effective_date', 'n/a')}",
        f"Authority: {_safe_join(analysis.get('authority', []))}",
        f"Countries: {_safe_join(analysis.get('countries', []))}",
        f"Entities: {_safe_join(analysis.get('entities', []))}",
        f"ECCNs: {_safe_join(analysis.get('eccns', []))}",
        f"EAR sections: {_safe_join(analysis.get('ear_sections', []))}",
        "",
        f"Licensing impact: {analysis.get('licensing_impact') or ''}",
        f"Defense impact: {analysis.get('defense_impact') or ''}",
        f"What did NOT change: {analysis.get('remaining_controls') or ''}",
        "",
        "Follow-up:",
    ]
    for action in _safe_actions(analysis.get("recommended_actions", [])):
        lines.append(f"  - {action}")
    lines += ["", f"Source: {doc_url}"]
    return "\n".join(lines)


def send_email(subject, body):
    user = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to_addr = os.environ.get("ALERT_TO", user)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())
    log.info("Sent email: %s", subject)


# ---------------------------------------------------------------------------
# PDF archive (alert summary PDF — unchanged from bis_watcher.py)
# ---------------------------------------------------------------------------

def _pdf_safe(text, max_token=45):
    """Insert soft break points into any unbroken token longer than max_token
    chars (e.g. a 100+ char federalregister.gov URL with no spaces) so fpdf2's
    word-wrap has somewhere to break — otherwise it errors trying to fit an
    unbreakable word into the line width. Also downgrades Unicode punctuation
    the core Helvetica font can't render (em/en dashes, curly quotes, etc.)."""
    if not text:
        return text
    text = str(text)
    replacements = {
        "—": "-", "–": "-",   # em dash, en dash
        "‘": "'", "’": "'",   # curly single quotes
        "“": '"', "”": '"',   # curly double quotes
        "…": "...",                 # ellipsis
        " ": " ",                   # non-breaking space
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    # Last-resort catch-all for anything else outside Latin-1
    text = text.encode("latin-1", "replace").decode("latin-1")

    def wrap_word(word):
        if len(word) <= max_token:
            return word
        return " ".join(word[i:i + max_token] for i in range(0, len(word), max_token))
    return " ".join(wrap_word(w) for w in text.split(" "))


def load_watch_profiles():
    """Load all watch_profiles/*.json files into {profile_name: [terms]}."""
    profiles = {}
    for path in _glob.glob("watch_profiles/*.json"):
        with open(path) as f:
            data = json.load(f)
        name = data.get("name", path)
        profiles[name] = [t.lower() for t in data.get("terms", [])]
    return profiles

WATCH_PROFILES = load_watch_profiles()

def check_watch_profiles(doc):
    """Check a document against every loaded watch profile, independent of
    the export-control score. Returns the matched profile name, or None."""
    text = " ".join(filter(None, [
        doc.get("title", ""),
        doc.get("abstract", "") or "",
    ])).lower()
    for profile_name, terms in WATCH_PROFILES.items():
        for term in terms:
            if term in text:
                return profile_name
    return None


def save_pdf(analysis, doc_url, score, doc_hash, document_number=None, fetched_at=None):
    """Write a legible PDF of the alert straight to disk. No Gmail round-trip."""
    os.makedirs(PDF_DIR, exist_ok=True)
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 8, _pdf_safe(analysis.get("title") or doc_hash))
    pdf.set_font("Helvetica", "", 10)
    pdf.ln(2)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 6, _pdf_safe(f"Change type: {analysis.get('change_type', 'n/a')}  |  "
                          f"Confidence: {analysis.get('confidence', 'n/a')}  |  Score: {score}"))
    pdf.ln(2)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 6, _pdf_safe(analysis.get("summary") or ""))
    pdf.ln(2)

    def field(label, value):
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 6, label)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 6, _pdf_safe(value) or "n/a")
        pdf.ln(1)

    field("Effective date:", analysis.get("effective_date"))
    field("Authority:", _safe_join(analysis.get("authority", [])))
    field("Countries:", _safe_join(analysis.get("countries", [])))
    field("Entities:", _safe_join(analysis.get("entities", [])))
    field("ECCNs:", _safe_join(analysis.get("eccns", [])))
    field("EAR sections:", _safe_join(analysis.get("ear_sections", [])))
    field("Licensing impact:", analysis.get("licensing_impact"))
    field("Defense impact:", analysis.get("defense_impact"))
    field("What did NOT change:", analysis.get("remaining_controls"))

    pdf.set_font("Helvetica", "B", 10)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 6, "Follow-up:")
    pdf.set_font("Helvetica", "", 10)
    for action in _safe_actions(analysis.get("recommended_actions", [])):
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 6, _pdf_safe(f"  - {action}"))

    pdf.ln(2)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 6, _pdf_safe(f"Source: {doc_url or 'n/a'}"))

    date_prefix = (fetched_at or datetime.now(timezone.utc).isoformat())[:10]
    safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in (analysis.get("title") or doc_hash))[:60]
    doc_id = document_number or doc_hash[:12]
    path = os.path.join(PDF_DIR, f"{date_prefix}_{doc_id}_{safe_title}.pdf")
    pdf.output(path)
    log.info("Saved PDF: %s", path)
    return path


# ---------------------------------------------------------------------------
# NEW (additive, isolated): source-PDF download + fitz full-text extraction
# + regex ECCN matching.
#
# This is the capability ported from regulus_v2.py. It runs only for
# documents that already cleared SCORE_DIGEST_MAX and got LLM analysis, and
# it is wrapped in try/except at every call site in main() so a failure here
# can NEVER break the existing, proven alert/email/PDF pipeline. Results are
# written to ECCN_TEST_DIR as a standalone JSON file per document and to two
# new, nullable DB columns (eccn_regex_matches, eccn_regex_source) — the
# existing LLM-derived `eccns` column is untouched. The two extraction
# methods are meant to be compared later, not merged automatically.
# ---------------------------------------------------------------------------

def is_valid_pdf_url(url):
    """Cheap shape check before spending a network round trip."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        if not all([parsed.scheme, parsed.netloc]):
            return False
        if not url.lower().endswith(".pdf"):
            return False
        return True
    except Exception:
        return False


def download_source_pdf(pdf_url, dest_dir, doc_id):
    """Download a source PDF with a HEAD-request content-type/size check
    before pulling the body, so a mislabeled link or an oversized file can't
    hang or blow up disk. Returns the local path, or None if skipped/failed."""
    if not is_valid_pdf_url(pdf_url):
        log.info("Skipping PDF download for %s: URL failed shape check (%s)", doc_id, pdf_url)
        return None

    try:
        head = requests.head(pdf_url, timeout=15, allow_redirects=True)
        content_type = head.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower():
            log.info("Skipping PDF download for %s: Content-Type=%r not a PDF", doc_id, content_type)
            return None
        content_length = head.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_PDF_SIZE_MB * 1024 * 1024:
            log.info("Skipping PDF download for %s: %s bytes exceeds %d MB cap",
                      doc_id, content_length, MAX_PDF_SIZE_MB)
            return None
    except requests.RequestException as e:
        log.warning("HEAD check failed for %s (%s): %s", doc_id, pdf_url, e)
        return None

    try:
        resp = requests.get(pdf_url, timeout=60, stream=True)
        resp.raise_for_status()
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, f"{doc_id}.pdf")
        size = 0
        cap_bytes = MAX_PDF_SIZE_MB * 1024 * 1024
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                size += len(chunk)
                if size > cap_bytes:
                    log.warning("Aborting download for %s: exceeded %d MB cap mid-stream", doc_id, MAX_PDF_SIZE_MB)
                    f.close()
                    os.remove(path)
                    return None
                f.write(chunk)
        return path
    except requests.RequestException as e:
        log.warning("PDF download failed for %s (%s): %s", doc_id, pdf_url, e)
        return None


def extract_pdf_text(pdf_path):
    """Full-text extraction via PyMuPDF. Returns "" on any failure rather
    than raising — this path must never be able to take down main()."""
    try:
        import pymupdf as fitz
    except ImportError:
        log.warning("pymupdf not installed — skipping full-text extraction. "
                     "Install with: pip install pymupdf --break-system-packages")
        return ""
    try:
        text_parts = []
        with fitz.open(pdf_path) as doc:
            for page in doc:
                text_parts.append(page.get_text())
        return "\n".join(text_parts)
    except Exception as e:
        log.warning("fitz extraction failed for %s: %s", pdf_path, e)
        return ""


def extract_eccns_regex(text):
    """Regex-based ECCN extraction over full document text. Deduped,
    order-preserving. This is intentionally dumb pattern matching — it will
    catch ECCNs cited for historical/background reasons (e.g. an amended
    CFR section referencing an old classification), not just ECCNs newly
    controlled by this document. That's the known limitation this test path
    exists to characterize, not fix, before it's cross-validated against the
    LLM's `eccns` field."""
    if not text:
        return []
    seen = []
    for m in ECCN_PATTERN.finditer(text):
        val = m.group(0).upper()
        if val not in seen:
            seen.append(val)
    return seen


def run_eccn_regex_test(doc, doc_hash, document_number):
    """Best-effort: download source PDF, extract text, regex-match ECCNs,
    write a standalone JSON result to ECCN_TEST_DIR, and return a dict of
    the two new DB column values (or None, None on any failure/skip).
    Never raises — every internal step is already defensive, but this
    wrapper is the backstop main() actually relies on."""
    pdf_url = doc.get("pdf_url")
    doc_id = document_number or doc_hash[:12]

    try:
        local_pdf = download_source_pdf(pdf_url, os.path.join(ECCN_TEST_DIR, "raw_pdfs"), doc_id)
        if not local_pdf:
            return None, None

        text = extract_pdf_text(local_pdf)
        matches = extract_eccns_regex(text)

        os.makedirs(ECCN_TEST_DIR, exist_ok=True)
        result = {
            "document_number": document_number,
            "doc_hash": doc_hash,
            "pdf_url": pdf_url,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "eccn_regex_matches": matches,
            "full_text_chars": len(text),
        }
        result_path = os.path.join(ECCN_TEST_DIR, f"{doc_id}.json")
        with open(result_path, "w") as f:
            json.dump(result, f, indent=2)
        log.info("ECCN regex test: %d match(es) for %s -> %s", len(matches), doc_id, result_path)

        return json.dumps(matches), pdf_url
    except Exception as e:
        log.error("ECCN regex test path failed for %s (non-fatal, pipeline continues): %s", doc_id, e)
        return None, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    conn = get_db()

    last_run = conn.execute(
        "SELECT MAX(fetched_at) FROM alerts"
    ).fetchone()[0]
    if last_run:
        since = datetime.fromisoformat(last_run).date() - timedelta(days=1)  # 1-day overlap, dedup handles it
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).date()

    docs = fetch_documents(since)
    log.info("Total unique documents fetched: %d", len(docs))

    for doc in docs:
        doc_number = doc.get("document_number", "")
        doc_hash = hashlib.sha256(doc_number.encode()).hexdigest()

        if already_seen(conn, doc_hash):
            continue

        score, hits = score_document(doc)
        watch_match = check_watch_profiles(doc)
        now = datetime.now(timezone.utc).isoformat()

        row = {
            "doc_hash": doc_hash,
            "document_number": doc_number,
            "title": doc.get("title"),
            "agency": json.dumps(doc.get("agencies")),
            "pub_date": doc.get("publication_date"),
            "effective_date": doc.get("effective_on"),
            "primary_source_url": doc.get("html_url"),
            "score": score,
            "raw_excerpt": doc.get("abstract") or "",
            "fetched_at": now,
            "source_retrieved_at": now,
            "watch_match": watch_match,
        }

        if watch_match:
            try:
                subject = f"[Watch: {watch_match}] {doc.get('title', doc_number)[:100]}"
                body = f"Matched watch profile: {watch_match}\n\nTitle: {doc.get('title')}\n\nAbstract: {doc.get('abstract') or 'n/a'}\n\nSource: {doc.get('html_url')}"
                send_email(subject, body)
            except Exception as e:
                log.error("Watch-profile email failed for %s: %s", doc_number, e)

        analysis = None
        if score > SCORE_DIGEST_MAX:
            try:
                analysis = analyze_with_llm(doc)
            except Exception as e:
                log.error("LLM analysis failed for %s: %s", doc_number, e)
                analysis = None  # still persist raw doc below — don't lose it

        if analysis:
            row.update({
                "authority": json.dumps(analysis.get("authority", [])),
                "countries": json.dumps(analysis.get("countries", [])),
                "entities": json.dumps(analysis.get("entities", [])),
                "eccns": json.dumps(analysis.get("eccns", [])),
                "ear_sections": json.dumps(analysis.get("ear_sections", [])),
                "change_type": analysis.get("change_type"),
                "summary": analysis.get("summary"),
                "licensing_impact": analysis.get("licensing_impact"),
                "defense_impact": analysis.get("defense_impact"),
                "remaining_controls": analysis.get("remaining_controls"),
                "recommended_actions": json.dumps(analysis.get("recommended_actions", [])),
                "confidence": analysis.get("confidence"),
                "analysis_model": "claude-sonnet-4-6",
                "analysis_generated_at": datetime.now(timezone.utc).isoformat(),
            })

        # NEW: additive regex/fitz ECCN test path — only for documents that
        # crossed the analysis threshold, and never allowed to affect the
        # row above (built before this runs) or raise past this point.
        if score > SCORE_DIGEST_MAX:
            try:
                regex_matches_json, regex_source = run_eccn_regex_test(doc, doc_hash, doc_number)
                if regex_matches_json is not None:
                    row["eccn_regex_matches"] = regex_matches_json
                    row["eccn_regex_source"] = regex_source
            except Exception as e:
                log.error("ECCN regex test dispatch failed for %s (non-fatal): %s", doc_number, e)

        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO alerts ({cols}) VALUES ({placeholders})", list(row.values()))
        conn.commit()

        if analysis and score > SCORE_DIGEST_MAX:
            try:
                save_pdf(analysis, doc.get("html_url"), score, doc_hash, doc.get("document_number"), now)
            except Exception as e:
                log.error("PDF save failed for %s: %s", doc_number, e)
            try:
                subject = f"[Export Control Alert] {analysis.get('title', doc_number)[:100]}"
                body = format_email(analysis, doc.get("html_url"), score)
                send_email(subject, body)
                conn.execute(
                    "UPDATE alerts SET emailed_at = ? WHERE doc_hash = ?",
                    (datetime.now(timezone.utc).isoformat(), doc_hash),
                )
                conn.commit()
            except Exception as e:
                log.error("Email send failed for %s: %s", doc_number, e)
                # row stays with emailed_at NULL -> retried below

    # Retry any material alerts that were stored but never successfully emailed
    unsent = conn.execute(
        "SELECT doc_hash, document_number, fetched_at, title, summary, effective_date, authority, countries, "
        "entities, eccns, ear_sections, licensing_impact, defense_impact, "
        "remaining_controls, recommended_actions, primary_source_url, confidence, "
        "change_type, score FROM alerts WHERE score > ? AND emailed_at IS NULL",
        (SCORE_DIGEST_MAX,),
    ).fetchall()

    for r in unsent:
        (doc_hash, document_number, fetched_at, title, summary, effective_date, authority, countries, entities,
         eccns, ear_sections, licensing_impact, defense_impact, remaining_controls,
         recommended_actions, url, confidence, change_type, score) = r
        analysis = {
            "title": title, "summary": summary, "effective_date": effective_date,
            "authority": json.loads(authority or "[]"), "countries": json.loads(countries or "[]"),
            "entities": json.loads(entities or "[]"), "eccns": json.loads(eccns or "[]"),
            "ear_sections": json.loads(ear_sections or "[]"), "licensing_impact": licensing_impact,
            "defense_impact": defense_impact, "remaining_controls": remaining_controls,
            "recommended_actions": json.loads(recommended_actions or "[]"),
            "confidence": confidence, "change_type": change_type,
        }
        try:
            save_pdf(analysis, url, score, doc_hash, document_number, fetched_at)
        except Exception as e:
            log.error("Retry PDF save failed for %s: %s", doc_hash, e)
        try:
            subject = f"[Export Control Alert] {(title or doc_hash)[:100]}"
            send_email(subject, format_email(analysis, url, score))
            conn.execute(
                "UPDATE alerts SET emailed_at = ? WHERE doc_hash = ?",
                (datetime.now(timezone.utc).isoformat(), doc_hash),
            )
            conn.commit()
        except Exception as e:
            log.error("Retry email failed for %s: %s", doc_hash, e)

    conn.close()


if __name__ == "__main__":
    main()
