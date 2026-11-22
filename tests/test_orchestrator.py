"""Orchestrator entry points with fakes (temp DB, no network, no real LLM)."""
from sqlmodel import select

from signalloop import orchestrator
from signalloop.db import session_scope
from signalloop.models import RunLog
from signalloop.services import icp as icp_svc, traits as traits_svc
from tests.test_ui_find import SEEDS, fake  # noqa: F401  (fixture: seeds, ICP, lookalike pool, people)
from tests.test_ui_loop import world  # noqa: F401  (fixture: approved ICP, accounts, contacts, LLM)


def test_run_find_builds_draft_icp(fake):  # noqa: F811
    res = orchestrator.run_find(SEEDS, "Series A software companies, 20-200 staff")
    assert res.ok, res.error
    assert res.data["seed_accounts"] == SEEDS and res.data["icp_version"] == 1
    assert icp_svc.latest_approved() is None            # draft only; the UI approves
    assert res.summary["llm_calls"] == 2                # traits + ICP


def test_run_find_after_approval(fake):  # noqa: F811
    first = orchestrator.run_find(SEEDS)
    icp_svc.approve_icp(first.data["icp_id"])
    res = orchestrator.run_find_after_approval()
    assert res.ok, res.error
    assert res.data["upserted"] == 60 and res.data["contacts"] == 60 and res.data["staffed_accounts"] == 30


def test_run_watch_engage(world):  # noqa: F811
    res = orchestrator.run_watch_engage(n=1)
    assert res.ok, res.error
    assert res.data["new_signals"] > 0 and res.data["drafts"] >= 2
    assert res.data["critic_passed"] == res.data["drafts"]


def test_run_learn_then_approve(world, tmp_path, monkeypatch):  # noqa: F811
    res = orchestrator.run_learn(generate=True)
    assert res.ok, res.error
    assert res.data["link_rate"] >= 0.95 and res.data["icp_diff_approval_id"]
    done = orchestrator.approve_icp_diff(res.data["icp_diff_approval_id"])
    assert done.ok, done.error
    assert done.data["icp_version"] == 2 and len(done.data["top10"]) == 10


def test_errors_are_logged_not_raised():
    res = orchestrator.run_find_after_approval()            # no approved ICP
    assert not res.ok and "no approved ICP" in res.error
    with session_scope() as s:
        row = s.exec(select(RunLog).where(RunLog.run_id == res.run_id, RunLog.ok == False)).first()  # noqa: E712
    assert row is not None and row.endpoint_or_model == "orchestrator"
    bad = orchestrator.run_learn()                          # neither CSV nor generate
    assert not bad.ok and "campaign_csv" in bad.error
    assert traits_svc  # imported module is used by run_find
