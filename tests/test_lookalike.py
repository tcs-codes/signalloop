import pytest

from signalloop.models import IcpStatus
from signalloop.schemas import ICP, FilterCond, FitWeights
from signalloop.services import icp as icp_svc
from signalloop.services import lookalike, store, suppression
from signalloop.tools import crustdata
from tests.fakes import FakeCrustdata, company

IND, STAGE = "taxonomy.professional_network_industry", "funding.last_round_type"


def _has(flt, field, typ=None):
    conds = flt.get("conditions", [flt]) if flt else []
    return any(c.get("field") == field and (typ is None or c.get("type") == typ) for c in conds)


@pytest.fixture
def fake():
    f = FakeCrustdata()
    f.autocomplete_values = {("company", IND): ["Software Development"],
                             ("company", STAGE): ["seed", "series_a", "series_b"]}
    f.search_pool = [company(f"co{i}.com", 1000 + i, headcount=20 + i, stage="series_a")
                     for i in range(60)]
    crustdata.set_client(f)
    return f


def _approved_icp(extra=None):
    spec = ICP(company_filters=[
        FilterCond(field=IND, type="in", value=["Software Development"]),
        FilterCond(field="headcount.total", type="=>", value=20),
        FilterCond(field="headcount.total", type="=<", value=200),
        FilterCond(field=STAGE, type="in", value=["series_a"]),
        FilterCond(field="locations.country", type="in", value=["USA"]),
        *(extra or []),
    ], fit_weights=FitWeights())
    return icp_svc.create_version(spec, description="test", traits=None, source="seed",
                                  status=IcpStatus.approved)


def test_in_range_needs_no_tuning(fake, fake_llm):
    fake.search_count = lambda flt: 100
    row = _approved_icp()
    res = lookalike.run_lookalike(row.id)
    assert res.converged and len(res.iterations) == 1 and fake_llm.calls == []
    assert res.upserted == 60


def test_too_few_loosens_and_converges(fake, fake_llm):
    # count is 10 while the funding-stage filter exists, 80 once it is removed
    fake.search_count = lambda flt: 10 if _has(flt, STAGE) else 80
    fake_llm.responses["tune_filters"] = {"direction": "loosen", "action": "remove", "index": 4,
                                          "reason": "funding stage is the most restrictive"}
    row = _approved_icp()
    # filter order after validation: industry, hc>=, hc<=, stage... find stage index dynamically
    idx = [c.field for c in icp_svc.spec_of(row).company_filters].index(STAGE)
    fake_llm.responses["tune_filters"]["index"] = idx
    res = lookalike.run_lookalike(row.id)
    assert res.converged and res.count == 80 and len(res.iterations) == 2
    tuned = icp_svc.get_icp(row.id).filters_json["tuned"]
    assert not _has(tuned["company"], STAGE) and tuned["converged"] is True


def test_too_many_tightens_and_converges(fake, fake_llm):
    fake.search_count = lambda flt: 120 if _has(flt, "headcount.growth_percent.6m") else 5000
    fake_llm.responses["tune_filters"] = {
        "direction": "tighten", "action": "add",
        "condition": {"field": "headcount.growth_percent.6m", "type": ">=", "value": 10},
        "reason": "focus on growing companies"}
    res = lookalike.run_lookalike(_approved_icp().id)
    assert res.converged and res.count == 120
    assert fake_llm.calls[0]["variables"]["direction"] == "tighten"


def test_tightening_cannot_change_approved_filters(fake, fake_llm):
    fake.search_count = lambda flt: 150 if _has(flt, "headcount.growth_percent.6m") else 3000
    fake_llm.responses["tune_filters"] = [
        # 1st try: raise the approved headcount floor -> rejected by the guard
        {"direction": "tighten", "action": "replace", "index": 1,
         "condition": {"field": "headcount.total", "type": "=>", "value": 50}, "reason": "bigger"},
        # retry: add growth instead -> accepted
        {"direction": "tighten", "action": "add",
         "condition": {"field": "headcount.growth_percent.6m", "type": "=>", "value": 10}, "reason": "growth"},
    ]
    row = _approved_icp()
    res = lookalike.run_lookalike(row.id)
    assert res.converged and res.count == 150
    assert "locked" in fake_llm.calls[1]["messages"][-1]["content"]
    tuned = icp_svc.get_icp(row.id).filters_json["tuned"]["spec"]["company_filters"]
    assert {"field": "headcount.total", "type": "=>", "value": 20} in tuned


def test_tightening_cannot_add_filter_on_locked_field():
    spec = ICP(company_filters=[FilterCond(field="headcount.total", type="=<", value=200)])
    guard = lookalike._patch_guard(spec, locked=1, direction="tighten")
    from signalloop.schemas import FilterPatch
    with pytest.raises(ValueError, match="already set"):
        guard(FilterPatch(direction="tighten", action="add", reason="x",
                          condition=FilterCond(field="headcount.total", type="=<", value=100)))
    # loosening may widen an approved filter
    lookalike._patch_guard(spec, 1, "loosen")(FilterPatch(
        direction="loosen", action="replace", index=0, reason="x",
        condition=FilterCond(field="headcount.total", type="=<", value=500)))


def test_stops_after_max_iters(fake, fake_llm):
    fake.search_count = lambda flt: 5000
    fake_llm.responses["tune_filters"] = {
        "direction": "tighten", "action": "add",
        "condition": {"field": "basic_info.year_founded", "type": ">", "value": 2015}, "reason": "x"}
    res = lookalike.run_lookalike(_approved_icp().id, max_iters=4)
    assert not res.converged and len(res.iterations) == 4 and len(fake_llm.calls) == 3
    assert res.fetched == 60  # still fetches up to the fetch limit


def test_seeds_and_suppressed_never_in_results(fake, fake_llm):
    fake.search_count = lambda flt: 100
    seed = store.upsert_account(company("co0.com", 1000), is_seed=True)
    suppression.suppress_seed(seed)
    suppression.add("domain", "co1.com", "competitor")
    lookalike.run_lookalike(_approved_icp().id)
    domains = {a.domain for a in lookalike.list_accounts()}
    assert "co0.com" not in domains and "co1.com" not in domains
    assert len(domains) == 58


def test_requires_approved_icp(fake, fake_llm):
    draft = icp_svc.create_version(ICP(company_filters=[FilterCond(field=IND, type="in",
                                                                   value=["Software Development"])]),
                                   description="d", traits=None, source="manual")
    with pytest.raises(ValueError):
        lookalike.run_lookalike(draft.id)


def test_fit_score_partial_credit():
    spec = ICP(company_filters=[
        FilterCond(field=STAGE, type="in", value=["series_a"]),
        FilterCond(field="headcount.total", type="=>", value=20),
        FilterCond(field="headcount.total", type="=<", value=50)])
    exact, _ = lookalike.fit_score({"funding_stage": "series_a", "headcount": 30}, spec)
    near, dims = lookalike.fit_score({"funding_stage": "series_b", "headcount": 100}, spec)
    far, _ = lookalike.fit_score({"funding_stage": "series_d", "headcount": 5000}, spec)
    assert exact == 1.0 and 0 < far < near < exact
    assert dims == {"headcount": 0.25, "funding_stage": 0.5}
    assert "near miss" in lookalike.template_reason({"funding_stage": "series_b"}, dims)
