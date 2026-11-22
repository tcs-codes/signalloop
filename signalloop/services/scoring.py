"""F14: fit x timing scoring (deterministic, no LLM).

    timing   = 1 - prod(1 - strength_i * decay_i),   decay_i = exp(-days_since_event_i / 14)
    priority = PRIORITY_FIT_WEIGHT * fit + PRIORITY_TIMING_WEIGHT * timing
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlmodel import select

from signalloop.config import get_settings
from signalloop.db import session_scope
from signalloop.models import Account, Signal
from signalloop.services import suppression, watch


@dataclass
class ScoreBreakdown:
    account_id: int
    domain: str
    name: str
    fit: float
    timing: float
    priority: float
    signals: list[dict] = field(default_factory=list)  # type, strength, days, decay, contribution

    @property
    def summary(self) -> str:
        parts = [f"{s['type']} ({s['days']:.0f}d, {s['contribution']:.2f})" for s in self.signals]
        return (f"priority {self.priority:.2f} = fit {self.fit:.2f} x {get_settings().PRIORITY_FIT_WEIGHT:g}"
                f" + timing {self.timing:.2f} x {get_settings().PRIORITY_TIMING_WEIGHT:g}"
                + (f" · {', '.join(parts)}" if parts else ""))


def timing_score(signals: list[Signal], today: date) -> tuple[float, list[dict]]:
    remaining = 1.0
    detail = []
    for s in signals:
        days = watch.signal_age_days(s, today)
        d = watch.decay(days)
        contribution = max(0.0, min(1.0, s.strength * d))
        remaining *= 1 - contribution
        detail.append({"signal_id": s.id, "type": s.type.value, "strength": s.strength,
                       "days": days, "decay": round(d, 4), "contribution": round(contribution, 4)})
    detail.sort(key=lambda x: x["contribution"], reverse=True)
    return round(1 - remaining, 4), detail


def score_account(account: Account, signals: list[Signal], today: date) -> ScoreBreakdown:
    cfg = get_settings()
    timing, detail = timing_score(signals, today)
    priority = round(cfg.PRIORITY_FIT_WEIGHT * account.fit_score + cfg.PRIORITY_TIMING_WEIGHT * timing, 4)
    return ScoreBreakdown(account.id, account.domain, account.name, account.fit_score, timing,
                          priority, detail)


def score_all(today: date | None = None) -> list[ScoreBreakdown]:
    today = today or watch.as_of_date()
    with session_scope() as s:
        accounts = list(s.exec(select(Account).where(Account.is_seed == False)).all())  # noqa: E712
        sigs: dict[int, list[Signal]] = {}
        for sig in s.exec(select(Signal)).all():
            sigs.setdefault(sig.account_id, []).append(sig)
    accounts = suppression.filter_accounts(accounts)
    out = [score_account(a, sigs.get(a.id, []), today) for a in accounts]
    with session_scope() as s:
        for b in out:
            row = s.get(Account, b.account_id)
            row.timing_score, row.priority = b.timing, b.priority
    out.sort(key=lambda b: (b.priority, b.timing), reverse=True)
    return out


def this_week(n: int = 10, today: date | None = None) -> list[ScoreBreakdown]:
    """Top-n accounts by priority that have at least one live signal."""
    return [b for b in score_all(today) if b.timing > 0][:n]
