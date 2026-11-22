"""F11: decision-maker finder (person search scoped to each top account)."""
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


def _rank(norm: dict, persona: Persona) -> float:
    sen = [s.lower() for s in persona.seniority]
    s = (norm.get("seniority") or "").lower()
    score = (len(sen) - sen.index(s)) if s in sen else 0.0
    title = (norm.get("title") or "").lower()
    if any(w in title for t in persona.titles for w in [t.lower()]):
        score += 2
    elif any(k in title for t in persona.titles for k in t.lower().split() if len(k) > 3):
        score += 1
    return score


def person_filter(domain: str, persona: Persona) -> dict:
    any_of = [{"field": F_SENIORITY, "type": "in", "value": persona.seniority}]
    any_of += [{"field": F_TITLE, "type": "(.)", "value": t} for t in persona.titles[:4]]
    return {"op": "and", "conditions": [
        {"field": F_DOMAIN, "type": "=", "value": domain},
        {"op": "or", "conditions": any_of},
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
    found: dict[int, list[Contact]] = {}
    for acc in accounts:
        try:
            resp = client.person_search(person_filter(acc.domain, persona), limit=per_account + 2)
        except CrustdataError:
            found[acc.id] = []
            continue
        profiles = resp.get("profiles") or []
        ranked = sorted(profiles, key=lambda p: _rank(store.normalize_person(p), persona), reverse=True)
        chosen: list[Contact] = []
        roles_taken: set[ContactRole] = set()
        # first pass: one per role (champion, economic buyer); second pass: fill
        for want_new_role in (True, False):
            for p in ranked:
                if len(chosen) >= per_account:
                    break
                norm = store.normalize_person(p)
                role = role_for(norm["title"], norm["seniority"])
                if want_new_role and (role in roles_taken or role == ContactRole.other):
                    continue
                if any(c.crustdata_person_id == norm["crustdata_person_id"] for c in chosen):
                    continue
                c = store.upsert_contact(p, acc.id, role=role)
                if suppression.is_suppressed(contact=c):
                    continue
                chosen.append(c)
                roles_taken.add(role)
        found[acc.id] = chosen
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
