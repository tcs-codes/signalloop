import random

import pandas as pd
import pytest

from signalloop.llm import LLMValidationError
from signalloop.services import analysis, campaigns


def synthetic_frame(n_accounts=240, seed=7) -> pd.DataFrame:
    """Planted pattern, like scripts/gen_campaign.py, built directly as an analysis frame."""
    rng = random.Random(seed)
    rows = []
    for i in range(n_accounts):
        seg = i % 4 == 0
        stage = "series_a" if (seg or i % 3 == 0) else rng.choice(["seed", "series_b"])
        band = "20-50" if (seg or i % 5 == 0) else "51-200"
        if seg:
            stage, band = "series_a", "20-50"
        elif stage == "series_a" and band == "20-50":
            band = "51-200"
        for sen in ("VP+", "Director"):
            p = 0.03 if sen == "Director" else (0.25 if seg else 0.06) * 1.5
            rows.append({"contact_id": len(rows), "account_id": i, "responded": rng.random() < p,
                         "meeting": False, "industry": "Software Development", "headcount_band": band,
                         "funding_stage": stage, "hq_country": "USA", "seniority": sen,
                         "function": "Sales", "signal_type_at_send": rng.choice(["funding", "job_post"])})
    return pd.DataFrame(rows)


@pytest.fixture
def campaign(monkeypatch):
    from signalloop.db import session_scope
    from signalloop.models import Campaign
    with session_scope() as s:
        c = Campaign(name="t")
        s.add(c)
        s.flush()
    df = synthetic_frame()
    monkeypatch.setattr(analysis, "load_frame", lambda cid: df)
    return c, df


def good_narrative(v):
    first = v["positives"].splitlines()[0]
    lift = first.split("lift=")[1].split("x")[0]
    n = first.split("n=")[1].split(",")[0]
    return {"headline": f"Series A companies with 20-50 staff replied at {lift}x baseline (n={n}).",
            "positives": [first.lstrip("- ")], "negatives": ["Directors under-performed."],
            "narrative": f"Baseline was {v['baseline_pct']}%."}


def test_top_insight_is_planted_segment_and_directors_negative(campaign, fake_llm):
    c, df = campaign
    fake_llm.responses["segment_insights"] = good_narrative
    res = analysis.analyze(c.id)
    top = res.top(1)[0]
    assert set(top.dimension.split(" × ")) == {"funding_stage", "headcount_band", "seniority"}
    assert set(top.segment_value.split(" × ")) == {"series_a", "20-50", "VP+"}
    assert top.lift >= 3
    neg = [i for i in res.insights if i.dimension == "seniority" and i.segment_value == "Director"]
    assert neg and neg[0].lift < 0.6
    assert res.narrative and "x baseline" in res.narrative.headline


def test_small_segments_hidden_and_low_confidence_flagged(campaign, fake_llm):
    c, df = campaign
    fake_llm.responses["segment_insights"] = good_narrative
    res = analysis.analyze(c.id)
    assert all(i.n >= analysis.MIN_N_SHOW for i in res.insights)
    assert all(i.low_confidence == (i.n < analysis.LOW_CONFIDENCE_N) for i in res.insights)
    assert all("unknown" not in i.segment_value for i in res.insights)


def test_rerun_replaces_insights(campaign, fake_llm):
    c, df = campaign
    fake_llm.responses["segment_insights"] = good_narrative
    n1 = len(analysis.analyze(c.id).insights)
    analysis.analyze(c.id)
    assert len(analysis.insights_for(c.id)) == n1


def test_narrative_with_invented_number_fails(campaign, fake_llm):
    c, df = campaign
    bad = {"headline": "Series A replied at 9.9x baseline (n=777).", "positives": [], "negatives": []}
    fake_llm.responses["segment_insights"] = [bad, bad]
    with pytest.raises(LLMValidationError):
        analysis.analyze(c.id)


def test_tiny_segment_cannot_pick_the_combined_dimensions():
    df = synthetic_frame()
    # a 10-person country segment with 6 replies: huge rate, tiny n
    df.loc[:9, "hq_country"] = "IND"
    df.loc[:9, "responded"] = [True] * 6 + [False] * 4
    assert analysis.dimension_spread(df, "hq_country") == 0.0     # only one segment has n >= 30
    _, ranked = analysis.compute(df)
    assert "hq_country" not in ranked and set(ranked) >= {"seniority", "funding_stage", "headcount_band"}


def test_narrative_prefers_reliable_negatives(campaign, fake_llm):
    c, df = campaign
    fake_llm.responses["segment_insights"] = good_narrative
    res = analysis.analyze(c.id)
    neg = res.highlights(2, positive=False)
    reliable = [i for i in res.insights if i.lift < 1 and not i.low_confidence]
    if reliable:
        assert not neg[0].low_confidence


def test_lift_math():
    df = pd.DataFrame({"responded": [True, False, False, False] * 10,
                       "seniority": ["VP+", "VP+", "Director", "Director"] * 10})
    t = analysis.segment_table(df, ["seniority"]).set_index("segment_value")
    assert t.loc["VP+", "rate"] == 0.5 and t.loc["VP+", "lift"] == 2.0 and t.loc["Director", "lift"] == 0.0
    assert campaigns.seniority_band("Vice President") == "VP+"
