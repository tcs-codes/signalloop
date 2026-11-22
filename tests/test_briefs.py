import pytest

from signalloop.db import session_scope
from signalloop.llm import LLMValidationError
from signalloop.models import Signal, SignalType
from signalloop.services import briefs, evidence, store
from tests.fakes import company


@pytest.fixture
def acc():
    a = store.upsert_account(company("pulley.io", 1, stage="series_b", amount=41_000_000.0,
                                     last_round_date="2026-11-18", headcount=122))
    with session_scope() as s:
        s.add(Signal(account_id=a.id, type=SignalType.funding, strength=0.9, dedupe_key="k",
                     payload_json={"round_type": "series_b", "amount_usd": 41_000_000.0,
                                   "event_date": "2026-11-18", "days_ago": 4}))
    return a


GOOD = {
    "headline": "Fresh $41M Series B and a sales team to scale",
    "bullets": [
        {"text": "Raised a $41M Series B four days ago.", "refs": [0, 1]},
        {"text": "122 employees: more deals than one founder can review.", "refs": [2]},
        {"text": "Quillpath sets up in about 15 minutes.", "refs": [3]},
    ],
    "recommended_angle": "funding",
    "evidence_refs": [
        {"field": "company.funding.last_round_amount_usd", "value": "41000000"},
        {"field": "signals[0].payload.days_ago", "value": "4"},
        {"field": "company.headcount.total", "value": "122"},
        {"field": "product", "value": "Setup takes about 15 minutes"},
    ],
}


def test_every_evidence_ref_resolves(acc, fake_llm):
    fake_llm.responses["why_now"] = GOOD
    row = briefs.build_brief(acc.id)
    b = briefs.brief_model(row)
    doc = evidence.build(acc, [])
    for ref in b.evidence_refs:
        if ref.field != "product":
            assert evidence.exists(evidence.build(acc, briefs.watch.signals_for(acc.id)), ref.field)
    assert row.signal_id is not None and b.recommended_angle == "funding"
    assert doc["company"]["funding"]["last_round_amount_usd"] == 41_000_000.0


def test_bad_path_or_wrong_value_triggers_retry(acc, fake_llm):
    bad = {**GOOD, "evidence_refs": [
        {"field": "company.funding.last_round_amount_usd", "value": "$50M"},   # wrong value
        {"field": "company.funding.valuation_usd", "value": "1B"},             # no such path
        *GOOD["evidence_refs"][2:]]}
    fake_llm.responses["why_now"] = [bad, GOOD]
    briefs.build_brief(acc.id)
    msg = fake_llm.calls[1]["messages"][-1]["content"]
    assert "not '$50M'" in msg and "does not exist" in msg


def test_bullet_without_refs_fails(acc, fake_llm):
    bad = {**GOOD, "bullets": [{"text": "x", "refs": []}] * 3}
    fake_llm.responses["why_now"] = [bad, bad]
    with pytest.raises(LLMValidationError):
        briefs.build_brief(acc.id)


def test_product_quotes_ignore_markdown():
    text = "- **Sales hiring:** onboarding AEs into a pipeline process that does not exist yet."
    assert evidence.norm_text("Sales hiring: onboarding AEs into a pipeline process") in evidence.norm_text(text)


def test_numbers_in_handles_lift_multipliers():
    assert evidence.numbers_in("replied at 3.6x baseline (n=39); Directors 0.2× (n=74)") == [3.6, 39, 0.2, 74]
    assert evidence.numbers_in("$41M Series B, 15% growth, 2,400 staff") == [41e6, 15, 2400]


def test_resolve_and_value_matching():
    doc = {"company": {"funding": {"last_round_amount_usd": 41e6}}, "signals": [{"payload": {"days_ago": 4}}]}
    assert evidence.resolve(doc, "signals[0].payload.days_ago") == 4
    assert evidence.resolve(doc, "signals.0.payload.days_ago") == 4
    assert not evidence.exists(doc, "signals[3].payload")
    assert evidence.value_matches(41e6, "$41M") and evidence.value_matches(41e6, "41,000,000")
    assert not evidence.value_matches(41e6, "$50M")
    assert evidence.number_supported(15.0, [15.38]) and not evidence.number_supported(50e6, [8e6])
