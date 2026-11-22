"""F3: unified account/contact store with dedupe.

Accounts dedupe by ``crustdata_company_id``, then by normalized domain.
Contacts dedupe by ``crustdata_person_id``, then email, then profile URL.

``Account.firmographics_json`` keeps the raw Crustdata ``company_data`` (so evidence
paths like ``funding.last_round_amount_usd`` resolve against it) plus a flat
``normalized`` block used by scoring and analytics.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlmodel import Session, select

from signalloop.db import session_scope
from signalloop.models import Account, Contact, ContactRole, utcnow
from signalloop.tools.crustdata import normalize_domain

# Headcount bands used across traits, scoring and LEARN segments.
HEADCOUNT_BANDS: list[tuple[int, int | None, str]] = [
    (1, 19, "1-19"), (20, 50, "20-50"), (51, 200, "51-200"), (201, 500, "201-500"),
    (501, 1000, "501-1000"), (1001, None, "1001+"),
]

# enrich period keys -> search period keys
_GROWTH_KEYS = {"mom": "1m", "qoq": "3m", "six_months": "6m", "yoy": "12m"}


def headcount_band(n: Optional[int]) -> Optional[str]:
    if n is None or n <= 0:
        return None
    for lo, hi, label in HEADCOUNT_BANDS:
        if n >= lo and (hi is None or n <= hi):
            return label
    return None


def growth_band(pct: Optional[float]) -> Optional[str]:
    if pct is None:
        return None
    if pct < 0:
        return "shrinking"
    if pct < 10:
        return "0-10%"
    if pct < 25:
        return "10-25%"
    return "25%+"


def _get(d: Any, path: str, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur if cur is not None else default


def _num(v) -> Optional[float]:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _date_str(v) -> Optional[str]:
    if not v:
        return None
    if isinstance(v, (date, datetime)):
        return v.date().isoformat() if isinstance(v, datetime) else v.isoformat()
    return str(v)[:10]


def _growth_6m(raw: dict) -> Optional[float]:
    gp = _get(raw, "headcount.growth_percent") or {}
    if not isinstance(gp, dict):
        return None
    for k in ("6m", "six_months"):
        if gp.get(k) is not None:
            return _num(gp[k])
    return None


def normalize_company(raw: dict) -> dict:
    """Flatten a Crustdata ``company_data`` / search ``companies[]`` record."""
    industry = _get(raw, "taxonomy.professional_network_industry")
    industries = _get(raw, "taxonomy.professional_network_industries") or \
        _get(raw, "basic_info.industries") or []
    if isinstance(industry, list):
        industries = industries or industry
        industry = industry[0] if industry else None
    if not industry and industries:
        industry = industries[0]

    headcount = _get(raw, "headcount.total")
    headcount = int(headcount) if _num(headcount) is not None else None
    growth = _growth_6m(raw)
    growth_map = {}
    gp = _get(raw, "headcount.growth_percent") or {}
    if isinstance(gp, dict):
        for k, v in gp.items():
            growth_map[_GROWTH_KEYS.get(k, k)] = _num(v)

    tech = _get(raw, "technographics.top_technologies") or [
        t.get("name") for t in (_get(raw, "technographics.technologies") or []) if isinstance(t, dict)
    ]
    domain = _get(raw, "basic_info.primary_domain") or _get(raw, "basic_info.website") or ""
    cid = raw.get("crustdata_company_id") or _get(raw, "basic_info.crustdata_company_id")
    return {
        "crustdata_company_id": str(cid) if cid is not None else None,
        "name": _get(raw, "basic_info.name") or "",
        "domain": normalize_domain(domain) if domain else "",
        "industry": industry,
        "industries": list(industries),
        "employee_count_range": _get(raw, "basic_info.employee_count_range"),
        "headcount": headcount,
        "headcount_band": headcount_band(headcount),
        "headcount_growth_6m_pct": growth,
        "headcount_growth_band": growth_band(growth),
        "headcount_growth_pct": growth_map,
        "funding_stage": _get(raw, "funding.last_round_type"),
        "last_funding_date": _date_str(_get(raw, "funding.last_fundraise_date")),
        "last_funding_amount_usd": _num(_get(raw, "funding.last_round_amount_usd")),
        "total_funding_usd": _num(_get(raw, "funding.total_investment_usd")),
        "hq_country": _get(raw, "locations.country") or _get(raw, "headcount.largest_headcount_country"),
        "year_founded": _get(raw, "basic_info.year_founded"),
        "openings_count": _get(raw, "hiring.openings_count"),
        "technologies": [t for t in tech if t],
    }


def _merge(old: dict, new: dict) -> dict:
    """Top-level section merge: new non-empty sections replace old ones."""
    out = dict(old or {})
    for k, v in (new or {}).items():
        if k == "normalized":
            continue
        if v not in (None, {}, []):
            out[k] = v
    return out


def upsert_account(company_payload: dict, *, is_seed: bool = False,
                   icp_version_id: int | None = None, session: Session | None = None) -> Account:
    norm = normalize_company(company_payload)
    if not norm["domain"]:
        raise ValueError(f"company payload has no domain: {norm.get('name') or company_payload}")

    def _do(s: Session) -> Account:
        acc = None
        if norm["crustdata_company_id"]:
            acc = s.exec(select(Account).where(
                Account.crustdata_company_id == norm["crustdata_company_id"])).first()
        if acc is None:
            acc = s.exec(select(Account).where(Account.domain == norm["domain"])).first()

        if acc is None:
            acc = Account(domain=norm["domain"])
            s.add(acc)
        raw = _merge(acc.firmographics_json, company_payload)
        merged_norm = normalize_company(raw)
        acc.firmographics_json = {**raw, "normalized": merged_norm}
        acc.name = merged_norm["name"] or acc.name
        if norm["crustdata_company_id"] and not acc.crustdata_company_id:
            acc.crustdata_company_id = norm["crustdata_company_id"]
        acc.is_seed = acc.is_seed or is_seed
        if icp_version_id is not None:
            acc.icp_version_id = icp_version_id
        acc.updated_at = utcnow()
        s.flush()
        return acc

    if session is not None:
        return _do(session)
    with session_scope() as s:
        return _do(s)


# ---------------------------------------------------------------- people
def normalize_profile_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    u = url.strip().lower().split("?")[0].rstrip("/")
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
    if u.startswith("www."):
        u = u[4:]
    return u


def _current_role(raw: dict) -> dict:
    cur = _get(raw, "experience.employment_details.current") or []
    if isinstance(cur, dict):
        return cur
    if not cur:
        return {}
    for role in cur:
        if isinstance(role, dict) and role.get("is_default"):
            return role
    return cur[0] if isinstance(cur[0], dict) else {}


def normalize_person(raw: dict) -> dict:
    role = _current_role(raw)
    emails = _get(raw, "contact.business_emails") or []
    email = raw.get("email") or (emails[0] if emails and isinstance(emails[0], str) else None)
    pid = raw.get("crustdata_person_id")
    domain = role.get("company_website_domain") or role.get("company_website") or ""
    return {
        "crustdata_person_id": str(pid) if pid is not None else None,
        "name": _get(raw, "basic_profile.name") or "",
        "title": role.get("title") or _get(raw, "basic_profile.current_title") or "",
        "seniority": role.get("seniority_level") or "",
        "function": role.get("function_category") or "",
        "company_name": role.get("name") or role.get("company_name") or "",
        "company_domain": normalize_domain(domain) if domain else "",
        "role_start_date": _date_str(role.get("start_date")),
        "email": email.lower() if email else None,
        "profile_url": _get(raw, "social_handles.professional_network_identifier.profile_url"),
        "country": _get(raw, "basic_profile.location.country"),
    }


def upsert_contact(person_payload: dict, account_id: int | None, *, email: str | None = None,
                   role: ContactRole | None = None, session: Session | None = None) -> Contact:
    norm = normalize_person(person_payload)
    email = (email or norm["email"] or None)
    email = email.lower() if email else None
    purl = normalize_profile_url(norm["profile_url"])

    def _do(s: Session) -> Contact:
        c = None
        if norm["crustdata_person_id"]:
            c = s.exec(select(Contact).where(
                Contact.crustdata_person_id == norm["crustdata_person_id"])).first()
        if c is None and email:
            c = s.exec(select(Contact).where(Contact.email == email)).first()
        if c is None and purl:
            c = s.exec(select(Contact).where(Contact.profile_url == purl)).first()
        if c is None:
            c = Contact()
            s.add(c)
        c.profile_json = {**_merge(c.profile_json, person_payload), "normalized": norm}
        if norm["crustdata_person_id"] and not c.crustdata_person_id:
            c.crustdata_person_id = norm["crustdata_person_id"]
        c.account_id = account_id if account_id is not None else c.account_id
        c.name = norm["name"] or c.name
        c.title = norm["title"] or c.title
        c.seniority = norm["seniority"] or c.seniority
        c.function = norm["function"] or c.function
        c.email = email or c.email
        c.profile_url = purl or c.profile_url
        if role is not None:
            c.role = role
        s.flush()
        return c

    if session is not None:
        return _do(session)
    with session_scope() as s:
        return _do(s)


def get_account_by_domain(domain: str) -> Account | None:
    with session_scope() as s:
        return s.exec(select(Account).where(Account.domain == normalize_domain(domain))).first()
