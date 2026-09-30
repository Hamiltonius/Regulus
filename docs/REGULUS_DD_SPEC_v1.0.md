# REGULUS_DD_SPEC_v1.0.md
STATUS: FROZEN
VERSION: 1.0
DATE: 2026-09-30

## Purpose
Implement risk-based due-diligence escalation and evidence-grounded
executive synthesis on top of the existing regulus_v3.py pipeline.
No redesign, no scope expansion during implementation — deviations
from this spec require a new frozen version, not silent drift.

## Architectural Rules (the constitution)
1. Stage 1 performs primary document analysis (existing `analyze_with_llm`).
2. The Gate alone determines whether due diligence is required.
3. Stage 2 performs external historical/regulatory research.
4. Stage 2 must preserve source provenance for every material claim.
5. Stage 2 output must pass schema validation before use.
6. Stage 3 may ONLY use information supplied by validated upstream evidence.
7. Stage 3 may not search, introduce facts, predict agency behavior,
   or independently reinterpret source material.
8. Final output contains decision-useful value only.
9. Failure of due diligence must be represented explicitly — never
   disguised as confidence.
10. Every material DD-derived final claim must be traceable to
    supporting evidence.

## Schema

### Gate (deterministic, no LLM call)
Signature takes both the Stage 1 analysis AND the original document
metadata/content, so a Stage 1 omission (e.g. the LLM fails to tag a
jurisdiction or change_type that's plainly present in the source) cannot
by itself prevent an otherwise-significant document from escalating.

```python
HIGH_CONTEXT_JURISDICTIONS = {"syria", "iran", "cuba", "north korea", "venezuela", "russia"}
HIGH_IMPACT_ACTIONS = {
    "entity_list_addition", "entity_list_removal", "license_policy_change",
    "country_group_change", "ccl_amendment", "sanctions_waiver"
}

def needs_due_diligence(analysis: dict, doc: dict) -> bool:
    if analysis.get("confidence") != "High":
        return True
    if analysis.get("change_type") in HIGH_IMPACT_ACTIONS:
        return True
    countries = [c.lower() for c in analysis.get("countries", [])]
    if any(j in countries for j in HIGH_CONTEXT_JURISDICTIONS):
        return True
    if analysis.get("unresolved_questions"):
        return True
    # Cross-check against raw document text/metadata (title, abstract,
    # excerpts) independent of what Stage 1 extracted, so a Stage 1 miss
    # on jurisdiction or action type doesn't suppress escalation.
    raw_text = " ".join(filter(None, [
        doc.get("title", ""), doc.get("abstract", "") or "",
    ])).lower()
    if any(j in raw_text for j in HIGH_CONTEXT_JURISDICTIONS):
        return True
    return False
```

### Stage 2 output schema (the evidence package)
```json
{
  "research_question": "",
  "current_event": {
    "action": "", "date": "", "effective_date": "",
    "agency": [], "authority": [], "jurisdictions": [],
    "entities": [], "controls_affected": []
  },
  "historical_context": {
    "program_origin": "",
    "major_prior_actions": [],
    "most_relevant_precedent": {
      "date": null, "description": "", "entities_involved": [],
      "authority": [], "mechanism": ""
    }
  },
  "precedent_comparison": {
    "similarities": [], "differences": [],
    "trend_classification": "consistent|escalation|relaxation|reversal|novel|insufficient_data"
  },
  "legal_regulatory_effect": {
    "changed": [], "unchanged": [], "superseded": [],
    "remaining_restrictions": [], "effective_date": ""
  },
  "scope": {
    "affected_countries": [], "affected_entities": [],
    "affected_item_categories": [], "affected_transaction_types": [],
    "affected_compliance_workflows": []
  },
  "impact_assessment": {
    "immediate": [], "operational": [], "licensing": [],
    "screening": [], "classification": [], "authorization_management": []
  },
  "follow_on_indicators": {
    "historically_observed_next_steps": [],
    "current_unresolved_actions": [],
    "items_to_monitor": []
  },
  "open_questions": [],
  "sources": [
    {"url": "", "source_type": "", "agency": "", "date": "",
     "supports": [], "primary_source": true}
  ],
  "validation_status": "valid|invalid",
  "research_status": "complete|partial|insufficient_data",
  "due_diligence_confidence": "High|Medium|Low"
}
```

### Stage 2 system prompt (source priority — non-negotiable)
```
SOURCE PRIORITY:
1. Federal Register / GovInfo / eCFR
2. BIS / Treasury / OFAC / State / DDTC
3. White House / other relevant U.S. government agencies
4. Congressional/statutory sources
5. Reputable secondary analysis only when primary sources don't
   answer the historical question.

Distinguish primary-source fact, agency characterization, secondary
interpretation, and model inference. Never use a secondary source to
contradict an available primary regulatory instrument without
explicitly identifying the discrepancy. If no meaningful precedent
can be established, return research_status: "insufficient_data" —
do not manufacture one.
```

### Stage 3 output schema (executive brief — ruthlessly small)
```json
{
  "headline": "", "bottom_line": "", "what_changed": "",
  "why_it_matters": "", "historical_significance": "",
  "what_did_not_change": "", "compliance_attention": [],
  "watch_next": [], "confidence": "High|Medium|Low", "sources": []
}
```

### Stage 3 system prompt
```
You are the final intelligence editor. Your inputs have already been
researched and validated. Do not perform additional research or
introduce facts not contained in the supplied evidence. Remove
procedural detail, duplicated information, generic regulatory
background, and low-value observations. Preserve material caveats
and uncertainty. Produce only information that changes the reader's
understanding of the event, its significance, its precedent, its
compliance consequences, or what requires monitoring. Never convert
"partial" research into apparent certainty. Never predict agency
behavior beyond what precedent explicitly supports.
```

## Pipeline (enforced order, no shortcuts)
```
Stage 1 → Gate → [NO: deterministic Python finalizer, no LLM call]
                → [YES: Stage 2 → Schema Validator →
                        (INVALID: error/retry, never proceeds)
                        (VALID: DD record) → Stage 3 → Output Validator]
                → DB / PDF / email
```

## Output behavior (email/PDF)
This is the rule that governs what the reader actually sees, and it is
part of the frozen spec, not an implementation detail:

- **Non-escalated alerts** (Gate returns NO): existing Regulus behavior is
  unchanged. Stage 1 analysis drives the email/PDF exactly as it does today.
- **DD-escalated alerts** (Gate returns YES): Stage 2's evidence package is
  the comprehensive internal research artifact. It is persisted to
  `due_diligence_records` and is NEVER appended to, or exposed in, the
  email/PDF.
- Stage 3's validated output is the only thing that changes what the reader
  sees for an escalated alert: its value-only synthesis REPLACES the normal
  analytical content in the email/PDF for that alert (headline, bottom
  line, what changed, why it matters, historical significance, what didn't
  change, compliance attention, watch next, confidence, sources) — the
  reader never sees raw Stage 2 output.
- Stage 3 may only synthesize what validated Stage 1/Stage 2 evidence
  supplies. It may not search the web, introduce new facts, independently
  perform research, predict agency behavior, or expand beyond the supplied
  evidence — restating rule 7 above in output-facing terms.

## SQL migrations (additive, use existing `_ensure_column` pattern)
```sql
ALTER TABLE alerts ADD COLUMN due_diligence_ran INTEGER DEFAULT 0;
ALTER TABLE alerts ADD COLUMN headline TEXT;
ALTER TABLE alerts ADD COLUMN bottom_line TEXT;
ALTER TABLE alerts ADD COLUMN why_it_matters TEXT;
ALTER TABLE alerts ADD COLUMN historical_significance TEXT;
ALTER TABLE alerts ADD COLUMN what_did_not_change TEXT;
ALTER TABLE alerts ADD COLUMN compliance_attention TEXT;
ALTER TABLE alerts ADD COLUMN watch_next TEXT;
ALTER TABLE alerts ADD COLUMN final_confidence TEXT;

CREATE TABLE due_diligence_records (
    dd_id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_number TEXT NOT NULL,
    doc_hash TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT,
    generated_at TEXT NOT NULL,
    dd_json TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    validation_errors TEXT,
    research_status TEXT NOT NULL,
    confidence TEXT NOT NULL,
    FOREIGN KEY (doc_hash) REFERENCES alerts(doc_hash)
);
```

## Implementation order (vertical, not horizontal)
1. Schemas + validators first. Feed deliberately malformed examples, prove rejection.
2. Gate — unit-test without touching an LLM.
3. Stage 2 — Syria as first integration test (known historical chain).
4. DD persistence — raw evidence, provenance, model/prompt/schema versions, validation results.
5. Final agent — validated Syria Stage 1 + Stage 2 in, check it adds nothing.
6. Wire gate → DD → validator → finalizer into normal Regulus processing only after Syria passes end to end.

## Six acceptance tests
| Test | Validates |
|---|---|
| Syria waiver | historical reconstruction + partial/repeated regulatory change |
| Entity List addition | entity + EAR authority + licensing consequences |
| OFAC derivative designation | entity relationships + designation basis + provenance |
| CCL/ECCN amendment | technical change + old/new distinction |
| Correction notice | negative control — gate must NOT escalate |
| No precedent found | DD must return `insufficient_data`, never fabricate one |

## Acceptance criteria (v1)
```
GATE: 5/5 significant cases routed correctly; correction notice NOT escalated
SCHEMA: 100% valid structure; malformed output never reaches Stage 3
PROVENANCE: every material claim ≥1 source; primary source preferred; no fabricated citations
FACTUALITY: no unsupported material claims; no contradiction with source
FINAL AGENT: zero new facts introduced; preserves uncertainty; never upgrades "partial" to certainty; never predicts agency action
TRACEABILITY: final claim → DD finding → source record → government document
FAILURE HANDLING: search failure stays visible; insufficient evidence stays visible; malformed DD cannot silently proceed
```

Explicitly NOT in scope for v1: a single collapsed confidence score, an `events` table for institutional memory, vector search, additional agents, dedicated inference hardware. Those are real Level-5 ideas, earned only after this version runs against the six test cases and you know which parts are actually expensive.
