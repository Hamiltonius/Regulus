#!/usr/bin/env python3
"""
Tests for epistemic_scorer.py (Regulus Epistemic Standard v1.0).

Zero network (requests.post patched to raise), no database, no files written.
Run: python3 tests/test_epistemic_scorer.py
"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

def _no_network(*a, **k):
    raise AssertionError("epistemic_scorer tests must never make a network call")
requests.post = _no_network
requests.get = _no_network

import epistemic_scorer as s
from epistemic_calibration_cards import CARDS, EXPECTED, ALL_EV, card, el, path, contra, ev

results = []

def check(name, cond, detail=""):
    results.append((name, "PASS" if cond else "FAIL"))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return cond

def rating(c, resolver=None):
    r = s.score_card(c, resolver)
    return r["status"], (r["computed"]["final_rating"] if r["computed"] else None), r

# ---- calibration vs the pre-implementation spec table
res = s.score_cards(CARDS)
check("calibration: 21 scorecards scored, none invalid",
      len(res) == 21 and all(r["status"] != "invalid" for r in res.values()),
      str({k: v.get("validation_errors") for k, v in res.items() if v["status"] == "invalid"}))
for pid, exp in EXPECTED.items():
    r = res[pid]
    c = r["computed"]
    got = tuple(c["components"][k]["score"] for k in s.COMP)
    if exp[0] is None:
        check(f"calibration {pid}: final rating + usage", (c["final_rating"], c["usage_class"]) == exp[-2:], str((c["final_rating"], c["usage_class"])))
        continue
    ok = ((got, c["total"], c["band"], c["floor"], c["final_rating"], c["usage_class"]) == (exp[0], exp[1], exp[2], exp[3], exp[4], exp[5]))
    check(f"calibration {pid}: components/total/band/floor/final/usage match spec", ok, str((got, c["total"], c["band"], c["floor"], c["final_rating"])))

check("calibration: 21/21 cards match the (corrected) spec table",
      all(((tuple(res[p]["computed"]["components"][k]["score"] for k in s.COMP), res[p]["computed"]["total"], res[p]["computed"]["band"],
            res[p]["computed"]["floor"], res[p]["computed"]["final_rating"], res[p]["computed"]["usage_class"]) == (e[0], e[1], e[2], e[3], e[4], e[5]))
          for p, e in EXPECTED.items() if e[0] is not None) and len(EXPECTED) == 21)
p20 = res["P20"]["computed"]
check("P20: topical-only path does not qualify for CO => CO=1, T=10, E1 / rejected_proposition",
      p20["components"]["CO"]["score"] == 1 and p20["total"] == 10 and p20["final_rating"] == "E1" and p20["usage_class"] == "rejected_proposition")

# ---- the seven audited failures
check("failure i: opposite polarity => separate cards, E3 vs E1 rejected_proposition",
      res["P19"]["computed"]["final_rating"] == "E3" and res["P20"]["computed"]["usage_class"] == "rejected_proposition")
check("failure ii: partially_supported items never promoted (P10 E0, P16 E1)",
      res["P10"]["computed"]["final_rating"] == "E0" and res["P16"]["computed"]["final_rating"] == "E1")
check("failure iii: weakened != rejected (P09, P17 are E2, not E0)",
      res["P09"]["computed"]["final_rating"] == "E2" and res["P17"]["computed"]["final_rating"] == "E2")
check("failure iv: retained != documented (P14 original E0, successor P15 E4)",
      res["P14"]["computed"]["final_rating"] == "E0" and res["P15"]["computed"]["final_rating"] == "E4")
check("failure v: inference can never reach E5 (P04/P05 < E5); assertion can (P01)",
      all(res[p]["computed"]["final_rating"] != "E5" for p in ("P04", "P05")) and res["P01"]["computed"]["final_rating"] == "E5")
check("failure vi: superseded P12 links to scored successor P13",
      res["P12"]["superseded_by"] == "P13" and res["P13"]["status"] == "active")
check("failure vii: chronology error capped at E2 by G-CHRONO",
      res["P03"]["computed"]["final_rating"] == "E2" and "G-CHRONO" in res["P03"]["computed"]["binding"])

# ---- gates and rules (synthetic cards)
base = copy.deepcopy(next(c for c in CARDS if c["proposition"]["proposition_id"] == "P01"))

c = copy.deepcopy(base); c["inputs"]["evidence_snapshot"][0]["source_identity_status"] = "unverified"
st, fr, r = rating(c)
check("E5 impossible when evidence unverified (SA=4)", fr != "E5" and r["computed"]["components"]["SA"]["score"] == 4, fr)

c = copy.deepcopy(base); c["inputs"]["evidence_snapshot"][0]["retrieved_at"] = None
st, fr, r = rating(c)
check("verified status without retrieved_at is treated as unverified", r["computed"]["components"]["SA"]["score"] == 4)

c = copy.deepcopy(base); c["inputs"]["evidence_snapshot"][0]["source_identity_status"] = "mismatch"
st, fr, r = rating(c)
check("identity mismatch on every path => E1 via G-MISMATCH", fr == "E1" and "G-MISMATCH" in r["computed"]["binding"], fr)

c = copy.deepcopy(base); c["proposition"]["text"] = "Syria may have been removed from the list."
st, fr, r = rating(c)
check("modal wording in proposition text => invalid (V3)", st == "invalid" and any("V3" in e for e in r["validation_errors"]))

c = copy.deepcopy(base); c["status"] = "superseded"; c["superseded_by"] = None
check("superseded without successor => invalid (V7)", rating(c)[0] == "invalid")

c = copy.deepcopy(base); c["status"] = "superseded"; c["superseded_by"] = "NOPE"
rr = s.score_cards([c])
check("superseded_by that resolves to nothing => invalid in batch", rr["P01"]["status"] == "invalid")

c = copy.deepcopy(base); c["inputs"]["analyst_contradiction_items"] = ["E-005.contradicts[0]"]
check("silently dropped analyst contradiction => invalid (V6)", rating(c)[0] == "invalid")

c = copy.deepcopy(base); c["proposition"]["claim_form"] = "inference"
check("claim_form inference with ED=5 => invalid (V5)", rating(c)[0] == "invalid")

c = copy.deepcopy(base); c["inputs"]["elements"][0]["paths"][0]["evidence_ids"] = ["E-404"]
check("unknown evidence id => invalid (V1)", rating(c)[0] == "invalid")

c = copy.deepcopy(base); c["inputs"]["elements"][0]["paths"][0]["derivation_steps"] = [{"type": "vibes", "note": ""}]
check("derivation step outside closed vocabulary => invalid (V2)", rating(c)[0] == "invalid")

c = copy.deepcopy(base); c["inputs"]["elements"] = []
check("empty elements => invalid (V4)", rating(c)[0] == "invalid")

# contradiction rules
c = copy.deepcopy(base)
c["inputs"]["contradictions"] = [contra("e1", ["E-005"], ["generalization"])]
st, fr, r = rating(c)
check("authority 5 but directness 3 contradiction => cap E2, not E0", fr == "E2" and "G-CONTRA" in r["computed"]["binding"], fr)

c = copy.deepcopy(base)
c["inputs"]["contradictions"] = [contra("e1", ["E-005"], ["date_arithmetic"])]
st, fr, r = rating(c)
check("authority 5 + directness 4 unresolved contradiction => E0", fr == "E0", fr)

c = copy.deepcopy(base)
c["inputs"]["evidence_snapshot"].append(ALL_EV["E-001"])
c["inputs"]["contradictions"] = [contra("e1", ["E-005"], ["date_arithmetic"], status="resolved", resolved_by_evidence_ids=["E-001"])]
st, fr, r = rating(c)
check("contradiction resolved by equal-authority evidence does not gate (CN=4)", fr == "E5" and r["computed"]["components"]["CN"]["score"] == 4, fr)

c = copy.deepcopy(base)
c["inputs"]["evidence_snapshot"].append(ALL_EV["E-009"])
c["inputs"]["contradictions"] = [contra("e1", ["E-005"], ["date_arithmetic"], status="resolved", resolved_by_evidence_ids=["E-009"])]
st, fr, r = rating(c)
check("'resolved' by lower-authority evidence is rejected => treated unresolved => E0", fr == "E0", fr)

c = copy.deepcopy(base)
c["inputs"]["contradictions"] = [contra("e1", ["E-005"], ["date_arithmetic"], status="inconclusive_incomplete_evidence")]
st, fr, r = rating(c)
check("truncation-inconclusive contradiction => CN=3, no gate", r["computed"]["components"]["CN"]["score"] == 3 and fr != "E0", fr)

c = copy.deepcopy(base); c["inputs"]["disconfirmation_search"] = {"ref": "Q4", "evidence_ids": []}
check("CN=5 only with a disconfirmation search ref", rating(c)[2]["computed"]["components"]["CN"]["score"] == 5)

# counter / premise
cards = [copy.deepcopy(base)]
cards[0]["proposition"]["proposition_id"] = "X1"
cards[0]["inputs"]["counter_card_ids"] = ["X2"]
x2 = copy.deepcopy(base); x2["proposition"]["proposition_id"] = "X2"
rr = s.score_cards(cards + [x2])
check("exclusive counter-card rated >=E4 forces E0 (G-E0-COUNTER)", rr["X1"]["computed"]["final_rating"] == "E0")

cards = [copy.deepcopy(base), copy.deepcopy(base)]
cards[0]["proposition"]["proposition_id"] = "Y1"; cards[0]["inputs"]["premise_card_ids"] = ["Y2"]
cards[1]["proposition"]["proposition_id"] = "Y2"; cards[1]["inputs"]["evidence_snapshot"][0]["source_identity_status"] = "unverified"
rr = s.score_cards(cards)
check("derived card capped by weakest premise (G-PREMISE)", rr["Y2"]["computed"]["final_rating"] == "E4" and rr["Y1"]["computed"]["final_rating"] == "E4")

cards = [copy.deepcopy(base), copy.deepcopy(base)]
cards[0]["proposition"]["proposition_id"] = "Z1"; cards[0]["inputs"]["counter_card_ids"] = ["Z2"]
cards[1]["proposition"]["proposition_id"] = "Z2"; cards[1]["inputs"]["counter_card_ids"] = ["Z1"]
rr = s.score_cards(cards)
check("dependency cycle => invalid, no crash", any(v["status"] == "invalid" for v in rr.values()))

# absence rules
c = next(copy.deepcopy(x) for x in CARDS if x["proposition"]["proposition_id"] == "P18a")
c["inputs"]["elements"][0]["absence"]["scope"] = ""
st, fr, r = rating(c)
check("unscoped absence claim capped at E2", fr in ("E1", "E2") and r["computed"]["components"]["ED"]["score"] <= 2, fr)

c = next(copy.deepcopy(x) for x in CARDS if x["proposition"]["proposition_id"] == "P18a")
c["inputs"]["elements"][0]["absence"]["scope_coverage"] = 1.5
check("scope_coverage outside [0,1] => invalid", rating(c)[0] == "invalid")

# temporal rules
c = copy.deepcopy(base)
c["inputs"]["temporal_assertions"] = [{"earlier_event": "a", "earlier_date": "2026-08-19", "later_event": "b", "later_date": "2026-08-20", "date_basis": "action"}]
check("consistent single-basis temporal assertion does not gate", rating(c)[1] == "E5")
c["inputs"]["temporal_assertions"][0]["date_basis"] = None
check("undeclared date basis fails the chronology check", rating(c)[1] == "E2")
c["inputs"]["temporal_assertions"][0].update({"date_basis": None, "earlier_basis": "action", "later_basis": "effective"})
check("mixed date bases fail the chronology check", rating(c)[1] == "E2")

# floors protect against averaging
c = copy.deepcopy(base); c["inputs"]["evidence_snapshot"][0]["authority_class"] = "D"; c["inputs"]["evidence_snapshot"][0]["source_type"] = "secondary"
st, fr, r = rating(c)
check("class-D source with otherwise perfect inputs cannot exceed E1 (floor, not total)", fr == "E1" and r["computed"]["total"] >= 9, str((fr, r["computed"]["total"])))

# determinism / replay
r1 = s.score_card(base); r2 = s.score_card(copy.deepcopy(base))
check("same inputs => identical scorecard_id and replay_hash", r1["scorecard_id"] == r2["scorecard_id"] and r1["computed"]["replay_hash"] == r2["computed"]["replay_hash"])
check("verify_replay passes on stored scorecard", s.verify_replay(r1))
tampered = copy.deepcopy(r1); tampered["computed"]["final_rating"] = "E4"
check("verify_replay detects tampered rating", not s.verify_replay(tampered))
changed = copy.deepcopy(base); changed["inputs"]["evidence_snapshot"][0]["text_source"] = "excerpt"
check("changed input => different scorecard_id", s.score_card(changed)["scorecard_id"] != r1["scorecard_id"])
check("legacy labels are never read (adding disposition/confidence changes nothing)",
      s.score_card({**base, "proposition": {**base["proposition"], "legacy_labels": {"disposition": "retained", "confidence": "high"}}})["computed"]["replay_hash"] == r1["computed"]["replay_hash"])
check("every scorecard carries components with evidence_ids, gates and binding",
      all(set(s.COMP) <= set(v["computed"]["components"]) and v["computed"]["gates"] and "binding" in v["computed"] for v in res.values()))

# ---- Corroboration counts independent origins, never paths
def co_of(c):
    r = s.score_card(c)
    assert r["status"] != "invalid", r.get("validation_errors")
    return r["computed"]["components"]["CO"]["score"]

def with_ev(c, *new):
    c = copy.deepcopy(c)
    have = {e["evidence_id"] for e in c["inputs"]["evidence_snapshot"]}
    c["inputs"]["evidence_snapshot"] += [e for e in new if e["evidence_id"] not in have]
    return c

one = copy.deepcopy(base)                                # single class-A origin, CO=3
one["inputs"]["elements"] = one["inputs"]["elements"][:1]   # one element so CO (min over elements) reflects it
check("CO baseline: one class-A origin => 3", co_of(one) == 3)

dup = copy.deepcopy(one); dup["inputs"]["elements"][0]["paths"].append(copy.deepcopy(dup["inputs"]["elements"][0]["paths"][0]))
check("exact duplicate path does not change CO", co_of(dup) == co_of(one))

dep_ev = ev("E-005b", "A", "verified", "full", group="G-20082")          # same origin group as E-005
dep = with_ev(one, dep_ev)
dep["inputs"]["elements"][0]["paths"].append(path(["E-005b"]))
check("dependent path (same independence group) does not raise CO", co_of(dep) == co_of(one))

dep_weak_ev = ev("E-005c", "C", "not_applicable", "web_snapshot", group="G-20082")
depw = with_ev(one, dep_weak_ev)
depw["inputs"]["elements"][0]["paths"].append(path(["E-005c"], ["generalization"]))
check("weaker dependent derivative (class C, same group) does not lower or raise CO", co_of(depw) == co_of(one))

chain = with_ev(one, ev("E-X1", "A", "verified", "full", group="G-1"), ev("E-X2", "A", "verified", "full", group="G-2"))
chain["inputs"]["elements"][0]["paths"] = [path(["E-X1"]), path(["E-X1", "E-X2"]), path(["E-X2"])]
check("transitively linked groups (G-1, G-1+G-2, G-2) collapse to ONE origin => CO=3", co_of(chain) == 3)

two = with_ev(one, ev("E-001b", "A", "verified", "full", group="G-18918"))
two["inputs"]["elements"][0]["paths"].append(path(["E-001b"]))
check("second INDEPENDENT class-A origin raises CO to 5", co_of(two) == 5)
two_dup = copy.deepcopy(two); two_dup["inputs"]["elements"][0]["paths"].append(path(["E-005b"] if False else ["E-001b"]))
check("adding a duplicate of one of two independent origins keeps CO=5 (does not lower)", co_of(two_dup) == 5)
two_dep = with_ev(two, ev("E-001c", "A", "verified", "full", group="G-18918"))
two_dep["inputs"]["elements"][0]["paths"].append(path(["E-001c"]))
check("dependent path on one of two independent origins keeps CO=5", co_of(two_dep) == 5)

cc = with_ev(one, ev("E-C1", "C", "not_applicable", "web_snapshot", group="G-C"), ev("E-C1b", "C", "not_applicable", "web_snapshot", group="G-C"))
cc["inputs"]["elements"][0]["paths"] = [path(["E-C1"], ["generalization"])]
cc["proposition"]["claim_form"] = "inference"
cc_dup = copy.deepcopy(cc); cc_dup["inputs"]["elements"][0]["paths"].append(path(["E-C1b"], ["generalization"]))
check("class-C-only origin: CO=2, and a dependent class-C copy does not raise it", co_of(cc) == 2 and co_of(cc_dup) == 2)
cc_b = with_ev(cc, ev("E-C1d", "B", "verified", "full", group="G-C-other"))
cc_b["inputs"]["elements"][0]["paths"].append(path(["E-C1d"], ["generalization"]))
check("an independent class-B origin does raise a class-C-only element (to 4: two strong origins, one B)", co_of(cc_b) == 4)

import random
perm = copy.deepcopy(two_dep); random.Random(7).shuffle(perm["inputs"]["elements"][0]["paths"])
check("CO is invariant to path ordering", co_of(perm) == co_of(two_dep))

failed = [n for n, st_ in results if st_ == "FAIL"]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
