from datetime import date

import pytest
from sqlmodel import select

from signalloop import config
from signalloop.db import session_scope
from signalloop.llm import LLMValidationError
from signalloop.models import Account, ApprovalKind, Channel, ContactRole, Signal, SignalType, Touch
from signalloop.schemas import CriticResult
from signalloop.services import approvals, outreach, store, suppression
from tests.fakes import company, person
from tests.test_briefs import GOOD as BRIEF

EMAIL = {"subject": "Your Series B and pipeline reviews",
         "body": "Hi Sam, congrats on the $41M Series B. Quillpath flags stalled deals every morning "
                 "so new reps start with a clean pipeline. Open to a 15-minute call next week?"}
NOTE = {"note": "Congrats on the Series B, Sam. Quillpath helps new sales teams keep pipeline reviews tight. Open to connecting?"}


def passing(text, doc):
    return CriticResult(passed=True)


@pytest.fixture
def world(fake_llm, monkeypatch):
    monkeypatch.setenv("DEMO_AS_OF", "2026-11-22")
    config.reset_settings()
    a = store.upsert_account(company("pulley.io", 1, stage="series_b", amount=41e6,
                                     last_round_date="2026-11-18", headcount=122))
    with session_scope() as s:
        s.get(Account, a.id).fit_score = 1.0
        s.add(Signal(account_id=a.id, type=SignalType.funding, strength=0.9, dedupe_key="k",
                     payload_json={"amount_usd": 41e6, "event_date": "2026-11-18", "days_ago": 4}))
    champ = store.upsert_contact(person(1, "Sam Vp", "VP Sales", "Vice President", "pulley.io"), a.id,
                                 role=ContactRole.champion)
    ceo = store.upsert_contact(person(2, "Cee Ceo", "CEO", "CXO", "pulley.io"), a.id,
                               role=ContactRole.economic_buyer)
    fake_llm.responses.update({"why_now": BRIEF, "write_email": EMAIL, "write_linkedin": NOTE})
    return a, champ, ceo


def test_drafts_respect_limits_and_create_pending_approvals(world, fake_llm):
    a, champ, ceo = world
    out = outreach.draft_this_week(review=passing)
    assert len(out) == 4                                   # 2 contacts x (email + linkedin)
    assert [o.touch.contact_id for o in out][:2] == [champ.id, champ.id]   # champion first
    for o in out:
        if o.touch.channel == Channel.email:
            assert len(o.touch.subject.split()) <= 8 and len(o.touch.body.split()) <= 120
        else:
            assert len(o.touch.body) <= 300
    pending = approvals.list_pending(ApprovalKind.outreach)
    assert len(pending) == 4 and all(p.payload_json["critic_passed"] for p in pending)
    with session_scope() as s:
        assert all(t.approval_id for t in s.exec(select(Touch)).all())


def test_suppressed_contacts_are_skipped(world):
    a, champ, ceo = world
    suppression.add("person_id", "2", "dnc")
    out = outreach.draft_this_week(review=passing)
    assert {o.touch.contact_id for o in out} == {champ.id}


def test_rerun_does_not_duplicate_open_drafts(world):
    outreach.draft_this_week(review=passing)
    assert outreach.draft_this_week(review=passing) == []


def test_too_long_email_is_rejected(world, fake_llm):
    long_body = {"subject": "one two three four five six seven eight nine", "body": "word " * 130}
    fake_llm.responses["write_email"] = [long_body, long_body]
    a, champ, ceo = world
    with pytest.raises(LLMValidationError):
        outreach.draft_this_week(review=passing, channels=(Channel.email,))


def test_failed_review_rewrites_twice_then_needs_human(world, fake_llm):
    calls = []

    def failing(text, doc):
        calls.append(text)
        return CriticResult(passed=False, violations=["claim '$50M' contradicts evidence (41000000)"])

    a, champ, ceo = world
    suppression.add("person_id", "2", "dnc")
    out = outreach.draft_this_week(review=failing, channels=(Channel.email,))
    assert len(out) == 1 and out[0].critic.needs_human and out[0].critic.rewrites == 2
    assert len(calls) == 3
    rewrite_prompt = [c for c in fake_llm.calls if c["prompt_name"] == "write_email"][-1]
    assert "$50M" in rewrite_prompt["variables"]["violations"]
    assert approvals.list_pending()[0].payload_json["needs_human"] is True
