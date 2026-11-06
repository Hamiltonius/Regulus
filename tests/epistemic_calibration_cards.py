"""
Calibration fixtures for epistemic_scorer: the 21 CS-01 / CS-02 scorecards
from the Epistemic Standard v1.0 draft (section G). Inputs are the typed
closed-vocabulary answers an Auditor would supply; EXPECTED holds the
component scores / rating written in the spec table BEFORE the scorer
existed. Any difference between EXPECTED and the scorer's output is a
calibration discrepancy and is reported, never silently edited away.
"""


def ev(eid, cls, ident, text="full", group=None, retrieved=None, stype=None):
    return {"evidence_id": eid, "authority_class": cls, "source_identity_status": ident,
            "independence_group": group or f"G-{eid}",
            "source_type": stype or ("secondary" if cls in ("C", "D") or ident == "not_applicable" else "primary"),
            "retrieved_at": ("2026-10-06T21:35:00+00:00" if ident == "verified" else None) if retrieved is None else retrieved,
            "text_source": text}


def S(*types):
    return [{"type": t, "note": t} for t in types]


def path(ids, steps=(), loc="locator", topical=False):
    return {"evidence_ids": list(ids), "derivation_steps": S(*steps), "locator": loc, "topical_only": topical}


def el(eid, *paths, **kw):
    d = {"element_id": eid, "text": eid, "paths": list(paths)}
    d.update(kw)
    return d


EV01 = {
    "E-001": ev("E-001", "A", "verified", "full", "G-18918"),
    "E-002": ev("E-002", "A", "verified", "excerpt", "G-19161"),
    "E-003": ev("E-003", "A", "verified", "full", "G-19404"),
    "E-004": ev("E-004", "A", "verified", "full", "G-19657"),
    "E-005": ev("E-005", "A", "verified", "full", "G-20082"),
    "E-006": ev("E-006", "A", "unverified", "web_snapshot", "G-EO14312"),
    "E-007": ev("E-007", "B", "unverified", "web_snapshot", "G-OFAC-SITE"),
    "E-009": ev("E-009", "C", "not_applicable", "web_snapshot", "G-BLOG"),
    "E-011": ev("E-011", "A", "unverified", "web_snapshot", "G-17653"),
}
EV02 = {
    "E-01": ev("E-01", "A", "verified", "excerpt", "G-19211"),
    "E-02": ev("E-02", "A", "verified", "excerpt", "G-20079"),
    "E-04": ev("E-04", "B", "not_applicable", "web_snapshot", "G-DDTC-PORTAL"),
    "E-05": ev("E-05", "B", "not_applicable", "web_snapshot", "G-DDTC-PORTAL-2"),
}
ALL_EV = {**EV01, **EV02}


def card(pid, text, form, elements, evids, status="active", superseded_by=None, **inp):
    inputs = {"evidence_snapshot": [ALL_EV[e] for e in evids], "elements": elements}
    inputs.update(inp)
    return {"proposition": {"proposition_id": pid, "text": text, "claim_form": form},
            "status": status, "superseded_by": superseded_by, "inputs": inputs}


def contra(eid, ids, steps=(), status="unresolved", **kw):
    d = {"element_id": eid, "evidence_ids": list(ids), "derivation_steps": S(*steps), "status": status}
    d.update(kw)
    return d


CARDS = [
    card("P01", "Syria was removed from the ITAR 126.1(d)(1) list effective 2026-10-01.", "assertion",
         [el("e1", path(["E-005"], ())), el("e2", path(["E-005"], ())), el("e3", path(["E-005"], ()))], ["E-005"]),
    card("P02", "The CBW Act waiver was executed in two steps and published 2026-09-16.", "assertion",
         [el("e1", path(["E-001"])), el("e2", path(["E-001"])), el("e3", path(["E-001"]))], ["E-001"]),
    card("P03", "Chronology chain: the CBW waiver postdates the SST rescission.", "assertion",
         [el("sst", path(["E-004"])), el("cbw_act", path(["E-001"])), el("cbw_pub", path(["E-001"])),
          el("itar_act", path(["E-005"])), el("itar_cod", path(["E-005"])),
          el("order", path(["E-001", "E-004"], ["date_arithmetic"]))],
         ["E-001", "E-004", "E-005"],
         temporal_assertions=[{"earlier_event": "SST rescission", "earlier_date": "2026-08-24", "earlier_basis": "effective",
                               "later_event": "CBW waiver", "later_date": "2026-08-20", "later_basis": "action"}]),
    card("P04", "EO 14312 is the common upstream authority of the Syria actions.", "inference",
         [el("e1", path(["E-001", "E-006"], ["entity_match_without_identifier"])), el("e2"),
          el("e3", path(["E-005", "E-006"], ["entity_match_without_identifier"]))],
         ["E-001", "E-005", "E-006"]),
    card("P05", "The August 2026 decisions form a genuine cluster that drove publication timing.", "inference",
         [el("e1", path(["E-005"])), el("e2", path(["E-001"])), el("e3", path(["E-004"])),
          el("e4", path(["E-005", "E-001", "E-004"], ["date_arithmetic"])),
          el("e5", path(["E-005", "E-001", "E-004"], ["causal_link"]))],
         ["E-001", "E-004", "E-005"]),
    card("P06", "The Syria actions were deliberately coordinated across agencies.", "hypothesis",
         [el("e1", path(["E-001", "E-004", "E-005", "E-006"], ["generalization", "pattern_or_proximity"]))],
         ["E-001", "E-004", "E-005", "E-006"]),
    card("P07", "ITAR removal required SST, CBW, SAA and CSPA prerequisites.", "assertion",
         [el("sst", path(["E-005"])), el("cbw", path(["E-005"])), el("saa", path(["E-005"])), el("cspa")],
         ["E-005"]),
    card("P08", "OFAC removed 31 CFR Part 542 effective 2025-07-01.", "assertion",
         [el("e1", path(["E-007"])), el("e2", path(["E-007"]))], ["E-007"]),
    card("P09", "The September-October 2026 cluster is a new initiative launched in September 2026.", "hypothesis",
         [el("e1", path(["E-001", "E-004", "E-005"], ["generalization", "intent_or_motive"]))],
         ["E-001", "E-004", "E-005", "E-007"],
         contradictions=[contra("e1", ["E-007"], ["generalization"])]),
    card("P10", "The decision cluster is an artifact of publication timing.", "hypothesis",
         [el("e1", path(["E-001"])),
          el("e2", path(["E-001", "E-003"], ["causal_link", "generalization"]))],
         ["E-001", "E-003", "E-004", "E-005"],
         contradictions=[contra("e2", ["E-005", "E-001", "E-004"], ["date_arithmetic"])]),
    card("P11", "No BIS/EAR Syria action was published 2026-09-14 to 2026-10-05.", "absence",
         [el("e1", path(["E-009"], ["absence_extrapolation"]),
             absence={"scope": "", "scope_coverage": 0.0})], ["E-009"]),
    card("P12", "The SST rescission has not been confirmed from the corpus.", "absence",
         [el("e1", absence={"scope": "run corpus", "scope_coverage": 1.0})], ["E-004"],
         status="superseded", superseded_by="P13",
         contradictions=[contra("e1", ["E-004"], ())]),
    card("P13", "SST rescission effective 2026-08-24, published 2026-08-31, FR Doc 2026-17653.", "assertion",
         [el("eff", path(["E-004"]), path(["E-011"])), el("pub", path(["E-004"])),
          el("doc", path(["E-011"])), el("signed", path(["E-011"])), el("cert", path(["E-011"])),
          el("xref", path(["E-004"]))], ["E-004", "E-011"]),
    card("P14", "2026-19211 is a final rule that removes certain UUVs from USML XX(a) effective 2026-10-19.", "assertion",
         [el("e1"), el("e2", path(["E-01"])), el("e3", path(["E-01"]))], ["E-01"],
         contradictions=[contra("e1", ["E-01"], ())]),
    card("P15", "2026-19211 is an interim final rule with thresholds, effective 2026-10-19.", "assertion",
         [el(f"e{i}", path(["E-01"])) for i in range(1, 6)], ["E-01"],
         analyst_contradiction_items=["E-01.contradicts[0]"],
         dismissed_contradictions=[{"source_item": "E-01.contradicts[0]", "reason_code": "not_about_any_element"}]),
    card("P16", "The NPRM 2026-20079 will result in minimal USML changes.", "hypothesis",
         [el("e1")], ["E-02"],
         contradictions=[contra("e1", ["E-02"], ["generalization"])]),
    card("P17", "The UUV rule and the NPRM are independent actions with coincidental timing.", "hypothesis",
         [el("e1", path(["E-01", "E-02"], ["absence_extrapolation", "intent_or_motive"]), truncation_relevant=True),
          el("e2", path(["E-01", "E-02"], ["absence_extrapolation", "intent_or_motive"]), truncation_relevant=True)],
         ["E-01", "E-02", "E-04", "E-05"],
         contradictions=[contra("e1", ["E-04", "E-05"], ["generalization"])]),
    card("P18a", "The retrieved excerpt of 2026-19211 contains no EO 14268 citation.", "absence",
         [el("e1", path(["E-01"], ["set_membership"]),
             absence={"scope": "retrieved excerpt of 2026-19211", "scope_coverage": 1.0, "truncation_relevant": False})],
         ["E-01"]),
    card("P18b", "2026-19211 does not cite EO 14268.", "absence",
         [el("e1", path(["E-01"], ["absence_extrapolation", "generalization"]),
             absence={"scope": "2026-19211 document", "scope_coverage": 1.0, "truncation_relevant": True})],
         ["E-01"]),
    card("P19", "2026-19161 contains no Syria-specific provisions.", "absence",
         [el("e1", path(["E-002"], ["absence_extrapolation"]),
             path(["E-005"], ["generalization", "absence_extrapolation"]),
             absence={"scope": "2026-19161 document", "scope_coverage": 1.0, "truncation_relevant": True})],
         ["E-002", "E-005"]),
    card("P20", "2026-19161 included Syria-relevant policy-of-denial clarifications.", "assertion",
         [el("e1", path(["E-002"], (), topical=True))], ["E-002"],
         contradictions=[contra("e1", ["E-002"], ["absence_extrapolation"])],
         counter_card_ids=["P19"]),
]

# (SA, ED, CO, CE, CN), total, band, floor, final, usage_class -- written in the spec BEFORE the scorer existed.
# None for components = not tabulated in the spec (E0 cards where support is absent).
EXPECTED = {
    "P01": ((5, 5, 3, 5, 4), 22, "E5", "E5", "E5", "established_context"),
    "P02": ((5, 5, 3, 5, 4), 22, "E5", "E5", "E5", "established_context"),
    "P03": ((5, 4, 3, 5, 2), 19, "E4", "E2", "E2", "hypothesis"),
    "P04": ((4, 3, 3, 3, 4), 17, "E4", "E3", "E3", "qualified_support"),
    "P05": ((5, 3, 3, 5, 4), 20, "E4", "E4", "E4", "strong_support"),
    "P06": ((4, 2, 3, 3, 4), 16, "E3", "E2", "E2", "hypothesis"),
    "P07": ((5, 5, 3, 3, 4), 20, "E4", "E3", "E3", "qualified_support"),
    "P08": ((3, 5, 3, 4, 4), 19, "E4", "E3", "E3", "qualified_support"),
    "P09": ((5, 2, 3, 3, 2), 15, "E3", "E2", "E2", "hypothesis"),
    "P10": ((5, 2, 3, 3, 1), 14, "E3", "E1", "E0", "rejected_proposition"),
    "P11": ((2, 2, 2, 1, 4), 11, "E2", "E1", "E1", "research_target"),
    "P12": (None, None, None, None, "E0", "history_only"),
    "P13": ((4, 5, 3, 4, 4), 20, "E4", "E4", "E4", "strong_support"),
    "P14": ((5, 5, 3, 3, 1), 17, "E4", "E1", "E0", "rejected_proposition"),
    "P15": ((5, 5, 3, 4, 4), 21, "E4", "E5", "E4", "strong_support"),
    "P16": ((1, 1, 1, 1, 2), 6, "E1", "E1", "E1", "research_target"),
    "P17": ((5, 2, 3, 3, 2), 15, "E3", "E2", "E2", "hypothesis"),
    "P18a": ((5, 4, 3, 4, 4), 20, "E4", "E5", "E4", "strong_support"),
    "P18b": ((5, 2, 3, 3, 4), 17, "E4", "E2", "E2", "hypothesis"),
    "P19": ((5, 3, 3, 3, 4), 18, "E4", "E3", "E3", "qualified_support"),
    "P20": ((5, 1, 1, 1, 2), 10, "E2", "E1", "E1", "rejected_proposition"),  # topical-only path does not qualify for CO (B.3)
}
