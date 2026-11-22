"""F9: lookalike search + iterative filter tuning, fit scoring, similarity reasons.

Tuning iterations only ask for a count (limit=1, ~1 credit each); the full result list is
fetched once at the end (LOOKALIKE_FETCH_LIMIT), which keeps premium-filter costs bounded.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import select

from signalloop.config import get_settings
from signalloop.db import session_scope
from signalloop.llm import call_llm
from signalloop.models import Account, AccountStatus, IcpStatus, IcpVersion, utcnow
from signalloop.schemas import ICP, FilterCond, FilterPatch, SimilarityReason
from signalloop.services import icp as icp_svc
from signalloop.services import store, suppression
from signalloop.tools.crustdata import CrustdataError, get_client, normalize_domain

STAGE_ORDER = ["pre_seed", "angel", "seed", "series_a", "series_b", "series_c",
               "series_d", "series_e", "series_f", "late_stage", "private_equity", "ipo"]
F_IND, F_STAGE, F_GEO = ("taxonomy.professional_network_industry", "funding.last_round_type",
                         "locations.country")
F_HC, F_GROWTH = "headcount.total", "headcount.growth_percent.6m"
COUNTRY_NAMES = {"USA": {"usa", "united states", "united states of america", "us"},
                 "IND": {"ind", "india"}, "GBR": {"gbr", "united kingdom", "uk"}}


# ---------------------------------------------------------------- targets & fit
def icp_targets(spec: ICP) -> dict[str, Any]:
    t: dict[str, Any] = {"industry": set(), "funding_stage": set(), "geo": set(),
                         "hc_min": None, "hc_max": None, "growth_min": None}
    for c in spec.company_filters:
        vals = c.value if isinstance(c.value, list) else [c.value]
        if c.type in ("in", "="):
            key = {F_IND: "industry", F_STAGE: "funding_stage", F_GEO: "geo"}.get(c.field)
            if key:
                t[key] |= {str(v).lower() for v in vals}
        if c.field == F_HC and c.type in ("=>", ">"):
            t["hc_min"] = float(c.value)
        if c.field == F_HC and c.type in ("=<", "<"):
            t["hc_max"] = float(c.value)
        if c.field == F_GROWTH and c.type in ("=>", ">"):
            t["growth_min"] = float(c.value)
    return t


def _geo_match(country: str | None, targets: set[str]) -> bool:
    if not country:
        return False
    c = country.lower()
    for code in targets:
        names = COUNTRY_NAMES.get(code.upper(), {code.lower()})
        if c in names or c == code.lower():
            return True
    return False


def dimension_scores(n: dict, t: dict) -> dict[str, float]:
    """Per-dimension credit in [0, 1] for dimensions the ICP constrains."""
    out: dict[str, float] = {}
    if t["industry"]:
        inds = {str(x).lower() for x in [n.get("industry"), *(n.get("industries") or [])] if x}
        out["industry"] = 1.0 if inds & t["industry"] else 0.0
    if t["hc_min"] is not None or t["hc_max"] is not None:
        h = n.get("headcount")
        lo, hi = t["hc_min"], t["hc_max"]
        if h is None:
            out["headcount"] = 0.0
        elif (lo is None or h >= lo) and (hi is None or h <= hi):
            out["headcount"] = 1.0
        elif lo is not None and h < lo:
            out["headcount"] = round(0.5 * h / lo, 3)
        else:
            out["headcount"] = round(0.5 * hi / h, 3)
    if t["funding_stage"]:
        st = (n.get("funding_stage") or "").lower()
        if st in t["funding_stage"]:
            out["funding_stage"] = 1.0
        elif st in STAGE_ORDER and any(x in STAGE_ORDER for x in t["funding_stage"]):
            dist = min(abs(STAGE_ORDER.index(st) - STAGE_ORDER.index(x))
                       for x in t["funding_stage"] if x in STAGE_ORDER)
            out["funding_stage"] = 0.5 if dist == 1 else 0.0
        else:
            out["funding_stage"] = 0.0
    if t["geo"]:
        out["geo"] = 1.0 if _geo_match(n.get("hq_country"), t["geo"]) else 0.0
    if t["growth_min"] is not None:
        g = n.get("headcount_growth_6m_pct")
        g0 = t["growth_min"]
        out["growth"] = 0.0 if g is None else (1.0 if g >= g0 else
                                               (round(0.5 * g / g0, 3) if g > 0 and g0 > 0 else 0.0))
    return out


def fit_score(normalized: dict, spec: ICP) -> tuple[float, dict[str, float]]:
    dims = dimension_scores(normalized, icp_targets(spec))
    if not dims:
        return 0.0, dims
    w = spec.fit_weights.model_dump()
    total_w = sum(w[d] for d in dims) or 1.0
    return round(sum(w[d] * s for d, s in dims.items()) / total_w, 4), dims


def template_reason(n: dict, dims: dict[str, float]) -> str:
    facts = []
    if n.get("industry"):
        facts.append(n["industry"])
    if n.get("funding_stage"):
        facts.append(n["funding_stage"].replace("_", " ").title())
    if n.get("headcount"):
        facts.append(f"{n['headcount']} staff")
    if n.get("hq_country"):
        facts.append(n["hq_country"])
    if n.get("headcount_growth_6m_pct") is not None:
        facts.append(f"{n['headcount_growth_6m_pct']:+.0f}% headcount (6m)")
    full = sum(1 for s in dims.values() if s >= 1.0)
    partial = [d.replace("_", " ") for d, s in dims.items() if 0 < s < 1]
    reason = " · ".join(facts) + f" — matches {full}/{len(dims)} ICP traits"
    if partial:
        reason += f" (near miss: {', '.join(partial)})"
    return reason


# ---------------------------------------------------------------- tuning
def _count(company_filter: dict | None) -> int | None:
    if not company_filter:
        return None
    resp = get_client().company_search(company_filter, limit=1, fields=["crustdata_company_id"])
    total = resp.get("total_count")
    return int(total) if total is not None else None


def apply_patch(spec: ICP, patch: FilterPatch) -> tuple[ICP, list[str]]:
    conds = list(spec.company_filters)
    if patch.action == "remove":
        if patch.index is None or not 0 <= patch.index < len(conds):
            raise ValueError(f"remove: bad index {patch.index}")
        conds.pop(patch.index)
    elif patch.action == "replace":
        if patch.index is None or not 0 <= patch.index < len(conds) or patch.condition is None:
            raise ValueError("replace needs a valid index and a condition")
        conds[patch.index] = patch.condition
    else:
        if patch.condition is None:
            raise ValueError("add needs a condition")
        conds.append(patch.condition)
    valid, warnings = icp_svc.normalize_conditions(conds, icp_svc.COMPANY_FIELDS)
    return spec.model_copy(update={"company_filters": valid}), warnings


@dataclass
class LookalikeResult:
    icp_id: int
    count: int | None
    converged: bool
    iterations: list[dict] = field(default_factory=list)
    fetched: int = 0
    upserted: int = 0
    skipped_suppressed: int = 0


def tune(spec: ICP, target_min: int, target_max: int, max_iters: int) -> tuple[ICP, int | None, bool, list[dict]]:
    log: list[dict] = []
    count: int | None = None
    for i in range(max_iters):
        flt = icp_svc.to_crustdata(spec.company_filters)
        count = _count(flt)
        entry: dict[str, Any] = {"iteration": i + 1, "count": count,
                                 "filters": [c.model_dump() for c in spec.company_filters]}
        log.append(entry)
        if count is None or target_min <= count <= target_max:
            return spec, count, count is not None, log
        if i == max_iters - 1:
            break
        direction = "loosen" if count < target_min else "tighten"
        history = [e["patch"] for e in log if "patch" in e]
        try:
            patch = call_llm(
                "tune_filters",
                {"filters": "\n".join(f"{j}: {json.dumps(c.model_dump())}"
                                      for j, c in enumerate(spec.company_filters)),
                 "count": count, "target_min": target_min, "target_max": target_max,
                 "direction": direction, "history": history or "(none)",
                 "field_guide": icp_svc.field_guide()},
                FilterPatch, model=get_settings().MODEL_FAST, agent="lookalike_builder")
            spec, warnings = apply_patch(spec, patch)
            entry["patch"] = patch.model_dump()
            entry["warnings"] = warnings
        except (ValueError, CrustdataError) as exc:
            entry["error"] = str(exc)
            break
    return spec, count, False, log


# ---------------------------------------------------------------- main
def _skip(norm: dict) -> bool:
    probe = Account(domain=norm["domain"])
    with session_scope() as s:
        existing = None
        if norm.get("crustdata_company_id"):
            existing = s.exec(select(Account).where(
                Account.crustdata_company_id == norm["crustdata_company_id"])).first()
        existing = existing or s.exec(select(Account).where(Account.domain == norm["domain"])).first()
    return suppression.is_suppressed(account=existing or probe)


def rescore_accounts(icp_row: IcpVersion, polish_top_n: int | None = None) -> list[Account]:
    spec = icp_svc.spec_of(icp_row)
    with session_scope() as s:
        candidates = list(s.exec(select(Account).where(Account.is_seed == False)).all())  # noqa: E712
    accounts = suppression.filter_accounts(candidates)  # read-only pass first
    updates: dict[int, tuple[float, str]] = {}
    for a in accounts:
        norm = (a.firmographics_json or {}).get("normalized", {})
        a.fit_score, dims = fit_score(norm, spec)
        a.similarity_reason = template_reason(norm, dims)
        updates[a.id] = (a.fit_score, a.similarity_reason)
    with session_scope() as s:  # then one write pass
        for acc_id, (score, reason) in updates.items():
            row = s.get(Account, acc_id)
            row.fit_score, row.similarity_reason, row.updated_at = score, reason, utcnow()
    n = get_settings().SIMILARITY_POLISH_TOP_N if polish_top_n is None else polish_top_n
    if n > 0:
        top = sorted(accounts, key=lambda a: a.fit_score, reverse=True)[:n]
        for a in top:
            norm = a.firmographics_json.get("normalized", {})
            _, dims = fit_score(norm, spec)
            r = call_llm("similarity_reason",
                         {"name": a.name, "matched": [d for d, v in dims.items() if v >= 1],
                          "missed": [d for d, v in dims.items() if v < 1],
                          "facts": a.similarity_reason},
                         SimilarityReason, model=get_settings().MODEL_FAST, agent="lookalike_builder")
            with session_scope() as s:
                row = s.get(Account, a.id)
                row.similarity_reason = r.reason
    return accounts


def run_lookalike(icp_id: int, target_min: int | None = None, target_max: int | None = None,
                  max_iters: int | None = None, fetch_limit: int | None = None) -> LookalikeResult:
    cfg = get_settings()
    target_min = target_min or cfg.LOOKALIKE_TARGET_MIN
    target_max = target_max or cfg.LOOKALIKE_TARGET_MAX
    max_iters = max_iters or cfg.LOOKALIKE_MAX_ITERS
    fetch_limit = fetch_limit or cfg.LOOKALIKE_FETCH_LIMIT

    row = icp_svc.get_icp(icp_id)
    if row.status != IcpStatus.approved:
        raise ValueError(f"ICP v{row.version} is not approved")
    spec, count, converged, log = tune(icp_svc.spec_of(row), target_min, target_max, max_iters)
    flt = icp_svc.to_crustdata(spec.company_filters)
    result = LookalikeResult(icp_id=icp_id, count=count, converged=converged, iterations=log)

    limit = min(count if count is not None else fetch_limit, fetch_limit)
    if flt and limit > 0:
        companies = get_client().company_search(flt, limit=limit).get("companies") or []
        result.fetched = len(companies)
        for c in companies:
            norm = store.normalize_company(c)
            if not norm["domain"]:
                continue
            if _skip(norm):
                result.skipped_suppressed += 1
                continue
            acc = store.upsert_account(c, icp_version_id=icp_id)
            if acc.status == AccountStatus.suppressed:
                continue
            result.upserted += 1

    with session_scope() as s:
        r = s.get(IcpVersion, icp_id)
        r.filters_json = {**(r.filters_json or {}), "tuned": {
            "spec": spec.model_dump(), "company": flt, "count": count, "converged": converged,
            "iterations": log, "fetched": result.fetched, "upserted": result.upserted}}
    rescore_accounts(icp_svc.get_icp(icp_id))
    return result


def list_accounts(limit: int | None = None, include_seeds: bool = False) -> list[Account]:
    with session_scope() as s:
        q = select(Account)
        if not include_seeds:
            q = q.where(Account.is_seed == False)  # noqa: E712
        rows = list(s.exec(q.order_by(Account.fit_score.desc())).all())
    return rows[:limit] if limit else rows


def domain_of(account: Account) -> str:
    return normalize_domain(account.domain)
