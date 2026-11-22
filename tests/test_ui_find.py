"""Click through the FIND pages (Setup, Accounts) with fakes: no network, no real LLM."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from signalloop.services import icp as icp_svc
from signalloop.services import traits as traits_svc
from signalloop.tools import crustdata
from tests.fakes import FakeCrustdata, company, person

APP = Path(__file__).resolve().parents[1] / "app" / "pages"
IND, STAGE = "taxonomy.professional_network_industry", "funding.last_round_type"
SEEDS = ["s1.io", "s2.io", "s3.io", "s4.io", "s5.io"]


def _btn(at, label):
    return next(b for b in at.button if b.label == label)


@pytest.fixture
def fake(fake_llm):
    f = FakeCrustdata()
    for i, d in enumerate(SEEDS):
        f.companies[d] = company(d, 10 + i, headcount=30 + i)
    f.autocomplete_values = {("company", IND): ["Software Development"],
                             ("company", STAGE): ["series_a"]}
    f.search_pool = [company(f"look{i}.com", 100 + i, headcount=25 + i) for i in range(60)]
    f.search_count = lambda flt: 90
    for i in range(60):
        d = f"look{i}.com"
        f.people[d] = [person(5000 + i, f"Vp {i}", "VP Sales", "Vice President", d),
                       person(6000 + i, f"Ceo {i}", "CEO", "CXO", d)]
    crustdata.set_client(f)

    def traits_response(v):
        st = traits_svc.compute_stats(traits_svc.seed_accounts())
        return {"traits": [{"dimension": d, "values": i["candidate"]["values"],
                            "coverage": i["candidate"]["coverage"],
                            "explanation": f"{i['candidate']['count']} of {st['n']} seeds"}
                           for d, i in st["dimensions"].items() if i["candidate"]["coverage"] >= 0.6],
                "persona_hypothesis": {"titles": ["VP Sales"], "seniority": ["Vice President", "CXO"]}}

    fake_llm.responses.update({
        "extract_traits": traits_response,
        "icp_to_filters": {"company_filters": [
            {"field": IND, "type": "in", "value": ["Software Development"]},
            {"field": "headcount.total", "type": "between", "value": [20, 200]},
            {"field": STAGE, "type": "in", "value": ["series_a"]}],
            "person_filters": [], "rationale": "Series A software companies."},
    })
    return f


def test_setup_then_accounts_click_through(fake):
    at = AppTest.from_file(str(APP / "1_Setup_ICP.py")).run(timeout=60)
    at.text_area[0].input("\n".join(SEEDS))
    _btn(at, "Import seeds").click().run(timeout=60)
    assert not at.exception and "Imported 5 seed accounts." in [s.value for s in at.success]

    _btn(at, "Extract traits").click().run(timeout=60)
    assert not at.exception and at.session_state["traits"]["traits"]

    at.text_area[1].input("Series A software companies, 20-200 staff")
    _btn(at, "Generate ICP").click().run(timeout=60)
    assert not at.exception
    _btn(at, "Approve this ICP").click().run(timeout=60)
    assert not at.exception and icp_svc.latest_approved() is not None

    acc = AppTest.from_file(str(APP / "2_Accounts.py")).run(timeout=60)
    _btn(acc, "Run lookalike search").click().run(timeout=60)
    assert not acc.exception
    assert any("Converged" in s.value for s in acc.success)
    _btn(acc, "Find decision-makers").click().run(timeout=60)
    assert not acc.exception
    assert any("contacts across 30 accounts" in s.value for s in acc.success)
