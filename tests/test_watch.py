import json
from datetime import date

import pytest
from sqlmodel import select

from signalloop.db import session_scope
from signalloop.jobs import daily_scan as cli
from signalloop.models import Signal, SignalType
from signalloop.services import store, suppression, watch
from signalloop.tools import crustdata
from tests.fakes import FakeCrustdata, company, job, person

TODAY = date(2026, 11, 22)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURES_DIR", str(tmp_path))
    from signalloop import config
    config.reset_settings()
    (tmp_path / "demo").mkdir()
    f = FakeCrustdata()
    crustdata.set_client(f)
    accs = {
        "fresh.io": store.upsert_account(company("fresh.io", 1, last_round_date="2026-11-18", amount=40e6, growth=5)),
        "grow.io": store.upsert_account(company("grow.io", 2, last_round_date="2024-01-01", growth=40)),
        "hire.io": store.upsert_account(company("hire.io", 3, last_round_date="2024-01-01", growth=2)),
        "quiet.io": store.upsert_account(company("quiet.io", 4, last_round_date="2023-01-01", growth=1)),
        "seed.io": store.upsert_account(company("seed.io", 5, last_round_date="2026-11-20"), is_seed=True),
    }
    f.jobs["hire.io"] = [job("hire.io", "Account Executive", "2026-11-15", 11),
                         job("hire.io", "SDR", "2026-11-01", 12),
                         job("hire.io", "Head of Sales", "2026-09-01", 13)]   # too old
    p = person(77, "New Vp", "VP Sales", "Vice President", "quiet.io")
    p["experience"]["employment_details"]["current"][0]["start_date"] = "2026-10-01"
    store.upsert_contact(p, accs["quiet.io"].id)
    return f, accs, tmp_path


def _signals():
    with session_scope() as s:
        return s.exec(select(Signal)).all()


def test_detects_each_signal_type(world):
    f, accs, _ = world
    res = watch.daily_scan(today=TODAY)
    got = {(s.account_id, s.type) for s in res.new_signals}
    assert (accs["fresh.io"].id, SignalType.funding) in got
    assert (accs["grow.io"].id, SignalType.headcount_growth) in got
    assert (accs["hire.io"].id, SignalType.job_post) in got
    assert (accs["quiet.io"].id, SignalType.leader_change) in got
    assert not any(s.account_id == accs["seed.io"].id for s in res.new_signals)   # seeds never watched
    jobsig = next(s for s in res.new_signals if s.type == SignalType.job_post)
    assert jobsig.payload_json["count"] == 2                                      # 30-day window
    assert f.count("job_search") == 1                                             # one call for all accounts


def test_running_twice_does_not_duplicate(world):
    first = watch.daily_scan(today=TODAY)
    second = watch.daily_scan(today=TODAY)
    assert first.new_signals and second.new_signals == []
    assert len(_signals()) == len(first.new_signals)


def test_day2_fixture_releases_exactly_three_new_signals(world):
    f, accs, fx = world
    store.upsert_account(company("a2.io", 6, last_round_date="2026-11-10"))
    store.upsert_account(company("a3.io", 7, last_round_date="2026-11-05"))
    (fx / "demo" / "signals_day2.json").write_text(json.dumps({"release": [
        {"domain": d, "type": "funding"} for d in ("fresh.io", "a2.io", "a3.io")]}))
    base = watch.daily_scan(today=TODAY)
    assert base.held_back == 3
    day2 = watch.daily_scan(today=TODAY, day2=True)
    assert len(day2.new_signals) == 3
    assert {s.type for s in day2.new_signals} == {SignalType.funding}


def test_suppressed_accounts_are_not_watched(world):
    f, accs, _ = world
    suppression.add("domain", "fresh.io", "dnc")
    res = watch.daily_scan(today=TODAY)
    assert not any(s.account_id == accs["fresh.io"].id for s in res.new_signals)


def test_strength_scales_with_recency_and_size():
    today = TODAY
    recent_big = watch.detect_funding({"last_funding_date": "2026-11-20", "last_funding_amount_usd": 30e6}, today, 90)
    old_small = watch.detect_funding({"last_funding_date": "2026-09-01", "last_funding_amount_usd": 2e6}, today, 90)
    assert recent_big.strength > old_small.strength
    assert watch.detect_funding({"last_funding_date": "2025-01-01"}, today, 90) is None


def test_cli_runs(world, capsys):
    assert cli.main(["--icp", "latest"]) == 1            # no approved ICP yet
