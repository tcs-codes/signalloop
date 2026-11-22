import pytest

from signalloop.llm import LLMValidationError
from signalloop.services import store, traits
from tests.fakes import company


@pytest.fixture
def seeds():
    specs = [("a.io", "series_a", 30, "USA"), ("b.io", "series_a", 45, "USA"),
             ("c.io", "seed", 25, "IND"), ("d.io", "series_a", 120, "USA"),
             ("e.io", "series_b", 40, "USA")]
    return [store.upsert_account(company(d, i, stage=st, headcount=h, country=c), is_seed=True)
            for i, (d, st, h, c) in enumerate(specs)]


def _good(stats):
    out = []
    for dim, info in stats["dimensions"].items():
        cand = info["candidate"]
        if cand["coverage"] >= 0.6:
            out.append({"dimension": dim, "values": cand["values"], "coverage": cand["coverage"],
                        "explanation": f"{cand['count']} of {stats['n']} seeds share this"})
    return {"traits": out, "persona_hypothesis": {"titles": ["VP Sales"], "seniority": ["Vice President"]}}


def test_stats_match_pandas(seeds):
    st = traits.compute_stats(seeds)
    d = st["dimensions"]
    assert st["n"] == 5
    assert d["funding_stage"]["candidate"] == {"values": ["series_a"], "count": 3, "coverage": 0.6}
    assert d["hq_country"]["candidate"]["coverage"] == 0.8
    assert d["headcount_band"]["candidate"] == {"values": ["20-50"], "count": 4, "coverage": 0.8}
    assert d["industry"]["candidate"]["coverage"] == 1.0


def test_output_validates_and_coverage_matches(seeds, fake_llm):
    fake_llm.responses["extract_traits"] = lambda v: _good(traits.compute_stats(seeds))
    t, stats = traits.extract_traits(seeds)
    assert {tr.dimension for tr in t.traits} >= {"funding_stage", "hq_country", "headcount_band", "industry"}
    for tr in t.traits:
        assert tr.coverage == traits.coverage_for(traits.seed_frame(seeds), tr.dimension, tr.values)[1]
    assert t.persona_hypothesis.seniority == ["Vice President"]
    # the prompt got the exact stats
    assert fake_llm.calls[0]["variables"]["n"] == 5


def test_wrong_coverage_is_rejected_then_retried(seeds, fake_llm):
    st = traits.compute_stats(seeds)
    bad = _good(st)
    bad["traits"][0]["coverage"] = 0.99
    fake_llm.responses["extract_traits"] = [bad, _good(st)]
    t, _ = traits.extract_traits(seeds)
    assert len(fake_llm.calls) == 2 and "coverage" in fake_llm.calls[1]["messages"][-1]["content"]


def test_low_coverage_or_unquoted_explanation_fails(seeds, fake_llm):
    bad = {"traits": [{"dimension": "funding_stage", "values": ["seed"], "coverage": 0.2,
                       "explanation": "some raised seed"}]}
    fake_llm.responses["extract_traits"] = [bad, bad]
    with pytest.raises(LLMValidationError):
        traits.extract_traits(seeds)


def test_no_seeds_raises():
    with pytest.raises(ValueError):
        traits.extract_traits([])
