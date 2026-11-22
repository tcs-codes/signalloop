from datetime import date

from signalloop.db import session_scope
from signalloop.models import Account, Signal, SignalType
from signalloop.services import scoring, store, suppression
from tests.fakes import company

TODAY = date(2026, 11, 22)


def _acc(domain, cid, fit=0.8):
    a = store.upsert_account(company(domain, cid))
    with session_scope() as s:
        s.get(Account, a.id).fit_score = fit
    return a


def _sig(acc, days_ago_date, strength=0.8, t=SignalType.funding):
    with session_scope() as s:
        s.add(Signal(account_id=acc.id, type=t, strength=strength, dedupe_key=f"{acc.id}{days_ago_date}{t}",
                     payload_json={"event_date": days_ago_date}))


def test_recent_signal_outranks_old_identical_account():
    fresh, stale = _acc("fresh.io", 1), _acc("stale.io", 2)
    _sig(fresh, "2026-11-20")   # 2 days old
    _sig(stale, "2026-09-23")   # 60 days old
    ranked = scoring.this_week(today=TODAY)
    assert [b.domain for b in ranked][:2] == ["fresh.io", "stale.io"]
    assert ranked[0].timing > ranked[1].timing and ranked[0].fit == ranked[1].fit


def test_suppressed_accounts_never_returned():
    a, b = _acc("keep.io", 1), _acc("dnc.io", 2)
    _sig(a, "2026-11-20")
    _sig(b, "2026-11-21")
    suppression.add("domain", "dnc.io", "dnc")
    assert [x.domain for x in scoring.this_week(today=TODAY)] == ["keep.io"]


def test_timing_formula_and_breakdown():
    a = _acc("x.io", 1, fit=1.0)
    _sig(a, "2026-11-22", strength=0.5)
    _sig(a, "2026-11-22", strength=0.5, t=SignalType.job_post)
    b = scoring.this_week(today=TODAY)[0]
    assert b.timing == 0.75                       # 1 - (1-0.5)(1-0.5)
    assert b.priority == round(0.6 * 1.0 + 0.4 * 0.75, 4)
    assert "timing 0.75" in b.summary and len(b.signals) == 2
    with session_scope() as s:
        assert s.get(Account, a.id).priority == b.priority


def test_decay_is_per_signal_type():
    from signalloop.services import watch
    assert watch.decay(26, "funding") > 0.5 > watch.decay(26, "job_post")
    assert watch.decay(400, "headcount_growth") == 1.0      # a state, not an event
    assert watch.detect_growth({"headcount_growth_6m_pct": 90}, TODAY, 15).strength == 0.5


def test_fresh_funding_beats_growth_only():
    funded, growing = _acc("funded.io", 1), _acc("growing.io", 2)
    _sig(funded, "2026-11-18", strength=0.9)
    _sig(growing, "2026-11-22", strength=0.5, t=SignalType.headcount_growth)
    assert scoring.this_week(today=TODAY)[0].domain == "funded.io"


def test_accounts_without_signals_not_in_this_week():
    _acc("none.io", 1)
    assert scoring.this_week(today=TODAY) == []
