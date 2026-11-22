"""Evidence documents shared by briefs (F15), outreach (F16) and the critic (F17).

The evidence JSON is the only source of facts an LLM may use about a prospect:
    {"company":  <stored Crustdata company_data: basic_info, headcount, funding, locations, taxonomy>,
     "signals":  [{"type", "strength", "payload"}, ...],
     "contact":  {"name", "title", "seniority", "role"}            (when drafting for a person)
     "product":  "<product one-pager text>"}
Paths look like ``company.funding.last_round_amount_usd`` or ``signals[0].payload.amount_usd``.
"""
from __future__ import annotations

import re
from typing import Any

from signalloop.models import Account, Contact, Signal
from signalloop.services import traits

COMPANY_SECTIONS = ("crustdata_company_id", "basic_info", "headcount", "funding", "locations", "taxonomy")
_MISSING = object()


def build(account: Account, signals: list[Signal], contact: Contact | None = None) -> dict:
    raw = account.firmographics_json or {}
    company = {k: raw[k] for k in COMPANY_SECTIONS if k in raw}
    doc: dict[str, Any] = {
        "company": company,
        "signals": [{"type": s.type.value, "strength": s.strength,
                     "payload": {k: v for k, v in (s.payload_json or {}).items() if k != "evidence"}}
                    for s in signals],
        "product": traits.product_text(),
    }
    if contact is not None:
        doc["contact"] = {"name": contact.name, "title": contact.title,
                          "seniority": contact.seniority, "role": contact.role.value}
    return doc


_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def resolve(doc: Any, path: str) -> Any:
    """Return the value at ``path`` or ``_MISSING``. Accepts ``a.b[0].c`` and ``a.b.0.c``."""
    cur = doc
    for name, idx in _TOKEN.findall(path.strip()):
        key = idx or name
        if isinstance(cur, list):
            if not key.isdigit() or int(key) >= len(cur):
                return _MISSING
            cur = cur[int(key)]
        elif isinstance(cur, dict):
            if key not in cur:
                return _MISSING
            cur = cur[key]
        else:
            return _MISSING
    return cur


def exists(doc: Any, path: str) -> bool:
    return resolve(doc, path) is not _MISSING


# ---------------------------------------------------------------- numbers
_NUM = re.compile(r"(?<![\w.])\$?\s?(\d+(?:[.,]\d+)*)\s?(k|m|mm|bn|b|million|billion|thousand|%)?(?![\w])", re.I)
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}


def numbers_in(text: str) -> list[float]:
    out = []
    for num, unit in _NUM.findall(text or ""):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        out.append(v * _MULT.get((unit or "").lower(), 1))
    return out


def evidence_numbers(doc: Any) -> list[float]:
    vals: list[float] = []

    def walk(x):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            vals.append(float(x))
        elif isinstance(x, str):
            vals.extend(numbers_in(x))
            vals.extend(float(p) for p in re.findall(r"\d{4}|\d{1,2}", x) if p)  # years, days in dates
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(doc)
    return vals


def number_supported(n: float, pool: list[float]) -> bool:
    for v in pool:
        if v == n:
            return True
        if v and abs(v - n) / abs(v) <= 0.05:   # rounding: $40M vs 40,000,000; 15% vs 15.38
            return True
        if abs(v - n) < 1 and n >= 1:           # 15 vs 15.4 (percentages, headcounts)
            return True
    return False


def value_matches(resolved: Any, claimed: str) -> bool:
    """Loose check that the quoted value is what the evidence says."""
    if resolved is _MISSING or resolved is None:
        return False
    c = str(claimed).strip().lower()
    if not c:
        return True
    r = str(resolved).lower()
    if c in r or r in c:
        return True
    cn, rn = numbers_in(c), evidence_numbers(resolved)
    return bool(cn) and all(number_supported(x, rn) for x in cn)


def norm_text(s: str) -> str:
    """Lowercase, drop markdown emphasis/bullets/headings, collapse whitespace."""
    s = re.sub(r"[*_`#>]+", "", s or "")
    s = re.sub(r"^\s*[-•]\s+", " ", s, flags=re.M)
    return re.sub(r"\s+", " ", s.lower()).strip()
