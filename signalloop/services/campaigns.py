"""F24: campaign results import, and F25: responder vs non-responder enrichment.

CSV columns (case-insensitive; unknown columns ignored):
    email, domain, sent_at, opened, replied, meeting, unsubscribed[, profile_url]
Rows link to contacts by email, then profile URL; to accounts by the contact or the domain.
Re-importing the same campaign is idempotent (outcomes dedupe on campaign/person/event).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import IO, Any

import pandas as pd
from sqlmodel import select

from signalloop.config import PROJECT_ROOT
from signalloop.db import session_scope
from signalloop.models import (
    Account, Campaign, CampaignSource, Contact, Outcome, OutcomeEvent, Signal, Touch,
)
from signalloop.services import store
from signalloop.tools.crustdata import get_client, normalize_domain

TRUE = {"1", "true", "yes", "y", "t", "x"}
EVENT_COLUMNS = {"opened": OutcomeEvent.open, "replied": OutcomeEvent.reply,
                 "meeting": OutcomeEvent.meeting, "unsubscribed": OutcomeEvent.unsubscribe}


@dataclass
class ImportResult:
    campaign: Campaign
    rows: int = 0
    linked_contacts: int = 0
    linked_accounts_only: int = 0
    unlinked: int = 0
    outcomes_added: int = 0
    unmatched: list[str] = field(default_factory=list)

    @property
    def contact_link_rate(self) -> float:
        return self.linked_contacts / self.rows if self.rows else 0.0


def _rows(source: str | Path | IO[str] | bytes) -> list[dict[str, str]]:
    if isinstance(source, bytes):
        source = io.StringIO(source.decode("utf-8-sig"))
    elif isinstance(source, (str, Path)) and Path(source).exists():
        source = io.StringIO(Path(source).read_text(encoding="utf-8-sig"))
    elif isinstance(source, str):
        source = io.StringIO(source)
    reader = csv.DictReader(source)
    return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in reader]


def _when(v: str) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.combine(date.fromisoformat(v[:10]), datetime.min.time())
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _get_campaign(name: str, source: CampaignSource) -> Campaign:
    with session_scope() as s:
        c = s.exec(select(Campaign).where(Campaign.name == name)).first()
        if c is None:
            c = Campaign(name=name, source=source)
            s.add(c)
            s.flush()
        return c


def _link(s, row: dict[str, str]) -> tuple[Contact | None, Account | None]:
    email = (row.get("email") or "").lower() or None
    contact = None
    if email:
        contact = s.exec(select(Contact).where(Contact.email == email)).first()
    purl = store.normalize_profile_url(row.get("profile_url"))
    if contact is None and purl:
        contact = s.exec(select(Contact).where(Contact.profile_url == purl)).first()
    account = s.get(Account, contact.account_id) if contact and contact.account_id else None
    domain = normalize_domain(row.get("domain") or (email.split("@")[1] if email and "@" in email else ""))
    if account is None and domain:
        account = s.exec(select(Account).where(Account.domain == domain)).first()
    return contact, account


def import_campaign(source: str | Path | IO[str] | bytes, name: str) -> ImportResult:
    campaign = _get_campaign(name, CampaignSource.csv)
    res = ImportResult(campaign=campaign)
    for row in _rows(source):
        res.rows += 1
        with session_scope() as s:
            contact, account = _link(s, row)
            if contact:
                res.linked_contacts += 1
            elif account:
                res.linked_accounts_only += 1
            else:
                res.unlinked += 1
                res.unmatched.append(row.get("email") or row.get("domain") or "?")
            events = [OutcomeEvent.sent] + [ev for col, ev in EVENT_COLUMNS.items()
                                            if (row.get(col) or "").lower() in TRUE]
            when = _when(row.get("sent_at", ""))
            email = (row.get("email") or "").lower() or None
            domain = normalize_domain(row.get("domain") or "") or (account.domain if account else None)
            for ev in events:
                q = select(Outcome.id).where(Outcome.campaign_id == campaign.id, Outcome.event == ev)
                if contact:
                    q = q.where(Outcome.contact_id == contact.id)
                elif email:
                    q = q.where(Outcome.contact_email == email)
                else:
                    q = q.where(Outcome.domain == domain, Outcome.contact_id == None)  # noqa: E711
                if s.exec(q).first() is not None:
                    continue
                s.add(Outcome(campaign_id=campaign.id, contact_id=contact.id if contact else None,
                              account_id=account.id if account else None, contact_email=email,
                              domain=domain, event=ev, occurred_at=when))
                res.outcomes_added += 1
    return res


def campaign_from_touches(name: str = "internal touches") -> Campaign:
    """Group outcomes recorded against touches (touch_id set, no campaign yet) into a campaign."""
    campaign = _get_campaign(name, CampaignSource.internal)
    with session_scope() as s:
        for o in s.exec(select(Outcome).where(Outcome.touch_id != None,  # noqa: E711
                                              Outcome.campaign_id == None)).all():  # noqa: E711
            t = s.get(Touch, o.touch_id)
            o.campaign_id, o.contact_id = campaign.id, o.contact_id or (t.contact_id if t else None)
            o.account_id = o.account_id or (t.account_id if t else None)
    return campaign


def list_campaigns() -> list[Campaign]:
    with session_scope() as s:
        return list(s.exec(select(Campaign).order_by(Campaign.id.desc())).all())


# ---------------------------------------------------------------- F25
SENIORITY_BANDS = {"cxo": "VP+", "owner / partner": "VP+", "vice president": "VP+",
                   "director": "Director", "manager": "Manager & below", "senior": "Manager & below",
                   "entry": "Manager & below", "training": "Manager & below"}
CORE_DIMS = ["industry", "headcount_band", "funding_stage", "hq_country", "seniority", "function",
             "signal_type_at_send"]


def seniority_band(raw: str | None) -> str:
    return SENIORITY_BANDS.get((raw or "").strip().lower(), "unknown")


def _signal_at_send(s, account_id: int | None, sent: datetime | None) -> str:
    if account_id is None:
        return "none"
    sigs = s.exec(select(Signal).where(Signal.account_id == account_id)).all()
    if sent is not None:
        sent_n = sent if sent.tzinfo else sent.replace(tzinfo=timezone.utc)
        sigs = [x for x in sigs if x.detected_at is None or
                (x.detected_at if x.detected_at.tzinfo else x.detected_at.replace(tzinfo=timezone.utc)) <= sent_n]
    if not sigs:
        return "none"
    return max(sigs, key=lambda x: x.strength).type.value


def enrich_campaign(campaign_id: int, out_dir: Path | None = None) -> pd.DataFrame:
    """Fill missing firmographics/person data (batched, cached), then build the analysis frame."""
    client = get_client()
    with session_scope() as s:
        outcomes = s.exec(select(Outcome).where(Outcome.campaign_id == campaign_id)).all()
        missing_domains = sorted({o.domain for o in outcomes if o.account_id is None and o.domain})
        missing_emails = sorted({o.contact_email for o in outcomes if o.contact_id is None and o.contact_email})
        accounts_without_data = sorted({a.domain for a in (s.get(Account, o.account_id) for o in outcomes
                                                           if o.account_id) if a and not
                                        (a.firmographics_json or {}).get("normalized", {}).get("industry")})
    to_enrich = sorted(set(missing_domains) | set(accounts_without_data))
    if to_enrich:
        for entry in client.company_enrich(domains=to_enrich):
            m = (entry.get("matches") or [None])[0]
            if m and m.get("company_data"):
                data = m["company_data"]
                data.setdefault("basic_info", {}).setdefault("primary_domain", entry.get("matched_on"))
                store.upsert_account(data)
    if missing_emails:
        for entry in client.person_enrich(emails=missing_emails):
            m = (entry.get("matches") or [None])[0]
            if m and m.get("person_data"):
                pdata = m["person_data"]
                email = entry.get("matched_on")
                dom = normalize_domain(email.split("@")[1]) if email and "@" in email else ""
                with session_scope() as s:
                    acc = s.exec(select(Account).where(Account.domain == dom)).first()
                store.upsert_contact(pdata, acc.id if acc else None, email=email)

    # relink outcomes now that accounts/contacts may exist
    with session_scope() as s:
        for o in s.exec(select(Outcome).where(Outcome.campaign_id == campaign_id)).all():
            if o.contact_id is None and o.contact_email:
                c = s.exec(select(Contact).where(Contact.email == o.contact_email)).first()
                if c:
                    o.contact_id, o.account_id = c.id, o.account_id or c.account_id
            if o.account_id is None and o.domain:
                a = s.exec(select(Account).where(Account.domain == o.domain)).first()
                if a:
                    o.account_id = a.id

    df = build_frame(campaign_id)
    out_dir = out_dir or PROJECT_ROOT / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"campaign_{campaign_id}.parquet"
    df.to_parquet(path, index=False)
    with session_scope() as s:
        c = s.get(Campaign, campaign_id)
        c.analysis_json = {**(c.analysis_json or {}), "frame_path": str(path), "rows": len(df)}
    return df


def build_frame(campaign_id: int) -> pd.DataFrame:
    rows: dict[Any, dict] = {}
    with session_scope() as s:
        outcomes = s.exec(select(Outcome).where(Outcome.campaign_id == campaign_id)).all()
        for o in outcomes:
            key = ("c", o.contact_id) if o.contact_id else ("e", o.contact_email or o.domain)
            r = rows.setdefault(key, {"contact_id": o.contact_id, "account_id": o.account_id,
                                      "sent_at": None, "responded": False, "meeting": False})
            if o.event == OutcomeEvent.sent:
                r["sent_at"] = o.occurred_at
            r["responded"] |= o.event in (OutcomeEvent.reply, OutcomeEvent.meeting)
            r["meeting"] |= o.event == OutcomeEvent.meeting
        out = []
        for r in rows.values():
            acc = s.get(Account, r["account_id"]) if r["account_id"] else None
            con = s.get(Contact, r["contact_id"]) if r["contact_id"] else None
            n = (acc.firmographics_json or {}).get("normalized", {}) if acc else {}
            out.append({
                "contact_id": r["contact_id"], "account_id": r["account_id"],
                "responded": bool(r["responded"]), "meeting": bool(r["meeting"]),
                "industry": n.get("industry") or "unknown",
                "headcount_band": n.get("headcount_band") or "unknown",
                "funding_stage": n.get("funding_stage") or "unknown",
                "hq_country": n.get("hq_country") or "unknown",
                "seniority": seniority_band(con.seniority) if con else "unknown",
                "function": (con.function or "unknown") if con else "unknown",
                "signal_type_at_send": _signal_at_send(s, r["account_id"], r["sent_at"]),
            })
    return pd.DataFrame(out, columns=["contact_id", "account_id", "responded", "meeting", *CORE_DIMS])
