import csv

import pytest

from signalloop.db import session_scope
from signalloop.models import ApprovalKind, ApprovalStatus, Touch, TouchStatus
from signalloop.services import approvals, store
from tests.fakes import company, person


@pytest.fixture
def queued():
    a = store.upsert_account(company("pulley.io", 1))
    c = store.upsert_contact(person(1, "Sam", "VP Sales", "Vice President", "pulley.io"), a.id)
    out = []
    for k in range(3):
        with session_scope() as s:
            t = Touch(contact_id=c.id, account_id=a.id, subject=f"S{k}", body=f"B{k}")
            s.add(t)
            s.flush()
        appr = approvals.enqueue(ApprovalKind.outreach, t.id, {
            "touch_id": t.id, "account": "Pulley", "domain": "pulley.io", "contact": "Sam",
            "title": "VP Sales", "role": "champion", "channel": "email", "subject": f"S{k}", "body": f"B{k}"})
        out.append((appr, t))
    return out


def _touch(tid):
    with session_scope() as s:
        return s.get(Touch, tid)


def _rows(path):
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_approve_edit_reject(queued, tmp_path):
    exp = tmp_path / "out.csv"
    (a0, t0), (a1, t1), (a2, t2) = queued
    approvals.decide_outreach(a0.id, "approve", note="good", export_to=exp)
    approvals.decide_outreach(a1.id, "edit", subject="S1 better", body="B1 better", export_to=exp)
    approvals.decide_outreach(a2.id, "reject", note="wrong person", export_to=exp)

    assert approvals.get(a0.id).status == ApprovalStatus.approved
    e = approvals.get(a1.id)
    assert e.status == ApprovalStatus.edited and e.payload_json["original"]["subject"] == "S1"
    assert approvals.get(a2.id).status == ApprovalStatus.rejected and approvals.get(a2.id).note == "wrong person"
    assert all(approvals.get(x.id).decided_at for x in (a0, a1, a2))

    assert _touch(t0.id).status == TouchStatus.exported
    assert _touch(t1.id).status == TouchStatus.exported and _touch(t1.id).subject == "S1 better"
    assert _touch(t2.id).status == TouchStatus.rejected

    rows = _rows(exp)
    assert [r["subject"] for r in rows] == ["S0", "S1 better"] and rows[1]["edited"] == "True"
    assert approvals.list_pending() == []
    assert approvals.counts() == {"outreach:approved": 1, "outreach:edited": 1, "outreach:rejected": 1}


def test_edit_without_changes_counts_as_approve(queued, tmp_path):
    (a0, t0), *_ = queued
    approvals.decide_outreach(a0.id, "edit", subject="S0", body="B0", export_to=tmp_path / "o.csv")
    assert approvals.get(a0.id).status == ApprovalStatus.approved


def test_cannot_decide_twice_or_with_bad_action(queued, tmp_path):
    (a0, _), (a1, _), _ = queued
    approvals.decide_outreach(a0.id, "approve", export_to=tmp_path / "o.csv")
    with pytest.raises(ValueError):
        approvals.decide_outreach(a0.id, "reject")
    with pytest.raises(ValueError):
        approvals.decide_outreach(a1.id, "send")
