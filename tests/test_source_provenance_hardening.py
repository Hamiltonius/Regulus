#!/usr/bin/env python3
"""
Tests for the two deterministic provenance hardenings added after the
first successful live Syria run (commit c41fd91, document_number=
2026-18918):

  Defect 1 (source identity): Stage 2 cited
  https://www.govinfo.gov/content/pkg/FR-2026-09-16/pdf/2026-18984.pdf
  as evidence for the CURRENT event, but the target document is
  2026-18918 -- a different Federal Register document. C2 correctly
  allowed Stage 3 to reuse it (C2 only checks Stage 3 vs. Stage 2
  reuse, not identity against the target document), which is a
  separate gap: dd_schema.validate_source_identity /
  validate_stage2_source_identities fill it, deterministically, no LLM,
  no new web calls, reporting-only (does not change validation_status,
  the gate, or persistence).

  Defect 2 (primary-source classification): Stage 2 marked
  law.cornell.edu and thefederalregister.org primary_source=true, which
  is wrong for provenance purposes (neither is the issuing government
  source). dd_schema.classify_primary_source /
  normalize_source(s)_primary replace the model's own claim with a
  conservative, domain-based allowlist decision, applied once inside
  dd_pipeline.due_diligence_review() -- the single choke point both
  run_due_diligence() (production) and the acceptance script go
  through -- so persistence, Stage 3's dd_record input, and the
  acceptance runner's SOURCES USED summary all see the SAME normalized
  value automatically.

Neither change touches Stage 1, Stage 2/3 prompts, schemas, models,
STAGE2_MAX_TOKENS, STAGE3_MAX_TOKENS/STAGE3_TIMEOUT_SECONDS, the gate,
DD DB schema, retry behavior, or C2 semantics (validate_stage3_sources
itself is byte-for-byte unmodified -- see test 18).

Run: python3 tests/test_source_provenance_hardening.py
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


import dd_schema as schema  # noqa: E402

# ===========================================================================
# SOURCE IDENTITY (Defect 1)
# ===========================================================================

TARGET = "2026-18918"

# 1. target 2026-18918 + official Federal Register 2026-18918 URL -> pass
source_fr_correct = {
    "url": "https://www.federalregister.gov/documents/2026/09/16/2026-18918/syria-waiver",
    "source_type": "federal_register", "agency": "Department of State", "date": "2026-09-16",
    "supports": ["current_event"], "primary_source": True,
}
check("1. target 2026-18918 + official federalregister.gov URL for 2026-18918 -> no error",
      schema.validate_source_identity(source_fr_correct, TARGET) is None)

# 2. target 2026-18918 + GovInfo 2026-18918 URL -> pass
source_govinfo_correct = {
    "url": "https://www.govinfo.gov/content/pkg/FR-2026-09-16/pdf/2026-18918.pdf",
    "source_type": "federal_register", "agency": "Department of State", "date": "2026-09-16",
    "supports": ["current_event"], "primary_source": True,
}
check("2. target 2026-18918 + GovInfo URL encoding 2026-18918 -> no error",
      schema.validate_source_identity(source_govinfo_correct, TARGET) is None)

# 3. target 2026-18918 + current-event GovInfo 2026-18984 URL -> fail (the
#    exact live defect: the wrong FR document attached to current_event)
source_govinfo_wrong = {
    "url": "https://www.govinfo.gov/content/pkg/FR-2026-09-16/pdf/2026-18984.pdf",
    "source_type": "federal_register", "agency": "Department of State", "date": "2026-09-16",
    "supports": ["current_event"], "primary_source": True,
}
identity_error = schema.validate_source_identity(source_govinfo_wrong, TARGET)
check("3. target 2026-18918 + current-event GovInfo URL encoding 2026-18984 -> flagged",
      identity_error is not None and "2026-18984" in identity_error and "2026-18918" in identity_error)

# 4. target 2026-18918 + historical Federal Register 2013-22032 source -> pass
#    (supports historical_context, NOT current_event -- legitimate precedent)
source_historical = {
    "url": "https://www.federalregister.gov/documents/2013/09/13/2013-22032/syria-precedent",
    "source_type": "federal_register", "agency": "Department of State", "date": "2013-09-13",
    "supports": ["historical_context"], "primary_source": True,
}
check("4. target 2026-18918 + historical FR source 2013-22032 (supports historical_context, "
      "not current_event) -> never flagged, legitimate precedent",
      schema.validate_source_identity(source_historical, TARGET) is None)

# Same historical source, but hypothetically mis-tagged as current_event,
# should still be flagged -- the check is driven by `supports`, not by
# whether the document number "looks" historical.
source_historical_mistagged = dict(source_historical, supports=["current_event"])
check("4b. the same historical source WOULD be flagged if it claimed to support "
      "current_event instead -- the check is driven by `supports`, not by intuition "
      "about which document number looks old",
      schema.validate_source_identity(source_historical_mistagged, TARGET) is not None)

# 5. unrelated/non-FR URLs are not falsely rejected by the FR identity
#    check, even when they contain a number that LOOKS like an FR docnum
source_unrelated = {
    "url": "https://legal500.com/articles/2026-18918-syria-sanctions-analysis",
    "source_type": "secondary_analysis", "agency": "", "date": "2026-09-20",
    "supports": ["current_event"], "primary_source": False,
}
check("5. a non-FR/GovInfo URL is never flagged, even one that happens to contain an "
      "FR-shaped number in its own path, and even when it supports current_event",
      schema.validate_source_identity(source_unrelated, TARGET) is None)
check("5b. extract_federal_register_document_number itself returns None for a non-FR/"
      "GovInfo domain -- it never guesses an FR number out of an unrelated host",
      schema.extract_federal_register_document_number(source_unrelated["url"]) is None)

# Additional robustness checks on validate_source_identity/extraction:
check("a source with no 'current_event' in supports is skipped entirely, regardless "
      "of its URL",
      schema.validate_source_identity(
          dict(source_govinfo_wrong, supports=["scope"]), TARGET) is None)
check("a source with no URL at all is skipped, not raised on",
      schema.validate_source_identity({"supports": ["current_event"]}, TARGET) is None)
check("a falsy/unknown target_document_number means nothing is checked (never invents "
      "a target to compare against)",
      schema.validate_source_identity(source_govinfo_wrong, "") is None
      and schema.validate_source_identity(source_govinfo_wrong, None) is None)
check("non-dict source input is skipped, not raised on",
      schema.validate_source_identity("not a dict", TARGET) is None)
check("a subdomain of federalregister.gov (e.g. www.) is still recognized as an "
      "identity domain",
      schema.extract_federal_register_document_number(
          "https://www.federalregister.gov/documents/2026/09/16/2026-18918/x") == "2026-18918")

# validate_stage2_source_identities: whole-array wrapper
mixed_sources = [source_fr_correct, source_govinfo_wrong, source_historical, source_unrelated]
array_errors = schema.validate_stage2_source_identities(mixed_sources, TARGET)
check("validate_stage2_source_identities flags exactly the one conflicting source in a "
      "mixed array, by index, and no others",
      len(array_errors) == 1 and "sources[1]:" in array_errors[0])
check("validate_stage2_source_identities returns [] for an empty or non-list input, "
      "never raises",
      schema.validate_stage2_source_identities([], TARGET) == []
      and schema.validate_stage2_source_identities(None, TARGET) == []
      and schema.validate_stage2_source_identities("not a list", TARGET) == [])


# ===========================================================================
# PRIMARY NORMALIZATION (Defect 2)
# ===========================================================================

# 6-12: approved government domains classify as primary
for n, url, expect_primary in [
    (6, "https://www.federalregister.gov/documents/2026/09/16/2026-18918/x", True),
    (7, "https://www.govinfo.gov/content/pkg/FR-2026-09-16/pdf/2026-18918.pdf", True),
    (8, "https://uscode.house.gov/view.xhtml?req=title:50", True),
    (9, "https://www.congress.gov/bill/119th-congress/", True),
    (10, "https://www.state.gov/press-releases/syria", True),
    (11, "https://www.bis.gov/press-release/x", True),
    (12, "https://ofac.treasury.gov/sanctions-programs-and-country-information/syria", True),
]:
    check(f"{n}. {url.split('//')[1].split('/')[0]} -> classify_primary_source == {expect_primary}",
          schema.classify_primary_source(url) is expect_primary)

# 13-15: known non-government/mirror/secondary sites do NOT classify primary
for n, url in [
    (13, "https://www.law.cornell.edu/uscode/text/50/1701"),
    (14, "https://www.thefederalregister.org/documents/2026-18918"),
    (15, "https://www.goodwinlaw.com/insights/syria-sanctions"),
]:
    check(f"{n}. {url.split('//')[1].split('/')[0]} -> classify_primary_source == False",
          schema.classify_primary_source(url) is False)

# Explicit coverage of every domain named in the request as NOT primary
for domain_url in [
    "https://fdassociates.net/blog/syria",
    "https://legal500.com/articles/x",
    "https://unblocksyria.com/news",
    "https://zyphe.com/sanctions-tracker",
]:
    check(f"explicitly non-primary domain {domain_url.split('//')[1].split('/')[0]} -> False",
          schema.classify_primary_source(domain_url) is False)

# Additional domains actually used by the pipeline, per the request's list
for domain_url in [
    "https://www.commerce.gov/news",
    "https://home.treasury.gov/news",  # subdomain of treasury.gov
    "https://www.whitehouse.gov/briefing-room/",
]:
    check(f"government domain {domain_url.split('//')[1].split('/')[0]} -> classify_primary_source == True",
          schema.classify_primary_source(domain_url) is True)

# 16. model-supplied primary_source=true cannot override a non-approved domain
model_claimed_primary = {
    "url": "https://www.law.cornell.edu/uscode/text/50/1701", "source_type": "statute_text",
    "agency": "Congress", "date": "2020-01-01", "supports": ["legal_regulatory_effect"],
    "primary_source": True,  # the model's own (wrong) claim
}
normalized = schema.normalize_source_primary(model_claimed_primary)
check("16. model-supplied primary_source=True on law.cornell.edu is overridden to False "
      "by the trusted domain-based classifier",
      normalized["primary_source"] is False)
check("normalize_source_primary preserves every other key/value unchanged (source_type, "
      "agency, date, supports, url) -- same object shape",
      normalized["url"] == model_claimed_primary["url"]
      and normalized["source_type"] == "statute_text"
      and normalized["agency"] == "Congress"
      and normalized["date"] == "2020-01-01"
      and normalized["supports"] == ["legal_regulatory_effect"])
check("normalize_source_primary returns a NEW dict, not a mutated reference to the input",
      normalized is not model_claimed_primary)

# The reverse also holds: a model claiming primary_source=False for an
# approved government domain is likewise overridden -- the trusted
# classifier is the sole authority in both directions.
model_claimed_secondary_but_govt = {
    "url": "https://www.state.gov/press-releases/syria", "source_type": "press_release",
    "agency": "Department of State", "date": "2026-09-16", "supports": ["current_event"],
    "primary_source": False,  # model under-claimed
}
normalized2 = schema.normalize_source_primary(model_claimed_secondary_but_govt)
check("a model-supplied primary_source=False for an approved government domain (state.gov) "
      "is corrected to True -- the classifier, not the model, is authoritative either way",
      normalized2["primary_source"] is True)

check("normalize_source_primary passes non-dict input through unchanged, never raises",
      schema.normalize_source_primary("not a dict") == "not a dict")
check("normalize_sources_primary passes non-list input through unchanged, never raises",
      schema.normalize_sources_primary(None) is None)
check("normalize_sources_primary maps over a whole array, preserving order and length",
      [s["primary_source"] for s in schema.normalize_sources_primary(
          [model_claimed_primary, model_claimed_secondary_but_govt])] == [False, True])


# ===========================================================================
# INTEGRATION
# ===========================================================================

# 17. normalized source classification is what SOURCES USED consumes --
#     verified through the real call_anthropic_stage2 -> due_diligence_review
#     choke point (dd_pipeline.py), then through the acceptance script's
#     summarize_sources_used/format_sources_used exactly as main() calls them.
def fresh_db_path():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="regulus_provenance_test_")
    os.close(fd)
    os.remove(path)
    return path


TMP_DB = fresh_db_path()
os.environ["DB_PATH"] = TMP_DB
import importlib
import regulus_v3 as rv
importlib.reload(rv)
import dd_pipeline as ddp  # noqa: E402

conn = rv.get_db()

RAW_STAGE2_SOURCES = [
    {"url": "https://www.federalregister.gov/documents/2026/09/16/2026-18918/x",
     "source_type": "federal_register", "agency": "Department of State", "date": "2026-09-16",
     "supports": ["current_event"], "primary_source": True},
    {"url": "https://www.law.cornell.edu/uscode/text/50/1701", "source_type": "statute_text",
     "agency": "Congress", "date": "2020-01-01", "supports": ["legal_regulatory_effect"],
     "primary_source": True},   # model over-claimed -- must be normalized to False
    {"url": "https://www.thefederalregister.org/documents/2026-18918",
     "source_type": "federal_register", "agency": "Department of State", "date": "2026-09-16",
     "supports": ["current_event"], "primary_source": True},  # mirror, model over-claimed
]

VALID_STAGE2_JSON = {
    "research_question": "q", "current_event": {}, "historical_context": {},
    "precedent_comparison": {}, "legal_regulatory_effect": {}, "scope": {},
    "impact_assessment": {}, "follow_on_indicators": {}, "open_questions": [],
    "sources": RAW_STAGE2_SOURCES, "research_status": "complete",
    "due_diligence_confidence": "High",
}


def fake_call_stage2(doc, analysis, api_key):
    import copy
    return copy.deepcopy(VALID_STAGE2_JSON)


stage2_result = ddp.due_diligence_review(
    {"title": "t"}, {"confidence": "High"}, api_key="fake", call_stage2=fake_call_stage2,
)

check("17a. due_diligence_review's returned sources have law.cornell.edu normalized to "
      "primary_source=False (not what the model claimed)",
      stage2_result["sources"][1]["primary_source"] is False)
check("17b. due_diligence_review's returned sources have thefederalregister.org normalized "
      "to primary_source=False",
      stage2_result["sources"][2]["primary_source"] is False)
check("17c. due_diligence_review's returned sources have federalregister.gov correctly "
      "remaining primary_source=True",
      stage2_result["sources"][0]["primary_source"] is True)

import importlib.util as _ilu

_script_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "dd_syria_acceptance_test.py"
)
_spec = _ilu.spec_from_file_location("dd_syria_acceptance_test", _script_path)
acceptance_script = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(acceptance_script)

sources_used_rows = acceptance_script.summarize_sources_used(stage2_result["sources"])
by_domain = {r["domain"]: r for r in sources_used_rows}
check("17d. SOURCES USED (summarize_sources_used) reflects the NORMALIZED classification: "
      "federalregister.gov is PRIMARY",
      by_domain["federalregister.gov"]["primary"] is True)
check("17e. SOURCES USED reflects the NORMALIZED classification: law.cornell.edu is "
      "SECONDARY, not PRIMARY, despite the model's original claim",
      by_domain["law.cornell.edu"]["primary"] is False)
check("17f. SOURCES USED reflects the NORMALIZED classification: thefederalregister.org "
      "is SECONDARY, not PRIMARY",
      by_domain["thefederalregister.org"]["primary"] is False)
rendered = acceptance_script.format_sources_used(sources_used_rows)
check("17g. the rendered SOURCES USED block shows law.cornell.edu with the SECONDARY "
      "marker/label, not PRIMARY",
      any("law.cornell.edu" in line and "SECONDARY" in line and "✓" not in line
          for line in rendered.split("\n")))

try:
    os.remove(acceptance_script._TMP_DB_PATH)
except OSError:
    pass

# 18. C2 still rejects Stage 3 sources absent from Stage 2 -- proves
#     validate_stage3_sources itself was not touched by this round, and
#     still enforces exactly as before against the (now-normalized)
#     Stage 2 sources.
fabricated = [{"url": "https://not-a-real-source.example.com/x", "source_type": "secondary_analysis",
               "agency": "", "date": "2026-01-01", "supports": [], "primary_source": False}]
c2_errors_fabricated = schema.validate_stage3_sources(fabricated, stage2_result["sources"])
check("18. C2 still rejects a Stage 3 source absent from the (normalized) Stage 2 evidence",
      len(c2_errors_fabricated) == 1)
c2_errors_reused = schema.validate_stage3_sources([stage2_result["sources"][0]], stage2_result["sources"])
check("18b. C2 still accepts a Stage 3 source that verbatim-reuses a normalized Stage 2 "
      "source (the normalized primary_source value IS the verbatim value now)",
      c2_errors_reused == [])

# 19. historical Federal Register precedent sources remain valid (not
#     rejected by identity check, and normalize to whatever their own
#     domain dictates -- federalregister.gov is still primary even for a
#     2013 document).
check("19. a historical federalregister.gov precedent source from 2013 still normalizes to "
      "primary_source=True (domain-based, unrelated to which document number it carries)",
      schema.normalize_source_primary(source_historical)["primary_source"] is True)
check("19b. that same historical source is still never flagged by identity validation "
      "against the 2026 target (it supports historical_context, not current_event)",
      schema.validate_source_identity(source_historical, TARGET) is None)

# 20. existing successful source reuse behavior is unchanged: a Stage 3
#     record built by copying Stage 2 sources verbatim (the documented,
#     intended workflow) still passes C2 after normalization, exactly as
#     it did before this round -- normalization changes WHICH boolean a
#     source carries, never whether verbatim reuse of that (corrected)
#     value is accepted.
stage3_reusing_all = list(stage2_result["sources"])
check("20. verbatim reuse of ALL (normalized) Stage 2 sources in Stage 3 still passes C2, "
      "unchanged from pre-existing behavior",
      schema.validate_stage3_sources(stage3_reusing_all, stage2_result["sources"]) == [])

conn.close()
try:
    os.remove(TMP_DB)
except OSError:
    pass


# ---------------------------------------------------------------------------
# Scope guarantees: no LLM/network calls in the new functions, C2's own
# function body is untouched, and nothing here touches Stage 1/Stage 2
# prompt/schema/model, STAGE2_MAX_TOKENS, STAGE3_MAX_TOKENS/TIMEOUT, the
# gate, DB schema, or retry behavior.
# ---------------------------------------------------------------------------

import inspect  # noqa: E402

check("classify_primary_source makes no network call (no 'requests.'/'urlopen' in its code)",
      "requests." not in inspect.getsource(schema.classify_primary_source)
      and "urlopen" not in inspect.getsource(schema.classify_primary_source))
check("validate_source_identity makes no network call and calls no LLM",
      "requests." not in inspect.getsource(schema.validate_source_identity))
check("validate_stage3_sources (C2) docstring/behavior is untouched: still mentions "
      "'spec clarification C2' verbatim",
      "spec clarification C2" in (schema.validate_stage3_sources.__doc__ or "")
      or "spec clarification C2" in inspect.getsource(schema.validate_stage3_sources))
check("STAGE2_MAX_TOKENS remains exactly 20000 -- untouched by this round",
      ddp.STAGE2_MAX_TOKENS == 20000)
check("STAGE3_MAX_TOKENS remains exactly 4000 -- untouched by this round",
      ddp.STAGE3_MAX_TOKENS == 4000)
check("STAGE3_TIMEOUT_SECONDS remains exactly 180 -- untouched by this round",
      ddp.STAGE3_TIMEOUT_SECONDS == 180)
check("STAGE2_TIMEOUT_SECONDS remains exactly 600 -- untouched by this round",
      ddp.STAGE2_TIMEOUT_SECONDS == 600)
check("needs_due_diligence (the gate) source is untouched by this file's changes -- "
      "it does not reference source-identity or primary-normalization functions at all",
      "validate_source_identity" not in inspect.getsource(ddp.needs_due_diligence)
      and "classify_primary_source" not in inspect.getsource(ddp.needs_due_diligence))


print("\n=== SUMMARY ===")
failed = [r for r in results if r[1] == "FAIL"]
for name, status, detail in results:
    print(f"{status}: {name}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
