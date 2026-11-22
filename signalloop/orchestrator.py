"""Orchestrator: the three loop entry points, each wrapped in a metered run.

    run_find(seed_domains, icp_description=None)        F7 -> F8 -> F1 (draft ICP; the UI approves)
    run_find_after_approval(icp_id=None)                F9 -> F11
    run_watch_engage(icp_id=None, day2=False, n=10)     F12 -> F14 -> F15 -> F16 -> F17 -> inbox
    run_learn(campaign_csv=None, generate=False, ...)   F24 -> F25 -> F26 -> F28 -> inbox

Every run is wrapped in ``meter.run(...)``. An exception is logged to ``run_log`` (ok=false)
and returned in ``RunResult.error`` instead of propagating, so a UI calling these never crashes.
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from signalloop.db import session_scope
from signalloop.models import RunLog
from signalloop.runctx import current_agent
from signalloop.services import (
    analysis, campaigns, contacts, demo_campaign, icp as icp_svc, icp_diff, lookalike, outreach,
    seeds, traits, watch,
)
from signalloop.tools import meter


@dataclass
class RunResult:
    name: str
    run_id: str = ""
    ok: bool = True
    error: str | None = None
    credits: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


def _run(name: str, fn: Callable[[dict[str, Any]], None], budget: float | None = None) -> RunResult:
    res = RunResult(name=name)
    with meter.run(name, budget=budget) as r:
        res.run_id = r.run_id
        try:
            fn(res.data)
        except Exception as exc:  # logged, not raised: callers (UI, CLI) stay up
            res.ok, res.error = False, f"{type(exc).__name__}: {exc}"
            with session_scope() as s:
                s.add(RunLog(run_id=r.run_id, agent=current_agent.get() or name,
                             endpoint_or_model="orchestrator", ok=False,
                             error=(res.error + "\n" + traceback.format_exc())[-2000:]))
        res.credits = r.credits
    res.summary = meter.summary(res.run_id)
    return res


# ---------------------------------------------------------------- FIND
def run_find(seed_domains: list[str], icp_description: str | None = None) -> RunResult:
    """Import seeds, extract traits, build a DRAFT ICP. Approval happens in the UI."""
    def steps(d: dict) -> None:
        with meter.agent("seeds"):
            imp = seeds.import_seeds(seed_domains)
        d["seed_accounts"] = [a.domain for a in imp.accounts]
        d["unresolved"] = imp.unresolved
        if not imp.accounts:
            raise ValueError("no seed domains could be resolved")
        with meter.agent("icp_strategist"):
            t, _ = traits.extract_traits(imp.accounts)
            d["traits"] = t.model_dump()
            desc = icp_description or "; ".join(
                f"{tr.dimension.replace('_', ' ')}: {', '.join(tr.values)}" for tr in t.traits)
            row = icp_svc.build_icp(desc, t)
        d["icp_id"], d["icp_version"] = row.id, row.version
        d["preview_count"] = row.filters_json.get("preview_count")
        d["warnings"] = row.filters_json.get("warnings", [])
    return _run("find", steps)


def run_find_after_approval(icp_id: int | None = None) -> RunResult:
    """Lookalike search + decision-makers for an approved ICP."""
    def steps(d: dict) -> None:
        row = icp_svc.get_icp(icp_id) if icp_id else icp_svc.latest_approved()
        if row is None:
            raise ValueError("no approved ICP")
        with meter.agent("lookalike_builder"):
            la = lookalike.run_lookalike(row.id)
        d.update(icp_id=row.id, count=la.count, converged=la.converged, upserted=la.upserted,
                 iterations=len(la.iterations))
        with meter.agent("account_researcher"):
            found = contacts.find_contacts(icp_id=row.id)
        d["contacts"] = sum(len(v) for v in found.values())
        d["staffed_accounts"] = sum(1 for v in found.values() if v)
    return _run("find.approved", steps)


# ---------------------------------------------------------------- WATCH -> ENGAGE
def run_watch_engage(icp_id: int | None = None, *, day2: bool = False, n: int = 10,
                     draft: bool = True) -> RunResult:
    def steps(d: dict) -> None:
        with meter.agent("signal_watcher"):
            scan = watch.daily_scan(icp_id, day2=day2)
        d.update(new_signals=len(scan.new_signals), held_back=scan.held_back,
                 new_accounts=scan.new_accounts, scan_errors=scan.errors, as_of=scan.as_of)
        from signalloop.services import scoring
        d["this_week"] = [b.domain for b in scoring.this_week(n)]
        if draft:
            out = outreach.draft_this_week(n)
            d["drafts"] = len(out)
            d["critic_passed"] = sum(o.critic.passed for o in out)
            d["needs_human"] = sum(o.critic.needs_human for o in out)
            d["rewrites"] = sum(o.critic.rewrites for o in out)
    return _run("watch_engage", steps)


# ---------------------------------------------------------------- LEARN
def staff_for_demo_campaign() -> int:
    """Mock mode only: find decision-makers (from fixtures, free) for every unstaffed account,
    so the synthetic campaign covers the same people as the recording. No-op in live mode."""
    from signalloop.config import get_settings
    if not get_settings().is_mock:
        return 0
    staffed = {c.account_id for c in contacts.list_contacts()}
    todo = [a.id for a in lookalike.list_accounts() if a.id not in staffed]
    if todo:
        contacts.find_contacts(account_ids=todo)
    return len(todo)


def run_learn(campaign_csv: str | Path | bytes | None = None, *, generate: bool = False,
              name: str = demo_campaign.DEFAULT_NAME, seed: int = demo_campaign.DEFAULT_SEED) -> RunResult:
    """Import results (or generate the synthetic demo campaign), enrich, analyze, propose an ICP diff."""
    def steps(d: dict) -> None:
        src = campaign_csv
        if generate:
            d["staffed_for_campaign"] = staff_for_demo_campaign()
            d["generated_rows"] = demo_campaign.generate(demo_campaign.DEFAULT_CSV, seed=seed)
            src = demo_campaign.DEFAULT_CSV
        if src is None:
            raise ValueError("pass campaign_csv or generate=True")
        with meter.agent("campaign_analyst"):
            imp = campaigns.import_campaign(src, name)
            campaigns.enrich_campaign(imp.campaign.id)
            res = analysis.analyze(imp.campaign.id)
        d.update(campaign_id=imp.campaign.id, rows=imp.rows, link_rate=round(imp.contact_link_rate, 3),
                 insights=len(res.insights), baseline=round(res.baseline, 4),
                 headline=res.narrative.headline if res.narrative else None)
        top = res.top(1)
        d["top_insight"] = (f"{top[0].dimension} = {top[0].segment_value} "
                            f"({top[0].lift:.2f}x, n={top[0].n})") if top else None
        with meter.agent("icp_strategist"):
            appr = icp_diff.propose(imp.campaign.id)
        d["icp_diff_approval_id"] = appr.id if appr else None
    return _run("learn", steps)


def approve_icp_diff(approval_id: int, note: str = "") -> RunResult:
    """F28 approval: ICP v(n+1) + lookalike re-run (the expensive step, metered on its own)."""
    def steps(d: dict) -> None:
        v2, la = icp_diff.approve(approval_id, note=note)
        d.update(icp_id=v2.id, icp_version=v2.version,
                 count=la.count if la else None, upserted=la.upserted if la else None,
                 top10=[a.domain for a in lookalike.list_accounts(10)])
    return _run("learn.approve", steps)
