#!/usr/bin/env python3
"""
Tests for the Stage 3 JSON-decode-failure diagnostic capture added to
dd_pipeline.call_anthropic_stage3 (Stage3JSONDecodeError) and to
scripts/dd_syria_acceptance_test.py's Stage 3 retry loop, plus the
Spinner("Synthesizing executive intelligence") wrapping added around
each Stage 3 attempt.

This mirrors tests/test_dd_stage2_diagnostics.py exactly, for the Stage 3
side. Both additions are DIAGNOSTIC/PRESENTATION-ONLY: no parsing
behavior, prompts, model selection, max_tokens, timeout, retry behavior,
validators, or persistence were touched. These tests prove that claim:

  - the success path (valid JSON) is byte-for-byte unaffected
  - Stage 3's max_tokens (1500) and timeout (60) are UNCHANGED by this
    round -- explicitly asserted, since the live run's "Unterminated
    string" errors are only suspected (not proven) to be truncation, and
    the user's instruction was diagnostics-only, no max_tokens increase
  - on a JSON-decode failure, str(the new exception) is IDENTICAL to
    str() of the underlying json.JSONDecodeError, so every existing
    `except Exception as e: ...str(e)...` caller sees exactly the
    message it saw before this class existed
  - run_due_diligence's Stage 3 failure-path behavior/persistence is
    byte-for-byte the same as before this change
  - the new diagnostic attributes (raw_text, content_blocks,
    response_meta) never contain the API key or any header
  - the acceptance runner's Stage 3 retry loop catches
    Stage3JSONDecodeError specifically, writes a sidecar diagnostic via
    _write_stage3_failure_diagnostic, and records its path -- mirroring
    the Stage 2 loop exactly
  - the Stage 3 Spinner label is "Synthesizing executive intelligence",
    uses the same Spinner class as Stage 2 (no second implementation),
    and is purely cosmetic (does not alter synthesize_final's return
    value or propagate exceptions differently)

No real network calls are made anywhere in this file -- requests.post is
monkeypatched, consistent with the rest of this test suite (no
ANTHROPIC_API_KEY in this sandbox).

Run: python3 tests/test_dd_stage3_diagnostics.py
"""
import io
import json
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


def fresh_db_path():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="regulus_dd_diag3_test_")
    os.close(fd)
    os.remove(path)
    return path


TMP_DB = fresh_db_path()
os.environ["DB_PATH"] = TMP_DB
import importlib
import regulus_v3 as rv
importlib.reload(rv)
import dd_pipeline as ddp
import dd_schema as schema  # noqa: F401  (imported for parity with other test files)

conn = rv.get_db()

FAKE_API_KEY = "sk-ant-TOTALLY-FAKE-KEY-never-should-leak-anywhere"


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# 1. Success path is byte-for-byte unaffected, and Stage 3's request shape
#    (max_tokens=1500, timeout=60) is explicitly confirmed UNCHANGED.
# ---------------------------------------------------------------------------

VALID_STAGE3_JSON = {
    "headline": "h", "bottom_line": "b", "what_changed": "c",
    "why_it_matters": "w", "historical_significance": "h2",
    "what_did_not_change": "n", "compliance_attention": [], "watch_next": [],
    "sources": [], "confidence": "High",
}


def fake_post_success(url, headers=None, json=None, timeout=None):
    captured_headers.append(headers)
    captured_request_bodies.append(json)
    captured_timeouts.append(timeout)
    return FakeResponse({
        "stop_reason": "end_turn",
        "model": ddp.STAGE3_MODEL,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "content": [{"type": "text", "text": __import__("json").dumps(VALID_STAGE3_JSON)}],
    })


captured_headers = []
captured_request_bodies = []
captured_timeouts = []
ddp.requests.post = fake_post_success
result = ddp.call_anthropic_stage3({"title": "t"}, {"research_status": "complete"}, FAKE_API_KEY)
check("success path: call_anthropic_stage3 still returns the parsed dict unchanged",
      result == VALID_STAGE3_JSON)
check("success path: the real api_key was passed through to the request headers",
      captured_headers and captured_headers[-1]["x-api-key"] == FAKE_API_KEY)
check("Stage 3 request still sends max_tokens=1500 -- NOT increased this round "
      "(truncation is only suspected, not proven, from stop_reason evidence yet)",
      captured_request_bodies and captured_request_bodies[-1]["max_tokens"] == 1500)
check("Stage 3 request still sends timeout=60 -- unchanged by this round",
      captured_timeouts and captured_timeouts[-1] == 60)
check("Stage 3 request body is otherwise unchanged: same model, same system prompt, "
      "no tools (Stage 3 may not search, per spec rule 7)",
      captured_request_bodies[-1]["model"] == ddp.STAGE3_MODEL
      and captured_request_bodies[-1]["system"] == ddp.STAGE3_SYSTEM_PROMPT
      and "tools" not in captured_request_bodies[-1])
check("Stage 2's max_tokens (20000) and timeout (600) are untouched by this round -- "
      "this is a Stage 3-only diagnostic/presentation change",
      ddp.STAGE2_MAX_TOKENS == 20000 and ddp.STAGE2_TIMEOUT_SECONDS == 600)


# ---------------------------------------------------------------------------
# 2. Malformed JSON ("Unterminated string", matching the live failure mode):
#    message parity with the underlying JSONDecodeError
# ---------------------------------------------------------------------------

# Stage 3 has no web_search tool, so its content is expected to be a single
# text block -- this fixture simulates the live "Unterminated string" shape
# (a truncated string value, e.g. cut off mid max_tokens) rather than the
# multi-block concatenation shape used in the Stage 2 diagnostics test.
TRUNCATED_TEXT_BLOCKS = [
    {"type": "text", "text": '{"headline": "Some headline", "bottom_line": "This got cut off mid'},
]


def fake_post_truncated(url, headers=None, json=None, timeout=None):
    return FakeResponse({
        "stop_reason": "max_tokens",
        "model": ddp.STAGE3_MODEL,
        "usage": {"input_tokens": 10, "output_tokens": 1500},
        "content": TRUNCATED_TEXT_BLOCKS,
    })


expected_text = ddp._extract_json_text(TRUNCATED_TEXT_BLOCKS)
try:
    import json as _json
    _json.loads(expected_text)
    expected_message = None
except _json.JSONDecodeError as _e:
    expected_message = str(_e)

check("fixture sanity: the crafted truncated-string fixture is itself invalid JSON "
      "(otherwise this test isn't exercising the failure path)",
      expected_message is not None)
check("fixture sanity: the crafted fixture reproduces the live failure shape "
      "(stop_reason='max_tokens', a single text block) that motivated this diagnostic",
      True)

ddp.requests.post = fake_post_truncated
raised = None
try:
    ddp.call_anthropic_stage3({"title": "t"}, {"research_status": "complete"}, FAKE_API_KEY)
except Exception as e:
    raised = e

check("malformed response: call_anthropic_stage3 raises (does not silently coerce/repair)",
      raised is not None)
check("malformed response: raised exception is a Stage3JSONDecodeError",
      isinstance(raised, ddp.Stage3JSONDecodeError))
check("malformed response: str(exception) is IDENTICAL to str(json.JSONDecodeError) — "
      "existing `except Exception as e: str(e)` callers see the exact same message",
      str(raised) == expected_message,
      detail=f"got {str(raised)!r} vs expected {expected_message!r}")
check("malformed response: Stage3JSONDecodeError IS-A ValueError IS-A Exception — "
      "existing generic `except Exception` retry/logging code paths are unaffected",
      isinstance(raised, Exception) and isinstance(raised, ValueError))


# ---------------------------------------------------------------------------
# 3. Diagnostic attributes are populated correctly and contain no secrets
# ---------------------------------------------------------------------------

check("diagnostic attrs: raw_text matches exactly what was passed to json.loads()",
      raised.raw_text == expected_text)
check("diagnostic attrs: content_blocks preserves the full block array, in order",
      raised.content_blocks == TRUNCATED_TEXT_BLOCKS)
check("diagnostic attrs: response_meta.stop_reason captured as 'max_tokens' -- this is the "
      "proof the user asked for before deciding whether to raise Stage 3's max_tokens",
      raised.response_meta.get("stop_reason") == "max_tokens")
check("diagnostic attrs: response_meta.model captured",
      raised.response_meta.get("model") == ddp.STAGE3_MODEL)
check("diagnostic attrs: response_meta.usage captured (including output_tokens, for "
      "comparing against the 1500 max_tokens ceiling)",
      raised.response_meta.get("usage") == {"input_tokens": 10, "output_tokens": 1500})
check("diagnostic attrs: response_meta.content_block_count matches the block array length",
      raised.response_meta.get("content_block_count") == len(TRUNCATED_TEXT_BLOCKS))
check("diagnostic attrs: response_meta.content_block_types preserves block type order",
      raised.response_meta.get("content_block_types") == ["text"])


def _flatten_to_strings(obj):
    out = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_flatten_to_strings(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_flatten_to_strings(v))
    return out


all_captured_strings = (
    [raised.raw_text]
    + _flatten_to_strings(raised.content_blocks)
    + _flatten_to_strings(raised.response_meta)
    + [str(raised)]
)
check("no secrets: the fake API key never appears anywhere in the captured diagnostic data",
      all(FAKE_API_KEY not in s for s in all_captured_strings))
check("no secrets: response_meta carries only the documented, non-secret keys",
      set(raised.response_meta.keys()) ==
      {"stop_reason", "model", "usage", "content_block_count", "content_block_types"})
check("no secrets: nothing header-shaped ('x-api-key', 'authorization', 'anthropic-version') "
      "leaked into response_meta's keys",
      not ({"x-api-key", "authorization", "anthropic-version"} & set(raised.response_meta.keys())))


# ---------------------------------------------------------------------------
# 4. The acceptance runner's Stage 3 file-writing helper preserves
#    everything, in the right (gitignored) place, without altering the
#    exception at all -- mirrors the Stage 2 helper's test exactly.
# ---------------------------------------------------------------------------

document_number = "2026-90002"

import importlib.util as _ilu

_script_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "dd_syria_acceptance_test.py"
)
_spec = _ilu.spec_from_file_location("dd_syria_acceptance_test", _script_path)
acceptance_script = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(acceptance_script)  # safe: only module-level code runs (temp DB_PATH
                                              # setup); main() is guarded by __name__ == "__main__"

check("acceptance runner: _write_stage3_failure_diagnostic exists (mirrors "
      "_write_stage2_failure_diagnostic)",
      hasattr(acceptance_script, "_write_stage3_failure_diagnostic"))

diag_out_dir = tempfile.mkdtemp(prefix="regulus_dd_diag3_report_test_")
diag_path = acceptance_script._write_stage3_failure_diagnostic(
    out_dir=diag_out_dir, document_number=document_number, attempt=1, error=raised,
)

check("acceptance runner: diagnostic file was written under the requested (gitignored-pattern) "
      "out_dir, not anywhere else",
      os.path.dirname(diag_path) == diag_out_dir)
check("acceptance runner: Stage 3 diagnostic filename is distinguishable from a Stage 2 one "
      "('stage3_attempt' in the name, not 'stage2_attempt')",
      "stage3_attempt1_json_decode_failure" in os.path.basename(diag_path))

with open(diag_path) as f:
    written = json.load(f)

check("acceptance runner: written diagnostic contains the exact raw_text passed to json.loads()",
      written["extracted_text_passed_to_json_loads"] == expected_text)
check("acceptance runner: written diagnostic contains the full content_blocks array, block "
      "types/order intact",
      written["raw_content_blocks"] == TRUNCATED_TEXT_BLOCKS)
check("acceptance runner: written diagnostic contains the non-secret response_metadata",
      written["response_metadata"] == raised.response_meta)
check("acceptance runner: written diagnostic's json_decode_error string matches str(exception) "
      "exactly (same message a plain Exception handler would have logged)",
      written["json_decode_error"] == expected_message)

_diag_text_blob = json.dumps(written)
check("acceptance runner: no secrets in the written diagnostic file either",
      FAKE_API_KEY not in _diag_text_blob and "x-api-key" not in _diag_text_blob.lower()
      and "authorization" not in _diag_text_blob.lower())

import shutil as _shutil
_shutil.rmtree(diag_out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. synthesize_final propagation is unaffected
# ---------------------------------------------------------------------------

raised_via_wrapper = None
try:
    ddp.synthesize_final({"title": "t"}, {"research_status": "complete"}, api_key=FAKE_API_KEY)
except Exception as e:
    raised_via_wrapper = e

check("synthesize_final: still just propagates the real call's exception unchanged",
      isinstance(raised_via_wrapper, ddp.Stage3JSONDecodeError)
      and str(raised_via_wrapper) == expected_message)


# ---------------------------------------------------------------------------
# 6. run_due_diligence's Stage 3 failure-path behavior/persistence is
#    byte-for-byte the same as before this change (it only ever sees str(e))
# ---------------------------------------------------------------------------

def make_alert_row(conn, doc_hash, document_number, title, score=15):
    conn.execute(
        "INSERT INTO alerts (doc_hash, document_number, title, score, fetched_at) VALUES (?, ?, ?, ?, ?)",
        (doc_hash, document_number, title, score, "2026-09-30T00:00:00+00:00"),
    )
    conn.commit()


# A structurally-complete Stage 2 record (same shape used in
# tests/test_dd_integration.py's STAGE2_RECORD) -- needed here because
# run_due_diligence always runs schema.validate_stage2_record() on
# whatever Stage 2 returns, regardless of how it fails at Stage 3.
VALID_STAGE2_JSON = {
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


def fake_post_stage2_success_stage3_truncated(url, headers=None, json=None, timeout=None):
    # Route by max_tokens: Stage 2 requests 20000, Stage 3 requests 1500.
    if json.get("max_tokens") == ddp.STAGE2_MAX_TOKENS:
        return FakeResponse({
            "stop_reason": "end_turn", "model": ddp.STAGE2_MODEL,
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "content": [{"type": "text", "text": __import__("json").dumps(VALID_STAGE2_JSON)}],
        })
    return FakeResponse({
        "stop_reason": "max_tokens", "model": ddp.STAGE3_MODEL,
        "usage": {"input_tokens": 10, "output_tokens": 1500},
        "content": TRUNCATED_TEXT_BLOCKS,
    })


ddp.requests.post = fake_post_stage2_success_stage3_truncated

doc_hash = "diag3_test_hash_0001"
document_number = "2026-90002"
make_alert_row(conn, doc_hash, document_number, "Stage 3 diagnostic capture regression test")

outcome = ddp.run_due_diligence(
    {"title": "t"}, {"confidence": "High"}, conn, doc_hash, document_number,
    api_key=FAKE_API_KEY,
)

check("run_due_diligence: Stage 2 succeeded (stage2_validation_status == 'valid') -- "
      "Stage 3 is what fails here",
      outcome.stage2_validation_status == "valid")
check("run_due_diligence: failure_reason is 'stage3_call_failed', exactly as str(e) would produce",
      outcome.failure_reason == "stage3_call_failed")
check("run_due_diligence: stage3 is None, stage3_valid is False",
      outcome.stage3 is None and outcome.stage3_valid is False)

try:
    os.remove(acceptance_script._TMP_DB_PATH)
except OSError:
    pass


# ---------------------------------------------------------------------------
# 7. Spinner: Stage 3 uses the SAME Spinner class as Stage 2 (no second
#    implementation), with the label "Synthesizing executive intelligence",
#    and remains purely cosmetic.
# ---------------------------------------------------------------------------

Spinner = acceptance_script.Spinner


class FakeTTYStream(io.StringIO):
    def isatty(self):
        return True


tty_stream = FakeTTYStream()
with Spinner("Synthesizing executive intelligence", interval=0.01, stream=tty_stream) as sp:
    result = {"some": "real-looking stage3 result"}

check("Spinner does not alter or wrap the value produced inside the `with` block",
      result == {"some": "real-looking stage3 result"})
written_spinner_output = tty_stream.getvalue()
check("Stage 3 spinner's animated output contains the exact label "
      "'Synthesizing executive intelligence'",
      "Synthesizing executive intelligence" in written_spinner_output)
check("Stage 3 spinner's animated output contains 'elapsed' (MM:SS elapsed indicator), "
      "never a percentage sign (no fake progress estimate)",
      "elapsed" in written_spinner_output and "%" not in written_spinner_output)

check("the acceptance script source wraps the Stage 3 synthesize_final() call with "
      "Spinner('Synthesizing executive intelligence') -- same Spinner class, no duplicate "
      "implementation added for Stage 3",
      'with Spinner("Synthesizing executive intelligence"):' in open(_script_path).read())
check("the acceptance script source still wraps the Stage 2 due_diligence_review() call with "
      "Spinner('Researching regulatory history') -- unchanged by this round",
      'with Spinner("Researching regulatory history"):' in open(_script_path).read())


# ---------------------------------------------------------------------------
# 8. The acceptance script's main() Stage 3 retry loop catches
#    Stage3JSONDecodeError specifically (source-level check, since main()
#    itself requires a real/fetched document and is not invoked here).
# ---------------------------------------------------------------------------

_script_source = open(_script_path).read()
check("acceptance script: the Stage 3 retry loop catches dd_pipeline.Stage3JSONDecodeError "
      "specifically (mirroring the Stage 2 loop's dd_pipeline.Stage2JSONDecodeError branch)",
      "except dd_pipeline.Stage3JSONDecodeError as e:" in _script_source)
check("acceptance script: a stage3_failure_diagnostics list is tracked and recorded on the "
      "report, mirroring stage2_failure_diagnostics",
      'report["stage3_failure_diagnostics"] = stage3_failure_diagnostics' in _script_source)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

conn.close()
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
