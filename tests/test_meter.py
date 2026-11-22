import pytest
from pydantic import BaseModel

from signalloop import llm
from signalloop.runctx import current_run_id
from signalloop.tools import crustdata as cd
from signalloop.tools import meter


def test_run_with_one_cached_call_records_two_billable():
    c = cd.CrustdataClient(mode="mock")
    with meter.run("find") as r:
        c.company_enrich(domains=["retool.com"])        # billable (fixture: 2 credits)
        c.company_identify("retool.com")                # billable (0.2)
        c.company_enrich(domains=["retool.com"])        # cached
    s = meter.summary(r.run_id)
    assert s["billable_calls"] == 2 and s["cached_calls"] == 1
    assert s["credits"] == pytest.approx(2.2)
    assert r.credits == pytest.approx(2.2)
    assert s["by_agent"]["find"]["billable_calls"] == 2


def test_budget_exceeded_raises_before_next_call():
    c = cd.CrustdataClient(mode="mock")
    with meter.run("find", budget=4.5) as r:
        c.company_enrich(domains=["a.com"])             # spent 2
        c.company_enrich(domains=["b.com"])             # spent 4
        with pytest.raises(meter.BudgetExceeded):
            c.company_enrich(domains=["c.com"])         # estimate 2 -> 6 > 4.5
    s = meter.summary(r.run_id)
    assert s["billable_calls"] == 2                      # third call never made / logged
    assert r.credits == pytest.approx(4.0)


def test_cached_calls_do_not_count_against_budget():
    c = cd.CrustdataClient(mode="mock")
    c.company_enrich(domains=["a.com"])                 # warm the cache outside any run
    with meter.run("replay", budget=0.5) as r:
        c.company_enrich(domains=["a.com"])
    assert r.credits == 0


def test_free_autocomplete_allowed_at_zero_budget():
    c = cd.CrustdataClient(mode="mock")
    with meter.run("icp", budget=0):
        assert c.autocomplete("company", "basic_info.industries", "")


def test_estimates_follow_list_prices():
    assert meter.estimate("company_enrich", {"domains": ["a", "b", "c"]}) == 6.0
    assert meter.estimate("company_identify", {"domains": ["a"]}) == 0.2
    assert meter.estimate("company_search", {"limit": 1}) == pytest.approx(1.03)
    assert meter.estimate("person_search", {"limit": 2}) == pytest.approx(0.46)
    assert meter.estimate("company_autocomplete", {}) == 0


class E(BaseModel):
    ok: bool


def test_llm_tokens_logged_to_run_and_agent(tmp_path, monkeypatch, fake_llm):
    (tmp_path / "p.md").write_text("hi")
    monkeypatch.setattr(llm, "PROMPTS_DIR", tmp_path)
    fake_llm.responses["p"] = {"ok": True}
    with meter.run("learn") as r:
        with meter.agent("analyst"):
            llm.call_llm("p", {}, E)
        assert current_run_id.get() == r.run_id
    assert current_run_id.get() is None
    s = meter.summary(r.run_id)
    assert s["llm_calls"] == 1 and s["input_tokens"] > 0
    assert "analyst" in s["by_agent"]
