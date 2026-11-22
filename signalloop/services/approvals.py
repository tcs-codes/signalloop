"""F4 backend: the approval queue. Nothing leaves the system without a decision here.

Stub created with F16 (enqueue / list_pending / decide); completed in Phase 5.
"""
from __future__ import annotations

from typing import Any

from sqlmodel import select

from signalloop.db import session_scope
from signalloop.models import Approval, ApprovalKind, ApprovalStatus, utcnow


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
