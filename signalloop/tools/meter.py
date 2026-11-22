"""F5: credit + token meter.

    with meter.run("find") as run:          # sets run_id; Crustdata + LLM calls log to it
        with meter.agent("lookalike"):      # optional finer-grained agent label
            ...

Before every billable (non-cached) Crustdata call the client asks
``check_budget(estimate)``; if the run's spend so far plus the call's estimate would
pass the budget, ``BudgetExceeded`` is raised and the call is not made. Actual spend
is the exact ``X-Credits-Used`` value (or the fixture's recorded credits in mock mode).
Per-endpoint estimates follow the list prices in docs/crustdata_api_notes.md.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from sqlmodel import select

from signalloop.config import get_settings
from signalloop.runctx import current_agent, current_run_id


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Run:
    run_id: str
    name: str
    budget: float
    credits: float = 0.0
    billable_calls: int = 0

    @property
    def remaining(self) -> float:
        return max(self.budget - self.credits, 0.0)


_current_run: ContextVar[Run | None] = ContextVar("current_run", default=None)


@contextmanager
def run(name: str, budget: float | None = None) -> Iterator[Run]:
    r = Run(run_id=f"{name}-{uuid.uuid4().hex[:10]}", name=name,
            budget=float(budget if budget is not None else get_settings().CREDIT_BUDGET_PER_RUN))
    tokens = (_current_run.set(r), current_run_id.set(r.run_id), current_agent.set(name))
    try:
        yield r
    finally:
        current_agent.reset(tokens[2])
        current_run_id.reset(tokens[1])
        _current_run.reset(tokens[0])


@contextmanager
def agent(name: str) -> Iterator[None]:
    tok = current_agent.set(name)
    try:
        yield
    finally:
        current_agent.reset(tok)


def active_run() -> Run | None:
    return _current_run.get()


# ---------------------------------------------------------------- estimates
_PER_RECORD = {
    "company_identify": 0.2, "company_enrich": 2.0,
    "person_enrich": 1.0, "person_identify": 1.0,
}
# per result, assuming the typical premium groups SignalLoop filters on / receives
_PER_RESULT = {"company_search": 1.03, "person_search": 0.23, "job_search": 0.03}
_ID_KEYS = ("domains", "names", "crustdata_company_ids", "professional_network_profile_urls", "emails")


def estimate(endpoint_name: str, body: dict[str, Any]) -> float:
    if endpoint_name.endswith("_autocomplete"):
        return 0.0
    if endpoint_name in _PER_RESULT:
        return _PER_RESULT[endpoint_name] * int(body.get("limit") or 20)
    if endpoint_name in _PER_RECORD:
        n = sum(len(body.get(k) or []) for k in _ID_KEYS)
        return _PER_RECORD[endpoint_name] * max(n, 1)
    return 0.0


def check_budget(estimated_credits: float) -> None:
    r = _current_run.get()
    if r is None or estimated_credits <= 0:  # free endpoints never blocked
        return
    if r.credits >= r.budget or r.credits + estimated_credits > r.budget:
        raise BudgetExceeded(
            f"run '{r.run_id}' credit budget {r.budget:g} would be exceeded: "
            f"spent {r.credits:g}, next call ~{estimated_credits:g}")


def add_credits(credits: float) -> None:
    r = _current_run.get()
    if r is not None:
        r.credits += credits
        r.billable_calls += 1


# ---------------------------------------------------------------- reporting
def summary(run_id: str) -> dict[str, Any]:
    from signalloop.db import session_scope
    from signalloop.models import RunLog

    with session_scope() as s:
        rows = s.exec(select(RunLog).where(RunLog.run_id == run_id)).all()
    by_agent: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    total = defaultdict(float)
    for r in rows:
        is_llm = r.input_tokens or r.output_tokens or not r.endpoint_or_model.startswith("/")
        for bucket in (total, by_agent[r.agent or "unknown"]):
            bucket["calls"] += 1
            bucket["credits"] += r.credits
            bucket["input_tokens"] += r.input_tokens
            bucket["output_tokens"] += r.output_tokens
            bucket["errors"] += 0 if r.ok else 1
            if not is_llm:
                bucket["cached_calls" if r.cached else "billable_calls"] += 1
            else:
                bucket["llm_calls"] += 1
    keys = ("calls", "billable_calls", "cached_calls", "llm_calls", "credits",
            "input_tokens", "output_tokens", "errors")
    fmt = lambda d: {k: (round(d.get(k, 0), 4) if k == "credits" else int(d.get(k, 0))) for k in keys}  # noqa: E731
    return {"run_id": run_id, **fmt(total), "by_agent": {a: fmt(d) for a, d in by_agent.items()}}
