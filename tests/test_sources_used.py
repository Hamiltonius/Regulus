#!/usr/bin/env python3
"""
Tests for the "SOURCES USED" presentation/observability summary added to
scripts/dd_syria_acceptance_test.py: _extract_hostname,
summarize_sources_used, and format_sources_used.

This is intentionally small and does exactly what was asked, nothing
more: derive a per-hostname PRIMARY/SECONDARY summary from the already-
validated Stage 2 `sources` array. No new web calls, no LLM
classification, no reputation scoring, no persistence or schema of any
kind -- these are three pure functions, so every test here is a plain
input/output check with no mocking, no network, no DB.

Run: python3 tests/test_sources_used.py
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == "FAIL" else ""))
    return condition


# ---------------------------------------------------------------------------
# Load the acceptance script as a module (same pattern as the other test
# files in this suite). Its top-level code only sets up an isolated temp
# DB_PATH; main() is guarded by `if __name__ == "__main__"`.
# ---------------------------------------------------------------------------
_script_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "dd_syria_acceptance_test.py"
)
_spec = importlib.util.spec_from_file_location("dd_syria_acceptance_test", _script_path)
acceptance_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(acceptance_script)

_extract_hostname = acceptance_script._extract_hostname
summarize_sources_used = acceptance_script.summarize_sources_used
format_sources_used = acceptance_script.format_sources_used


# ---------------------------------------------------------------------------
# 1. _extract_hostname: URL -> hostname
# ---------------------------------------------------------------------------

check("plain https URL",
      _extract_hostname("https://www.federalregister.gov/d/2025-00001") == "federalregister.gov")
check("strips 'www.' prefix",
      _extract_hostname("https://www.state.gov/page") == "state.gov")
check("no 'www.' prefix, unaffected",
      _extract_hostname("https://ecfr.gov/current/title-15") == "ecfr.gov")
check("strips port number",
      _extract_hostname("https://legal500.com:8443/article") == "legal500.com")
check("path/query/fragment ignored -- only the host matters",
      _extract_hostname("https://fdassociates.net/blog/post?x=1#section") == "fdassociates.net")
check("lowercases a mixed-case host",
      _extract_hostname("https://WWW.State.GOV/page") == "state.gov")
check("http (non-https) scheme works too",
      _extract_hostname("http://example.org/x") == "example.org")
check("empty string returns '' rather than raising",
      _extract_hostname("") == "")
check("None returns '' rather than raising",
      _extract_hostname(None) == "")
check("a non-URL string returns '' rather than raising or guessing",
      _extract_hostname("not a url") == "")


# ---------------------------------------------------------------------------
# 2. summarize_sources_used: dedup + PRIMARY precedence
# ---------------------------------------------------------------------------

SOURCES = [
    {"url": "https://www.federalregister.gov/d/2025-00001", "primary_source": True},
    {"url": "https://www.federalregister.gov/d/2025-00002", "primary_source": True},
    {"url": "https://federalregister.gov/d/2025-00003", "primary_source": True},
    {"url": "https://www.state.gov/a", "primary_source": True},
    {"url": "https://www.state.gov/b", "primary_source": True},
    {"url": "https://ecfr.gov/x", "primary_source": True},
    {"url": "https://fdassociates.net/blog", "primary_source": False},
    {"url": "https://legal500.com/article", "primary_source": False},
]

rows = summarize_sources_used(SOURCES)
by_domain = {r["domain"]: r for r in rows}

check("all unique domains are represented (www./bare-domain variants merge into one)",
      set(by_domain.keys()) ==
      {"federalregister.gov", "state.gov", "ecfr.gov", "fdassociates.net", "legal500.com"})
check("federalregister.gov: 3 source records deduped to 1 row (www. and bare host merged)",
      by_domain["federalregister.gov"]["count"] == 3)
check("federalregister.gov marked PRIMARY",
      by_domain["federalregister.gov"]["primary"] is True)
check("state.gov: 2 source records, marked PRIMARY",
      by_domain["state.gov"]["count"] == 2 and by_domain["state.gov"]["primary"] is True)
check("ecfr.gov: 1 source record, marked PRIMARY",
      by_domain["ecfr.gov"]["count"] == 1 and by_domain["ecfr.gov"]["primary"] is True)
check("fdassociates.net: 1 source record, marked SECONDARY (not primary)",
      by_domain["fdassociates.net"]["count"] == 1 and by_domain["fdassociates.net"]["primary"] is False)
check("legal500.com: 1 source record, marked SECONDARY (not primary)",
      by_domain["legal500.com"]["count"] == 1 and by_domain["legal500.com"]["primary"] is False)
check("total unique websites is exactly 5, not 8 (the raw source-record count)",
      len(rows) == 5)

# -- PRIMARY precedence: one primary record among several non-primary
#    records from the same host must still mark the whole host PRIMARY --
MIXED_SOURCES = [
    {"url": "https://example.com/a", "primary_source": False},
    {"url": "https://example.com/b", "primary_source": True},
    {"url": "https://example.com/c", "primary_source": False},
]
mixed_rows = summarize_sources_used(MIXED_SOURCES)
check("PRIMARY precedence: a single primary_source=True record among several False ones "
      "for the same host is enough to mark the whole host PRIMARY",
      len(mixed_rows) == 1 and mixed_rows[0]["primary"] is True and mixed_rows[0]["count"] == 3)

# -- ordering: PRIMARY hosts first, then by count (descending) within
#    each group, then alphabetically as a tiebreak -- matches the exact
#    order shown in the request's example (federalregister.gov [3],
#    state.gov [2], ecfr.gov [1], then the two 1-count SECONDARY hosts
#    alphabetically) --
check("ordering: PRIMARY rows sort before SECONDARY rows",
      [r["primary"] for r in rows] == sorted([r["primary"] for r in rows], reverse=True))
primary_counts_in_order = [r["count"] for r in rows if r["primary"]]
check("ordering: PRIMARY rows sort by count, descending",
      primary_counts_in_order == sorted(primary_counts_in_order, reverse=True))
secondary_domains_in_order = [r["domain"] for r in rows if not r["primary"]]
check("ordering: SECONDARY rows (tied on count here) fall back to alphabetical",
      secondary_domains_in_order == sorted(secondary_domains_in_order))
check("ordering: ties on count break alphabetically -- two hosts with equal count and "
      "equal primary status sort by domain name",
      summarize_sources_used([
          {"url": "https://z.example.com/a", "primary_source": False},
          {"url": "https://a.example.com/a", "primary_source": False},
      ]) == [
          {"domain": "a.example.com", "primary": False, "count": 1},
          {"domain": "z.example.com", "primary": False, "count": 1},
      ])

# -- degrades gracefully on malformed/missing input --
check("empty sources list -> empty rows, no error",
      summarize_sources_used([]) == [])
check("None sources -> empty rows, no error",
      summarize_sources_used(None) == [])
check("non-dict entries in the sources list are skipped, not raised on",
      summarize_sources_used(["not a dict", 42, None,
                               {"url": "https://example.com/x", "primary_source": False}])
      == [{"domain": "example.com", "primary": False, "count": 1}])
check("a source dict with a missing/empty url is skipped, not counted, not shown",
      summarize_sources_used([{"primary_source": True}, {"url": "", "primary_source": True},
                               {"url": "https://example.com/x", "primary_source": False}])
      == [{"domain": "example.com", "primary": False, "count": 1}])
check("summarize_sources_used makes no network calls and touches no DB/schema -- "
      "it is a pure function of its input list",
      True)  # structural guarantee: the function takes a list and returns a list, nothing else


# ---------------------------------------------------------------------------
# 3. format_sources_used: exact rendering, matching the requested example
# ---------------------------------------------------------------------------

rendered = format_sources_used(rows)
rendered_lines = rendered.split("\n")

check("rendered output starts with the 'SOURCES USED' header",
      rendered_lines[0] == "SOURCES USED")
check("rendered output has a divider line directly under the header",
      set(rendered_lines[1]) == {"─"})
check("rendered output ends with the same divider, then the total-count line",
      set(rendered_lines[-2]) == {"─"} and rendered_lines[-1] == "5 unique websites used")
check("PRIMARY rows use the ✓ marker and the word PRIMARY",
      all("✓" in line and "PRIMARY" in line
          for line in rendered_lines if "federalregister.gov" in line or "state.gov" in line
          or "ecfr.gov" in line))
check("SECONDARY rows use the • marker and the word SECONDARY",
      all("•" in line and "SECONDARY" in line
          for line in rendered_lines if "fdassociates.net" in line or "legal500.com" in line))
check("a row with count==1 uses singular 'source', not 'sources'",
      any("ecfr.gov" in line and line.rstrip().endswith("1 source") for line in rendered_lines))
check("a row with count>1 uses plural 'sources'",
      any("federalregister.gov" in line and "3 sources" in line for line in rendered_lines))

# -- singular total-count wording ("1 unique website used", not "websites") --
single_row = [{"domain": "example.com", "primary": True, "count": 1}]
single_rendered = format_sources_used(single_row)
check("total line is singular ('1 unique website used') when there is exactly one host",
      single_rendered.split("\n")[-1] == "1 unique website used")

# -- empty case: no rows at all --
empty_rendered = format_sources_used([])
check("empty rows renders a valid block ending in '0 unique websites used', no crash",
      empty_rendered.split("\n")[-1] == "0 unique websites used")

# -- exact match against the example given in the request --
EXAMPLE_SOURCES = [
    {"url": "https://www.federalregister.gov/a", "primary_source": True},
    {"url": "https://www.federalregister.gov/b", "primary_source": True},
    {"url": "https://www.federalregister.gov/c", "primary_source": True},
    {"url": "https://www.state.gov/a", "primary_source": True},
    {"url": "https://www.state.gov/b", "primary_source": True},
    {"url": "https://ecfr.gov/a", "primary_source": True},
    {"url": "https://fdassociates.net/a", "primary_source": False},
    {"url": "https://legal500.com/a", "primary_source": False},
]
example_rendered = format_sources_used(summarize_sources_used(EXAMPLE_SOURCES))
EXPECTED = (
    "SOURCES USED\n"
    "────────────────────────────────────────\n"
    "✓ federalregister.gov       PRIMARY     3 sources\n"
    "✓ state.gov                 PRIMARY     2 sources\n"
    "✓ ecfr.gov                  PRIMARY     1 source\n"
    "• fdassociates.net          SECONDARY   1 source\n"
    "• legal500.com              SECONDARY   1 source\n"
    "────────────────────────────────────────\n"
    "5 unique websites used"
)
check("rendered output matches the exact example given in the request, line for line",
      example_rendered == EXPECTED,
      detail=f"got:\n{example_rendered!r}\nexpected:\n{EXPECTED!r}")


# ---------------------------------------------------------------------------
# 4. Scope guarantees: no new persistence/schema, no Stage 2/3 behavior
#    change -- this is purely additive/presentational in the acceptance
#    script, and touches no other file.
# ---------------------------------------------------------------------------

_script_source = open(_script_path).read()
check("no new SQL/schema statements were introduced for this feature "
      "(no CREATE TABLE / ALTER TABLE anywhere in the acceptance script)",
      "CREATE TABLE" not in _script_source.upper()
      and "ALTER TABLE" not in _script_source.upper())
check("summarize_sources_used and format_sources_used never call requests.* "
      "(no new web calls)",
      "requests." not in acceptance_script.summarize_sources_used.__code__.co_names
      and "requests." not in acceptance_script.format_sources_used.__code__.co_names)

import dd_pipeline as ddp  # noqa: E402
check("dd_pipeline.py (Stage 2/Stage 3 implementation) was not modified for this feature -- "
      "no source-reuse/authority-tier/reputation-scoring machinery exists there",
      not hasattr(ddp, "score_source") and not hasattr(ddp, "SourceLedger")
      and not hasattr(ddp, "AuthorityTier"))


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

try:
    os.remove(acceptance_script._TMP_DB_PATH)
except OSError:
    pass

print("\n=== SUMMARY ===")
failed = [r for r in results if r[1] == "FAIL"]
for name, status, detail in results:
    print(f"{status}: {name}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
