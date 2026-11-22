from datetime import datetime, timedelta, timezone

from signalloop.db import session_scope
from signalloop.models import Outcome, OutcomeEvent, SuppressionKind, Touch, TouchStatus
from signalloop.services import store, suppression


def _acc(domain, **kw):
    return store.upsert_account({"basic_info": {"primary_domain": domain}}, **kw)


def _contact(acc, email=None, pid=None):
    return store.upsert_contact({"crustdata_person_id": pid, "basic_profile": {"name": "X"}},
                                acc.id, email=email)


def test_seed_accounts_are_suppressed_and_listed():
    seed = _acc("customer.com", is_seed=True)
    suppression.suppress_seed(seed)
    assert suppression.is_suppressed(account=seed)
    other = _acc("prospect.com")
    assert not suppression.is_suppressed(account=other)
    # a contact at a seed account is suppressed too
    assert suppression.is_suppressed(contact=_contact(seed, "a@customer.com"))


def test_manual_domain_email_and_person_id():
    a = _acc("ok.com")
    suppression.add("domain", "https://www.Blocked.com/", "dnc")
    assert suppression.is_suppressed(account=_acc("blocked.com"))
    c1 = _contact(a, email="Jane@OK.com")
    suppression.add(SuppressionKind.email, "jane@ok.com")
    assert suppression.is_suppressed(contact=c1)
    c2 = _contact(a, pid=99)
    assert not suppression.is_suppressed(contact=c2)
    suppression.add("person_id", "99")
    assert suppression.is_suppressed(contact=c2)


def test_add_is_idempotent():
    suppression.add("email", "x@y.com", "first")
    suppression.add("email", "X@Y.com", "second")
    with session_scope() as s:
        from sqlmodel import select
        from signalloop.models import Suppression
        rows = s.exec(select(Suppression)).all()
    assert len(rows) == 1 and rows[0].reason == "first"


def test_unsubscribed_contact_is_suppressed():
    a = _acc("acme.com")
    c = _contact(a, email="bob@acme.com")
    assert not suppression.is_suppressed(contact=c)
    with session_scope() as s:
        s.add(Outcome(contact_email="bob@acme.com", event=OutcomeEvent.unsubscribe))
    assert suppression.is_suppressed(contact=c)


def test_recently_contacted_suppressed_but_old_contact_is_not():
    a = _acc("acme.com")
    recent, old = _contact(a, email="r@acme.com"), _contact(a, email="o@acme.com")
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        s.add(Touch(contact_id=recent.id, account_id=a.id, status=TouchStatus.approved,
                    created_at=now - timedelta(days=3)))
        s.add(Touch(contact_id=old.id, account_id=a.id, status=TouchStatus.approved,
                    created_at=now - timedelta(days=45)))
        # a pending draft is not "contacted"
        s.add(Touch(contact_id=old.id, account_id=a.id, status=TouchStatus.draft, created_at=now))
    assert suppression.is_suppressed(contact=recent)
    assert not suppression.is_suppressed(contact=old)


def test_sent_outcome_within_30_days():
    a = _acc("acme.com")
    c = _contact(a, email="s@acme.com")
    with session_scope() as s:
        s.add(Outcome(contact_id=c.id, event=OutcomeEvent.sent,
                      occurred_at=datetime.now(timezone.utc) - timedelta(days=10)))
    assert suppression.is_suppressed(contact=c)


def test_load_csv_and_filters(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("kind,value,reason\ndomain,bad.com,competitor\nemail,a@good.com,dnc\n")
    assert suppression.load_csv(p) == 2
    assert suppression.load_csv(tmp_path / "missing.csv") == 0
    good, bad = _acc("good.com"), _acc("bad.com")
    assert suppression.filter_accounts([good, bad]) == [good]
    c1, c2 = _contact(good, "a@good.com"), _contact(good, "b@good.com")
    assert [c.id for c in suppression.filter_contacts([c1, c2])] == [c2.id]


def test_repo_suppression_fixture_loads():
    assert suppression.load_csv() == 2
