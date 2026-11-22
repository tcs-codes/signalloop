"""F1: ICP Builder (plain English -> validated Crustdata filters) + versioning.

IcpVersion.filters_json layout:
    {"spec": <ICP model dump, validated>,
     "company": <Crustdata company filter>, "person": <Crustdata person filter or None>,
     "warnings": [...], "preview_count": int | None,
     "tuned": {...}            # added by F9
    }
"""
from __future__ import annotations

import difflib
import json
from typing import Any

from sqlmodel import func, select

from signalloop.config import get_settings
from signalloop.db import session_scope
from signalloop.llm import call_llm
from signalloop.models import IcpSource, IcpStatus, IcpVersion
from signalloop.schemas import ICP, FilterCond, Traits
from signalloop.tools.crustdata import CrustdataError, get_client

# field -> spec. `dim` links a field to a fit-score dimension (F9).
COMPANY_FIELDS: dict[str, dict[str, Any]] = {
    "taxonomy.professional_network_industry": {"kind": "categorical", "dataset": "company", "dim": "industry",
                                               "desc": "industry label, e.g. 'Software Development'"},
    "funding.last_round_type": {"kind": "categorical", "dataset": "company", "dim": "funding_stage",
                                "desc": "latest round, snake_case, e.g. 'seed', 'series_a', 'series_b'"},
    "locations.country": {"kind": "country", "dim": "geo",
                          "desc": "HQ country as ISO-3 code, e.g. 'USA', 'IND', 'GBR'"},
    "headcount.total": {"kind": "number", "dim": "headcount", "desc": "total employees (integer)"},
    "headcount.growth_percent.6m": {"kind": "number", "dim": "growth",
                                    "desc": "6-month headcount growth in percent, e.g. 10 = +10%"},
    "funding.last_fundraise_date": {"kind": "date", "desc": "date of last round, ISO 'YYYY-MM-DD'"},
    "funding.last_round_amount_usd": {"kind": "number", "desc": "last round size in USD"},
    "funding.total_investment_usd": {"kind": "number", "desc": "total funding in USD"},
    "basic_info.year_founded": {"kind": "number", "desc": "founding year"},
}
PERSON_FIELDS: dict[str, dict[str, Any]] = {
    "experience.employment_details.current.seniority_level": {
        "kind": "categorical", "dataset": "person",
        "desc": "seniority bucket: 'CXO', 'Owner / Partner', 'Vice President', 'Director', 'Manager'"},
    "experience.employment_details.current.title": {"kind": "text", "desc": "title keywords, use (.)"},
    "experience.employment_details.current.function_category": {
        "kind": "categorical", "dataset": "person", "desc": "job function, e.g. 'Sales'"},
}

COUNTRY_ALIASES = {
    "us": "USA", "usa": "USA", "u.s.": "USA", "united states": "USA", "united states of america": "USA",
    "america": "USA", "india": "IND", "in": "IND", "uk": "GBR", "united kingdom": "GBR",
    "great britain": "GBR", "england": "GBR", "canada": "CAN", "germany": "DEU", "france": "FRA",
    "singapore": "SGP", "australia": "AUS", "israel": "ISR", "netherlands": "NLD", "uae": "ARE",
}
OP_FIX = {">=": "=>", "<=": "=<"}
LIST_OPS = {"in", "not_in"}


def field_guide() -> str:
    lines = ["Company filters:"]
    lines += [f"- `{f}` ({s['kind']}): {s['desc']}" for f, s in COMPANY_FIELDS.items()]
    lines += ["Person filters:"]
    lines += [f"- `{f}` ({s['kind']}): {s['desc']}" for f, s in PERSON_FIELDS.items()]
    return "\n".join(lines)


# ---------------------------------------------------------------- validation
def _match_value(dataset: str, field: str, value: str) -> tuple[str | None, str | None]:
    """Return (valid_value, warning). Exact (case-insensitive) match keeps the value,
    a close / prefix match replaces it, no match drops it."""
    q = str(value).strip()
    if not q:
        return None, None
    client = get_client()
    try:
        suggestions = client.autocomplete(dataset, field, q, limit=10)
        if not suggestions and " " in q:
            suggestions = client.autocomplete(dataset, field, q.split()[0], limit=10)
    except CrustdataError as exc:
        return q, f"could not validate {field}='{q}' ({exc}); kept as-is"
    norm = lambda s: s.lower().replace("_", " ").replace("-", " ").strip()  # noqa: E731
    for s in suggestions:
        if norm(s) == norm(q):
            return s, None
    for s in suggestions:
        words = norm(s).split()
        if norm(q) in words or norm(s).startswith(norm(q)) or \
                difflib.SequenceMatcher(None, norm(q), norm(s)).ratio() >= 0.8:
            return s, f"{field}: '{q}' -> '{s}' (closest valid value)"
    return None, f"{field}: dropped '{q}' (no matching value in Crustdata)"


def _country(value: str) -> tuple[str | None, str | None]:
    v = str(value).strip()
    if len(v) == 3 and v.isalpha():
        return v.upper(), None
    iso = COUNTRY_ALIASES.get(v.lower())
    if iso:
        return iso, None
    return None, f"locations.country: dropped '{v}' (use an ISO-3 code)"


def _coerce_scalar(kind: str, v: Any) -> Any:
    if kind == "number":
        f = float(v)
        return int(f) if f.is_integer() else f
    if kind == "date":
        return str(v)[:10]
    return v


def normalize_conditions(conds: list[FilterCond], specs: dict[str, dict]) -> tuple[list[FilterCond], list[str]]:
    out: list[FilterCond] = []
    warnings: list[str] = []
    for c in conds:
        spec = specs.get(c.field)
        if spec is None:
            warnings.append(f"dropped unsupported field '{c.field}'")
            continue
        op = OP_FIX.get(c.type, c.type)
        kind = spec["kind"]
        try:
            if op == "between":
                lo, hi = (c.value if isinstance(c.value, (list, tuple)) else [None, None])[:2]
                if lo is not None:
                    out.append(FilterCond(field=c.field, type="=>", value=_coerce_scalar(kind, lo)))
                if hi is not None:
                    out.append(FilterCond(field=c.field, type="=<", value=_coerce_scalar(kind, hi)))
                continue
            if kind in ("categorical", "country"):
                values = c.value if isinstance(c.value, list) else [c.value]
                if op == "=" and len(values) > 1:
                    op = "in"
                if op == "!=" and len(values) > 1:
                    op = "not_in"
                valid = []
                for v in values:
                    nv, warn = _country(v) if kind == "country" else _match_value(spec["dataset"], c.field, v)
                    if warn:
                        warnings.append(warn)
                    if nv is not None and nv not in valid:
                        valid.append(nv)
                if not valid:
                    warnings.append(f"dropped filter on '{c.field}' (no valid values left)")
                    continue
                if op in ("=", "!=") :
                    value: Any = valid[0] if len(valid) == 1 else valid
                    op = op if len(valid) == 1 else ("in" if op == "=" else "not_in")
                else:
                    value = valid
                out.append(FilterCond(field=c.field, type=op if op in LIST_OPS | {"=", "!="} else "in",
                                      value=value))
            elif kind in ("number", "date"):
                if isinstance(c.value, list):
                    warnings.append(f"dropped '{c.field}' {op} {c.value} (expected one value)")
                    continue
                out.append(FilterCond(field=c.field, type=op, value=_coerce_scalar(kind, c.value)))
            else:  # text
                out.append(FilterCond(field=c.field, type=op, value=c.value))
        except (TypeError, ValueError):
            warnings.append(f"dropped '{c.field}' {op} {c.value!r} (bad value)")
    return out, warnings


def validate_icp(icp: ICP) -> tuple[ICP, list[str]]:
    comp, w1 = normalize_conditions(icp.company_filters, COMPANY_FIELDS)
    pers, w2 = normalize_conditions(icp.person_filters, PERSON_FIELDS)
    w = icp.fit_weights
    total = w.industry + w.headcount + w.funding_stage + w.geo + w.growth
    weights = w if total <= 0 else type(w)(**{k: v / total for k, v in w.model_dump().items()})
    return icp.model_copy(update={"company_filters": comp, "person_filters": pers,
                                  "fit_weights": weights}), w1 + w2


def to_crustdata(conds: list[FilterCond]) -> dict | None:
    if not conds:
        return None
    items = [{"field": c.field, "type": c.type, "value": c.value} for c in conds]
    return items[0] if len(items) == 1 else {"op": "and", "conditions": items}


def preview_count(company_filter: dict | None) -> tuple[int | None, str | None]:
    if not company_filter:
        return None, "no company filters"
    try:
        resp = get_client().company_search(company_filter, limit=1, fields=["crustdata_company_id"])
    except CrustdataError as exc:
        return None, f"preview failed: {exc}"
    return resp.get("total_count"), None


# ---------------------------------------------------------------- versions
def _next_version() -> int:
    with session_scope() as s:
        return (s.exec(select(func.max(IcpVersion.version))).one() or 0) + 1


def _save(icp: ICP, warnings: list[str], *, description: str, traits: dict | None,
          source: IcpSource) -> IcpVersion:
    company_filter = to_crustdata(icp.company_filters)
    count, warn = preview_count(company_filter)
    if warn:
        warnings = warnings + [warn]
    row = IcpVersion(
        version=_next_version(), description=description, rationale=icp.rationale,
        filters_json={"spec": icp.model_dump(), "company": company_filter,
                      "person": to_crustdata(icp.person_filters),
                      "warnings": warnings, "preview_count": count},
        traits_json=traits or {}, source=source, status=IcpStatus.draft,
    )
    with session_scope() as s:
        s.add(row)
        s.flush()
        return row


def build_icp(description: str, traits: Traits | None = None) -> IcpVersion:
    raw = call_llm("icp_to_filters",
                   {"description": description or "(none; use the seed traits)",
                    "traits": traits.model_dump() if traits else "(none)",
                    "field_guide": field_guide()},
                   ICP, model=get_settings().MODEL_STRONG, agent="icp_strategist")
    icp, warnings = validate_icp(raw)
    return _save(icp, warnings, description=description,
                 traits=traits.model_dump() if traits else None,
                 source=IcpSource.seed if traits else IcpSource.manual)


def revise_icp(icp_id: int, instruction: str) -> IcpVersion:
    current = get_icp(icp_id)
    raw = call_llm("revise_icp",
                   {"icp": spec_of(current).model_dump(), "instruction": instruction,
                    "field_guide": field_guide()},
                   ICP, model=get_settings().MODEL_STRONG, agent="icp_strategist")
    icp, warnings = validate_icp(raw)
    desc = f"{current.description}\n\nEdit: {instruction}".strip()
    return _save(icp, warnings, description=desc, traits=current.traits_json, source=IcpSource.manual)


def create_version(icp: ICP, *, description: str, traits: dict | None, source: IcpSource,
                   status: IcpStatus = IcpStatus.draft) -> IcpVersion:
    """Used by F28 (learned diffs): validates and stores a given ICP."""
    icp, warnings = validate_icp(icp)
    row = _save(icp, warnings, description=description, traits=traits, source=source)
    return approve_icp(row.id) if status == IcpStatus.approved else row


def approve_icp(icp_id: int) -> IcpVersion:
    with session_scope() as s:
        row = s.get(IcpVersion, icp_id)
        if row is None:
            raise ValueError(f"ICP {icp_id} not found")
        for old in s.exec(select(IcpVersion).where(IcpVersion.status == IcpStatus.approved,
                                                   IcpVersion.id != icp_id)).all():
            old.status = IcpStatus.superseded
        row.status = IcpStatus.approved
        s.flush()
        return row


def get_icp(icp_id: int) -> IcpVersion:
    with session_scope() as s:
        row = s.get(IcpVersion, icp_id)
    if row is None:
        raise ValueError(f"ICP {icp_id} not found")
    return row


def latest_approved() -> IcpVersion | None:
    with session_scope() as s:
        return s.exec(select(IcpVersion).where(IcpVersion.status == IcpStatus.approved)
                      .order_by(IcpVersion.version.desc())).first()


def list_versions() -> list[IcpVersion]:
    with session_scope() as s:
        return list(s.exec(select(IcpVersion).order_by(IcpVersion.version.desc())).all())


def spec_of(row: IcpVersion) -> ICP:
    return ICP.model_validate((row.filters_json or {}).get("spec") or {})


def describe_filters(row: IcpVersion) -> list[dict]:
    """Flat rows for UI tables."""
    spec = spec_of(row)
    return ([{"scope": "company", "field": c.field, "type": c.type, "value": json.dumps(c.value)}
             for c in spec.company_filters] +
            [{"scope": "person", "field": c.field, "type": c.type, "value": json.dumps(c.value)}
             for c in spec.person_filters])
