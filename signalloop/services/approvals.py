"""F4 backend: the approval queue. Nothing leaves the system without a decision here.

Outreach decisions update the touch and append approved drafts to
``exports/approved_outreach.csv`` (the only "output"; nothing is ever sent).
ICP-diff decisions live in ``services/icp_diff.py`` (approve creates the new ICP version).
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from sqlmodel import func, select

from signalloop.config import get_settings
from signalloop.db import session_scope
from signalloop.models import Approval, ApprovalKind, ApprovalStatus, Touch, TouchStatus, utcnow

EXPORT_FIELDS = ["approval_id", "touch_id", "decided_at", "account", "domain", "contact", "title",
                 "role", "channel", "subject", "body", "edited", "note"]


def enqueue(kind: ApprovalKind | str, ref_id: int | None, payload: dict[str, Any]) -> Approval:
    row = Approval(kind=ApprovalKind(kind), ref_id=ref_id, payload_json=payload)
    with session_scope() as s:
        s.add(row)
        s.flush()
        return row


def list_pending(kind: ApprovalKind | str | None = None) -> list[Approval]:
    with session_scope() as s:
        q = select(Approval).where(Approval.status == ApprovalStatus.pending)
        if kind is not None:
            q = q.where(Approval.kind == ApprovalKind(kind))
        return list(s.exec(q.order_by(Approval.id)).all())


def get(approval_id: int) -> Approval:
    with session_scope() as s:
        row = s.get(Approval, approval_id)
    if row is None:
        raise ValueError(f"approval {approval_id} not found")
    return row


def decide(approval_id: int, status: ApprovalStatus | str, note: str = "",
           payload_update: dict[str, Any] | None = None) -> Approval:
    status = ApprovalStatus(status)
    if status == ApprovalStatus.pending:
        raise ValueError("decide() needs approved, rejected or edited")
    with session_scope() as s:
        row = s.get(Approval, approval_id)
        if row is None:
            raise ValueError(f"approval {approval_id} not found")
        if row.status != ApprovalStatus.pending:
            raise ValueError(f"approval {approval_id} already decided ({row.status.value})")
        if payload_update:
            row.payload_json = {**row.payload_json, **payload_update}
        row.status, row.note, row.decided_at = status, note, utcnow()
        s.flush()
        return row


# ---------------------------------------------------------------- outreach decisions
def export_path() -> Path:
    return get_settings().exports_dir / "approved_outreach.csv"


def _export(appr: Approval, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    p = appr.payload_json
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EXPORT_FIELDS)
        if new:
            w.writeheader()
        w.writerow({"approval_id": appr.id, "touch_id": p.get("touch_id"),
                    "decided_at": appr.decided_at.isoformat() if appr.decided_at else "",
                    "account": p.get("account"), "domain": p.get("domain"), "contact": p.get("contact"),
                    "title": p.get("title"), "role": p.get("role"), "channel": p.get("channel"),
                    "subject": p.get("subject", ""), "body": p.get("body", ""),
                    "edited": appr.status == ApprovalStatus.edited, "note": appr.note})


def decide_outreach(approval_id: int, action: str, *, subject: str | None = None,
                    body: str | None = None, note: str = "", export_to: Path | None = None) -> Approval:
    """action: 'approve' | 'edit' (edit & approve) | 'reject'."""
    appr = get(approval_id)
    if appr.kind != ApprovalKind.outreach:
        raise ValueError(f"approval {approval_id} is not outreach")
    update: dict[str, Any] = {}
    if action == "edit":
        if subject is not None and subject != appr.payload_json.get("subject"):
            update["subject"] = subject
        if body is not None and body != appr.payload_json.get("body"):
            update["body"] = body
        update["original"] = {"subject": appr.payload_json.get("subject"), "body": appr.payload_json.get("body")}
        status = ApprovalStatus.edited if (set(update) - {"original"}) else ApprovalStatus.approved
    elif action == "approve":
        status = ApprovalStatus.approved
    elif action == "reject":
        status = ApprovalStatus.rejected
    else:
        raise ValueError(f"unknown action '{action}'")

    appr = decide(approval_id, status, note, payload_update=update or None)
    with session_scope() as s:
        t = s.get(Touch, appr.ref_id) if appr.ref_id else None
        if t is not None:
            if status == ApprovalStatus.rejected:
                t.status = TouchStatus.rejected
            else:
                t.subject = appr.payload_json.get("subject", t.subject)
                t.body = appr.payload_json.get("body", t.body)
                t.status = TouchStatus.approved
    if status != ApprovalStatus.rejected:
        _export(appr, export_to or export_path())
        with session_scope() as s:
            t = s.get(Touch, appr.ref_id) if appr.ref_id else None
            if t is not None:
                t.status = TouchStatus.exported
    return appr


def counts() -> dict[str, int]:
    with session_scope() as s:
        rows = s.exec(select(Approval.kind, Approval.status, func.count()).group_by(
            Approval.kind, Approval.status)).all()
    out: dict[str, int] = {}
    for kind, status, n in rows:
        out[f"{kind.value}:{status.value}"] = n
    return out


def list_decided(kind: ApprovalKind | str | None = None, limit: int = 50) -> list[Approval]:
    with session_scope() as s:
        q = select(Approval).where(Approval.status != ApprovalStatus.pending)
        if kind is not None:
            q = q.where(Approval.kind == ApprovalKind(kind))
        return list(s.exec(q.order_by(Approval.decided_at.desc()).limit(limit)).all())
