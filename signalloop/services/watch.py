"""F12: scheduled daily scan -> signals.

Signals per watched account (status new/watching, not suppressed):
  funding           last round within WATCH_FUNDING_DAYS; strength from recency and size
  headcount_growth  6-month growth >= WATCH_GROWTH_MIN_PCT; strength scaled by growth
  job_post          GTM job postings in the last WATCH_JOB_DAYS (ONE job search covering all
                    watched domains, so a scan costs a few credits, not one call per account)
  leader_change     a stored contact started their current role within WATCH_LEADER_DAYS

Also re-runs the tuned ICP search with a funding-recency filter to discover new accounts.
Signals are deduplicated by (account, type, key).

Demo day 2: ``fixtures/demo/signals_day2.json`` names real events (from recorded data) that
the baseline scan holds back and the "day 2" scan releases, so the demo shows exactly those
accounts becoming in-market without inventing news about real companies.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlmodel import select

from signalloop.config import get_settings
from signalloop.db import session_scope
from signalloop.models import Account, AccountStatus, Contact, Signal, SignalSource, SignalType
from signalloop.services import icp as icp_svc
from signalloop.services import store, suppression
from signalloop.tools.crustdata import CrustdataError, get_client, normalize_domain

JOB_TITLE_KEYWORDS = ["Sales", "Account Executive", "Business Development", "Revenue", "SDR", "BDR"]


def as_of_date() -> date:
    s = get_settings()
    if s.DEMO_MODE and s.DEMO_AS_OF:
        return date.fromisoformat(s.DEMO_AS_OF)
    return datetime.now(timezone.utc).date()


def day2_file() -> Path:
    return get_settings().fixtures_dir / "demo" / "signals_day2.json"


def load_day2() -> list[dict]:
    p = day2_file()
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("release", [])


def _days(event: str | None, today: date) -> int | None:
    if not event:
        return None
    try:
        return (today - date.fromisoformat(str(event)[:10])).days
    except ValueError:
        return None


# ---------------------------------------------------------------- detectors (pure)
@dataclass
class Detected:
    type: SignalType
    strength: float
    payload: dict[str, Any]
    key: str


def detect_funding(norm: dict, today: date, window: int) -> Detected | None:
    d = _days(norm.get("last_funding_date"), today)
    if d is None or d < 0 or d > window:
        return None
    amount = norm.get("last_funding_amount_usd") or 0
    recency = 1 - d / window
    size = min(max(amount / 25e6, 0.3), 1.0) if amount else 0.3
    strength = round(min(1.0, (0.4 + 0.6 * recency) * (0.7 + 0.3 * size)), 3)
    return Detected(SignalType.funding, strength, {
        "round_type": norm.get("funding_stage"), "amount_usd": amount or None,
        "event_date": norm.get("last_funding_date"), "days_ago": d,
        "evidence": ["company.funding.last_round_type", "company.funding.last_round_amount_usd",
                     "company.funding.last_fundraise_date"],
    }, key=f"{norm.get('funding_stage')}:{norm.get('last_funding_date')}")


def detect_growth(norm: dict, today: date, min_pct: float) -> Detected | None:
    g = norm.get("headcount_growth_6m_pct")
    if g is None or g < min_pct:
        return None
    cap = get_settings().GROWTH_MAX_STRENGTH  # growth is a state: never as strong as an event
    return Detected(SignalType.headcount_growth, round(min(cap, g / 100), 3), {
        "growth_6m_pct": round(g, 1), "headcount": norm.get("headcount"),
        "event_date": today.isoformat(), "days_ago": 0,
        "evidence": ["company.headcount.growth_percent", "company.headcount.total"],
    }, key=f"6m:{round(g)}")


def detect_jobs(listings: list[dict], today: date, window: int) -> Detected | None:
    recent = []
    for j in listings:
        added = (j.get("metadata") or {}).get("date_added")
        d = _days(added, today)
        if d is not None and 0 <= d <= window:
            recent.append({"title": (j.get("job_details") or {}).get("title"),
                           "url": (j.get("job_details") or {}).get("url"), "date_added": str(added)[:10]})
    if not recent:
        return None
    recent.sort(key=lambda r: r["date_added"], reverse=True)
    return Detected(SignalType.job_post, round(min(1.0, 0.3 + 0.15 * len(recent)), 3), {
        "postings": recent[:5], "count": len(recent), "event_date": recent[0]["date_added"],
        "days_ago": _days(recent[0]["date_added"], today),
    }, key="jobs:" + ",".join(sorted(r["url"] or r["title"] or "" for r in recent)))


def detect_leader(contact: Contact, today: date, window: int) -> Detected | None:
    norm = (contact.profile_json or {}).get("normalized", {})
    d = _days(norm.get("role_start_date"), today)
    if d is None or d < 0 or d > window:
        return None
    return Detected(SignalType.leader_change, 0.7, {
        "contact_id": contact.id, "name": contact.name, "title": contact.title,
        "event_date": norm.get("role_start_date"), "days_ago": d,
    }, key=f"leader:{contact.crustdata_person_id or contact.id}:{norm.get('role_start_date')}")


# ---------------------------------------------------------------- scan
@dataclass
class ScanResult:
    as_of: str
    new_signals: list[Signal] = field(default_factory=list)
    held_back: int = 0
    new_accounts: int = 0
    watched: int = 0
    errors: list[str] = field(default_factory=list)


def watched_accounts() -> list[Account]:
    with session_scope() as s:
        rows = list(s.exec(select(Account).where(
            Account.is_seed == False,  # noqa: E712
            Account.status.in_([AccountStatus.new, AccountStatus.watching]))).all())
    return suppression.filter_accounts(rows)


def _job_listings_by_domain(domains: list[str], today: date) -> dict[str, list[dict]]:
    if not domains:
        return {}
    cfg = get_settings()
    flt = {"op": "and", "conditions": [
        {"field": "company.basic_info.primary_domain", "type": "in", "value": sorted(domains)},
        {"op": "or", "conditions": [{"field": "job_details.title", "type": "(.)", "value": k}
                                    for k in JOB_TITLE_KEYWORDS]},
        {"field": "metadata.date_added", "type": "=>",
         "value": (today - timedelta(days=cfg.WATCH_JOB_DAYS)).isoformat()},
    ]}
    resp = get_client().job_search(flt, limit=cfg.WATCH_JOB_LIMIT, sorts=[
        {"field": "metadata.date_added", "order": "desc"}])
    out: dict[str, list[dict]] = {}
    for j in resp.get("job_listings") or []:
        d = normalize_domain(((j.get("company") or {}).get("basic_info") or {}).get("primary_domain") or "")
        out.setdefault(d, []).append(j)
    return out


def _discover(icp_id: int | None, today: date) -> int:
    cfg = get_settings()
    if not cfg.WATCH_DISCOVERY_LIMIT or icp_id is None:
        return 0
    row = icp_svc.get_icp(icp_id)
    tuned = (row.filters_json or {}).get("tuned", {}).get("company") or row.filters_json.get("company")
    if not tuned:
        return 0
    conds = list(tuned.get("conditions", [tuned]))
    conds = [c for c in conds if c.get("field") != "funding.last_fundraise_date"]
    conds.append({"field": "funding.last_fundraise_date", "type": "=>",
                  "value": (today - timedelta(days=30)).isoformat()})
    resp = get_client().company_search({"op": "and", "conditions": conds}, limit=cfg.WATCH_DISCOVERY_LIMIT,
                                       sorts=[{"field": "funding.last_fundraise_date", "order": "desc"}])
    added = 0
    for c in resp.get("companies") or []:
        norm = store.normalize_company(c)
        if not norm["domain"]:
            continue
        with session_scope() as s:
            exists = s.exec(select(Account.id).where(Account.domain == norm["domain"])).first()
        if exists or suppression.is_suppressed(account=Account(domain=norm["domain"])):
            continue
        store.upsert_account(c, icp_version_id=icp_id)
        added += 1
    return added


def _save(account_id: int, det: Detected, source: SignalSource) -> Signal | None:
    key = f"{account_id}|{det.type.value}|{det.key}"
    with session_scope() as s:
        if s.exec(select(Signal.id).where(Signal.dedupe_key == key)).first() is not None:
            return None
        sig = Signal(account_id=account_id, type=det.type, strength=det.strength,
                     payload_json=det.payload, dedupe_key=key, source=source)
        s.add(sig)
        s.flush()
        return sig


def daily_scan(icp_id: int | None = None, *, day2: bool = False, today: date | None = None,
               source: SignalSource = SignalSource.cron) -> ScanResult:
    """Run one scan. ``day2=False`` holds back the demo day-2 events; ``day2=True`` releases them."""
    cfg = get_settings()
    today = today or as_of_date()
    icp_id = icp_id or getattr(icp_svc.latest_approved(), "id", None)
    result = ScanResult(as_of=today.isoformat())
    release = {(normalize_domain(r["domain"]), r["type"]) for r in load_day2()}

    try:
        result.new_accounts = _discover(icp_id, today)
    except CrustdataError as exc:
        result.errors.append(f"discovery: {exc}")

    accounts = watched_accounts()
    result.watched = len(accounts)
    try:
        jobs = _job_listings_by_domain([a.domain for a in accounts], today)
    except CrustdataError as exc:
        jobs = {}
        result.errors.append(f"job search: {exc}")
    with session_scope() as s:
        contacts_by_acc: dict[int, list[Contact]] = {}
        for c in s.exec(select(Contact)).all():
            contacts_by_acc.setdefault(c.account_id, []).append(c)

    for acc in accounts:
        norm = (acc.firmographics_json or {}).get("normalized", {})
        found = [detect_funding(norm, today, cfg.WATCH_FUNDING_DAYS),
                 detect_growth(norm, today, cfg.WATCH_GROWTH_MIN_PCT),
                 detect_jobs(jobs.get(acc.domain, []), today, cfg.WATCH_JOB_DAYS)]
        found += [detect_leader(c, today, cfg.WATCH_LEADER_DAYS) for c in contacts_by_acc.get(acc.id, [])]
        for det in filter(None, found):
            if (acc.domain, det.type.value) in release and not day2:
                result.held_back += 1
                continue
            sig = _save(acc.id, det, source)
            if sig is not None:
                result.new_signals.append(sig)
    return result


def signals_for(account_id: int) -> list[Signal]:
    with session_scope() as s:
        return list(s.exec(select(Signal).where(Signal.account_id == account_id)
                           .order_by(Signal.strength.desc())).all())


def signal_age_days(sig: Signal, today: date | None = None) -> float:
    today = today or as_of_date()
    d = _days((sig.payload_json or {}).get("event_date"), today)
    if d is None:
        d = (today - sig.detected_at.date()).days if sig.detected_at else 0
    return float(max(d, 0))


def decay(days: float, signal_type: str | None = None) -> float:
    cfg = get_settings()
    tau = cfg.TIMING_DECAY_BY_TYPE.get(signal_type or "", cfg.TIMING_DECAY_DAYS)
    return 1.0 if tau <= 0 else math.exp(-days / tau)
