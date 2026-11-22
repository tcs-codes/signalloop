"""F11: decision-maker finder.

Two narrow person searches per account, both scoped to the current employer's domain:
  * champion:        title matches GTM keywords (sales, revenue, growth, ...), ranked by seniority
  * economic buyer:  seniority CXO / Owner-Partner or a founder title, CEO/founder first
Crustdata leaves `function_category` empty for most profiles, so a single
"senior OR title" query returned many non-sales leaders; two queries fix that.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlmodel import select

from signalloop.config import get_settings  # noqa: F401  (re-exported for the UI)
from signalloop.db import session_scope
from signalloop.models import Account, AccountStatus, Contact, ContactRole, IcpVersion
from signalloop.services import icp as icp_svc
from signalloop.services import lookalike, store, suppression
from signalloop.tools.crustdata import CrustdataError, get_client

F_DOMAIN = "experience.employment_details.current.company_website_domain"
F_SENIORITY = "experience.employment_details.current.seniority_level"
F_TITLE = "experience.employment_details.current.title"

DEFAULT_SENIORITY = ["Vice President", "Director", "CXO"]
DEFAULT_TITLES = ["Head of Sales", "VP Sales"]
ECON_SENIORITY = {"cxo", "owner / partner"}
ECON_TITLE = re.compile(r"\b(ceo|cto|cfo|coo|cro|chief|founder|co-founder|cofounder|president)\b", re.I)
CHAMP_SENIORITY = {"vice president", "director", "head"}
CHAMP_TITLE = re.compile(r"\b(vp|vice president|head|director|lead)\b", re.I)

CHAMPION_KEYWORDS = ["Sales", "Revenue", "Business Development", "Growth", "Go To Market", "GTM"]
ECON_SENIORITY_VALUES = ["CXO", "Owner / Partner"]
SENIORITY_ORDER = ["cxo", "owner / partner", "vice president", "director", "manager", "senior"]
_CEO = re.compile(r"\b(ceo|chief executive|founder|co-founder|cofounder)\b", re.I)
_GTM_EXEC = re.compile(r"\b(cro|chief revenue|coo|chief operating|chief commercial|president)\b", re.I)
_GTM = re.compile(r"\b(sales|revenue|business development|growth|go[- ]to[- ]market|gtm|commercial|partnerships)\b", re.I)


@dataclass
class Persona:
    seniority: list[str] = field(default_factory=lambda: list(DEFAULT_SENIORITY))
    titles: list[str] = field(default_factory=lambda: list(DEFAULT_TITLES))


def persona_for(icp_row: IcpVersion) -> Persona:
    spec = icp_svc.spec_of(icp_row)
    sen: list[str] = []
    titles: list[str] = []
    for c in spec.person_filters:
        vals = c.value if isinstance(c.value, list) else [c.value]
        if c.field == F_SENIORITY:
            sen += [str(v) for v in vals]
        elif c.field == F_TITLE:
            titles += [str(v) for v in vals]
    ph = (icp_row.traits_json or {}).get("persona_hypothesis") or {}
    sen += [s for s in ph.get("seniority", []) if s not in sen]
    titles += [t for t in ph.get("titles", []) if t not in titles]
    return Persona(seniority=sen or list(DEFAULT_SENIORITY), titles=titles or list(DEFAULT_TITLES))


def role_for(title: str, seniority: str) -> ContactRole:
    s = (seniority or "").lower()
    if s in ECON_SENIORITY or ECON_TITLE.search(title or ""):
        return ContactRole.economic_buyer
    if s in CHAMP_SENIORITY or CHAMP_TITLE.search(title or ""):
        return ContactRole.champion
    return ContactRole.other


def _seniority_rank(norm: dict) -> float:
    s = (norm.get("seniority") or "").lower()
    return float(len(SENIORITY_ORDER) - SENIORITY_ORDER.index(s)) if s in SENIORITY_ORDER else 0.0


def champion_rank(norm: dict) -> float:
    title = norm.get("title") or ""
    score = _seniority_rank(norm)
    if _GTM.search(title):
        score += 10
    if re.search(r"\b(head|vp|vice president|director)\b", title, re.I):
        score += 2
    return score


def econ_rank(norm: dict) -> float:
    title = norm.get("title") or ""
    if _CEO.search(title):
        return 30.0
    if _GTM_EXEC.search(title):
        return 20.0
    return 10.0 + _seniority_rank(norm)


def champion_filter(domain: str, keywords: list[str] | None = None) -> dict:
    return {"op": "and", "conditions": [
        {"field": F_DOMAIN, "type": "=", "value": domain},
        {"op": "or", "conditions": [{"field": F_TITLE, "type": "(.)", "value": k}
                                    for k in (keywords or CHAMPION_KEYWORDS)]},
    ]}


def econ_filter(domain: str) -> dict:
    return {"op": "and", "conditions": [
        {"field": F_DOMAIN, "type": "=", "value": domain},
        {"op": "or", "conditions": [
            {"field": F_SENIORITY, "type": "in", "value": ECON_SENIORITY_VALUES},
            {"field": F_TITLE, "type": "(.)", "value": "Founder"},
        ]},
    ]}


def top_accounts(n: int) -> list[Account]:
    return suppression.filter_accounts(lookalike.list_accounts())[:n]


def find_contacts(account_ids: list[int] | None = None, per_account: int | None = None,
                  top_n: int | None = None, icp_id: int | None = None) -> dict[int, list[Contact]]:
    cfg = get_settings()
    per_account = per_account or cfg.CONTACTS_PER_ACCOUNT
    icp_row = icp_svc.get_icp(icp_id) if icp_id else icp_svc.latest_approved()
    persona = persona_for(icp_row) if icp_row else Persona()

    if account_ids is None:
        accounts = top_accounts(top_n or cfg.CONTACTS_TOP_N)
    else:
        with session_scope() as s:
            accounts = [a for a in (s.get(Account, i) for i in account_ids) if a is not None]
        accounts = suppression.filter_accounts(accounts)

    client = get_client()
    keywords = list(dict.fromkeys(CHAMPION_KEYWORDS))
    found: dict[int, list[Contact]] = {}
    for acc in accounts:
        try:
            champs = client.person_search(champion_filter(acc.domain, keywords), limit=3).get("profiles") or []
            econs = client.person_search(econ_filter(acc.domain), limit=3).get("profiles") or []
        except CrustdataError:
            found[acc.id] = []
            continue
        champs = sorted((p for p in champs if _GTM.search(store.normalize_person(p)["title"] or "")),
                        key=lambda p: champion_rank(store.normalize_person(p)), reverse=True)
        econs = sorted(econs, key=lambda p: econ_rank(store.normalize_person(p)), reverse=True)

        chosen: list[Contact] = []

        def take(pool: list[dict], role_override: ContactRole | None) -> bool:
            for p in pool:
                norm = store.normalize_person(p)
                if any(c.crustdata_person_id == norm["crustdata_person_id"] for c in chosen):
                    continue
                role = role_override or role_for(norm["title"], norm["seniority"])
                c = store.upsert_contact(p, acc.id, role=role)
                if suppression.is_suppressed(contact=c):
                    continue
                chosen.append(c)
                return True
            return False

        # a GTM-titled CXO (e.g. CRO) found by the champion search is still a fine champion
        take(champs, ContactRole.champion)
        if len(chosen) < per_account:
            take(econs, ContactRole.economic_buyer)
        while len(chosen) < per_account and (take(champs, ContactRole.champion) or take(econs, None)):
            pass
        found[acc.id] = chosen[:per_account]
        if chosen:
            with session_scope() as s:
                row = s.get(Account, acc.id)
                if row.status == AccountStatus.new:
                    row.status = AccountStatus.watching
    return found


def list_contacts(account_id: int | None = None) -> list[Contact]:
    with session_scope() as s:
        q = select(Contact)
        if account_id is not None:
            q = q.where(Contact.account_id == account_id)
        return list(s.exec(q).all())
