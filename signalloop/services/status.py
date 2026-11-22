"""Loop status for the Home page."""
from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import func, select

from signalloop.db import session_scope
from signalloop.models import Account, Approval, ApprovalStatus, Brief, Campaign, Contact, Insight, Signal
from signalloop.services import icp as icp_svc
from signalloop.services import scoring


@dataclass
class LoopStatus:
    icp_version: int | None
    icp_source: str | None
    accounts: int
    seeds: int
    contacts: int
    signals: int
    in_market: int
    pending_outreach: int
    pending_icp: int
    last_insight: str | None


def loop_status() -> LoopStatus:
    icp = icp_svc.latest_approved()
    with session_scope() as s:
        accounts = s.exec(select(func.count()).select_from(Account).where(Account.is_seed == False)).one()  # noqa: E712
        seeds = s.exec(select(func.count()).select_from(Account).where(Account.is_seed == True)).one()  # noqa: E712
        contacts = s.exec(select(func.count()).select_from(Contact)).one()
        signals = s.exec(select(func.count()).select_from(Signal)).one()
        pend = dict(s.exec(select(Approval.kind, func.count()).where(Approval.status == ApprovalStatus.pending)
                           .group_by(Approval.kind)).all())
        camp = s.exec(select(Campaign).order_by(Campaign.id.desc())).first()
        last = None
        if camp is not None:
            last = ((camp.analysis_json or {}).get("narrative") or {}).get("headline")
            if not last:
                top = s.exec(select(Insight).where(Insight.campaign_id == camp.id)
                             .order_by(Insight.lift.desc())).first()
                last = f"{top.dimension} = {top.segment_value}: {top.lift:.1f}x (n={top.n})" if top else None
    in_market = len(scoring.this_week(10)) if signals else 0
    return LoopStatus(icp.version if icp else None, icp.source.value if icp else None, accounts, seeds,
                      contacts, signals, in_market,
                      pend.get("outreach", 0), pend.get("icp_diff", 0), last)


def brief_by_id(brief_id: int | None) -> Brief | None:
    if not brief_id:
        return None
    with session_scope() as s:
        return s.get(Brief, brief_id)
