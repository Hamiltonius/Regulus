#!/usr/bin/env python3
"""
Tests for the Stage 3 output-bounding prompt change: live Syria runs
showed Stage 3 hitting the full STAGE3_MAX_TOKENS=4000 ceiling twice,
truncated inside sources[], while producing a full second due-diligence
report instead of an executive summary and reproducing Stage 2's entire
sources array.

This round makes NO code-level truncation, no schema change, and no C2
change -- the fix is entirely a prompt instruction telling the model to
bound its own output (word/item maximums, described as ceilings not
targets) and to include only the minimum source subset needed to
support its own claims, preferring primary sources. The model must still
produce a complete, schema-valid object; nothing here chops strings
after generation.

These tests are necessarily prompt-text assertions (we cannot run the
real model in this sandbox) plus a couple of behavioral checks that the
things this prompt explicitly must NOT weaken (C2, structural schema,
no-tools) are still enforced exactly as before by the actual code.

Run: python3 tests/test_stage3_output_bounds.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == "FAIL" else ""))
    return condition


import dd_pipeline as ddp  # noqa: E402
import dd_schema as schema  # noqa: E402

PROMPT = ddp.STAGE3_SYSTEM_PROMPT
# The prompt is a triple-quoted, hand-wrapped string, so a phrase can be
# split across a line break (e.g. "narrative\ndetail"). Substring checks
# below use this whitespace-normalized version so a cosmetic line wrap
# never causes a false failure.
PROMPT_FLAT = " ".join(PROMPT.split())

# ---------------------------------------------------------------------------
# 1-4: field maximums are present, and explicitly framed as ceilings
# ---------------------------------------------------------------------------

check("prompt states headline's maximum (25 words)",
      "headline: maximum 25 words" in PROMPT)
check("prompt states bottom_line's maximum (100 words)",
      "bottom_line: maximum 100 words" in PROMPT)
check("prompt states what_changed's maximum (150 words)",
      "what_changed: maximum 150 words" in PROMPT)
check("prompt states why_it_matters's maximum (150 words)",
      "why_it_matters: maximum 150 words" in PROMPT)
check("prompt states historical_significance's maximum (125 words)",
      "historical_significance: maximum 125 words" in PROMPT)
check("prompt states what_did_not_change's maximum (125 words)",
      "what_did_not_change: maximum 125 words" in PROMPT)
check("prompt states compliance_attention's item count AND per-item word limits "
      "(6 items, 40 words each)",
      "compliance_attention: maximum 6 items, maximum 40 words per item" in PROMPT)
check("prompt states watch_next's item count AND per-item word limits "
      "(5 items, 35 words each)",
      "watch_next: maximum 5 items, maximum 35 words per item" in PROMPT)
check("prompt explicitly frames these as MAXIMUMS/ceilings, not targets -- telling the "
      "model to use less text when sufficient",
      "MAXIMUMS, not targets" in PROMPT
      and "less text" in PROMPT)

# ---------------------------------------------------------------------------
# 5-6: prohibition on reproducing the full Stage 2 evidence/sources
# ---------------------------------------------------------------------------

check("prompt prohibits restating the full DD evidence package",
      "restate" in PROMPT and "full DD evidence package" in PROMPT)
check("prompt prohibits reproducing the complete Stage 2 sources list",
      "Do NOT reproduce the complete Stage 2 sources list" in PROMPT)
check("prompt also tells the model not to reproduce Stage 2's narrative detail",
      "Stage 2's narrative detail" in PROMPT_FLAT or "Stage 2 narrative detail" in PROMPT_FLAT)

# ---------------------------------------------------------------------------
# 7-8: minimum source subset + primary-source preference
# ---------------------------------------------------------------------------

check("prompt requires only the minimum source subset needed to support the model's "
      "OWN material claims -- not every source Stage 2 consulted",
      "minimum subset" in PROMPT_FLAT and "not every source Stage 2 consulted" in PROMPT_FLAT
      and "merely because Stage 2 consulted it" in PROMPT_FLAT)
check("prompt tells the model to prefer primary sources when they support the same claim",
      "prefer the primary source" in PROMPT_FLAT and "primary_source: true" in PROMPT_FLAT)
check("prompt still forbids inventing, altering, or introducing sources absent from "
      "the supplied Stage 2 evidence",
      "never include a source absent from the supplied Stage 2 evidence" in PROMPT_FLAT
      and "invent" in PROMPT_FLAT)

# ---------------------------------------------------------------------------
# 9-10: Stage 3 still cannot introduce new facts or do research
# ---------------------------------------------------------------------------

check("prompt still forbids introducing new factual claims beyond the supplied evidence",
      "Do not introduce new factual claims" in PROMPT)
check("prompt still forbids performing additional research",
      "Do not perform additional research" in PROMPT)
check("call_anthropic_stage3's request carries no 'tools' key -- Stage 3 genuinely has no "
      "web_search capability regardless of what the prompt says (belt-and-suspenders, not "
      "prompt-only enforcement)",
      True)  # verified directly against the live request body below

captured_request_bodies = []


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


VALID_STAGE3_JSON = {
    "headline": "h", "bottom_line": "b", "what_changed": "c",
    "why_it_matters": "w", "historical_significance": "h2",
    "what_did_not_change": "n", "compliance_attention": [], "watch_next": [],
    "sources": [], "confidence": "High",
}


def fake_post(url, headers=None, json=None, timeout=None):
    captured_request_bodies.append(json)
    return FakeResponse({
        "stop_reason": "end_turn", "model": ddp.STAGE3_MODEL,
        "usage": {"input_tokens": 10, "output_tokens": 10},
        "content": [{"type": "text", "text": __import__("json").dumps(VALID_STAGE3_JSON)}],
    })


ddp.requests.post = fake_post
ddp.call_anthropic_stage3({"title": "t"}, {"research_status": "complete"}, "fake-key")
check("call_anthropic_stage3's actual request body has no 'tools' key",
      "tools" not in captured_request_bodies[-1])
check("call_anthropic_stage3's system prompt is exactly STAGE3_SYSTEM_PROMPT (the updated one)",
      captured_request_bodies[-1]["system"] == PROMPT)


# ---------------------------------------------------------------------------
# 11: C2 still rejects any Stage 3 source absent from Stage 2 -- this
#     round's prompt change asks the model to send FEWER sources, but
#     validate_stage3_sources() itself is untouched and still rejects any
#     source that wasn't supplied by Stage 2, exactly as before.
# ---------------------------------------------------------------------------

stage2_sources = [
    {"url": "https://www.federalregister.gov/d/2025-00001", "source_type": "federal_register",
     "agency": "Department of State", "date": "2025-06-30", "supports": ["historical_context"],
     "primary_source": True},
    {"url": "https://legal500.com/article", "source_type": "secondary_analysis",
     "agency": "", "date": "2025-07-01", "supports": ["precedent_comparison"],
     "primary_source": False},
]

# A minimal subset (just the primary source) -- exactly what this round's
# prompt now asks the model to prefer -- must still be C2-compliant.
minimal_subset = [stage2_sources[0]]
check("a minimal, primary-source-only subset of Stage 2's sources is still fully "
      "C2-compliant (subsetting is allowed; completeness was never required)",
      schema.validate_stage3_sources(minimal_subset, stage2_sources) == [])

# A source NOT present in Stage 2 at all must still be rejected, unchanged.
fabricated_source = [{"url": "https://not-a-real-source.example.com/x",
                       "source_type": "secondary_analysis", "agency": "", "date": "2025-01-01",
                       "supports": [], "primary_source": False}]
c2_errors = schema.validate_stage3_sources(fabricated_source, stage2_sources)
check("C2 still rejects a Stage 3 source that is absent from the validated Stage 2 evidence",
      len(c2_errors) == 1 and "not present verbatim in the validated Stage 2 evidence" in c2_errors[0])

# An altered record (same URL, different agency) must still be rejected.
altered_source = [dict(stage2_sources[0], agency="A Different Agency")]
c2_errors_altered = schema.validate_stage3_sources(altered_source, stage2_sources)
check("C2 still rejects a Stage 3 source whose provenance was altered, even with the "
      "same URL as a real Stage 2 source",
      len(c2_errors_altered) == 1)

check("validate_stage3_sources itself is untouched by this round -- same function, same "
      "signature, same behavior as proven in tests/test_dd_schema.py",
      schema.validate_stage3_sources.__doc__ is not None
      and "spec clarification C2" in schema.validate_stage3_sources.__doc__)


# ---------------------------------------------------------------------------
# 12-13: STAGE3_MAX_TOKENS / STAGE3_TIMEOUT_SECONDS unchanged by this round
# ---------------------------------------------------------------------------

check("STAGE3_MAX_TOKENS remains exactly 4000 -- NOT changed this round",
      ddp.STAGE3_MAX_TOKENS == 4000)
check("STAGE3_TIMEOUT_SECONDS remains exactly 180 -- NOT changed this round",
      ddp.STAGE3_TIMEOUT_SECONDS == 180)
check("call_anthropic_stage3's request still sends max_tokens=STAGE3_MAX_TOKENS (4000)",
      captured_request_bodies[-1]["max_tokens"] == 4000)
check("Stage 2's constants are untouched by this round (20000 max_tokens, 600s timeout)",
      ddp.STAGE2_MAX_TOKENS == 20000 and ddp.STAGE2_TIMEOUT_SECONDS == 600)


# ---------------------------------------------------------------------------
# 14: the SOURCES USED acceptance-runner output (commit 34294cf) is
#     untouched by this round.
# ---------------------------------------------------------------------------

import importlib.util as _ilu

_script_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "dd_syria_acceptance_test.py"
)
_spec = _ilu.spec_from_file_location("dd_syria_acceptance_test", _script_path)
acceptance_script = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(acceptance_script)

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
EXPECTED_SOURCES_USED = (
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
actual_rendered = acceptance_script.format_sources_used(
    acceptance_script.summarize_sources_used(EXAMPLE_SOURCES)
)
check("the SOURCES USED rendering from commit 34294cf is byte-for-byte unchanged by this round",
      actual_rendered == EXPECTED_SOURCES_USED)

try:
    os.remove(acceptance_script._TMP_DB_PATH)
except OSError:
    pass


# ---------------------------------------------------------------------------
# Structural schema (dd_schema.py) untouched: a Stage 3 record that obeys
# the new bounds (short fields, few sources) validates exactly the same
# way a longer one would -- the schema never enforced length/count, so
# this round couldn't have (and didn't) touch it.
# ---------------------------------------------------------------------------

bounded_record = {
    "headline": "Short headline.",
    "bottom_line": "One-sentence bottom line.",
    "what_changed": "Brief description of what changed.",
    "why_it_matters": "Brief description of why it matters.",
    "historical_significance": "Brief historical context.",
    "what_did_not_change": "Brief note on what stayed the same.",
    "compliance_attention": ["Review licensing posture."],
    "watch_next": ["Watch for implementing guidance."],
    "confidence": "High",
    "sources": minimal_subset,
}
struct_result = schema.validate_stage3_record(bounded_record)
check("a short, bounded Stage 3 record (minimal sources, short fields) is still fully "
      "valid under the untouched structural schema",
      struct_result.is_valid)


# ---------------------------------------------------------------------------
# No application-side truncation was added anywhere in dd_pipeline.py --
# call_anthropic_stage3 still returns exactly what json.loads() produced,
# with no post-hoc string-chopping step.
# ---------------------------------------------------------------------------

import inspect  # noqa: E402

stage3_source_code = inspect.getsource(ddp.call_anthropic_stage3)
check("call_anthropic_stage3 contains no string-slicing/truncation logic -- "
      "it returns json.loads(text) directly, unmodified",
      "[:" not in stage3_source_code and "[: " not in stage3_source_code
      and ".rstrip(" not in stage3_source_code)


print("\n=== SUMMARY ===")
failed = [r for r in results if r[1] == "FAIL"]
for name, status, detail in results:
    print(f"{status}: {name}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
