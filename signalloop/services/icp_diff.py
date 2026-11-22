"""F28: ICP diff proposal -> approval -> ICP v(n+1) -> lookalike re-run.

Eligible insights: lift >= 1.5 (targeting) or <= 0.5 (exclusion) with n >= ICP_DIFF_MIN_N
(ICP_DIFF_DEMO_MIN_N when DEMO_MODE). The proposal is queued as an ``icp_diff`` approval;
nothing changes until a human approves it.
"""
from __future__ import annotations

import json
from typing import Any

from sqlmodel import select

from signalloop.config import get_settings
from signalloop.db import session_scope
from signalloop.llm import call_llm
from signalloop.models import (
    Approval, ApprovalKind, ApprovalStatus, Campaign, IcpSource, IcpStatus, IcpVersion, Insight,
)
from signalloop.schemas import ICP, IcpDiff
from signalloop.services import approvals, icp as icp_svc, lookalike

POSITIVE_LIFT, NEGATIVE_LIFT = 1.5, 0.5
UNMAPPABLE_DIMS = {"signal_type_at_send", "function"}


def min_n() -> int:
    s = get_settings()
    return s.ICP_DIFF_DEMO_MIN_N if s.DEMO_MODE else s.ICP_DIFF_MIN_N


def eligible_insights(campaign_id: int) -> list[Insight]:
    with session_scope() as s:
        rows = s.exec(select(Insight).where(Insight.campaign_id == campaign_id)).all()
    out = [i for i in rows if i.n >= min_n() and (i.lift >= POSITIVE_LIFT or i.lift <= NEGATIVE_LIFT)
           and not (set(i.dimension.split(" × ")) & UNMAPPABLE_DIMS)]
    return sorted(out, key=lambda i: i.lift, reverse=True)


def _conds_text(conds) -> str:
    return "\n".join(f"- {c.field} {c.type} {json.dumps(c.value)}" for c in conds) or "(none)"


def _insight_text(label: int, i: Insight) -> str:
    # local labels (1..n), not DB ids: the prompt must not depend on row ids so recorded
    # LLM outputs replay in a fresh demo DB (fixtures/llm keys hash the full prompt)
    return (f"{label}: {i.dimension} = {i.segment_value}, n={i.n}, replies={i.responders}, "
            f"rate={i.rate * 100:.1f}%, lift={i.lift:.2f}x")


def apply_changes(spec: ICP, diff: IcpDiff) -> ICP:
    company, person = list(spec.company_filters), list(spec.person_filters)
    for ch in diff.changes:
        target = company if ch.scope == "company" else person
        target[:] = [c for c in target if c.field != ch.field]
        if ch.op != "remove":
            target.extend(c.model_copy(update={"field": ch.field}) for c in ch.to)
    return spec.model_copy(update={"company_filters": company, "person_filters": person})


def _validator(eligible_ids: set[int]):
    def check(d: IcpDiff) -> None:
        errs = []
        if not d.changes:
            errs.append("propose at least one change")
        for k, ch in enumerate(d.changes):
            allowed = icp_svc.COMPANY_FIELDS if ch.scope == "company" else icp_svc.PERSON_FIELDS
            if ch.field not in allowed:
                errs.append(f"changes[{k}]: '{ch.field}' is not an allowed {ch.scope} field")
            if not ch.evidence_insight_ids or not set(ch.evidence_insight_ids) <= eligible_ids:
                errs.append(f"changes[{k}]: evidence_insight_ids must be non-empty and from {sorted(eligible_ids)}")
            if ch.op == "remove" and ch.to:
                errs.append(f"changes[{k}]: 'remove' takes no 'to' conditions")
            if ch.op != "remove" and not ch.to:
                errs.append(f"changes[{k}]: '{ch.op}' needs 'to' conditions")
            if any(c.field != ch.field for c in ch.to):
                errs.append(f"changes[{k}]: every 'to' condition must be on '{ch.field}'")
        if errs:
            raise ValueError("; ".join(errs))
    return check


def diff_rows(old: ICP, new: ICP) -> list[dict[str, Any]]:
    """Side-by-side rows (field, before, after) for the inbox."""
    def by_field(conds):
        out: dict[str, list[str]] = {}
        for c in conds:
            out.setdefault(c.field, []).append(f"{c.type} {json.dumps(c.value)}")
        return out
    rows = []
    for scope, a, b in (("company", old.company_filters, new.company_filters),
                        ("person", old.person_filters, new.person_filters)):
        fa, fb = by_field(a), by_field(b)
        for f in list(dict.fromkeys([*fa, *fb])):
            before, after = "; ".join(fa.get(f, [])) or "—", "; ".join(fb.get(f, [])) or "—"
            rows.append({"scope": scope, "field": f, "before": before, "after": after,
                         "changed": before != after})
    return rows


def propose(campaign_id: int, icp_id: int | None = None) -> Approval | None:
    current = icp_svc.get_icp(icp_id) if icp_id else icp_svc.latest_approved()
    if current is None:
        raise ValueError("no approved ICP to diff against")
    elig = eligible_insights(campaign_id)
    if not elig:
        return None
    with session_scope() as s:
        camp = s.get(Campaign, campaign_id)
    spec = icp_svc.spec_of(current)
    baseline = (camp.analysis_json or {}).get("baseline") or elig[0].baseline_rate
    labels = {k + 1: i for k, i in enumerate(elig)}
    diff = call_llm("propose_icp_diff",
                    {"company_filters": _conds_text(spec.company_filters),
                     "person_filters": _conds_text(spec.person_filters),
                     "insights": "\n".join(_insight_text(k, i) for k, i in labels.items()),
                     "baseline_pct": f"{baseline * 100:.1f}", "field_guide": icp_svc.field_guide()},
                    IcpDiff, model=get_settings().MODEL_STRONG, agent="icp_strategist",
                    validator=_validator(set(labels)))
    for ch in diff.changes:  # labels -> real insight ids
        ch.evidence_insight_ids = [labels[k].id for k in ch.evidence_insight_ids]
    proposed, warnings = icp_svc.validate_icp(apply_changes(spec, diff))
    proposed = proposed.model_copy(update={"rationale": (
        f"Learned from campaign '{camp.name}': {diff.expected_effect}").strip()})
    used = {i for ch in diff.changes for i in ch.evidence_insight_ids}
    return approvals.enqueue(ApprovalKind.icp_diff, current.id, {
        "campaign_id": campaign_id, "campaign": camp.name,
        "current_icp_id": current.id, "current_version": current.version,
        "changes": [ch.model_dump(by_alias=True) for ch in diff.changes],
        "expected_effect": diff.expected_effect, "proposed_spec": proposed.model_dump(),
        "warnings": warnings, "diff": diff_rows(spec, proposed),
        "evidence": [{"id": i.id, "dimension": i.dimension, "segment": i.segment_value, "n": i.n,
                      "responders": i.responders, "rate": i.rate, "lift": i.lift,
                      "low_confidence": i.low_confidence} for i in elig if i.id in used],
    })


def approve(approval_id: int, note: str = "", run_lookalike: bool = True
            ) -> tuple[IcpVersion, lookalike.LookalikeResult | None]:
    appr = approvals.get(approval_id)
    if appr.kind != ApprovalKind.icp_diff or appr.status != ApprovalStatus.pending:
        raise ValueError(f"approval {approval_id} is not a pending ICP diff")
    p = appr.payload_json
    current = icp_svc.get_icp(p["current_icp_id"])
    new = icp_svc.create_version(
        ICP.model_validate(p["proposed_spec"]),
        description=f"v{current.version} + learned from campaign '{p['campaign']}'",
        traits=current.traits_json, source=IcpSource.learned, status=IcpStatus.approved)
    approvals.decide(approval_id, ApprovalStatus.approved, note,
                     payload_update={"new_icp_id": new.id, "new_version": new.version})
    result = lookalike.run_lookalike(new.id) if run_lookalike else None
    return new, result


def reject(approval_id: int, note: str = "") -> Approval:
    return approvals.decide(approval_id, ApprovalStatus.rejected, note)
