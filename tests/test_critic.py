import pytest

from signalloop.models import Channel
from signalloop.services import approvals, critic, outreach, suppression
from tests.test_outreach import world  # noqa: F401  (fixture)

DOC = {"company": {"funding": {"last_round_amount_usd": 8_000_000.0, "last_round_type": "series_a"},
                   "headcount": {"total": 42}},
       "signals": [{"type": "funding", "payload": {"days_ago": 4}}],
       "product": "Setup takes about 15 minutes; free 14-day pilot."}


def extractor(v):
    """Fake Pass A: one claim per sentence; questions are 'other'."""
    sents = [s.strip() for s in v["draft"].replace("?", ".").split(".") if s.strip()]
    return {"claims": [{"claim": s, "type": "other" if s.lower().startswith(("open to", "hi ")) else
                        ("product_claim" if "quillpath" in s.lower() else "prospect_fact")} for s in sents]}


def lenient_checker(v):
    """Fake Pass B that (wrongly) marks everything supported: the code check must still catch numbers."""
    claims = [line.split(". ", 1)[1] for line in v["claims"].splitlines()]
    return {"verdicts": [{"claim": c, "verdict": "supported", "reason": "ok"} for c in claims]}


def test_planted_wrong_amount_is_contradicted(fake_llm):
    fake_llm.responses.update({"critic_extract": extractor, "critic_check": lenient_checker})
    r = critic.review("Congrats on raising $50M in your Series A. Open to a call?", DOC)
    assert not r.passed
    bad = [c for c in r.claims if c["verdict"] == "contradicted"]
    assert bad and bad[0]["source"] == "code" and bad[0]["evidence"] == "company.funding.last_round_amount_usd"
    assert "50,000,000 does not match the evidence (8,000,000)" in r.violations[0]


def test_correct_draft_passes(fake_llm):
    fake_llm.responses.update({"critic_extract": extractor, "critic_check": lenient_checker})
    r = critic.review("Congrats on the $8M Series A. Quillpath sets up in about 15 minutes. Open to a call?", DOC)
    assert r.passed and r.violations == []


def test_llm_verdict_used_when_stricter(fake_llm):
    fake_llm.responses.update({
        "critic_extract": {"claims": [{"claim": "You are hiring 10 AEs", "type": "prospect_fact"}]},
        "critic_check": {"verdicts": [{"claim": "You are hiring 10 AEs", "verdict": "unsupported",
                                       "reason": "no hiring data"}]}})
    r = critic.review("You are hiring 10 AEs.", DOC)
    assert not r.passed and r.claims[0]["source"] in ("llm", "code")


def test_no_factual_claims_skips_pass_b(fake_llm):
    fake_llm.responses["critic_extract"] = {"claims": [{"claim": "Open to a chat?", "type": "other"}]}
    assert critic.review("Open to a chat?", DOC).passed
    assert [c["prompt_name"] for c in fake_llm.calls] == ["critic_extract"]


def test_wrong_verdict_count_retries(fake_llm):
    fake_llm.responses.update({
        "critic_extract": {"claims": [{"claim": "a 42-person team", "type": "prospect_fact"},
                                      {"claim": "an $8M round", "type": "prospect_fact"}]},
        "critic_check": [{"verdicts": [{"claim": "a 42-person team", "verdict": "supported"}]},
                         {"verdicts": [{"claim": "a 42-person team", "verdict": "supported"},
                                       {"claim": "an $8M round", "verdict": "supported"}]}]})
    assert critic.review("x", DOC).passed


# ---------------------------------------------------------------- with the F16 loop
PLANTED = {"subject": "Congrats on the Series B",
           "body": "Hi Sam, congrats on raising $50M. Quillpath sets up in about 15 minutes. Open to a call?"}
FIXED = {"subject": "Congrats on the Series B",
         "body": "Hi Sam, congrats on the $41M Series B. Quillpath sets up in about 15 minutes. Open to a call?"}


@pytest.fixture
def critic_llm(fake_llm):
    fake_llm.responses.update({"critic_extract": extractor, "critic_check": lenient_checker})
    return fake_llm


def test_planted_claim_is_caught_and_rewritten(world, critic_llm):  # noqa: F811
    a, champ, ceo = world
    suppression.add("person_id", "2", "dnc")
    critic_llm.responses["write_email"] = [PLANTED, FIXED]
    out = outreach.draft_this_week(channels=(Channel.email,))
    res = out[0].critic
    assert res.passed and res.rewrites == 1 and not res.needs_human
    assert "$41M" in out[0].touch.body
    rewrite_vars = [c for c in critic_llm.calls if c["prompt_name"] == "write_email"][1]["variables"]
    assert "contradicted" in rewrite_vars["violations"]
    assert out[0].touch.critic_json["passed"] is True


def test_two_failed_rewrites_queue_for_human(world, critic_llm):  # noqa: F811
    a, champ, ceo = world
    suppression.add("person_id", "2", "dnc")
    critic_llm.responses["write_email"] = PLANTED
    out = outreach.draft_this_week(channels=(Channel.email,))
    res = out[0].critic
    assert res.needs_human and res.rewrites == 2 and res.violations
    p = approvals.list_pending()[0].payload_json
    assert p["needs_human"] is True and p["violations"]
    assert critic.summarize(res).startswith("needs human")
