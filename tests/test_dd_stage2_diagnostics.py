#!/usr/bin/env python3
"""
Tests for the Stage 2 JSON-decode-failure diagnostic capture added to
dd_pipeline.call_anthropic_stage2 (Stage2JSONDecodeError) and to
scripts/dd_syria_acceptance_test.py's Stage 2 retry loop.

This is a DIAGNOSTIC-ONLY change: no parsing behavior, prompts, model
selection, max_tokens, web_search config, retry behavior, validators, or
Gate behavior were touched. These tests exist specifically to prove that
claim -- that adding diagnostic capture does not change success/failure
semantics anywhere in the pipeline:

  - the success path (valid JSON) is byte-for-byte unaffected
  - on a JSON-decode failure, str(the new exception) is IDENTICAL to
    str() of the underlying json.JSONDecodeError, so every existing
    `except Exception as e: ...str(e)...` caller (run_due_diligence's
    retry/logging/persistence, the acceptance runner) sees exactly the
    message it saw before this class existed
  - run_due_diligence's DDOutcome (failure_reason, validation_errors,
    persisted due_diligence_records row) is unchanged
  - the new diagnostic attributes (raw_text, content_blocks,
    response_meta) never contain the API key or any header

No real network calls are made anywhere in this file -- requests.post is
monkeypatched, consistent with the rest of this test suite (no
ANTHROPIC_API_KEY in this sandbox).

Run: python3 tests/test_dd_stage2_diagnostics.py
"""
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
    fd, path = tempfile.mkstemp(suffix=".db", prefix="regulus_dd_diag_test_")
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
# 1. Success path is byte-for-byte unaffected
# ---------------------------------------------------------------------------

VALID_STAGE2_JSON = {
    "research_question": "q", "current_event": {}, "historical_context": {},
    "precedent_comparison": {}, "legal_regulatory_effect": {}, "scope": {},
    "impact_assessment": {}, "follow_on_indicators": {}, "open_questions": [],
    "sources": [], "research_status": "complete", "due_diligence_confidence": "High",
}


def fake_post_success(url, headers=None, json=None, timeout=None):
    captured_headers.append(headers)
    return FakeResponse({
        "stop_reason": "end_turn",
        "model": ddp.STAGE2_MODEL,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "content": [{"type": "text", "text": __import__("json").dumps(VALID_STAGE2_JSON)}],
    })


captured_headers = []
ddp.requests.post = fake_post_success
result = ddp.call_anthropic_stage2({"title": "t"}, {"confidence": "High"}, FAKE_API_KEY)
check("success path: call_anthropic_stage2 still returns the parsed dict unchanged",
      result == VALID_STAGE2_JSON)
check("success path: no exception raised, no diagnostic machinery engaged",
      True)  # implicit — reaching here means no exception was raised above
check("success path: the real api_key was passed through to the request headers "
      "(sanity check that our fake captures what a real call would send)",
      captured_headers and captured_headers[-1]["x-api-key"] == FAKE_API_KEY)


# ---------------------------------------------------------------------------
# 2. Malformed JSON: message parity with the underlying JSONDecodeError
# ---------------------------------------------------------------------------

# Simulates the real-world shape that motivated this change: two
# concatenated text blocks (e.g. narration + a second JSON-shaped block)
# that _extract_json_text joins with no separator, producing a string
# json.loads() cannot parse.
MALFORMED_TEXT_BLOCKS = [
    {"type": "text", "text": '{"a": 1}'},
    {"type": "server_tool_use", "name": "web_search", "input": {"query": "irrelevant"}},
    {"type": "web_search_tool_result", "content": []},
    {"type": "text", "text": '{"b": 2}'},
]


def fake_post_malformed(url, headers=None, json=None, timeout=None):
    return FakeResponse({
        "stop_reason": "end_turn",
        "model": ddp.STAGE2_MODEL,
        "usage": {"input_tokens": 10, "output_tokens": 10},
        "content": MALFORMED_TEXT_BLOCKS,
    })


# Compute what json.loads() would actually raise, independent of our
# change, so we can assert the new exception's message is identical.
expected_text = ddp._extract_json_text(MALFORMED_TEXT_BLOCKS)
try:
    import json as _json
    _json.loads(expected_text)
    expected_message = None
except _json.JSONDecodeError as _e:
    expected_message = str(_e)

check("fixture sanity: the crafted concatenated-text-blocks fixture is itself invalid JSON "
      "(otherwise this test isn't exercising the failure path)",
      expected_message is not None)

ddp.requests.post = fake_post_malformed
raised = None
try:
    ddp.call_anthropic_stage2({"title": "t"}, {"confidence": "High"}, FAKE_API_KEY)
except Exception as e:
    raised = e

check("malformed response: call_anthropic_stage2 raises (does not silently coerce/repair)",
      raised is not None)
check("malformed response: raised exception is a Stage2JSONDecodeError",
      isinstance(raised, ddp.Stage2JSONDecodeError))
check("malformed response: str(exception) is IDENTICAL to str(json.JSONDecodeError) — "
      "existing `except Exception as e: str(e)` callers see the exact same message",
      str(raised) == expected_message,
      detail=f"got {str(raised)!r} vs expected {expected_message!r}")
check("malformed response: Stage2JSONDecodeError IS-A ValueError IS-A Exception — "
      "existing generic `except Exception` retry/logging code paths are unaffected",
      isinstance(raised, Exception) and isinstance(raised, ValueError))


# ---------------------------------------------------------------------------
# 3. Diagnostic attributes are populated correctly and contain no secrets
# ---------------------------------------------------------------------------

check("diagnostic attrs: raw_text matches exactly what was passed to json.loads()",
      raised.raw_text == expected_text)
check("diagnostic attrs: content_blocks preserves the full block array, in order, "
      "including server_tool_use / web_search_tool_result block types",
      raised.content_blocks == MALFORMED_TEXT_BLOCKS)
check("diagnostic attrs: response_meta.stop_reason captured",
      raised.response_meta.get("stop_reason") == "end_turn")
check("diagnostic attrs: response_meta.model captured",
      raised.response_meta.get("model") == ddp.STAGE2_MODEL)
check("diagnostic attrs: response_meta.usage captured",
      raised.response_meta.get("usage") == {"input_tokens": 10, "output_tokens": 10})
check("diagnostic attrs: response_meta.content_block_count matches the block array length",
      raised.response_meta.get("content_block_count") == len(MALFORMED_TEXT_BLOCKS))
check("diagnostic attrs: response_meta.content_block_types preserves block type order",
      raised.response_meta.get("content_block_types") ==
      ["text", "server_tool_use", "web_search_tool_result", "text"])


def _flatten_to_strings(obj):
    """Recursively collect every string value out of a nested dict/list
    structure, for a no-secret-leaked-anywhere sweep."""
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
# 4. The acceptance runner's file-writing helper preserves everything, in
#    the right (gitignored) place, without altering the exception at all.
# ---------------------------------------------------------------------------

document_number = "2026-90001"

import importlib.util as _ilu

_script_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "dd_syria_acceptance_test.py"
)
_spec = _ilu.spec_from_file_location("dd_syria_acceptance_test", _script_path)
acceptance_script = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(acceptance_script)  # safe: only module-level code runs (temp DB_PATH
                                              # setup); main() is guarded by __name__ == "__main__"

diag_out_dir = tempfile.mkdtemp(prefix="regulus_dd_diag_report_test_")
diag_path = acceptance_script._write_stage2_failure_diagnostic(
    out_dir=diag_out_dir, document_number=document_number, attempt=1, error=raised,
)

check("acceptance runner: diagnostic file was written under the requested (gitignored-pattern) "
      "out_dir, not anywhere else",
      os.path.dirname(diag_path) == diag_out_dir)

with open(diag_path) as f:
    written = json.load(f)

check("acceptance runner: written diagnostic contains the exact raw_text passed to json.loads()",
      written["extracted_text_passed_to_json_loads"] == expected_text)
check("acceptance runner: written diagnostic contains the full content_blocks array, block "
      "types/order intact",
      written["raw_content_blocks"] == MALFORMED_TEXT_BLOCKS)
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
try:
    os.remove(acceptance_script._TMP_DB_PATH)
except OSError:
    pass


# ---------------------------------------------------------------------------
# 5. due_diligence_review propagation is unaffected
# ---------------------------------------------------------------------------

raised_via_wrapper = None
try:
    ddp.due_diligence_review({"title": "t"}, {"confidence": "High"}, api_key=FAKE_API_KEY)
except Exception as e:
    raised_via_wrapper = e

check("due_diligence_review: still just propagates the real call's exception unchanged",
      isinstance(raised_via_wrapper, ddp.Stage2JSONDecodeError)
      and str(raised_via_wrapper) == expected_message)


# ---------------------------------------------------------------------------
# 6. run_due_diligence's failure-path behavior/persistence is byte-for-byte
#    the same as before this change (it only ever sees str(e))
# ---------------------------------------------------------------------------

def make_alert_row(conn, doc_hash, document_number, title, score=15):
    conn.execute(
        "INSERT INTO alerts (doc_hash, document_number, title, score, fetched_at) VALUES (?, ?, ?, ?, ?)",
        (doc_hash, document_number, title, score, "2026-09-30T00:00:00+00:00"),
    )
    conn.commit()


doc_hash = "diag_test_hash_0001"
document_number = "2026-90001"
make_alert_row(conn, doc_hash, document_number, "Diagnostic capture regression test")

outcome = ddp.run_due_diligence(
    {"title": "t"}, {"confidence": "High"}, conn, doc_hash, document_number,
    api_key=FAKE_API_KEY,
)

check("run_due_diligence: failure_reason is 'stage2_call_failed', exactly as before this change",
      outcome.failure_reason == "stage2_call_failed")
check("run_due_diligence: stage2_validation_status is 'invalid'",
      outcome.stage2_validation_status == "invalid")
check("run_due_diligence: stage2_validation_errors contains exactly str(e), same shape as before",
      outcome.stage2_validation_errors == [f"stage2_call_failed: {expected_message}"])
check("run_due_diligence: Stage 3 was never invoked (stage3 is None)",
      outcome.stage3 is None and outcome.stage3_valid is False)

row = conn.execute(
    "SELECT validation_status, validation_errors, research_status, confidence "
    "FROM due_diligence_records WHERE document_number = ?",
    (document_number,),
).fetchone()
check("persistence: a due_diligence_records row was written with the same "
      "validation_status/research_status/confidence shape as before this change",
      row is not None and row[0] == "invalid" and row[2] == ddp.RESEARCH_STATUS_ERROR
      and row[3] == "Low")
check("persistence: validation_errors persisted is exactly [str(e)], not the raw diagnostic payload",
      json.loads(row[1]) == [f"stage2_call_failed: {expected_message}"])


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
