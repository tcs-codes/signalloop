"""Click through Home -> Inbox -> Learn -> Inbox (ICP change) -> Credits with fakes (no network, no real LLM)."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from signalloop.models import IcpStatus
from signalloop.schemas import ICP, FilterCond
from signalloop.services import approvals, contacts, demo_campaign, icp as icp_svc, lookalike
from signalloop.tools import crustdata
from tests.fakes import FakeCrustdata, company, person
from tests.test_analysis import good_narrative

APP = Path(__file__).resolve().parents[1] / "app"
IND, STAGE, SEN = ("taxonomy.professional_network_industry", "funding.last_round_type",
                   "experience.employment_details.current.seniority_level")
TITLES = [("VP Sales", "Vice President"), ("Director of Sales", "Director"), ("CEO", "CXO")]


def _btn(at, label, index=0):
    return [b for b in at.button if b.label == label][index]


def brief_from_evidence(v):
    ev = v["evidence"]
    return {"headline": "Growing fast and hiring", "recommended_angle": "growth",
            "bullets": [{"text": "Domain on file.", "refs": [0]}, {"text": "Headcount on file.", "refs": [1]},
                        {"text": "Quick setup.", "refs": [2]}],
            "evidence_refs": [
                {"field": "company.basic_info.primary_domain", "value": ev["company"]["basic_info"]["primary_domain"]},
                {"field": "company.headcount.total", "value": str(ev["company"]["headcount"]["total"])},
                {"field": "product", "value": "Setup takes about 15 minutes"}]}


def diff_from_insights(v):
    ids = [int(line.split(":")[0]) for line in v["insights"].splitlines() if line.strip()]
    return {"changes": [{"op": "narrow", "scope": "person", "field": SEN, "from": None,
                         "to": [{"field": SEN, "type": "in", "value": ["CXO", "Vice President"]}],
                         "evidence_insight_ids": ids[:1], "reason": "seniority insight"}],
            "expected_effect": "Fewer, more senior buyers."}


@pytest.fixture
def world(fake_llm, tmp_path, monkeypatch):
    monkeypatch.setattr(demo_campaign, "DEFAULT_CSV", tmp_path / "campaign.csv")
    f = FakeCrustdata()
    f.autocomplete_values = {("company", IND): ["Software Development"],
                             ("company", STAGE): ["seed", "series_a", "series_b"],
                             ("person", SEN): ["CXO", "Vice President", "Director"]}
    f.search_count = lambda flt: 100
    pid = 10_000
    for i in range(60):
        seg = i < 20
        d = f"co{i}.io"
        f.search_pool.append(company(d, i + 1, stage="series_a" if seg else "series_b",
                                     headcount=35 if seg else 120, growth=40 if i < 12 else 5,
                                     last_round_date="2026-11-10" if i < 5 else "2025-01-01"))
        people = []
        for title, sen in TITLES:
            pid += 1
            people.append(person(pid, f"P {pid}", title, sen, d))
        f.people[d] = people
    crustdata.set_client(f)
    v1 = icp_svc.create_version(ICP(company_filters=[
        FilterCond(field=IND, type="in", value=["Software Development"]),
        FilterCond(field=STAGE, type="in", value=["seed", "series_a", "series_b"])],
        person_filters=[FilterCond(field=SEN, type="in", value=["CXO", "Vice President", "Director"])]),
        description="v1", traits=None, source="seed", status=IcpStatus.approved)
    lookalike.run_lookalike(v1.id)
    contacts.find_contacts(top_n=60)
    fake_llm.responses.update({
        "why_now": brief_from_evidence,
        "write_email": {"subject": "Quick question", "body": "Hi, Quillpath flags stalled deals. Open to a call?"},
        "write_linkedin": {"note": "Hi, Quillpath flags stalled deals every morning. Open to connecting?"},
        "critic_extract": {"claims": []},
        "segment_insights": good_narrative,
        "propose_icp_diff": diff_from_insights,
    })
    return f, v1


def test_full_loop_click_through(world):
    home = AppTest.from_file(str(APP / "Home.py")).run(timeout=60)
    assert not home.exception
    _btn(home, "Run daily scan now").click().run(timeout=60)
    assert not home.exception and any("new signal" in s.value for s in home.success)
    home.slider[0].set_value(1)
    _btn(home, "Draft outreach").click().run(timeout=120)
    assert not home.exception and any("passed the critic" in s.value for s in home.success)
    n_pending = len(approvals.list_pending("outreach"))
    assert n_pending >= 2

    inbox = AppTest.from_file(str(APP / "pages" / "3_Inbox.py")).run(timeout=60)
    assert not inbox.exception
    _btn(inbox, "Approve").click().run(timeout=60)
    assert not inbox.exception and len(approvals.list_pending("outreach")) == n_pending - 1
    _btn(inbox, "Reject").click().run(timeout=60)
    assert len(approvals.list_pending("outreach")) == n_pending - 2

    learn = AppTest.from_file(str(APP / "pages" / "4_Learn.py")).run(timeout=60)
    _btn(learn, "Generate demo campaign").click().run(timeout=60)
    _btn(learn, "Run LEARN").click().run(timeout=120)
    assert not learn.exception
    assert approvals.list_pending("icp_diff"), "LEARN should propose an ICP change"

    inbox = AppTest.from_file(str(APP / "pages" / "3_Inbox.py")).run(timeout=60)
    _btn(inbox, "Approve → create ICP v2 + rerun FIND").click().run(timeout=120)
    assert not inbox.exception
    assert icp_svc.latest_approved().version == 2

    credits = AppTest.from_file(str(APP / "pages" / "5_Credits.py")).run(timeout=60)
    assert not credits.exception
    assert any(m.label == "Crustdata credits" for m in credits.metric)
