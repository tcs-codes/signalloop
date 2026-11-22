"""F23: suppression list.

An account is suppressed when it is a seed (existing customer), has status
``suppressed``, or its domain is on the list. A contact is suppressed when it is
flagged, its email / person id is on the list, its account is suppressed, it has an
``unsubscribe`` outcome, or it was contacted (approved/exported touch or ``sent``
outcome) in the last ``RECENT_CONTACT_DAYS`` days.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from sqlmodel import Session, select

from signalloop.config import get_settings
from signalloop.db import session_scope
from signalloop.models import (
    Account, AccountStatus, Contact, Outcome, OutcomeEvent, Suppression, SuppressionKind,
    Touch, TouchStatus,
)
from signalloop.tools.crustdata import normalize_domain

RECENT_CONTACT_DAYS = 30


def _norm(kind: SuppressionKind, value: str) -> str:
    v = str(value).strip()
    if kind == SuppressionKind.domain:
        return normalize_domain(v)
    if kind == SuppressionKind.email:
        return v.lower()
    return v


def add(kind: SuppressionKind | str, value: str, reason: str = "") -> Suppression:
    kind = SuppressionKind(kind)
    v = _norm(kind, value)
    with session_scope() as s:
        row = s.exec(select(Suppression).where(Suppression.kind == kind,
                                               Suppression.value == v)).first()
        if row is None:
            row = Suppression(kind=kind, value=v, reason=reason)
            s.add(row)
        elif reason and not row.reason:
            row.reason = reason
        s.flush()
        return row


def suppress_seed(account: Account) -> None:
    add(SuppressionKind.domain, account.domain, "existing customer (seed)")


def load_csv(path: Path | None = None) -> int:
    """Load ``kind,value,reason`` rows (fixtures/suppression.csv by default). Missing file → 0."""
    path = path or get_settings().fixtures_dir / "suppression.csv"
    if not path.exists():
        return 0
    n = 0
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            kind, value = (row.get("kind") or "").strip(), (row.get("value") or "").strip()
            if kind and value:
                add(kind, value, (row.get("reason") or "").strip())
                n += 1
    return n


def _listed(s: Session, kind: SuppressionKind, value: Optional[str]) -> bool:
    if not value:
        return False
    return s.exec(select(Suppression.id).where(
        Suppression.kind == kind, Suppression.value == _norm(kind, value))).first() is not None


def _cutoff() -> datetime:
    # SQLModel's datetime type requires timezone-aware values
    return datetime.now(timezone.utc) - timedelta(days=RECENT_CONTACT_DAYS)


def _account_suppressed(s: Session, account: Account) -> bool:
    return bool(account.is_seed or account.status == AccountStatus.suppressed
                or _listed(s, SuppressionKind.domain, account.domain))


def _contact_suppressed(s: Session, contact: Contact) -> bool:
    if contact.suppressed:
        return True
    if _listed(s, SuppressionKind.email, contact.email) or \
            _listed(s, SuppressionKind.person_id, contact.crustdata_person_id):
        return True
    if contact.account_id is not None:
        acc = s.get(Account, contact.account_id)
        if acc is not None and _account_suppressed(s, acc):
            return True

    match_contact = Outcome.contact_id == contact.id
    if contact.email:
        match_contact = match_contact | (Outcome.contact_email == contact.email.lower())
    if s.exec(select(Outcome.id).where(match_contact,
                                       Outcome.event == OutcomeEvent.unsubscribe)).first():
        return True

    cutoff = _cutoff()
    if s.exec(select(Outcome.id).where(match_contact, Outcome.event == OutcomeEvent.sent,
                                       Outcome.occurred_at >= cutoff)).first():
        return True
    recent_touch = s.exec(select(Touch.id).where(
        Touch.contact_id == contact.id,
        Touch.status.in_([TouchStatus.approved, TouchStatus.exported]),
        Touch.created_at >= cutoff)).first()
    return recent_touch is not None


def is_suppressed(account: Account | None = None, contact: Contact | None = None) -> bool:
    if account is None and contact is None:
        raise ValueError("is_suppressed: pass an account and/or a contact")
    with session_scope() as s:
        if account is not None and _account_suppressed(s, account):
            return True
        if contact is not None and _contact_suppressed(s, contact):
            return True
    return False


def filter_accounts(accounts: Iterable[Account]) -> list[Account]:
    return [a for a in accounts if not is_suppressed(account=a)]


def filter_contacts(contacts: Iterable[Contact]) -> list[Contact]:
    return [c for c in contacts if not is_suppressed(contact=c)]
