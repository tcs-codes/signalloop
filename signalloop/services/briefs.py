"""F15: why-now brief (every bullet cites evidence paths that resolve in the stored JSON)."""
from __future__ import annotations

from sqlmodel import select

from signalloop.config import get_settings
from signalloop.db import session_scope
from signalloop.llm import call_llm
from signalloop.models import Account
from signalloop.models import Brief as BriefRow
from signalloop.schemas import Brief
from signalloop.services import evidence, icp as icp_svc, watch

def validate_brief(b: Brief, doc: dict) -> list[str]:
    errors: list[str] = []
    if not 3 <= len(b.bullets) <= 5:
        errors.append(f"need 3-5 bullets, got {len(b.bullets)}")
    product = evidence.norm_text(doc.get("product", ""))
    for i, ref in enumerate(b.evidence_refs):
        if ref.field == "product":
            if evidence.norm_text(ref.value) not in product:
                errors.append(f"evidence_refs[{i}]: product quote not found in product text: '{ref.value}'")
            continue
        val = evidence.resolve(doc, ref.field)
        if val is evidence._MISSING:
            errors.append(f"evidence_refs[{i}]: path '{ref.field}' does not exist in the evidence")
        elif not evidence.value_matches(val, ref.value):
            errors.append(f"evidence_refs[{i}]: '{ref.field}' is {val!r}, not '{ref.value}'")
    for i, bullet in enumerate(b.bullets):
        if not bullet.refs:
            errors.append(f"bullets[{i}] cites no evidence_refs")
        bad = [r for r in bullet.refs if not 0 <= r < len(b.evidence_refs)]
        if bad:
            errors.append(f"bullets[{i}] cites missing evidence_refs indices {bad}")
    return errors


def _validator(doc: dict):
    def check(b: Brief) -> None:
        errs = validate_brief(b, doc)
        if errs:
            raise ValueError("; ".join(errs))
    return check


def build_brief(account_id: int) -> BriefRow:
    with session_scope() as s:
        account = s.get(Account, account_id)
    if account is None:
        raise ValueError(f"account {account_id} not found")
    signals = watch.signals_for(account_id)
    doc = evidence.build(account, signals)
    icp = icp_svc.latest_approved()
    brief = call_llm("why_now", {"evidence": doc, "icp_rationale": icp.rationale if icp else "(none)"},
                     Brief, model=get_settings().MODEL_STRONG, agent="account_researcher",
                     validator=_validator(doc))
    row = BriefRow(account_id=account_id, signal_id=signals[0].id if signals else None,
                   why_now_json=brief.model_dump(),
                   evidence_refs_json=[r.model_dump() for r in brief.evidence_refs])
    with session_scope() as s:
        s.add(row)
        s.flush()
        return row


def latest_brief(account_id: int) -> BriefRow | None:
    with session_scope() as s:
        return s.exec(select(BriefRow).where(BriefRow.account_id == account_id)
                      .order_by(BriefRow.id.desc())).first()


def brief_model(row: BriefRow) -> Brief:
    return Brief.model_validate(row.why_now_json)
