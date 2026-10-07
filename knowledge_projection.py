#!/usr/bin/env python3
"""
knowledge_projection.py -- deterministic structural projection from
already-validated Regulus story_artifacts/briefs rows into the additive
knowledge layer (propositions / knowledge_links) in intelligence_store.py.

DISCIPLINE:
  - No model call, no retrieval, no prose parsing. Every proposition is a
    verbatim copy of a field the frozen upstream schemas already required.
    Nothing is inferred about facts, authorities, countries, causality or
    epistemic class from text.
  - A proposition records WHAT the statement is (statement_role), the
    upstream analyst's own disposition string VERBATIM (never remapped),
    and lifecycle status. It carries NO epistemic rating: that exists only
    when a real scorecard from epistemic_scorer.py is attached afterwards
    via intelligence_store.attach_scorecard(). Unscored is a valid state.
  - upstream_confidence is the analyst's own self-reported label, copied
    verbatim. It is not an epistemic rating and must not be read as one.
  - Nothing is deleted. A removed/weakened/rejected proposition is kept
    with status "contested"; a modified claim is kept "historical" with
    superseded_by -> its revised_claim proposition.
  - knowledge_links are structural only (identifier -> identifier).
  - All IDs are deterministic fingerprints; re-projection is idempotent
    and never erases an attached scorecard (see save_proposition).
"""

import json
from typing import Any, Optional

import intelligence_store as store

EXTRACTION_METHOD = "structural_projection"

# Statuses derived mechanically from an upstream disposition enum. Contested
# = upstream itself said the proposition was removed/weakened/contradicted.
_CONTESTED_DISPOSITIONS = {"removed", "weakened", "contradicted"}


def _pid(run_id, story_id, stage, field_name, index, role) -> str:
    return store.compute_fingerprint("proposition", run_id, story_id, stage, field_name, index, role)


def _link_id(run_id: str, predicate: str, subject_type: str, subject: str,
             object_type: str, object_: str) -> str:
    return store.compute_fingerprint(
        "knowledge_link", run_id, predicate, subject_type, subject, object_type, object_,
    )


def _text(v) -> Optional[str]:
    return v if isinstance(v, str) and v.strip() else None


def _ids(v) -> list:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


_EVIDENCE_KEYS = ("document_number", "source_type", "primary_source", "source_identity_status",
                  "publication_date", "effective_date", "limitations")


def evidence_index(evidence_payload: Optional[dict]) -> dict:
    """evidence_id -> verbatim identity/verification context copied from
    evidence_records[]. Used only to attach context; never interpreted."""
    idx = {}
    if isinstance(evidence_payload, dict):
        for rec in evidence_payload.get("evidence_records") or []:
            if isinstance(rec, dict) and isinstance(rec.get("evidence_id"), str):
                idx[rec["evidence_id"]] = {k: rec.get(k) for k in _EVIDENCE_KEYS if k in rec}
    return idx


def _ctx(extra: dict, evidence_ids: list, ev_idx: dict) -> dict:
    ctx = {k: v for k, v in extra.items() if v is not None}
    found = {e: ev_idx[e] for e in evidence_ids if e in ev_idx}
    if found:
        ctx["evidence"] = found
    return ctx


def _prop(run_id, story_id, stage, field_name, index, role, text, saved_at, *,
          disposition=None, confidence=None, evidence_ids=None, context=None,
          status="active", superseded_by=None, ev_idx=None):
    eids = _ids(evidence_ids)
    return store.PropositionRecord(
        proposition_id=_pid(run_id, story_id, stage, field_name, index, role),
        source_run_id=run_id, source_story_id=story_id, source_stage=stage,
        source_field=field_name, source_index=index, statement_role=role,
        statement_text=text, extraction_method=EXTRACTION_METHOD,
        upstream_disposition=disposition,
        upstream_confidence=confidence if isinstance(confidence, str) else None,
        evidence_ids=eids, context=_ctx(context or {}, eids, ev_idx or {}),
        status=status, superseded_by=superseded_by, source_saved_at=saved_at,
    )


def project_evidence_analyst_propositions(run_id, story_id, payload, saved_at=None) -> list:
    """question_findings (ALL statuses, status kept verbatim as disposition),
    contradictions, disconfirming_evidence_found, remaining_gaps."""
    ev_idx = evidence_index(payload)
    out = []
    S = "evidence_analyst"
    for i, qf in enumerate(payload.get("question_findings") or []):
        text = _text(qf.get("finding")) if isinstance(qf, dict) else None
        if text:
            out.append(_prop(run_id, story_id, S, "question_findings", i, "evidence_finding", text,
                             saved_at, disposition=qf.get("status"), confidence=qf.get("confidence"),
                             evidence_ids=qf.get("evidence_ids"),
                             context={"question": qf.get("question")}, ev_idx=ev_idx))
    for i, c in enumerate(payload.get("contradictions") or []):
        text = _text(c.get("description")) if isinstance(c, dict) else None
        if text:
            out.append(_prop(run_id, story_id, S, "contradictions", i, "contradiction", text,
                             saved_at, evidence_ids=c.get("evidence_ids"),
                             context={"significance": c.get("significance")}, ev_idx=ev_idx))
    for i, t in enumerate(payload.get("disconfirming_evidence_found") or []):
        if _text(t):
            out.append(_prop(run_id, story_id, S, "disconfirming_evidence_found", i,
                             "disconfirming_evidence", t, saved_at, ev_idx=ev_idx))
    for i, t in enumerate(payload.get("remaining_gaps") or []):
        if _text(t):
            out.append(_prop(run_id, story_id, S, "remaining_gaps", i, "uncertainty", t,
                             saved_at, ev_idx=ev_idx))
    return out


def project_pass2_propositions(run_id, story_id, payload, saved_at=None, ev_idx=None) -> list:
    """supported_findings, weakened_or_rejected_findings, remaining_uncertainties,
    material_changes (original_claim + revised_claim), alternative hypotheses."""
    ev_idx = ev_idx or {}
    out = []
    S = "pass2"
    for i, t in enumerate(payload.get("supported_findings") or []):
        if _text(t):
            out.append(_prop(run_id, story_id, S, "supported_findings", i, "supported_finding",
                             t, saved_at))
    for i, t in enumerate(payload.get("weakened_or_rejected_findings") or []):
        if _text(t):  # kept, never deleted; no disposition string exists upstream
            out.append(_prop(run_id, story_id, S, "weakened_or_rejected_findings", i,
                             "reviewed_finding", t, saved_at, status="contested"))
    for i, t in enumerate(payload.get("remaining_uncertainties") or []):
        if _text(t):
            out.append(_prop(run_id, story_id, S, "remaining_uncertainties", i, "uncertainty",
                             t, saved_at))

    for i, mc in enumerate(payload.get("material_changes") or []):
        if not isinstance(mc, dict):
            continue
        orig, rev = _text(mc.get("original_claim")), _text(mc.get("revised_claim"))
        disp, eids = mc.get("disposition"), mc.get("evidence_ids")
        ctx = {"reason": mc.get("reason")}
        rev_id = _pid(run_id, story_id, S, "material_changes", i, "revised_claim") if rev else None
        # Explicit upstream replacement only: disposition == modified AND a revised text.
        linked = bool(orig and rev and disp == "modified")
        if orig:
            if linked:
                status = "historical"
            elif disp in _CONTESTED_DISPOSITIONS:
                status = "contested"
            else:
                status = "active"
            out.append(_prop(run_id, story_id, S, "material_changes", i, "original_claim", orig,
                             saved_at, disposition=disp, evidence_ids=eids, context=ctx,
                             status=status, superseded_by=rev_id if linked else None, ev_idx=ev_idx))
        if rev:
            out.append(_prop(run_id, story_id, S, "material_changes", i, "revised_claim", rev,
                             saved_at, disposition=disp, evidence_ids=eids, context=ctx,
                             ev_idx=ev_idx))

    for i, ah in enumerate(payload.get("alternative_hypotheses_assessment") or []):
        text = _text(ah.get("hypothesis")) if isinstance(ah, dict) else None
        if text:
            disp = ah.get("disposition")
            out.append(_prop(run_id, story_id, S, "alternative_hypotheses_assessment", i,
                             "alternative_hypothesis", text, saved_at, disposition=disp,
                             evidence_ids=ah.get("evidence_ids"),
                             context={"explanation": ah.get("explanation")},
                             status="contested" if disp in _CONTESTED_DISPOSITIONS else "active",
                             ev_idx=ev_idx))
    return out


def project_corpus_analyst_links(run_id: str, story_id: str, payload: dict) -> list:
    """supporting_document_numbers[] / observational_basis[].document_number
    -> knowledge_links (cites_document, story -> document). Only fields
    corpus_analyst_schema.py already requires/validates are read."""
    links = []
    seen = set()

    def _add(doc_number):
        if not isinstance(doc_number, str) or not doc_number.strip():
            return
        key = ("cites_document", story_id, doc_number)
        if key in seen:
            return
        seen.add(key)
        links.append(store.KnowledgeLinkRecord(
            link_id=_link_id(run_id, "cites_document", "story", story_id, "document", doc_number),
            subject=story_id, subject_type="story", predicate="cites_document",
            object=doc_number, object_type="document", source_run_id=run_id,
        ))

    for doc_number in payload.get("supporting_document_numbers") or []:
        _add(doc_number)
    for item in payload.get("observational_basis") or []:
        if isinstance(item, dict):
            _add(item.get("document_number"))

    return links


def project_evidence_analyst_links(run_id: str, story_id: str, payload: dict) -> list:
    """evidence_records[] -> knowledge_links: evidence_for_story
    (evidence -> story) and evidence_cites_document (evidence ->
    document, when document_number is not null). Only fields
    evidence_analyst_schema.py already requires/validates are read."""
    links = []
    for rec in payload.get("evidence_records") or []:
        if not isinstance(rec, dict):
            continue
        evidence_id = rec.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            continue
        links.append(store.KnowledgeLinkRecord(
            link_id=_link_id(run_id, "evidence_for_story", "evidence", evidence_id, "story", story_id),
            subject=evidence_id, subject_type="evidence", predicate="evidence_for_story",
            object=story_id, object_type="story", source_run_id=run_id,
        ))
        document_number = rec.get("document_number")
        if isinstance(document_number, str) and document_number.strip():
            links.append(store.KnowledgeLinkRecord(
                link_id=_link_id(run_id, "evidence_cites_document", "evidence", evidence_id,
                                  "document", document_number),
                subject=evidence_id, subject_type="evidence", predicate="evidence_cites_document",
                object=document_number, object_type="document", source_run_id=run_id,
            ))
    return links


def project_brief_links(run_id: str, brief_id: str, payload: dict) -> list:
    """stories_included[] / stories_excluded[].story_id -> knowledge_links
    (story_included_in_brief / story_excluded_from_brief, story -> brief).
    Only fields intelligence_editor_schema.py already requires/validates
    are read; stories_excluded[].reason (prose) is deliberately not
    captured here."""
    links = []
    for story_id in payload.get("stories_included") or []:
        if isinstance(story_id, str) and story_id.strip():
            links.append(store.KnowledgeLinkRecord(
                link_id=_link_id(run_id, "story_included_in_brief", "story", story_id,
                                  "brief", brief_id),
                subject=story_id, subject_type="story", predicate="story_included_in_brief",
                object=brief_id, object_type="brief", source_run_id=run_id,
            ))
    for item in payload.get("stories_excluded") or []:
        if isinstance(item, dict) and isinstance(item.get("story_id"), str) and item["story_id"].strip():
            story_id = item["story_id"]
            links.append(store.KnowledgeLinkRecord(
                link_id=_link_id(run_id, "story_excluded_from_brief", "story", story_id,
                                  "brief", brief_id),
                subject=story_id, subject_type="story", predicate="story_excluded_from_brief",
                object=brief_id, object_type="brief", source_run_id=run_id,
            ))
    return links


def _valid_payload(rec):
    return rec is not None and rec.is_valid and isinstance(rec.payload, dict)


def project_story(run_id: str, story_id: str, *,
                   db_path: Optional[str] = None,
                   conn: Optional[Any] = None) -> dict:
    """Project every valid stage persisted for (run_id, story_id). Invalid
    artifacts contribute nothing. Idempotent."""
    owns_conn = conn is None
    conn = conn or store.get_connection(db_path)
    try:
        props, links, stages_seen = [], [], []

        corpus = store.load_story_artifact(run_id, story_id, "corpus_analyst", conn=conn)
        if _valid_payload(corpus):
            stages_seen.append("corpus_analyst")
            links.extend(project_corpus_analyst_links(run_id, story_id, corpus.payload))

        ev = store.load_story_artifact(run_id, story_id, "evidence_analyst", conn=conn)
        ev_idx = {}
        if _valid_payload(ev):
            stages_seen.append("evidence_analyst")
            ev_idx = evidence_index(ev.payload)
            props.extend(project_evidence_analyst_propositions(run_id, story_id, ev.payload, ev.saved_at))
            links.extend(project_evidence_analyst_links(run_id, story_id, ev.payload))

        p2 = store.load_story_artifact(run_id, story_id, "pass2", conn=conn)
        if _valid_payload(p2):
            stages_seen.append("pass2")
            props.extend(project_pass2_propositions(run_id, story_id, p2.payload, p2.saved_at, ev_idx))

        for p in props:
            store.save_proposition(p, conn=conn)
        for link in links:
            store.save_knowledge_link(link, conn=conn)

        by_role: dict = {}
        for p in props:
            by_role[p.statement_role] = by_role.get(p.statement_role, 0) + 1
        return {"story_id": story_id, "stages_seen": stages_seen,
                "propositions_written": len(props), "by_role": by_role,
                "links_written": len(links)}
    finally:
        if owns_conn:
            conn.close()


def project_run(run_id: str, *,
                 db_path: Optional[str] = None,
                 conn: Optional[Any] = None) -> dict:
    """Project every story under run_id plus its valid brief(s). Idempotent."""
    owns_conn = conn is None
    conn = conn or store.get_connection(db_path)
    try:
        story_ids = sorted({rec.story_id for rec in store.list_story_artifacts(run_id, conn=conn)})
        per_story = [project_story(run_id, sid, conn=conn) for sid in story_ids]

        brief_links_written = 0
        rows = conn.execute("SELECT brief_id, payload_json, is_valid FROM briefs WHERE run_id = ?",
                            (run_id,)).fetchall()
        for brief_id, payload_json, is_valid in rows:
            if not is_valid or payload_json is None:
                continue
            links = project_brief_links(run_id, brief_id, json.loads(payload_json))
            for link in links:
                store.save_knowledge_link(link, conn=conn)
            brief_links_written += len(links)

        return {
            "run_id": run_id, "story_ids": story_ids, "per_story": per_story,
            "total_propositions_written": sum(s["propositions_written"] for s in per_story),
            "total_links_written": sum(s["links_written"] for s in per_story) + brief_links_written,
            "brief_links_written": brief_links_written,
        }
    finally:
        if owns_conn:
            conn.close()
