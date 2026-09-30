#!/usr/bin/env python3
"""
Integration test for regulus_v3.main()'s actual DD wiring — not just
dd_pipeline in isolation. Proves:

  - a non-escalated alert goes out exactly as it did before this feature
    (format_email/save_pdf, unchanged content), and
  - an escalated alert with a valid Stage 3 output goes out via
    format_email_dd/save_pdf_dd with Stage 3 content REPLACING Stage 1
    content in the alert the reader sees, per the frozen spec's Output
    behavior section.

Uses a temporary SQLite database only. All network-touching functions
(fetch_documents, analyze_with_llm, the two Anthropic DD calls, send_email)
are monkeypatched — this test makes zero real network calls, consistent
with the sandbox having no ANTHROPIC_API_KEY/GMAIL credentials.

Run: python3 tests/test_dd_integration.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == "FAIL" else ""))
    return condition


fd, TMP_DB = tempfile.mkstemp(suffix=".db", prefix="regulus_integration_test_")
os.close(fd)
os.remove(TMP_DB)
os.environ["DB_PATH"] = TMP_DB
os.environ["PDF_DIR"] = tempfile.mkdtemp(prefix="regulus_integration_pdfs_")
os.environ["LOOKBACK_DAYS"] = "3"

import importlib
import regulus_v3 as rv
import dd_pipeline as ddp
importlib.reload(ddp)
importlib.reload(rv)

# ---------------------------------------------------------------------------
# Fixture documents: one negative control, one escalating (Syria)
# ---------------------------------------------------------------------------

CORRECTION_DOC = {
    # Worded to score above SCORE_DIGEST_MAX (so it reaches Stage 1 analysis,
    # same as a real Entity List correction notice would) while remaining a
    # correction the DD Gate must NOT escalate — this is the negative
    # control the frozen spec's six acceptance tests call for: the DD gate
    # declining to escalate, not Regulus's separate keyword-scoring gate
    # suppressing the document before Stage 1 ever runs.
    "document_number": "2026-30001",
    "title": "Correction: Amendment to the Entity List on the Commerce Control List",
    "abstract": "This document corrects a citation error in a previously published Entity List rule; "
                "no substantive change to the Commerce Control List or Entity List is made.",
    "agencies": [{"name": "Bureau of Industry and Security"}], "type": "Rule",
    "publication_date": "2026-09-29", "effective_on": "2026-09-29",
    "html_url": "https://www.federalregister.gov/d/2026-30001", "pdf_url": None,
    "excerpts": [], "citation": "91 FR 1",
}

SYRIA_DOC = {
    "document_number": "2026-30002", "title": "Waiver of Sanctions on Syria Under the CBW Act",
    "abstract": "State waives the two remaining CBW Act restrictions on Syria.",
    "agencies": [{"name": "Department of State"}], "type": "Notice",
    "publication_date": "2026-09-29", "effective_on": "2026-09-30",
    "html_url": "https://www.federalregister.gov/d/2026-30002", "pdf_url": None,
    "excerpts": [], "citation": "91 FR 2",
}

CORRECTION_ANALYSIS = {
    "title": CORRECTION_DOC["title"], "confidence": "High", "change_type": "correction",
    "countries": [], "unresolved_questions": [], "summary": "A citation correction.",
    "authority": [], "entities": [], "eccns": [], "ear_sections": [],
    "licensing_impact": "None", "defense_impact": "None", "remaining_controls": "n/a",
    "recommended_actions": [], "effective_date": "2026-09-29",
}

SYRIA_ANALYSIS = {
    "title": SYRIA_DOC["title"], "confidence": "High", "change_type": "sanctions_waiver",
    "countries": ["Syria"], "unresolved_questions": [], "summary": "Two CBW Act restrictions waived.",
    "authority": ["CBW Act"], "entities": [], "eccns": [], "ear_sections": [],
    "licensing_impact": "AECA/USML licensing backdrop changes", "defense_impact": "Moderate",
    "remaining_controls": "Other Syria sanctions regimes remain.", "recommended_actions": [],
    "effective_date": "2026-09-30",
}

STAGE2_RECORD = {
    "research_question": "What is the precedent for this Syria CBW Act waiver?",
    "current_event": {
        "action": "waiver", "date": "2026-09-30", "effective_date": "2026-09-30",
        "agency": ["Department of State"], "authority": ["CBW Act"], "jurisdictions": ["Syria"],
        "entities": [], "controls_affected": ["AECA arms sales"],
    },
    "historical_context": {
        "program_origin": "CBW Act sanctions.", "major_prior_actions": ["2025 partial waiver"],
        "most_relevant_precedent": {
            "date": "2025-06-30", "description": "Partial waiver.", "entities_involved": [],
            "authority": ["CBW Act"], "mechanism": "presidential determination",
        },
    },
    "precedent_comparison": {"similarities": [], "differences": [], "trend_classification": "consistent"},
    "legal_regulatory_effect": {
        "changed": ["AECA restriction waived"], "unchanged": [], "superseded": [],
        "remaining_restrictions": [], "effective_date": "2026-09-30",
    },
    "scope": {
        "affected_countries": ["Syria"], "affected_entities": [], "affected_item_categories": [],
        "affected_transaction_types": [], "affected_compliance_workflows": [],
    },
    "impact_assessment": {
        "immediate": [], "operational": [], "licensing": [], "screening": [],
        "classification": [], "authorization_management": [],
    },
    "follow_on_indicators": {
        "historically_observed_next_steps": [], "current_unresolved_actions": [], "items_to_monitor": [],
    },
    "open_questions": [],
    "sources": [{
        "url": "https://www.federalregister.gov/d/2025-00001", "source_type": "federal_register",
        "agency": "Department of State", "date": "2025-06-30", "supports": ["historical_context"],
        "primary_source": True,
    }],
    "research_status": "complete", "due_diligence_confidence": "High",
}

STAGE3_RECORD = {
    "headline": "Syria — Remaining CBW Act Arms Restrictions Waived",
    "bottom_line": "State waived the two remaining CBW Act restrictions on Syria.",
    "what_changed": "AECA/USML licensing restriction waived.",
    "why_it_matters": "Completes the staged removal begun in 2025.",
    "historical_significance": "Consistent with the 2025 trajectory.",
    "what_did_not_change": "Other Syria sanctions regimes remain in place.",
    "compliance_attention": ["Review AECA/USML licensing posture"],
    "watch_next": ["Subsequent DDTC/State implementing guidance"],
    "confidence": "High", "sources": STAGE2_RECORD["sources"],
}

_ANALYSES_BY_DOC_NUM = {
    CORRECTION_DOC["document_number"]: CORRECTION_ANALYSIS,
    SYRIA_DOC["document_number"]: SYRIA_ANALYSIS,
}

# ---------------------------------------------------------------------------
# Monkeypatches — no real network calls anywhere in this test
# ---------------------------------------------------------------------------

sent_emails = []
saved_pdfs = []


def fake_fetch_documents(since_date):
    return [CORRECTION_DOC, SYRIA_DOC]


def fake_analyze_with_llm(doc):
    return dict(_ANALYSES_BY_DOC_NUM[doc["document_number"]])


def fake_call_stage2(doc, analysis, api_key):
    assert doc["document_number"] == SYRIA_DOC["document_number"], \
        "Stage 2 must only be invoked for the escalated document"
    return dict(STAGE2_RECORD)


def fake_call_stage3(analysis, dd_record, api_key):
    return dict(STAGE3_RECORD)


def fake_send_email(subject, body):
    sent_emails.append({"subject": subject, "body": body})


def fake_save_pdf(analysis, doc_url, score, doc_hash, document_number=None, fetched_at=None):
    saved_pdfs.append({"kind": "stage1", "title": analysis.get("title")})
    return "/fake/path/stage1.pdf"


def fake_save_pdf_dd(final, doc_url, score, doc_hash, document_number=None, fetched_at=None):
    saved_pdfs.append({"kind": "stage3", "headline": final.get("headline")})
    return "/fake/path/stage3.pdf"


def fake_run_eccn_regex_test(doc, doc_hash, document_number):
    return None, None


rv.fetch_documents = fake_fetch_documents
rv.analyze_with_llm = fake_analyze_with_llm
rv.send_email = fake_send_email
rv.save_pdf = fake_save_pdf
rv.save_pdf_dd = fake_save_pdf_dd
rv.run_eccn_regex_test = fake_run_eccn_regex_test
ddp.call_anthropic_stage2 = fake_call_stage2
ddp.call_anthropic_stage3 = fake_call_stage3

# ---------------------------------------------------------------------------
# Run main()
# ---------------------------------------------------------------------------

print("=== Running regulus_v3.main() with monkeypatched I/O ===")
rv.main()

check("exactly 2 emails sent (one per document)", len(sent_emails) == 2, str(sent_emails))

correction_email = next((e for e in sent_emails if "Correction" in e["subject"] or "2026-30001" in e["subject"]), None)
syria_email = next((e for e in sent_emails if "Syria" in e["subject"] or "DD" in e["subject"]), None)

check("correction notice used the ORIGINAL (non-DD) subject prefix",
      correction_email is not None and correction_email["subject"].startswith("[Export Control Alert]")
      and "DD" not in correction_email["subject"], str(correction_email))
check("Syria alert used the DD subject prefix", syria_email is not None and "DD" in syria_email["subject"],
      str(syria_email))
check("Syria email body contains Stage 3 headline, not raw Stage 1 summary",
      syria_email is not None and STAGE3_RECORD["headline"] in syria_email["body"], str(syria_email))
check("Syria email body does NOT contain the Stage 1 analysis summary text",
      syria_email is not None and SYRIA_ANALYSIS["summary"] not in syria_email["body"])

check("exactly 2 PDFs saved", len(saved_pdfs) == 2, str(saved_pdfs))
check("correction notice got a stage1-style PDF", any(p["kind"] == "stage1" for p in saved_pdfs), str(saved_pdfs))
check("Syria alert got a stage3-style PDF", any(p["kind"] == "stage3" for p in saved_pdfs), str(saved_pdfs))

# ---------------------------------------------------------------------------
# Verify DB state directly
# ---------------------------------------------------------------------------
conn = rv.get_db()
correction_row = conn.execute(
    "SELECT due_diligence_ran, headline, emailed_at FROM alerts WHERE document_number = ?",
    (CORRECTION_DOC["document_number"],),
).fetchone()
syria_row = conn.execute(
    "SELECT due_diligence_ran, headline, final_confidence, emailed_at FROM alerts WHERE document_number = ?",
    (SYRIA_DOC["document_number"],),
).fetchone()

check("correction notice: due_diligence_ran == 0", correction_row[0] == 0, str(correction_row))
check("correction notice: headline column is NULL (no DD synthesis)", correction_row[1] is None, str(correction_row))
check("correction notice: emailed_at set", correction_row[2] is not None)

check("Syria alert: due_diligence_ran == 1", syria_row[0] == 1, str(syria_row))
check("Syria alert: headline persisted to alerts row", syria_row[1] == STAGE3_RECORD["headline"], str(syria_row))
check("Syria alert: final_confidence persisted", syria_row[2] == "High", str(syria_row))
check("Syria alert: emailed_at set", syria_row[3] is not None)

dd_records = conn.execute(
    "SELECT document_number, validation_status, research_status FROM due_diligence_records"
).fetchall()
check("exactly one due_diligence_records row (only Syria escalated)", len(dd_records) == 1, str(dd_records))
check("due_diligence_records row is for the Syria document",
      dd_records[0][0] == SYRIA_DOC["document_number"] if dd_records else False, str(dd_records))
check("due_diligence_records row is valid/complete", dd_records[0][1:] == ("valid", "complete") if dd_records else False,
      str(dd_records))

conn.close()

# ---------------------------------------------------------------------------
# Scenario 2: DD alert's first email send fails; the SAME main() run's
# unsent-alert retry recovers it. Proves the fix end to end: the retried
# email carries the original validated sources, and Stage 2/Stage 3 are
# NOT invoked a second time just to reconstruct it.
# ---------------------------------------------------------------------------
print("\n=== Scenario 2: DD alert email fails first send, recovered on retry ===")

fd2, TMP_DB2 = tempfile.mkstemp(suffix=".db", prefix="regulus_integration_test2_")
os.close(fd2)
os.remove(TMP_DB2)
os.environ["DB_PATH"] = TMP_DB2
os.environ["PDF_DIR"] = tempfile.mkdtemp(prefix="regulus_integration_pdfs2_")

importlib.reload(ddp)
importlib.reload(rv)

SYRIA_DOC_2 = dict(SYRIA_DOC, document_number="2026-30003",
                   html_url="https://www.federalregister.gov/d/2026-30003")
_ANALYSES_BY_DOC_NUM_2 = {SYRIA_DOC_2["document_number"]: dict(SYRIA_ANALYSIS, title=SYRIA_DOC_2["title"])}

stage2_call_count = {"n": 0}
stage3_call_count = {"n": 0}
send_attempts = {"n": 0}
sent_emails_2 = []
saved_pdfs_2 = []


def fake_fetch_documents_2(since_date):
    return [SYRIA_DOC_2]


def fake_analyze_with_llm_2(doc):
    return dict(_ANALYSES_BY_DOC_NUM_2[doc["document_number"]])


def fake_call_stage2_2(doc, analysis, api_key):
    stage2_call_count["n"] += 1
    return dict(STAGE2_RECORD)


def fake_call_stage3_2(analysis, dd_record, api_key):
    stage3_call_count["n"] += 1
    return dict(STAGE3_RECORD)


def fake_send_email_2(subject, body):
    send_attempts["n"] += 1
    if send_attempts["n"] == 1:
        raise RuntimeError("simulated SMTP failure on first attempt")
    sent_emails_2.append({"subject": subject, "body": body})


def fake_save_pdf_2(analysis, doc_url, score, doc_hash, document_number=None, fetched_at=None):
    saved_pdfs_2.append({"kind": "stage1"})
    return "/fake/path/stage1.pdf"


def fake_save_pdf_dd_2(final, doc_url, score, doc_hash, document_number=None, fetched_at=None):
    saved_pdfs_2.append({"kind": "stage3", "sources": final.get("sources")})
    return "/fake/path/stage3.pdf"


def fake_run_eccn_regex_test_2(doc, doc_hash, document_number):
    return None, None


rv.fetch_documents = fake_fetch_documents_2
rv.analyze_with_llm = fake_analyze_with_llm_2
rv.send_email = fake_send_email_2
rv.save_pdf = fake_save_pdf_2
rv.save_pdf_dd = fake_save_pdf_dd_2
rv.run_eccn_regex_test = fake_run_eccn_regex_test_2
ddp.call_anthropic_stage2 = fake_call_stage2_2
ddp.call_anthropic_stage3 = fake_call_stage3_2

rv.main()

check("send_email was attempted twice (first-send failure + retry)", send_attempts["n"] == 2,
      f"attempts={send_attempts['n']}")
check("exactly one email actually delivered", len(sent_emails_2) == 1, str(sent_emails_2))
check("Stage 2 was called exactly ONCE despite the retry (not re-run to reconstruct the email)",
      stage2_call_count["n"] == 1, f"stage2_calls={stage2_call_count['n']}")
check("Stage 3 was called exactly ONCE despite the retry (not re-run to reconstruct the email)",
      stage3_call_count["n"] == 1, f"stage3_calls={stage3_call_count['n']}")

retried_email = sent_emails_2[0] if sent_emails_2 else {"subject": "", "body": ""}
check("retried DD email used the DD subject prefix", "DD" in retried_email.get("subject", ""), str(retried_email))
check("retried DD email body contains the original validated source URL",
      STAGE2_RECORD["sources"][0]["url"] in retried_email.get("body", ""), str(retried_email))

stage3_pdf_calls = [p for p in saved_pdfs_2 if p["kind"] == "stage3"]
# Two stage3-style PDF saves are expected: one from the initial per-document
# pass (save_pdf_dd runs before the email attempt, regardless of whether
# that email succeeds) and one from the retry pass — the fix under test is
# that BOTH carry the original validated sources, never an empty list.
check("two stage3-style PDFs saved (initial pass + retry pass)", len(stage3_pdf_calls) == 2, str(saved_pdfs_2))
check("every stage3-style PDF carries the original validated sources, never an empty list",
      all(p["sources"] == STAGE2_RECORD["sources"] for p in stage3_pdf_calls), str(stage3_pdf_calls))

conn2 = rv.get_db()
dd_records_2 = conn2.execute("SELECT COUNT(*) FROM due_diligence_records").fetchone()[0]
check("retry did NOT create a second due_diligence_records row", dd_records_2 == 1, f"count={dd_records_2}")
emailed_row = conn2.execute("SELECT emailed_at FROM alerts WHERE document_number = ?",
                             (SYRIA_DOC_2["document_number"],)).fetchone()
check("alert is marked emailed after the successful retry", emailed_row is not None and emailed_row[0] is not None)
conn2.close()

try:
    os.remove(TMP_DB2)
except OSError:
    pass

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
try:
    os.remove(TMP_DB)
except OSError:
    pass

print("\n=== SUMMARY ===")
failed = [r for r in results if r[1] == "FAIL"]
for name, status, detail in results:
    print(f"{status}: {name}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
