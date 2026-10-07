#!/usr/bin/env python3
"""
epistemic_scorer.py -- Regulus Epistemic Standard v1.0 (EES-1.0), deterministic
offline scorer.

Pure function of its inputs: no network, no model call, no database, no
clock, no randomness. The caller supplies closed-vocabulary facts (source
classes, derivation step types, evidence paths, contradictions, temporal
assertions); this module derives the five component scores (SA, ED, CO,
CE, CN), the total, band, floor, gates and the E0-E5 rating. The model is
never asked for a 1-5 number.

Public API:
    score_card(card, resolver=None) -> scorecard dict
    score_cards(cards)              -> {proposition_id: scorecard}  (dependency-ordered)
    verify_replay(scorecard)        -> bool  (recompute from stored inputs, compare)

Input card shape (see the spec, section F):
    {"proposition": {"proposition_id", "text", "claim_form", ...},
     "status": "active"|"superseded"|"withdrawn", "superseded_by": id|None,
     "inputs": {"evidence_snapshot": [...], "elements": [...],
                "contradictions": [...], "dismissed_contradictions": [...],
                "analyst_contradiction_items": [...],
                "temporal_assertions": [...], "disconfirmation_search": {...}|None,
                "premise_card_ids": [...], "counter_card_ids": [...],
                "remaining_gap_element_ids": [...]}}
"""
import copy
import hashlib
import json
import re
from datetime import date

STANDARD_VERSION = "EES-1.0"
THRESHOLD_TABLE_VERSION = "EES-1.0-T1"

AUTHORITY_CLASSES = ("A", "B", "C", "D")
IDENTITY_STATUSES = ("verified", "unverified", "mismatch", "not_applicable")
TEXT_SOURCES = ("full", "excerpt", "web_snapshot", "unknown")
CLAIM_FORMS = ("assertion", "inference", "absence", "hypothesis")
CONTRADICTION_STATUSES = ("resolved", "unresolved", "inconclusive_incomplete_evidence")
DISMISS_REASONS = ("different_proposition", "superseded_by_correction",
                   "identity_mismatch_source", "not_about_any_element")
DATE_BASES = ("action", "effective", "publication")
MECHANICAL_STEPS = ("explicit_identifier_match", "date_arithmetic",
                    "set_membership", "quote_restatement")
INTERPRETIVE_STEPS = ("entity_match_without_identifier", "causal_link",
                      "intent_or_motive", "generalization",
                      "pattern_or_proximity", "absence_extrapolation")
ALL_STEPS = MECHANICAL_STEPS + INTERPRETIVE_STEPS

BAND_THRESHOLDS = ((22, 5), (17, 4), (13, 3), (9, 2))  # total >= x -> E{n}; else E1
# rating -> minimums (SA, ED, CO, CE, CN)
FLOORS = {5: (5, 4, 3, 4, 4), 4: (4, 3, 2, 4, 4), 3: (3, 3, 2, 3, 3), 2: (2, 2, 2, 2, 2)}
COMP = ("SA", "ED", "CO", "CE", "CN")

USAGE_BY_RATING = {5: "established_context", 4: "strong_support", 3: "qualified_support",
                   2: "hypothesis", 1: "research_target", 0: "rejected_proposition"}

_MODAL_RE = re.compile(r"\b(may|might|could|possibly|potentially|perhaps|appears? to|seems? to)\b", re.I)


class CardInvalid(Exception):
    pass


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- primitives

def sa_of_evidence(ev: dict) -> int:
    """Source Authority of one evidence record (spec B.1)."""
    cls, ident = ev["authority_class"], ev["source_identity_status"]
    if ident == "mismatch" or cls == "D":
        return 1
    verified = ident == "verified" and bool(ev.get("retrieved_at"))
    if cls == "A":
        return 5 if verified else 4
    if cls == "B":
        return 4 if verified else 3
    return 2  # class C


def ed_of_steps(steps, topical_only=False, has_evidence=True) -> int:
    """Evidence Directness of a derivation (spec B.2)."""
    if topical_only or not has_evidence:
        return 1
    types = [s["type"] for s in steps]
    interp = [t for t in types if t in INTERPRETIVE_STEPS]
    if "pattern_or_proximity" in interp:
        return 2
    if len(interp) >= 2:
        return 2
    if len(interp) == 1:
        return 3
    if not types or all(t == "quote_restatement" for t in types):
        return 5
    return 4


def _parse_date(s):
    try:
        y, m, d = (int(x) for x in str(s).split("-"))
        return date(y, m, d)
    except Exception:
        return None


# ---------------------------------------------------------------- validation

def _validate(card: dict):
    """Return (errors list). Structural / vocabulary / spec V1-V7 checks."""
    errs = []
    prop = card.get("proposition") or {}
    inp = card.get("inputs") or {}
    if not prop.get("proposition_id"):
        errs.append("V0: proposition.proposition_id missing")
    text = prop.get("text") or ""
    if not text.strip():
        errs.append("V0: proposition.text missing")
    if _MODAL_RE.search(text):
        errs.append("V3: modal/hedge wording in proposition text")
    if prop.get("claim_form") not in CLAIM_FORMS:
        errs.append("V2: claim_form not in vocabulary")
    status = card.get("status", "active")
    if status not in ("active", "superseded", "withdrawn"):
        errs.append("V2: status not in vocabulary")
    if status == "superseded" and not card.get("superseded_by"):
        errs.append("V7: superseded card has no superseded_by (orphan supersession)")

    snap = {}
    for ev in inp.get("evidence_snapshot", []):
        eid = ev.get("evidence_id")
        if not eid or eid in snap:
            errs.append(f"V1: evidence_id missing or duplicated: {eid!r}")
            continue
        snap[eid] = ev
        if ev.get("authority_class") not in AUTHORITY_CLASSES:
            errs.append(f"V2: {eid} authority_class")
        if ev.get("source_identity_status") not in IDENTITY_STATUSES:
            errs.append(f"V2: {eid} source_identity_status")
        if ev.get("text_source") not in TEXT_SOURCES:
            errs.append(f"V2: {eid} text_source")
        if not ev.get("independence_group"):
            errs.append(f"V2: {eid} independence_group missing")
        if ev.get("source_type") == "secondary" and ev.get("authority_class") == "A":
            errs.append(f"V2: {eid} secondary source cannot be class A")

    def check_steps(steps, where):
        for s in steps or []:
            if s.get("type") not in ALL_STEPS:
                errs.append(f"V2: {where} step type {s.get('type')!r}")

    elements = inp.get("elements") or []
    if not elements:
        errs.append("V4: elements[] is empty")
    seen = set()
    for el in elements:
        eid = el.get("element_id")
        if not eid or eid in seen:
            errs.append(f"V4: element_id missing or duplicated: {eid!r}")
        seen.add(eid)
        for p in el.get("paths", []):
            if not p.get("evidence_ids"):
                errs.append(f"V8: path in {eid} has no evidence_ids")
            for x in p.get("evidence_ids", []):
                if x not in snap:
                    errs.append(f"V1: {eid} cites unknown evidence {x!r}")
            check_steps(p.get("derivation_steps"), eid)
        ab = el.get("absence")
        if ab is not None:
            cov = ab.get("scope_coverage")
            if cov is None or not (0 <= cov <= 1):
                errs.append(f"V2: {eid} absence.scope_coverage must be in [0,1]")
    for c in inp.get("contradictions", []):
        if c.get("element_id") not in seen:
            errs.append(f"V1: contradiction element_id {c.get('element_id')!r} unknown")
        if c.get("status") not in CONTRADICTION_STATUSES:
            errs.append("V2: contradiction status")
        for x in c.get("evidence_ids", []):
            if x not in snap:
                errs.append(f"V1: contradiction cites unknown evidence {x!r}")
        for x in c.get("resolved_by_evidence_ids", []):
            if x not in snap:
                errs.append(f"V1: resolution cites unknown evidence {x!r}")
        check_steps(c.get("derivation_steps"), "contradiction")
    handled = {c.get("source_item") for c in inp.get("contradictions", []) if c.get("source_item")}
    for d in inp.get("dismissed_contradictions", []):
        if d.get("reason_code") not in DISMISS_REASONS:
            errs.append("V6: dismissal reason_code not in closed list")
        handled.add(d.get("source_item"))
    for item in inp.get("analyst_contradiction_items", []):
        if item not in handled:
            errs.append(f"V6: analyst contradiction item {item!r} silently dropped")
    for t in inp.get("temporal_assertions", []):
        for k in ("earlier_event", "earlier_date", "later_event", "later_date"):
            if not t.get(k):
                errs.append(f"V2: temporal assertion missing {k}")
    return errs


# ---------------------------------------------------------------- scoring

def _path_info(path, snap):
    evs = [snap[x] for x in path["evidence_ids"]]
    void = any(e["source_identity_status"] == "mismatch" for e in evs)
    sa = min(sa_of_evidence(e) for e in evs)
    return {"void": void, "sa": sa,
            "classes": {e["authority_class"] for e in evs},
            "groups": {e["independence_group"] for e in evs},
            "text_sources": {e["text_source"] for e in evs},
            "ids": list(path["evidence_ids"])}


def _element_ed(el, path):
    steps = path.get("derivation_steps", [])
    ed = ed_of_steps(steps, path.get("topical_only", False), True)
    ab = el.get("absence")
    if ab is not None and not (ab.get("scope") or "").strip():
        ed = min(ed, 2)  # unscoped absence claim
    return ed


_CLASS_RANK = {"A": 3, "B": 2, "C": 1, "D": 0}


def _origin_clusters(paths):
    """Cluster paths into independent origins: paths that share an independence
    group (transitively) derive from the same originating source and form ONE
    origin. Order-independent."""
    parent = {}
    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for p in paths:
        gs = sorted(p["groups"])
        for g in gs[1:]:
            parent[find(g)] = find(gs[0])
        find(gs[0])
    clusters = {}
    for p in paths:
        clusters.setdefault(find(sorted(p["groups"])[0]), []).append(p)
    return list(clusters.values())


def _corroboration(qual, gclass):
    """Spec B.3. Multiple paths from one originating source are ONE origin, so
    duplicate or dependent paths cannot add corroboration. An origin's class is
    the best class of the evidence in its group(s) in the snapshot (so adding a
    path that reuses snapshot evidence cannot change it); an origin is 'strong'
    if any of its paths has ED >= 3."""
    origins = []
    for cl in _origin_clusters(qual):
        groups = set().union(*[p["groups"] for p in cl])
        cls = max((gclass.get(g, "D") for g in groups), key=lambda c: _CLASS_RANK[c])
        origins.append({"cls": cls, "strong": any(p["ed"] >= 3 for p in cl)})
    strong = [o for o in origins if o["strong"]]
    n_a = sum(1 for o in strong if o["cls"] == "A")
    n_ab = sum(1 for o in strong if o["cls"] in ("A", "B"))
    if n_a >= 2:
        return 5
    if len(strong) >= 2 and n_ab >= 1:
        return 4
    if any(o["cls"] in ("A", "B") for o in origins):
        return 3
    if any(o["cls"] == "C" for o in origins):
        return 2
    return 1


def _compute_components(card, snap):
    inp = card["inputs"]
    notes = {k: [] for k in COMP}
    ev_used = {k: set() for k in COMP}
    gclass = {}
    for e in snap.values():
        if e["source_identity_status"] != "mismatch":
            g = e["independence_group"]
            if g not in gclass or _CLASS_RANK[e["authority_class"]] > _CLASS_RANK[gclass[g]]:
                gclass[g] = e["authority_class"]
    elem_rows, covered_weights = [], []
    sa_list, ed_list, co_list = [], [], []
    any_nonvoid_path = False
    any_path = False
    all_void = True
    trunc_relevant = False
    full_text_needed_locator_missing = False
    load_bearing = set()
    unsupported = []
    declared_absence_ok = True
    has_absence = False
    best_sa_any = []

    for el in inp["elements"]:
        paths = el.get("paths", [])
        infos = []
        for p in paths:
            any_path = True
            pi = _path_info(p, snap)
            pi["ed"] = _element_ed(el, p)
            pi["locator"] = p.get("locator")
            infos.append(pi)
            if not pi["void"]:
                all_void = False
                any_nonvoid_path = True
        live = [i for i in infos if not i["void"]]
        qual = [i for i in live if i["ed"] >= 2]
        ab = el.get("absence")
        if ab is not None:
            has_absence = True
            if not (ab.get("scope") or "").strip():
                declared_absence_ok = False
        if el.get("truncation_relevant") or (ab or {}).get("truncation_relevant"):
            trunc_relevant = True
        if live:
            best_sa_any.append(max(i["sa"] for i in live))
        row = {"element_id": el["element_id"], "qualifying_paths": len(qual)}
        if not qual:
            weight = 0.0
            row.update(SA=None, ED=max([i["ed"] for i in live], default=None), CO=None, weight=0.0)
            elem_rows.append(row)
            covered_weights.append(0.0)
            unsupported.append(el["element_id"])
            continue
        best = max(qual, key=lambda i: (i["ed"], i["sa"]))
        ed_el = best["ed"]
        sa_el = max(i["sa"] for i in qual)
        # corroboration: count independent ORIGINS, not paths
        co_el = _corroboration(qual, gclass)
        # coverage weight
        if ab is not None:
            weight = float(ab["scope_coverage"]) if (ab.get("scope") or "").strip() else 0.0
        else:
            weight = 1.0 if ed_el >= 3 else 0.5
        if weight < 1.0:
            unsupported.append(el["element_id"])
        for i in qual:
            load_bearing.update(i["ids"])
        sa_list.append(sa_el)
        ed_list.append(ed_el)
        co_list.append(co_el)
        for i in qual:
            ev_used["SA"].update(i["ids"])
        # locator requirement for non-full text at full coverage
        non_full = any(t != "full" for t in best["text_sources"])
        if non_full and not best.get("locator"):
            full_text_needed_locator_missing = True
        row.update(SA=sa_el, ED=ed_el, CO=co_el, weight=weight)
        row["_non_full"] = non_full
        elem_rows.append(row)
        covered_weights.append(weight)

    n_el = len(inp["elements"])
    f = sum(covered_weights) / n_el if n_el else 0.0
    has_qualifying = bool(sa_list)

    SA = min(sa_list) if sa_list else (max(best_sa_any) if best_sa_any else 1)
    ED = min(ed_list) if ed_list else 1
    CO = min(co_list) if co_list else 1

    # CE
    if f >= 1.0 - 1e-9:
        any_non_full = any(r.get("_non_full") for r in elem_rows)
        gaps = bool(inp.get("remaining_gap_element_ids"))
        if any_non_full or gaps:
            CE = 4
            if full_text_needed_locator_missing:
                CE = 3
        else:
            CE = 5
    elif f >= 0.5:
        CE = 3
    elif f > 0:
        CE = 2
    else:
        CE = 1
    if trunc_relevant:
        CE = min(CE, 3)
    for r in elem_rows:
        r.pop("_non_full", None)
    ev_used["ED"] = set(ev_used["SA"])
    ev_used["CO"] = set(ev_used["SA"])
    ev_used["CE"] = set(ev_used["SA"])
    return {
        "SA": SA, "ED": ED, "CO": CO, "CE": CE, "f": f, "elem_rows": elem_rows,
        "has_qualifying": has_qualifying, "any_path": any_path,
        "all_void": all_void and any_path, "unsupported": unsupported,
        "ev_used": ev_used, "has_absence": has_absence,
        "declared_absence_ok": declared_absence_ok, "trunc_relevant": trunc_relevant,
    }


def _compute_cn(card, snap, comp):
    inp = card["inputs"]
    caps, reasons = [], []
    e0 = False
    contra_cap_e2 = False
    cn_ev = set()
    for c in inp.get("contradictions", []):
        evs = [snap[x] for x in c["evidence_ids"] if x in snap]
        live = [e for e in evs if e["source_identity_status"] != "mismatch"]
        if not live:
            reasons.append(f"contradiction on {c['element_id']} ignored: all evidence mismatched")
            continue
        authority = min(sa_of_evidence(e) for e in live)
        directness = ed_of_steps(c.get("derivation_steps", []), c.get("topical_only", False), True)
        status = c["status"]
        cn_ev.update(c["evidence_ids"])
        if status == "resolved":
            res = [snap[x] for x in c.get("resolved_by_evidence_ids", []) if x in snap]
            if res and min(sa_of_evidence(e) for e in res) >= authority:
                caps.append(4)
                reasons.append(f"contradiction on {c['element_id']} resolved by equal/higher authority")
                continue
            status = "unresolved"
            reasons.append(f"contradiction on {c['element_id']}: resolution rejected (no equal/higher-authority evidence)")
        if status == "inconclusive_incomplete_evidence":
            caps.append(3)
            reasons.append(f"contradiction on {c['element_id']} inconclusive (incomplete evidence)")
            continue
        # unresolved
        if authority >= 4 and directness >= 4:
            e0 = True
            caps.append(1)
            reasons.append(f"E0 test met on {c['element_id']}: authority={authority}, directness={directness}")
        elif authority >= 3:
            contra_cap_e2 = True
            caps.append(2)
            reasons.append(f"unresolved contradiction on {c['element_id']}: authority={authority}, directness={directness}")
        else:
            caps.append(3)
            reasons.append(f"unresolved low-authority contradiction on {c['element_id']}")
    chrono_fail = False
    for t in inp.get("temporal_assertions", []):
        eb = t.get("earlier_basis") or t.get("date_basis")
        lb = t.get("later_basis") or t.get("date_basis")
        d1, d2 = _parse_date(t["earlier_date"]), _parse_date(t["later_date"])
        if eb not in DATE_BASES or lb not in DATE_BASES:
            chrono_fail = True
            reasons.append(f"temporal assertion {t['earlier_event']!r}->{t['later_event']!r}: date basis undeclared")
        elif eb != lb:
            chrono_fail = True
            reasons.append(f"temporal assertion {t['earlier_event']!r}->{t['later_event']!r}: mixed date bases ({eb} vs {lb})")
        elif d1 is None or d2 is None or d1 > d2:
            chrono_fail = True
            reasons.append(f"temporal assertion {t['earlier_event']!r}->{t['later_event']!r}: date order fails")
    if chrono_fail:
        caps.append(2)
    base = 5 if inp.get("disconfirmation_search") else 4
    CN = min([base] + caps)
    return CN, e0, contra_cap_e2, chrono_fail, reasons, cn_ev


def _band(total):
    for thr, r in BAND_THRESHOLDS:
        if total >= thr:
            return r
    return 1


def _floor(scores):
    sa, ed, co, ce, cn = scores
    for r in (5, 4, 3, 2):
        m = FLOORS[r]
        if sa >= m[0] and ed >= m[1] and co >= m[2] and ce >= m[3] and cn >= m[4]:
            return r
    return 1


def _next_blocker(scores, final):
    if final >= 5 or final == 0:
        return None
    m = FLOORS[final + 1]
    names = COMP
    fails = [f"{n}={s}<{req}" for n, s, req in zip(names, scores, m) if s < req]
    return f"E{final + 1} blocked: " + ", ".join(fails) if fails else f"E{final + 1} blocked by total"


def _inputs_hash(card):
    return _sha(_canon({"proposition": {k: card["proposition"].get(k) for k in
                                         ("proposition_id", "text", "claim_form")},
                        "inputs": card["inputs"]}) + STANDARD_VERSION + THRESHOLD_TABLE_VERSION)


def _invalid(card, errs):
    return {"standard_version": STANDARD_VERSION, "threshold_table_version": THRESHOLD_TABLE_VERSION,
            "scorecard_id": _sha(_canon(card) + "INVALID"),
            "proposition": card.get("proposition"), "status": "invalid",
            "validation_errors": errs, "inputs": card.get("inputs"), "computed": None}


def score_card(card: dict, resolver=None) -> dict:
    """Score one card. `resolver(proposition_id)` returns an int rating (0-5)
    for premise/counter cards, or None if unknown."""
    card = copy.deepcopy(card)
    errs = _validate(card)
    prop, inp = card.get("proposition", {}), card.get("inputs", {})
    if errs:
        return _invalid(card, errs)
    snap = {e["evidence_id"]: e for e in inp["evidence_snapshot"]}
    comp = _compute_components(card, snap)
    CN, e0_elem, contra_cap, chrono_fail, cn_reasons, cn_ev = _compute_cn(card, snap, comp)
    SA, ED, CO, CE = comp["SA"], comp["ED"], comp["CO"], comp["CE"]

    # V5: claim_form vs computed ED (only when at least one covered element exists)
    cf = prop["claim_form"]
    if comp["has_qualifying"]:
        bad = ((cf == "assertion" and ED < 4) or (cf == "inference" and ED > 3)
               or (cf == "hypothesis" and ED > 2))
        if bad:
            return _invalid(card, [f"V5: claim_form={cf!r} inconsistent with computed ED={ED}"])

    scores = (SA, ED, CO, CE, CN)
    total = sum(scores)
    band = _band(total)
    floor = _floor(scores)

    gates = []
    def gate(gid, fired, effect="", detail=""):
        gates.append({"gate_id": gid, "fired": bool(fired), "effect": effect, "detail": detail})

    # counter / premise resolution
    counter_ratings, premise_ratings = {}, {}
    for cid in inp.get("counter_card_ids", []):
        r = resolver(cid) if resolver else None
        if r is None:
            return _invalid(card, [f"counter card {cid!r} unresolved"])
        counter_ratings[cid] = r
    for pid in inp.get("premise_card_ids", []):
        r = resolver(pid) if resolver else None
        if r is None:
            return _invalid(card, [f"premise card {pid!r} unresolved"])
        premise_ratings[pid] = r

    e0_counter = any(r >= 4 for r in counter_ratings.values())
    gate("G-E0-ELEMENT", e0_elem, "-> E0", "; ".join(cn_reasons))
    gate("G-E0-COUNTER", e0_counter, "-> E0", f"counter ratings {counter_ratings}")
    caps = {}
    if contra_cap:
        caps["G-CONTRA"] = 2
    gate("G-CONTRA", contra_cap and not e0_elem, "cap E2")
    if chrono_fail:
        caps["G-CHRONO"] = 2
    gate("G-CHRONO", chrono_fail, "cap E2")
    absence_fired = cf == "absence" or comp["has_absence"]
    if absence_fired:
        caps["G-ABSENCE"] = 2 if not comp["declared_absence_ok"] else 4
    gate("G-ABSENCE", absence_fired, "cap E4 (E2 if scope undeclared)")
    if premise_ratings:
        caps["G-PREMISE"] = min(premise_ratings.values())
    gate("G-PREMISE", bool(premise_ratings), "cap at weakest premise", str(premise_ratings))
    nopath = not comp["has_qualifying"]
    if nopath:
        caps["G-NOPATH"] = 1
    gate("G-NOPATH", nopath, "-> E1")
    mismatch = comp["all_void"]
    if mismatch:
        caps["G-MISMATCH"] = 1
    gate("G-MISMATCH", mismatch, "-> E1")

    if e0_elem or e0_counter:
        final = 0
        binding = [g["gate_id"] for g in gates if g["fired"] and g["gate_id"].startswith("G-E0")]
    else:
        final = min([band, floor] + list(caps.values()))
        binding = []
        if band == final:
            binding.append("BAND")
        if floor == final:
            binding.append("FLOOR")
        binding += [k for k, v in caps.items() if v == final]

    status = card.get("status", "active")
    if status == "superseded":
        usage = "history_only"
    elif final <= 1 and any(r >= 3 for r in counter_ratings.values()):
        usage = "rejected_proposition"
    else:
        usage = USAGE_BY_RATING[final]

    def cinfo(k, v):
        ids = sorted(comp["ev_used"][k]) if k != "CN" else sorted(cn_ev)
        return {"score": v, "evidence_ids": ids}
    components = {k: cinfo(k, v) for k, v in zip(COMP, scores)}
    components["CE"]["basis"] = f"coverage f={comp['f']:.3f}"
    components["CN"]["basis"] = "; ".join(cn_reasons) or ("no contradiction; disconfirmation search recorded"
                                                           if inp.get("disconfirmation_search") else "no contradiction; no targeted search ref")
    for k in ("SA", "ED", "CO"):
        components[k]["basis"] = "min over covered elements" if comp["has_qualifying"] else "no qualifying path"

    computed = {
        "components": components,
        "element_scores": comp["elem_rows"],
        "total": total, "band": f"E{band}", "floor": f"E{floor}",
        "floor_blocker": _next_blocker(scores, floor) if floor < 5 else None,
        "gates": gates, "final_rating": f"E{final}", "binding": binding,
        "next_rating_blocker": _next_blocker(scores, final),
        "unsupported_elements": comp["unsupported"],
        "usage_class": usage,
    }
    out = {
        "standard_version": STANDARD_VERSION, "threshold_table_version": THRESHOLD_TABLE_VERSION,
        "scorecard_id": _inputs_hash(card), "proposition": prop, "status": status,
        "superseded_by": card.get("superseded_by"),
        "contested_by": list(inp.get("counter_card_ids", [])),
        "inputs": inp, "computed": computed,
    }
    out["computed"]["replay_hash"] = _sha(_canon(computed))
    return out


def score_cards(cards: list) -> dict:
    """Score many cards, dependency-ordered (counter/premise cards first).
    Cycles or missing dependencies make the dependent card invalid."""
    by_id = {c["proposition"]["proposition_id"]: c for c in cards}
    done, visiting = {}, set()

    def deps(c):
        i = c.get("inputs", {})
        return list(i.get("counter_card_ids", [])) + list(i.get("premise_card_ids", []))

    def run(pid):
        if pid in done:
            return done[pid]
        c = by_id[pid]
        if pid in visiting:
            return _invalid(c, ["dependency cycle"])
        visiting.add(pid)
        for d in deps(c):
            if d in by_id:
                run(d)
        visiting.discard(pid)

        def resolver(x):
            r = done.get(x)
            if r is None or r["status"] == "invalid":
                return None
            return int(r["computed"]["final_rating"][1])
        done[pid] = score_card(c, resolver)
        return done[pid]

    for pid in by_id:
        run(pid)
    # V7 second pass: superseded_by must resolve to a scored, valid card
    for pid, sc in list(done.items()):
        if sc["status"] == "superseded":
            tgt = done.get(sc["superseded_by"])
            if tgt is None or tgt["status"] == "invalid":
                done[pid] = _invalid(by_id[pid], ["V7: superseded_by does not resolve to a valid scored card"])
    return done


def verify_replay(scorecard: dict, resolver=None) -> bool:
    """Recompute from stored inputs and compare final rating, gates and hash."""
    if scorecard.get("status") == "invalid":
        return True
    card = {"proposition": scorecard["proposition"], "status": scorecard["status"],
            "superseded_by": scorecard.get("superseded_by"), "inputs": scorecard["inputs"]}
    again = score_card(card, resolver)
    if again.get("computed") is None:
        return False
    stored = scorecard["computed"]
    body = {k: v for k, v in stored.items() if k != "replay_hash"}
    return (_canon(again["computed"]) == _canon(stored)
            and _sha(_canon(body)) == stored.get("replay_hash"))
