import pytest

from signalloop.db import session_scope
from signalloop.llm import LLMValidationError
from signalloop.models import ApprovalKind, Campaign, IcpSource, IcpStatus, Insight
from signalloop.schemas import ICP, FilterCond
from signalloop.services import approvals, icp as icp_svc, icp_diff, lookalike
from signalloop.tools import crustdata
from tests.fakes import FakeCrustdata, company

IND, STAGE, SEN = ("taxonomy.professional_network_industry", "funding.last_round_type",
                   "experience.employment_details.current.seniority_level")


@pytest.fixture
def world():
    f = FakeCrustdata()
    f.autocomplete_values = {("company", IND): ["Software Development"],
                             ("company", STAGE): ["seed", "series_a", "series_b"],
                             ("person", SEN): ["CXO", "Owner / Partner", "Vice President", "Director"]}
    # v1 results: mixed; v2 results: Series A, 20-50 staff
    f.search_pool = [company(f"big{i}.com", 100 + i, headcount=150, stage="series_b") for i in range(10)] + \
                    [company(f"small{i}.com", 200 + i, headcount=30, stage="series_a") for i in range(10)]
    f.search_count = lambda flt: 100
    crustdata.set_client(f)
    v1 = icp_svc.create_version(ICP(company_filters=[
        FilterCond(field=IND, type="in", value=["Software Development"]),
        FilterCond(field="headcount.total", type="=>", value=20),
        FilterCond(field="headcount.total", type="=<", value=200),
        FilterCond(field=STAGE, type="in", value=["seed", "series_a", "series_b"])],
        person_filters=[FilterCond(field=SEN, type="in", value=["CXO", "Vice President", "Director"])]),
        description="v1", traits=None, source="seed", status=IcpStatus.approved)
    with session_scope() as s:
        c = Campaign(name="demo campaign", analysis_json={"baseline": 0.08})
        s.add(c)
        s.flush()
        ins = [Insight(campaign_id=c.id, dimension="funding_stage × headcount_band × seniority",
                       segment_value="series_a × 20-50 × VP+", n=24, responders=9, rate=0.375,
                       baseline_rate=0.08, lift=4.69),
               Insight(campaign_id=c.id, dimension="seniority", segment_value="Director", n=60,
                       responders=2, rate=0.033, baseline_rate=0.08, lift=0.42),
               Insight(campaign_id=c.id, dimension="signal_type_at_send", segment_value="funding",
                       n=50, responders=8, rate=0.16, baseline_rate=0.08, lift=2.0),
               Insight(campaign_id=c.id, dimension="hq_country", segment_value="IND", n=8,
                       responders=3, rate=0.375, baseline_rate=0.08, lift=4.7)]
        for i in ins:
            s.add(i)
        s.flush()
    lookalike.run_lookalike(v1.id)
    return f, v1, c, ins


def _diff(ins):
    # evidence ids are the prompt's local labels: eligible insights sorted by lift (1 = top, 2 = Directors)
    top, director = 1, 2
    return {"changes": [
        {"op": "narrow", "scope": "company", "field": STAGE, "from": ["seed", "series_a", "series_b"],
         "to": [{"field": STAGE, "type": "in", "value": ["series_a"]}], "evidence_insight_ids": [top],
         "reason": "Series A x 20-50 x VP+ replied at 4.69x (n=24)"},
        {"op": "narrow", "scope": "company", "field": "headcount.total", "from": "20-200",
         "to": [{"field": "headcount.total", "type": "=>", "value": 20},
                {"field": "headcount.total", "type": "=<", "value": 50}],
         "evidence_insight_ids": [top], "reason": "20-50 staff in the top segment"},
        {"op": "narrow", "scope": "person", "field": SEN, "from": ["CXO", "Vice President", "Director"],
         "to": [{"field": SEN, "type": "in", "value": ["CXO", "Owner / Partner", "Vice President"]}],
         "evidence_insight_ids": [top, director], "reason": "VP+ up, Directors 0.42x (n=60)"}],
        "expected_effect": "A smaller list of Series A companies with 20-50 staff, VP+ buyers."}


def test_eligibility_uses_lift_n_and_mappable_dims(world, monkeypatch):
    f, v1, c, ins = world
    ids = {i.id for i in icp_diff.eligible_insights(c.id)}
    assert ids == {ins[0].id, ins[1].id}            # signal type unmappable, IND n=8 too small
    monkeypatch.setenv("DEMO_MODE", "false")
    from signalloop import config
    config.reset_settings()
    assert {i.id for i in icp_diff.eligible_insights(c.id)} == {ins[1].id}   # n>=30 without demo override


def test_proposal_narrows_stage_headcount_and_adds_vp(world, fake_llm):
    f, v1, c, ins = world
    fake_llm.responses["propose_icp_diff"] = _diff(ins)
    appr = icp_diff.propose(c.id)
    assert appr.kind == ApprovalKind.icp_diff and appr.ref_id == v1.id
    p = appr.payload_json
    spec = ICP.model_validate(p["proposed_spec"])
    by = {(x.field, x.type): x.value for x in spec.company_filters}
    assert by[(STAGE, "in")] == ["series_a"] and by[("headcount.total", "=<")] == 50
    assert spec.person_filters[0].value == ["CXO", "Owner / Partner", "Vice President"]
    changed = {r["field"] for r in p["diff"] if r["changed"]}
    assert changed == {STAGE, "headcount.total", SEN}
    assert {e["id"] for e in p["evidence"]} == {ins[0].id, ins[1].id}
    assert icp_svc.latest_approved().id == v1.id     # nothing changes before approval


def test_approval_creates_v2_and_reruns_lookalike(world, fake_llm):
    f, v1, c, ins = world
    fake_llm.responses["propose_icp_diff"] = _diff(ins)
    top_v1 = [a.domain for a in lookalike.list_accounts(10)]
    appr = icp_diff.propose(c.id)
    v2, res = icp_diff.approve(appr.id, note="looks right")
    assert v2.version == 2 and v2.source == IcpSource.learned and v2.status == IcpStatus.approved
    assert icp_svc.get_icp(v1.id).status == IcpStatus.superseded
    assert res is not None and res.icp_id == v2.id
    top_v2 = [a.domain for a in lookalike.list_accounts(10)]
    assert top_v2 != top_v1 and all(d.startswith("small") for d in top_v2)
    assert approvals.get(appr.id).payload_json["new_version"] == 2
    with pytest.raises(ValueError):
        icp_diff.approve(appr.id)                    # already decided


def test_invalid_evidence_or_field_is_rejected(world, fake_llm):
    f, v1, c, ins = world
    bad = _diff(ins)
    bad["changes"][0]["evidence_insight_ids"] = [9]                # no such label
    bad["changes"][1]["field"] = "made.up"
    fake_llm.responses["propose_icp_diff"] = [bad, bad]
    with pytest.raises(LLMValidationError):
        icp_diff.propose(c.id)


def test_prompt_uses_labels_not_db_ids(world, fake_llm):
    f, v1, c, ins = world
    fake_llm.responses["propose_icp_diff"] = _diff(ins)
    icp_diff.propose(c.id)
    lines = fake_llm.calls[-1]["variables"]["insights"].splitlines()
    assert [ln.split(":")[0] for ln in lines] == ["1", "2"]
    assert all(str(i.id) + ":" not in "\n".join(lines) or i.id in (1, 2) for i in ins)


def test_reject_leaves_icp_unchanged(world, fake_llm):
    f, v1, c, ins = world
    fake_llm.responses["propose_icp_diff"] = _diff(ins)
    appr = icp_diff.propose(c.id)
    icp_diff.reject(appr.id, "not convinced")
    assert icp_svc.latest_approved().id == v1.id and approvals.list_pending("icp_diff") == []
