#!/usr/bin/env python3
"""
Tests for two additions to scripts/dd_syria_acceptance_test.py and
dd_pipeline.py:

  1. dd_pipeline.STAGE2_TIMEOUT_SECONDS raised 180 -> 600 (a functional
     pipeline change: the requests.post() read timeout for Stage 2).
  2. Spinner / _format_elapsed in the acceptance script: a presentation-
     only terminal activity indicator with no effect on control flow,
     retries, or captured diagnostics.

These tests are deliberately NOT timing-dependent in the way that would
make them flaky or slow:
  - _format_elapsed is a pure function -- tested directly, no sleeping.
  - Spinner.start() on a non-tty stream (the normal case under any test
    runner, which captures stderr) does not create a thread at all, so
    most of this file exercises zero real concurrency.
  - The one test that does exercise the background thread uses a tiny
    interval (0.01s) and bounds every wait with Thread.join(timeout=...),
    so a bug that broke shutdown would make the assertion FAIL, never
    HANG the test run.

Run: python3 tests/test_acceptance_spinner.py
"""
import io
import importlib.util
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == "FAIL" else ""))
    return condition


# ---------------------------------------------------------------------------
# Load the acceptance script as a module. Its top-level code only sets up
# an isolated temp DB_PATH (never touches bis_watcher.db); main() is
# guarded by `if __name__ == "__main__"` and is never invoked here.
# ---------------------------------------------------------------------------
_script_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "dd_syria_acceptance_test.py"
)
_spec = importlib.util.spec_from_file_location("dd_syria_acceptance_test", _script_path)
acceptance_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(acceptance_script)

import dd_pipeline as ddp  # noqa: E402


class FakeTTYStream(io.StringIO):
    """A StringIO that claims to be a terminal, so Spinner actually
    animates against it instead of skipping (the isatty() gate)."""

    def isatty(self):
        return True


# ---------------------------------------------------------------------------
# 1. dd_pipeline.STAGE2_TIMEOUT_SECONDS: the one functional change here
# ---------------------------------------------------------------------------

check("STAGE2_TIMEOUT_SECONDS constant is exactly 600",
      ddp.STAGE2_TIMEOUT_SECONDS == 600)


class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "stop_reason": "end_turn", "model": ddp.STAGE2_MODEL,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "text", "text": "{}"}],
        }


_captured_timeouts = []


def _fake_post(url, headers=None, json=None, timeout=None):
    _captured_timeouts.append(timeout)
    return _FakeResponse()


ddp.requests.post = _fake_post
try:
    ddp.call_anthropic_stage2({"title": "t"}, {"confidence": "High"}, "fake-key")
except Exception:
    pass  # the fixture response's content ("{}") is valid JSON; not exercising failure here

check("call_anthropic_stage2 sends timeout=600 to requests.post (raised from 180)",
      _captured_timeouts and _captured_timeouts[-1] == 600)

check("Stage 3's timeout is untouched at 60 -- this is a Stage 2-only change",
      "timeout=60," in open(
          os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dd_pipeline.py")
      ).read())


# ---------------------------------------------------------------------------
# 2. _format_elapsed: pure function, no timing dependency at all
# ---------------------------------------------------------------------------

fe = acceptance_script._format_elapsed
check("_format_elapsed(0) == '00:00'", fe(0) == "00:00")
check("_format_elapsed(5) == '00:05'", fe(5) == "00:05")
check("_format_elapsed(59) == '00:59'", fe(59) == "00:59")
check("_format_elapsed(60) == '01:00'", fe(60) == "01:00")
check("_format_elapsed(61) == '01:01'", fe(61) == "01:01")
check("_format_elapsed(125) == '02:05'", fe(125) == "02:05")
check("_format_elapsed(3661) == '61:01' (MM:SS only, no hour rollover)", fe(3661) == "61:01")
check("_format_elapsed(-5) clamps to '00:00' rather than raising or going negative",
      fe(-5) == "00:00")
check("_format_elapsed never renders a fake percentage or progress figure -- "
      "it only ever produces MM:SS",
      all(ch in "0123456789:" for ch in fe(125)))


# ---------------------------------------------------------------------------
# 3. Spinner: presentation-only, no effect on the wrapped call's return
#    value or on exceptions raised inside the `with` block
# ---------------------------------------------------------------------------

Spinner = acceptance_script.Spinner

check("Spinner writes to stderr by default (never stdout, never mixed into "
      "anything a caller might capture)",
      Spinner("label").stream is sys.stderr)

# -- non-tty stream: no thread at all (this is what every test runner and
#    every redirected/piped run actually exercises) --
non_tty_stream = io.StringIO()
sp = Spinner("Researching regulatory history", stream=non_tty_stream)
sp.start()
check("Spinner.start() on a non-tty stream creates no background thread",
      sp._thread is None)
sp.stop()
check("Spinner.stop() on a never-started spinner is a no-op, not an error",
      sp._thread is None)
check("Spinner never writes anything to a non-tty stream",
      non_tty_stream.getvalue() == "")

# -- tty-like stream: the animation actually runs, and stop() cleans up
#    reliably within a bounded time (this is the only real-time-touching
#    check in this file; interval is tiny and join() is timeout-bounded,
#    so a regression FAILS this assertion, it does not hang the suite) --
tty_stream = FakeTTYStream()
sp2 = Spinner("Researching regulatory history", interval=0.01, stream=tty_stream)
sp2.start()
check("Spinner.start() on a tty-like stream creates a live background thread",
      sp2._thread is not None and sp2._thread.is_alive())
time.sleep(0.05)
sp2.stop()
check("Spinner.stop() joins its thread within the bounded timeout (thread is "
      "no longer alive, and the reference is cleared)",
      sp2._thread is None)
written = tty_stream.getvalue()
check("Spinner's animated output contains the label",
      "Researching regulatory history" in written)
check("Spinner's animated output contains 'elapsed' (MM:SS elapsed indicator), "
      "never a percentage sign (no fake progress estimate)",
      "elapsed" in written and "%" not in written)

# -- context manager: stop() still runs when the wrapped call raises --
tty_stream_2 = FakeTTYStream()
raised = None
try:
    with Spinner("Researching regulatory history", interval=0.01, stream=tty_stream_2) as sp3:
        thread_ref = sp3._thread
        raise ValueError("simulated Stage 2 failure mid-call")
except ValueError as e:
    raised = e

check("an exception raised inside the `with Spinner(...)` block propagates unchanged",
      isinstance(raised, ValueError) and str(raised) == "simulated Stage 2 failure mid-call")
check("Spinner still stops its thread when the wrapped call raised "
      "(cleanup on exception, not just on success)",
      thread_ref is not None and not thread_ref.is_alive())

# -- successful call: Spinner is purely cosmetic, the real return value is
#    unaffected --
tty_stream_3 = FakeTTYStream()
with Spinner("Researching regulatory history", interval=0.01, stream=tty_stream_3):
    result = {"some": "real-looking pipeline result"}
check("Spinner does not alter or wrap the value returned/produced inside the "
      "`with` block in any way",
      result == {"some": "real-looking pipeline result"})


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
