"""End-to-end (build plan Phase 6), mock mode + recorded fixtures, no network.

FIND prompts replay from fixtures/llm (copied to a temp dir so tests never write there);
ENGAGE and LEARN prompts get canned FakeLLM output. Steps:
  1. seeds -> ICP v1 approved -> >=50 lookalikes -> contacts for the top 30
  2. daily scan (day 2) -> 3 newly in-market accounts -> >=3 drafts pending, each critic-passed
     or flagged needs_human
  3. generate + import campaign -> the planted segment is the top insight
  4. ICP diff pending -> approve -> ICP v2 approved -> lookalike re-run, different top 10
Steps 3-4 need fixtures recorded after staffing every account and recording LEARN; until
then that test is skipped with the reason.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from signalloop import llm, orchestrator
from signalloop.config import get_settings
from signalloop.models import IcpStatus
from signalloop.services import (
    approvals, contacts, demo_campaign, icp as icp_svc, lookalike, outreach, scoring, seeds, traits, watch,
)
from signalloop.tools import crustdata

ROOT = Path(__file__).resolve().parents[1]
REC = json.loads((ROOT / "fixtures" / "demo" / "find_recording.json").read_text(encoding="utf-8"))
DAY2 = {r["domain"] for r in json.loads((ROOT / "fixtures" / "demo" / "signals_day2.json")
                                         .read_text(encoding="utf-8"))["release"]}
LEARN_REC = ROOT / "fixtures" / "demo" / "learn_recording.json"


def brief_from_evidence(v):
    ev = v["evidence"]
    refs = [{"field": "company.basic_info.name", "value": ev["company"]["basic_info"]["name"]},
            {"field": "product", "value": "Setup takes about 15 minutes"}]
    if ev["signals"]:
        refs.insert(1, {"field": "signals[0].type", "value": ev["signals"][0]["type"]})
    return {"headline": "In market now", "recommended_angle": "funding",
            "bullets": [{"text": "Company on file.", "refs": [0]}, {"text": "Fresh signal.", "refs": [1]},
                        {"text": "Quick setup.", "refs": [len(refs) - 1]}],
            "evidence_refs": refs}


@pytest.fixture
def replay(tmp_path, fake_llm):
    """LLM: recorded outputs for FIND (replay cache copy), canned FakeLLM for everything else."""
    cache = tmp_path / "llm"
    shutil.copytree(ROOT / "fixtures" / "llm", cache)
    fake_llm.responses.update({
        "why_now": brief_from_evidence,
        "write_email": {"subject": "Congrats on the momentum", "body": "Hi, Quillpath flags stalled deals. Open to a call?"},
        "write_linkedin": {"note": "Hi, Quillpath flags stalled deals every morning. Open to connecting?"},
        "critic_extract": {"claims": []},
    })
    llm.set_backend(llm.ReplayBackend(fake_llm, cache, "replay"))
    crustdata.set_client(crustdata.CrustdataClient(mode="mock",
                                                   fixtures_dir=get_settings().fixtures_dir / "crustdata"))
    return fake_llm


def steps_1_and_2() -> dict:
    # 1. FIND
    res = seeds.import_seeds(seeds.parse_csv(str(get_settings().fixtures_dir / "seeds.csv")))
    assert len(res.accounts) == 5
    t, _ = traits.extract_traits(res.accounts)
    v1 = icp_svc.build_icp(REC["icp_description"], t)
    icp_svc.approve_icp(v1.id)
    la = lookalike.run_lookalike(v1.id)
    assert la.upserted >= 50
    found = contacts.find_contacts()
    assert len(found) == 30 and all(found.values())

    # 2. WATCH -> ENGAGE
    base = watch.daily_scan()
    assert base.held_back == 3 and not base.errors
    day2 = watch.daily_scan(day2=True)
    newly = {s.account_id for s in day2.new_signals}
    assert len(day2.new_signals) == 3 and len(newly) == 3
    top10 = scoring.this_week(10)
    assert {b.domain for b in top10} >= DAY2
    out = outreach.draft_this_week(3)
    pending = approvals.list_pending("outreach")
    assert len(out) >= 3 and len(pending) == len(out)
    assert all(p.payload_json["critic_passed"] or p.payload_json["needs_human"] for p in pending)
    return {"v1": v1, "top10_v1": [a.domain for a in lookalike.list_accounts(10)]}


def test_e2e_find_watch_engage(replay):
    steps_1_and_2()


def _learn_fixtures_ready() -> str | None:
    if not LEARN_REC.exists():
        return "LEARN not recorded yet (fixtures/demo/learn_recording.json)"
    if not list((ROOT / "fixtures" / "llm").glob("propose_icp_diff__*.json")):
        return "no recorded ICP diff in fixtures/llm"
    return None


@pytest.mark.skipif(_learn_fixtures_ready() is not None, reason=str(_learn_fixtures_ready()))
def test_e2e_full_loop(replay, tmp_path, monkeypatch):
    learn = json.loads(LEARN_REC.read_text(encoding="utf-8"))
    monkeypatch.setattr(demo_campaign, "DEFAULT_CSV", tmp_path / "campaign.csv")
    state = steps_1_and_2()   # run_learn(generate=True) staffs the remaining accounts from fixtures

    # 3. LEARN (no canned LEARN responses: a replay miss would fail, proving the prompts match)
    res = orchestrator.run_learn(generate=True, seed=learn["seed"])
    assert res.ok, res.error
    assert res.data["rows"] == learn["campaign_rows"]
    assert set(res.data["top_insight"].split(" = ")[1].split(" (")[0].split(" × ")) == {"series_a", "20-50", "VP+"}
    assert res.data["headline"] == learn["headline"]

    # 4. ICP diff -> v2 -> lookalike re-run
    appr_id = res.data["icp_diff_approval_id"]
    assert appr_id
    done = orchestrator.approve_icp_diff(appr_id)
    assert done.ok, done.error
    assert icp_svc.latest_approved().version == 2
    assert icp_svc.get_icp(state["v1"].id).status == IcpStatus.superseded
    assert done.data["count"] == learn["v2_lookalike"]["count"]
    assert done.data["top10"] != state["top10_v1"]
