import pytest
from sqlmodel import select

from signalloop.db import session_scope
from signalloop.models import IcpStatus, IcpVersion
from signalloop.schemas import ICP, FilterCond
from signalloop.services import icp as icp_svc
from signalloop.tools import crustdata
from tests.fakes import FakeCrustdata

IND = "taxonomy.professional_network_industry"
STAGE = "funding.last_round_type"
SEN = "experience.employment_details.current.seniority_level"


@pytest.fixture
def fake():
    f = FakeCrustdata()
    f.autocomplete_values = {
        ("company", IND): ["Software Development", "IT Services and IT Consulting", "Financial Services"],
        ("company", STAGE): ["seed", "series_a", "series_b", "series_c", "pre_seed"],
        ("person", SEN): ["CXO", "Vice President", "Director", "Manager"],
    }
    f.search_count = lambda flt: 1234
    crustdata.set_client(f)
    return f


LLM_ICP = {
    "company_filters": [
        {"field": IND, "type": "in", "value": ["Software", "SaaS"]},
        {"field": "headcount.total", "type": "between", "value": [20, 200]},
        {"field": STAGE, "type": "in", "value": ["Seed", "Series A", "series_b"]},
        {"field": "locations.country", "type": "in", "value": ["US", "India"]},
        {"field": "made.up.field", "type": "=", "value": 1},
    ],
    "person_filters": [{"field": SEN, "type": "in", "value": ["VP", "Vice President", "CXO"]}],
    "fit_weights": {"industry": 3, "headcount": 2.5, "funding_stage": 2.5, "geo": 1, "growth": 1},
    "rationale": "B2B SaaS at Seed–Series B in the US and India.",
}


def test_description_produces_validated_filters(fake, fake_llm):
    fake_llm.responses["icp_to_filters"] = LLM_ICP
    row = icp_svc.build_icp("B2B SaaS, 20–200 employees, Seed to Series B, US/India")
    spec = icp_svc.spec_of(row)
    by = {(c.field, c.type): c.value for c in spec.company_filters}
    assert by[(IND, "in")] == ["Software Development"]                 # 'Software' mapped, 'SaaS' dropped
    assert by[("headcount.total", "=>")] == 20 and by[("headcount.total", "=<")] == 200
    assert by[(STAGE, "in")] == ["seed", "series_a", "series_b"]
    assert by[("locations.country", "in")] == ["USA", "IND"]
    assert all(c.field in icp_svc.COMPANY_FIELDS for c in spec.company_filters)
    assert spec.person_filters[0].value == ["Vice President", "CXO"]
    w = row.filters_json["warnings"]
    assert any("made.up.field" in x for x in w) and any("SaaS" in x for x in w)
    assert row.filters_json["company"]["op"] == "and"
    assert row.filters_json["preview_count"] == 1234
    assert abs(sum(spec.fit_weights.model_dump().values()) - 1) < 1e-9
    assert row.status == IcpStatus.draft and row.version == 1
    # preview used limit=1
    assert [c for c in fake.calls if c[0] == "company_search"][0][1]["limit"] == 1


def test_operator_fixes():
    conds, _ = icp_svc.normalize_conditions(
        [FilterCond(field="headcount.total", type=">=", value="20"),
         FilterCond(field="funding.last_fundraise_date", type="<=", value="2026-11-01T00:00:00")],
        icp_svc.COMPANY_FIELDS)
    assert [(c.type, c.value) for c in conds] == [("=>", 20), ("=<", "2026-11-01")]


def test_revision_creates_next_version(fake, fake_llm):
    fake_llm.responses["icp_to_filters"] = LLM_ICP
    v1 = icp_svc.build_icp("B2B SaaS")
    revised = {**LLM_ICP, "company_filters": LLM_ICP["company_filters"][:2]}
    fake_llm.responses["revise_icp"] = revised
    v2 = icp_svc.revise_icp(v1.id, "drop the funding and geo filters")
    assert v2.version == v1.version + 1 and v2.status == IcpStatus.draft
    assert len(icp_svc.spec_of(v2).company_filters) == 3   # industry + between expanded to 2
    assert "Edit: drop the funding" in v2.description


def test_only_one_approved_at_a_time(fake, fake_llm):
    fake_llm.responses["icp_to_filters"] = LLM_ICP
    a, b = icp_svc.build_icp("x"), icp_svc.build_icp("y")
    icp_svc.approve_icp(a.id)
    icp_svc.approve_icp(b.id)
    with session_scope() as s:
        rows = {r.id: r.status for r in s.exec(select(IcpVersion)).all()}
    assert rows == {a.id: IcpStatus.superseded, b.id: IcpStatus.approved}
    assert icp_svc.latest_approved().id == b.id


def test_create_version_for_learned_diffs(fake):
    icp = ICP(company_filters=[FilterCond(field=STAGE, type="in", value=["series_a"])])
    row = icp_svc.create_version(icp, description="learned", traits=None,
                                 source="learned", status=IcpStatus.approved)
    assert row.status == IcpStatus.approved and row.source == "learned"
