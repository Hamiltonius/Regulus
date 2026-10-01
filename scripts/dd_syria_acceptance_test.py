#!/usr/bin/env python3
"""
scripts/dd_syria_acceptance_test.py — live, isolated acceptance-test runner
for the feature/dd-foundation DD pipeline, against one real document.

WHAT THIS RUNS
--------------
The actual, unmodified production implementation, end to end, for exactly
one Federal Register document:

    Stage 1    regulus_v3.analyze_with_llm            (real Anthropic call)
    Gate       dd_pipeline.needs_due_diligence         (deterministic, no LLM)
    Stage 2    dd_pipeline.call_anthropic_stage2       (real Anthropic call,
               via dd_pipeline.due_diligence_review)     with web_search tool
    Validate   dd_schema.validate_stage2_record        (deterministic)
    Stage 3    dd_pipeline.call_anthropic_stage3       (real Anthropic call,
               via dd_pipeline.synthesize_final)
    Validate   dd_schema.validate_stage3_record        (deterministic)
    C2 check   dd_schema.validate_stage3_sources       (deterministic)

No test doubles or canned Stage 2/Stage 3 responses are used anywhere in
this script. The only wrapping applied to call_anthropic_stage2/3 is a
transparent timing shim (see _timed_call_stage2/3 below) that calls the
real function and returns its real, unmodified result.

This script does not alter any DD prompt or implementation file, does not
retry to get a "nicer" answer, and does not change behavior based on what
comes back. If Stage 2 or Stage 3 produces an invalid record, that record
is preserved in the diagnostic report exactly as generated.

ISOLATION GUARANTEES
---------------------
  - DB_PATH is forced to a fresh temp sqlite file BEFORE regulus_v3 is
    imported (regulus_v3.DB_PATH is resolved at import time), regardless
    of any DB_PATH already exported in your shell. bis_watcher.db is
    never opened, read, or written by this script.
  - No email is sent — regulus_v3.send_email is never imported or called.
  - No production PDFs are written — save_pdf/save_pdf_dd are never
    called.
  - The `alerts` table and regulus_v3.main() are never touched or called.
  - ANTHROPIC_API_KEY is read from the environment only (never a CLI
    flag, never a prompt, never a default). It is never printed, logged,
    or written into the diagnostic report or the temp database.
  - The temp sqlite file is deleted after the run unless --keep-db is
    passed.

WHAT IT DOES NOT DO
--------------------
  - Does not modify prompts or implementation based on the result.
  - Does not deploy anything or touch node02 configuration.
  - Does not merge or interact with any pull request.

USAGE — run manually, on node02, from the repo root:

    cd /path/to/regulus
    git fetch origin
    git checkout feature/dd-foundation
    git pull

    ANTHROPIC_API_KEY=sk-ant-... \\
      python3 scripts/dd_syria_acceptance_test.py \\
      --document-number 2026-XXXXX

  If you only have the Federal Register URL:

    ANTHROPIC_API_KEY=sk-ant-... \\
      python3 scripts/dd_syria_acceptance_test.py \\
      --url "https://www.federalregister.gov/documents/2026/xx/xx/2026-XXXXX/title-slug"

  Fully offline against a document you already saved as JSON (skips the
  Federal Register fetch; the JSON must contain at least document_number,
  title, agencies, type, publication_date, effective_on, html_url,
  abstract):

    ANTHROPIC_API_KEY=sk-ant-... \\
      python3 scripts/dd_syria_acceptance_test.py \\
      --document-json /path/to/saved_fr_document.json

  Optional flags:
    --out-dir DIR   where the diagnostic report JSON is written
                    (default: diagnostics/dd_acceptance/ under repo root)
    --keep-db       keep the temp sqlite file for manual inspection
                    instead of deleting it at the end (path is printed)

The diagnostic report (JSON) is written to --out-dir and a condensed
summary is printed to stdout. Nothing about ANTHROPIC_API_KEY is ever
included in either.
"""

import os
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Isolation: DB_PATH must be forced to a private temp file BEFORE regulus_v3
# is imported, because regulus_v3.DB_PATH is a module-level global resolved
# at import time from the environment. This happens unconditionally, so a
# DB_PATH already exported in the shell (e.g. node02's production env) can
# never cause this script to touch the real database.
# ---------------------------------------------------------------------------
_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="regulus_dd_acceptance_", suffix=".sqlite3")
os.close(_TMP_DB_FD)
os.environ["DB_PATH"] = _TMP_DB_PATH

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import argparse          # noqa: E402
import hashlib            # noqa: E402
import json               # noqa: E402
import re                 # noqa: E402
import threading          # noqa: E402
import time               # noqa: E402
import urllib.parse       # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import requests           # noqa: E402

import regulus_v3         # noqa: E402
import dd_pipeline        # noqa: E402
import dd_schema as schema  # noqa: E402

API_KEY_ENV = "ANTHROPIC_API_KEY"
FR_SINGLE_DOC_API = "https://www.federalregister.gov/api/v1/documents/{}.json"


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

def require_api_key() -> str:
    """Read ANTHROPIC_API_KEY from the environment only. Fail immediately,
    before any network call, if it's absent. Never accepts it any other
    way (CLI flag, prompt, config file, default)."""
    key = os.environ.get(API_KEY_ENV)
    if not key:
        print(
            f"ERROR: {API_KEY_ENV} is not set in the environment.\n"
            f"This script reads it from the environment ONLY — it will not "
            f"prompt for it or accept it as a command-line argument.\n"
            f"Export it and re-run, e.g.:\n"
            f"  export {API_KEY_ENV}=sk-ant-...\n",
            file=sys.stderr,
        )
        sys.exit(2)
    return key


# ---------------------------------------------------------------------------
# Document acquisition
# ---------------------------------------------------------------------------

def parse_document_number_from_url(url: str) -> str:
    m = re.search(r"/(\d{4}-\d{4,6})(?:/|$)", url)
    if not m:
        raise ValueError(
            f"Could not find a Federal Register document number (e.g. 2026-12345) in URL: {url}"
        )
    return m.group(1)


def fetch_document_by_number(document_number: str) -> dict:
    """Real fetch against the Federal Register single-document API, using
    the same field set regulus_v3.fetch_documents() uses."""
    resp = requests.get(
        FR_SINGLE_DOC_API.format(document_number),
        params={"fields[]": regulus_v3.FR_FIELDS},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def load_document_json(path: str) -> dict:
    with open(path) as f:
        doc = json.load(f)
    if "document_number" not in doc:
        raise ValueError(f"{path} has no document_number field.")
    return doc


# ---------------------------------------------------------------------------
# Gate explanation (reporting only — does not affect the real gate decision)
# ---------------------------------------------------------------------------

def explain_gate(analysis: dict, doc: dict) -> list:
    """Human-readable list of which of dd_pipeline.needs_due_diligence's
    OR-conditions matched. Reads the same public constants that function
    uses (dd_pipeline.HIGH_IMPACT_ACTIONS / HIGH_CONTEXT_JURISDICTIONS).
    This is purely explanatory: the authoritative decision is whatever
    dd_pipeline.needs_due_diligence() itself returns, computed separately
    in main() below — this function never feeds back into that result."""
    analysis = analysis or {}
    doc = doc or {}
    reasons = []

    if analysis.get("confidence") != "High":
        reasons.append(f"stage1 confidence != 'High' (got {analysis.get('confidence')!r})")

    if analysis.get("change_type") in dd_pipeline.HIGH_IMPACT_ACTIONS:
        reasons.append(f"stage1 change_type {analysis.get('change_type')!r} is a HIGH_IMPACT_ACTION")

    countries = [str(c).lower() for c in (analysis.get("countries") or [])]
    matched_countries = [j for j in dd_pipeline.HIGH_CONTEXT_JURISDICTIONS if j in countries]
    if matched_countries:
        reasons.append(f"stage1 countries include high-context jurisdiction(s): {matched_countries}")

    if analysis.get("unresolved_questions"):
        reasons.append(
            f"stage1 unresolved_questions is non-empty "
            f"({len(analysis.get('unresolved_questions'))} item(s))"
        )

    raw_text = " ".join(filter(None, [doc.get("title", "") or "", doc.get("abstract", "") or ""])).lower()
    matched_raw = [j for j in dd_pipeline.HIGH_CONTEXT_JURISDICTIONS if j in raw_text]
    if matched_raw:
        reasons.append(f"raw document title/abstract mentions high-context jurisdiction(s): {matched_raw}")

    return reasons


# ---------------------------------------------------------------------------
# Terminal activity indicator (presentation only).
#
# Stage 2 research calls can legitimately run for several minutes (20000
# max_tokens, 5-6 web_search rounds, 600s read timeout). This is purely
# cosmetic: it writes to the terminal on a background thread and has no
# effect on control flow, retries, timing capture, or any captured
# diagnostic data. It lives only in this script, never in dd_pipeline.py.
# ---------------------------------------------------------------------------

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _format_elapsed(seconds: float) -> str:
    """MM:SS only -- no hour rollover, no percentage/progress estimate
    (there is nothing to estimate against; we don't know how long a
    given Stage 2 call will take)."""
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


class Spinner:
    """A one-line animated spinner with elapsed MM:SS, for use as a
    context manager around a single blocking call:

        with Spinner("Researching regulatory history"):
            result = some_blocking_call()

    Starts immediately on __enter__, stops and clears its line on
    __exit__ -- on success, on an exception (re-raised unchanged), or
    simply because the caller chose to wrap only one retry attempt at a
    time. Runs on a daemon background thread so a failure to join on
    stop() can never hang the process.

    Writes to stderr by default so it never mixes into anything a
    caller might capture from stdout. Skips the animation entirely when
    the stream isn't a terminal (redirected to a file, captured by a
    test runner, non-interactive) -- there's nothing useful to animate
    for a log file, and this keeps every test deterministic: no thread,
    no timer, no real-time dependency, when output isn't a tty.
    """

    def __init__(self, label: str, interval: float = 0.1, stream=None):
        self.label = label
        self.interval = interval
        self.stream = stream if stream is not None else sys.stderr
        self._stop_event = threading.Event()
        self._thread = None
        self._start_time = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False  # never swallow an exception from the wrapped call

    def start(self):
        is_tty = getattr(self.stream, "isatty", lambda: False)()
        if not is_tty:
            return
        self._start_time = time.monotonic()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        i = 0
        while not self._stop_event.is_set():
            elapsed = _format_elapsed(time.monotonic() - self._start_time)
            frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
            line = f"\r{frame} {self.label} │ {elapsed} elapsed "
            try:
                self.stream.write(line)
                self.stream.flush()
            except Exception:
                # Presentation-only: a write failure here must never
                # surface as a pipeline error. Just stop animating.
                return
            i += 1
            self._stop_event.wait(self.interval)

    def stop(self):
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=2)
        self._thread = None
        try:
            # Clear the line so retry/warning output below it is clean.
            self.stream.write("\r" + " " * (len(self.label) + 40) + "\r")
            self.stream.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Timed, transparent wrappers around the REAL Stage 2/3 calls. These do not
# alter behavior or output in any way — they exist only to capture elapsed
# wall-clock time (diagnostic report item 12) around the actual functions.
# ---------------------------------------------------------------------------

_timing = {}


def _timed_call_stage2(doc, analysis, api_key):
    t0 = time.monotonic()
    result = dd_pipeline.call_anthropic_stage2(doc, analysis, api_key)
    _timing["stage2_seconds"] = time.monotonic() - t0
    return result


def _timed_call_stage3(analysis, dd_record, api_key):
    t0 = time.monotonic()
    result = dd_pipeline.call_anthropic_stage3(analysis, dd_record, api_key)
    _timing["stage3_seconds"] = time.monotonic() - t0
    return result


# ---------------------------------------------------------------------------
# Supplementary, non-authoritative grounding check (report item 10).
#
# This is NOT the spec's C2 source-reuse check (that's
# dd_schema.validate_stage3_sources, run separately and authoritatively
# below). It is a best-effort, deterministic aid for a human reviewer:
# it flags proper-noun/number/citation-shaped phrases in Stage 3's text
# that do not appear anywhere (case-insensitive substring) in the Stage 2
# JSON blob. A flagged phrase is not proof of fabrication (Stage 3 is
# allowed to paraphrase), and an unflagged phrase is not proof of
# grounding. It never alters validation_status, confidence, or
# persistence — reporting only.
# ---------------------------------------------------------------------------

_CANDIDATE_PHRASE_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9\-]{2,}(?:\s+[A-Z][A-Za-z0-9\-]{2,}){0,3}\b"
    r"|\b\d{4}\b"
    r"|\b\d{1,2}\s?(?:CFR|U\.S\.C\.|USC)\b"
)


def _stage3_text_fields(stage3_raw: dict) -> list:
    if not isinstance(stage3_raw, dict):
        return []
    texts = []
    for f in ("headline", "bottom_line", "what_changed", "why_it_matters",
              "historical_significance", "what_did_not_change"):
        v = stage3_raw.get(f)
        if isinstance(v, str):
            texts.append(v)
    for f in ("compliance_attention", "watch_next"):
        v = stage3_raw.get(f)
        if isinstance(v, list):
            texts.extend(str(x) for x in v)
    return texts


def check_ungrounded_claims(stage3_raw, stage2_raw) -> dict:
    if not isinstance(stage2_raw, dict):
        return {
            "authoritative": False,
            "note": "Stage 2 result was not a dict; grounding check skipped.",
            "flagged_phrases": [],
        }
    corpus = json.dumps(stage2_raw, ensure_ascii=False).lower()
    flagged, seen = [], set()
    for text in _stage3_text_fields(stage3_raw):
        for m in _CANDIDATE_PHRASE_RE.finditer(text):
            phrase = m.group(0).strip()
            if len(phrase) < 4 or phrase.lower() in seen:
                continue
            if phrase.lower() not in corpus:
                seen.add(phrase.lower())
                flagged.append({"phrase": phrase, "found_in_stage3_text": text})
    return {
        "authoritative": False,
        "method": (
            "case-insensitive substring search of candidate proper-noun/number/"
            "citation phrases pulled from Stage 3 text fields against the full "
            "Stage 2 JSON blob"
        ),
        "caveat": (
            "A flagged phrase is NOT proof of fabrication (Stage 3 may legitimately "
            "paraphrase Stage 2 evidence); an unflagged phrase is NOT proof of "
            "grounding. This supplements, and does not replace, the authoritative "
            "C2 source-reuse check (dd_schema.validate_stage3_sources)."
        ),
        "flagged_phrases": flagged,
    }


# ---------------------------------------------------------------------------
# "SOURCES USED" summary (presentation/observability only).
#
# Reads ONLY the already-validated Stage 2 `sources` array -- no new web
# calls, no LLM classification, no reputation scoring, no persistence or
# schema of any kind. Just: extract each source's hostname, deduplicate,
# and show whether any record from that host was marked primary_source.
# ---------------------------------------------------------------------------

def _extract_hostname(url: str) -> str:
    """Best-effort hostname extraction for display purposes only (e.g.
    'https://www.federalregister.gov/d/2025-00001' -> 'federalregister.gov').
    Strips a leading 'www.' for readability. Returns '' for anything that
    doesn't parse to a usable host -- callers skip those rather than
    inventing a placeholder domain."""
    if not isinstance(url, str) or not url:
        return ""
    try:
        netloc = urllib.parse.urlparse(url).netloc
    except ValueError:
        return ""
    host = netloc.split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def summarize_sources_used(stage2_sources: list) -> list:
    """Deterministic summary of the Stage 2 sources array: one entry per
    unique hostname, in the order first seen, with:
      - primary: True if ANY source record from that host has
        primary_source == True (primary takes precedence over any
        secondary record from the same host)
      - count: how many Stage 2 source records came from that host

    Returns a list of {"domain", "primary", "count"} dicts sorted with
    PRIMARY hosts first, then by source-record count (descending) within
    each group, then alphabetically as a tiebreak -- matching the
    requested display order. Non-dict entries and URLs that don't yield a
    hostname are skipped (not counted, not shown)."""
    by_domain = {}
    order = []
    for s in stage2_sources or []:
        if not isinstance(s, dict):
            continue
        domain = _extract_hostname(s.get("url"))
        if not domain:
            continue
        if domain not in by_domain:
            by_domain[domain] = {"domain": domain, "primary": False, "count": 0}
            order.append(domain)
        by_domain[domain]["count"] += 1
        if s.get("primary_source"):
            by_domain[domain]["primary"] = True
    rows = [by_domain[d] for d in order]
    rows.sort(key=lambda r: (not r["primary"], -r["count"], r["domain"]))
    return rows


def format_sources_used(rows: list) -> str:
    """Pure formatting of summarize_sources_used()'s output into the
    requested fixed-width 'SOURCES USED' block. No computation here."""
    width = 40
    lines = ["SOURCES USED", "─" * width]
    for r in rows:
        marker = "✓" if r["primary"] else "•"
        tier = "PRIMARY" if r["primary"] else "SECONDARY"
        noun = "source" if r["count"] == 1 else "sources"
        lines.append(f"{marker} {r['domain']:<26}{tier:<11} {r['count']} {noun}")
    lines.append("─" * width)
    lines.append(f"{len(rows)} unique website{'s' if len(rows) != 1 else ''} used")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Live, isolated acceptance test for the DD pipeline against one real document.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--document-number", help="Federal Register document number, e.g. 2026-12345")
    src.add_argument("--url", help="Federal Register document URL")
    src.add_argument("--document-json", help="Path to a saved Federal Register document JSON file")
    p.add_argument(
        "--out-dir", default=str(_REPO_ROOT / "diagnostics" / "dd_acceptance"),
        help="Directory to write the diagnostic report JSON (default: diagnostics/dd_acceptance/)",
    )
    p.add_argument(
        "--keep-db", action="store_true",
        help="Keep the temp sqlite database after the run instead of deleting it",
    )
    return p


def main() -> int:
    args = build_arg_parser().parse_args()
    api_key = require_api_key()

    report = {
        "script": "scripts/dd_syria_acceptance_test.py",
        "run_started_at": datetime.now(timezone.utc).isoformat(),
        "isolation": {
            "db_path_used": _TMP_DB_PATH,
            "production_db_touched": False,
            "email_sent": False,
            "production_pdfs_written": False,
        },
        "api_model_identifiers": {
            "stage1_model": "claude-sonnet-4-6",  # matches regulus_v3.analyze_with_llm's inline model string
            "stage2_model": dd_pipeline.STAGE2_MODEL,
            "stage3_model": dd_pipeline.STAGE3_MODEL,
        },
    }

    # -- Document acquisition -------------------------------------------------
    try:
        if args.document_json:
            doc = load_document_json(args.document_json)
            report["document_source"] = f"local file: {args.document_json}"
        else:
            document_number = args.document_number or parse_document_number_from_url(args.url)
            doc = fetch_document_by_number(document_number)
            report["document_source"] = f"Federal Register API: {FR_SINGLE_DOC_API.format(document_number)}"
    except Exception as e:
        print(f"ERROR: could not obtain the source document: {e}", file=sys.stderr)
        _cleanup(args.keep_db)
        return 3

    document_number = doc.get("document_number")
    if not document_number:
        print("ERROR: source document has no document_number field.", file=sys.stderr)
        _cleanup(args.keep_db)
        return 3
    doc_hash = hashlib.sha256(document_number.encode()).hexdigest()
    report["document_number"] = document_number
    report["doc_hash"] = doc_hash
    report["source_document"] = doc

    # -- Stage 1 ---------------------------------------------------------------
    print(f"[1/6] Stage 1 analysis for {document_number} ...")
    t0 = time.monotonic()
    try:
        analysis = regulus_v3.analyze_with_llm(doc)
    except Exception as e:
        report["stage1_error"] = str(e)
        _write_report(report, args.out_dir, document_number)
        print(f"ERROR: Stage 1 call failed: {e}", file=sys.stderr)
        _cleanup(args.keep_db)
        return 4
    report["stage1_elapsed_seconds"] = time.monotonic() - t0
    report["stage1_result"] = analysis

    # -- Gate --------------------------------------------------------------
    print("[2/6] Gate decision ...")
    gate_decision = dd_pipeline.needs_due_diligence(analysis, doc)
    report["gate"] = {
        "needs_due_diligence": gate_decision,
        "triggering_conditions": explain_gate(analysis, doc),
    }

    if not gate_decision:
        print("Gate decision: NO — this document would not escalate to DD under the real pipeline.")
        report["run_finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_report(report, args.out_dir, document_number)
        _cleanup(args.keep_db)
        return 0

    print(f"Gate decision: YES — {report['gate']['triggering_conditions']}")

    # -- DB (isolated temp sqlite; real schema via regulus_v3.get_db) ----------
    conn = regulus_v3.get_db()

    # -- Stage 2 -----------------------------------------------------------
    print("[3/6] Stage 2 research (real Anthropic call, web_search enabled) ...")
    stage2_raw, stage2_call_error = None, None
    stage2_failure_diagnostics = []
    for attempt in range(1, dd_pipeline.STAGE2_MAX_ATTEMPTS + 1):
        try:
            with Spinner("Researching regulatory history"):
                stage2_raw = dd_pipeline.due_diligence_review(
                    doc, analysis, api_key=api_key, call_stage2=_timed_call_stage2
                )
            stage2_call_error = None
            break
        except dd_pipeline.Stage2JSONDecodeError as e:
            # Diagnostic-only path: preserve exactly what the model/response
            # produced for this failed attempt (raw text passed to
            # json.loads(), the full content-block array, non-secret
            # response metadata) without repairing or re-parsing it. The
            # retry/failure semantics below are identical to the plain
            # Exception branch -- stage2_call_error is still just str(e).
            stage2_call_error = str(e)
            diag_path = _write_stage2_failure_diagnostic(
                out_dir=args.out_dir, document_number=document_number,
                attempt=attempt, error=e,
            )
            stage2_failure_diagnostics.append(diag_path)
            print(f"  [warn] Stage 2 attempt {attempt}/{dd_pipeline.STAGE2_MAX_ATTEMPTS} failed JSON "
                  f"parsing (raw response preserved at {diag_path}): {e}", file=sys.stderr)
        except Exception as e:
            stage2_call_error = str(e)
            print(f"  [warn] Stage 2 attempt {attempt}/{dd_pipeline.STAGE2_MAX_ATTEMPTS} failed: {e}",
                  file=sys.stderr)

    report["stage2_elapsed_seconds"] = _timing.get("stage2_seconds")
    report["stage2_failure_diagnostics"] = stage2_failure_diagnostics

    if stage2_call_error is not None:
        dd_pipeline.persist_due_diligence(
            conn, document_number=document_number, doc_hash=doc_hash,
            dd_json_raw={"error": f"stage2_call_failed: {stage2_call_error}"},
            validation_status="invalid",
            validation_errors=[f"stage2_call_failed: {stage2_call_error}"],
            research_status=dd_pipeline.RESEARCH_STATUS_ERROR, confidence="Low",
            model=dd_pipeline.STAGE2_MODEL,
        )
        report["stage2_result"] = None
        report["stage2_validation"] = {"validation_status": "invalid",
                                        "validation_errors": [f"stage2_call_failed: {stage2_call_error}"]}
        report["failure_reason"] = "stage2_call_failed"
        report["run_finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_report(report, args.out_dir, document_number)
        conn.close()
        _cleanup(args.keep_db)
        return 5

    report["stage2_result"] = stage2_raw

    # -- Stage 2 validation --------------------------------------------------
    print("[4/6] Stage 2 structural validation ...")
    # BLOCKING, matching the real pipeline (dd_pipeline.run_due_diligence):
    # source-identity validation is now folded into structural validity via
    # validate_stage2_record_and_identity -- a current-event source whose
    # encoded FR/GovInfo document number conflicts with this run's target
    # document now makes Stage 2 invalid. Historical sources are
    # unaffected (see that function's docstring).
    stage2_validation = schema.validate_stage2_record_and_identity(stage2_raw, document_number)
    report["stage2_validation"] = {
        "validation_status": stage2_validation.validation_status,
        "validation_errors": stage2_validation.validation_errors,
    }
    report["research_status"] = stage2_validation.research_status
    report["due_diligence_confidence"] = stage2_validation.due_diligence_confidence
    report["stage2_sources"] = [
        {
            "url": s.get("url") if isinstance(s, dict) else s,
            "source_type": s.get("source_type") if isinstance(s, dict) else None,
            "agency": s.get("agency") if isinstance(s, dict) else None,
            "date": s.get("date") if isinstance(s, dict) else None,
            "primary_source": s.get("primary_source") if isinstance(s, dict) else None,
            "supports": s.get("supports") if isinstance(s, dict) else None,
        }
        for s in (stage2_raw.get("sources") or []) if isinstance(stage2_raw, dict)
    ]

    # Informational only (post go-live audit hardening, commit c41fd91):
    # flags any current-event-supporting source whose own embedded
    # Federal Register/GovInfo document number conflicts with THIS run's
    # target document_number (e.g. a source citing 2026-18984 offered as
    # evidence for 2026-18918). Deterministic, no LLM, no new web calls.
    # Never alters validation_status, the gate decision, or what gets
    # persisted -- same reporting-only relationship to the pipeline that
    # ungrounded_claims_check (below) already has.
    report["source_identity_validation"] = {
        "note": (
            "informational only -- does not affect validation_status, the gate, "
            "or persistence; flags a current-event source whose encoded FR/GovInfo "
            "document number conflicts with this run's target document_number"
        ),
        "errors": schema.validate_stage2_source_identities(
            stage2_raw.get("sources", []) if isinstance(stage2_raw, dict) else [],
            document_number,
        ),
    }

    dd_id = dd_pipeline.persist_due_diligence(
        conn, document_number=document_number, doc_hash=doc_hash, dd_json_raw=stage2_raw,
        validation_status=stage2_validation.validation_status,
        validation_errors=stage2_validation.validation_errors,
        research_status=stage2_validation.research_status or dd_pipeline.RESEARCH_STATUS_ERROR,
        confidence=stage2_validation.due_diligence_confidence or "Low",
        model=dd_pipeline.STAGE2_MODEL,
    )
    report["dd_id"] = dd_id

    if not stage2_validation.is_valid:
        print("Stage 2 INVALID — per spec, Stage 3 is never invoked. Preserving result as-is.")
        report["failure_reason"] = "stage2_invalid"
        report["run_finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_report(report, args.out_dir, document_number)
        conn.close()
        _cleanup(args.keep_db)
        return 6

    # -- Stage 3 -----------------------------------------------------------
    print("[5/6] Stage 3 synthesis (real Anthropic call, no tools) ...")
    stage3_raw, stage3_call_error = None, None
    stage3_failure_diagnostics = []
    for attempt in range(1, dd_pipeline.STAGE3_MAX_ATTEMPTS + 1):
        try:
            with Spinner("Synthesizing executive intelligence"):
                stage3_raw = dd_pipeline.synthesize_final(
                    analysis, stage2_raw, api_key=api_key, call_stage3=_timed_call_stage3
                )
            stage3_call_error = None
            break
        except dd_pipeline.Stage3JSONDecodeError as e:
            # Diagnostic-only path, mirroring the Stage 2 branch above:
            # preserve exactly what the model/response produced for this
            # failed attempt (raw text passed to json.loads(), the full
            # content-block array, non-secret response metadata) without
            # repairing or re-parsing it. Retry/failure semantics below are
            # identical to the plain Exception branch -- stage3_call_error
            # is still just str(e).
            stage3_call_error = str(e)
            diag_path = _write_stage3_failure_diagnostic(
                out_dir=args.out_dir, document_number=document_number,
                attempt=attempt, error=e,
            )
            stage3_failure_diagnostics.append(diag_path)
            print(f"  [warn] Stage 3 attempt {attempt}/{dd_pipeline.STAGE3_MAX_ATTEMPTS} failed JSON "
                  f"parsing (raw response preserved at {diag_path}): {e}", file=sys.stderr)
        except Exception as e:
            stage3_call_error = str(e)
            print(f"  [warn] Stage 3 attempt {attempt}/{dd_pipeline.STAGE3_MAX_ATTEMPTS} failed: {e}",
                  file=sys.stderr)

    report["stage3_elapsed_seconds"] = _timing.get("stage3_seconds")
    report["stage3_failure_diagnostics"] = stage3_failure_diagnostics

    if stage3_call_error is not None:
        report["stage3_result"] = None
        report["stage3_validation"] = {"validation_status": "invalid",
                                        "validation_errors": [f"stage3_call_failed: {stage3_call_error}"]}
        report["failure_reason"] = "stage3_call_failed"
        report["run_finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_report(report, args.out_dir, document_number)
        conn.close()
        _cleanup(args.keep_db)
        return 7

    report["stage3_result"] = stage3_raw

    # -- Stage 3 validation + C2 source-reuse (report item 8 & 9) -------------
    print("[6/6] Stage 3 structural validation + C2 source-reuse check ...")
    stage3_struct = schema.validate_stage3_record(stage3_raw)

    if stage3_struct.is_valid:
        c2_errors = schema.validate_stage3_sources(
            stage3_raw.get("sources", []), stage2_raw.get("sources", [])
        )
        c2_note = "run (Stage 3 was structurally valid, matching real pipeline behavior)"
    else:
        # The real pipeline (dd_pipeline.run_due_diligence) never runs the C2
        # check when Stage 3 is structurally invalid. Still computed here,
        # informationally, since validate_stage3_sources is a pure function
        # that degrades gracefully on malformed input — but it is NOT part
        # of the authoritative failure_reason determination below.
        c2_errors = schema.validate_stage3_sources(
            stage3_raw.get("sources", []) if isinstance(stage3_raw, dict) else [],
            stage2_raw.get("sources", []) if isinstance(stage2_raw, dict) else [],
        )
        c2_note = "computed informationally only — real pipeline skips C2 when Stage 3 structural validation fails"

    combined_errors = list(stage3_struct.validation_errors)
    if stage3_struct.is_valid:
        combined_errors.extend(c2_errors)

    report["stage3_validation"] = {
        "validation_status": stage3_struct.validation_status,
        "validation_errors": stage3_struct.validation_errors,
        "confidence": stage3_struct.confidence,
    }
    report["c2_source_reuse_validation"] = {
        "note": c2_note,
        "errors": c2_errors,
        "compliant": len(c2_errors) == 0,
    }
    report["ungrounded_claims_check"] = check_ungrounded_claims(stage3_raw, stage2_raw)

    # ADVISORY ONLY (claim/evidence/inference hardening v2) -- never
    # affects validation_status, failure_reason, or persistence. Flags
    # candidate Stage 3 text that shares substantial topic vocabulary with
    # a Stage 2 open_questions entry but contains no hedge/uncertainty
    # phrase -- a lexical heuristic for human review, not proof of an
    # improper resolution. See dd_schema.check_open_question_resolution's
    # own docstring for the full caveat.
    report["open_question_resolution_check"] = schema.check_open_question_resolution(
        stage2_raw.get("open_questions", []) if isinstance(stage2_raw, dict) else [],
        stage3_raw,
    )

    if combined_errors:
        print("Stage 3 INVALID (structural and/or C2 reuse violation) — preserving result as-is.")
        report["failure_reason"] = "stage3_invalid"
        report["stage3_valid"] = False
    else:
        print("Stage 3 VALID and C2-compliant — persisting final_json onto the DD record.")
        dd_pipeline.persist_stage3_result(conn, dd_id, stage3_raw)
        report["stage3_valid"] = True

    report["run_finished_at"] = datetime.now(timezone.utc).isoformat()

    sources_used = summarize_sources_used(
        stage2_raw.get("sources", []) if isinstance(stage2_raw, dict) else []
    )
    report["sources_used"] = sources_used

    out_path = _write_report(report, args.out_dir, document_number)
    conn.close()
    _cleanup(args.keep_db)

    print()
    print(format_sources_used(sources_used))
    print(f"\nDone. Diagnostic report: {out_path}")
    return 0


def _write_report(report: dict, out_dir: str, document_number: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_doc_num = re.sub(r"[^A-Za-z0-9_-]", "_", document_number or "unknown")
    path = os.path.join(out_dir, f"{ts}_{safe_doc_num}_dd_acceptance.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    return path


def _write_stage2_failure_diagnostic(out_dir: str, document_number: str, attempt: int,
                                      error: "dd_pipeline.Stage2JSONDecodeError") -> str:
    """Preserve everything needed to diagnose a Stage 2 JSON-decode
    failure, exactly as produced, under the isolated acceptance-test
    diagnostics directory:

      - the exact text that was passed to json.loads() (raw_text)
      - the full, unmodified content-block array from the Anthropic
        response, in order (content_blocks) -- so block types/boundaries
        around web_search activity (text / server_tool_use /
        web_search_tool_result) are visible exactly as returned
      - non-secret response metadata (stop_reason, model, usage,
        content-block count/types)

    Writes only. Never repairs, re-parses, regex-extracts, or coerces the
    captured content in any way, and never includes ANTHROPIC_API_KEY,
    request headers, or any other secret -- none of those are present on
    the Stage2JSONDecodeError object in the first place; only the
    response-side data dd_pipeline.call_anthropic_stage2 attached to it.
    """
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_doc_num = re.sub(r"[^A-Za-z0-9_-]", "_", document_number or "unknown")
    path = os.path.join(
        out_dir, f"{ts}_{safe_doc_num}_stage2_attempt{attempt}_json_decode_failure.json"
    )
    payload = {
        "document_number": document_number,
        "attempt": attempt,
        "json_decode_error": str(error),
        "extracted_text_passed_to_json_loads": getattr(error, "raw_text", None),
        "response_metadata": getattr(error, "response_meta", None),
        "raw_content_blocks": getattr(error, "content_blocks", None),
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    return path


def _write_stage3_failure_diagnostic(out_dir: str, document_number: str, attempt: int,
                                      error: "dd_pipeline.Stage3JSONDecodeError") -> str:
    """Preserve everything needed to diagnose a Stage 3 JSON-decode
    failure, exactly as produced, under the isolated acceptance-test
    diagnostics directory. Mirrors _write_stage2_failure_diagnostic
    exactly -- see that function's docstring for the full rationale.

      - the exact text that was passed to json.loads() (raw_text)
      - the full, unmodified content-block array from the Anthropic
        response, in order (content_blocks) -- Stage 3 has no web_search
        tool (spec rule 7), so this is expected to be a single text
        block, but it is preserved exactly as returned rather than
        assumed
      - non-secret response metadata (stop_reason, model, usage,
        content-block count/types)

    Writes only. Never repairs, re-parses, regex-extracts, or coerces the
    captured content in any way, and never includes ANTHROPIC_API_KEY,
    request headers, or any other secret -- none of those are present on
    the Stage3JSONDecodeError object in the first place; only the
    response-side data dd_pipeline.call_anthropic_stage3 attached to it.
    """
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_doc_num = re.sub(r"[^A-Za-z0-9_-]", "_", document_number or "unknown")
    path = os.path.join(
        out_dir, f"{ts}_{safe_doc_num}_stage3_attempt{attempt}_json_decode_failure.json"
    )
    payload = {
        "document_number": document_number,
        "attempt": attempt,
        "json_decode_error": str(error),
        "extracted_text_passed_to_json_loads": getattr(error, "raw_text", None),
        "response_metadata": getattr(error, "response_meta", None),
        "raw_content_blocks": getattr(error, "content_blocks", None),
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    return path


def _cleanup(keep_db: bool) -> None:
    if keep_db:
        print(f"[keep-db] temp sqlite file retained at: {_TMP_DB_PATH}")
        return
    try:
        os.remove(_TMP_DB_PATH)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
