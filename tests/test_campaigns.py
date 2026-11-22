import importlib.util
from pathlib import Path

import pytest
from sqlmodel import select

from signalloop.db import session_scope
from signalloop.models import Outcome, OutcomeEvent, RunLog
from signalloop.services import campaigns, store
from signalloop.tools import crustdata
from tests.fakes import company, person

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("gen_campaign", ROOT / "scripts" / "gen_campaign.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

SEN = [("VP Sales", "Vice President"), ("Director of Sales", "Director"), ("CEO", "CXO"),
       ("Sales Manager", "Manager")]


@pytest.fixture
def world():
    """60 accounts (20 in the planted segment), 2 contacts each = 120 contacts."""
    pid = 1000
    for i in range(60):
        seg = i < 20
        d = f"co{i}.io"
        a = store.upsert_account(company(d, i + 1, stage="series_a" if seg else "series_b",
                                         headcount=35 if seg else 120))
        for j in range(2):
            title, sen = SEN[(i + j) % 4]
            pid += 1
            store.upsert_contact(person(pid, f"P {pid}", title, sen, d), a.id)
    return 120


def test_generated_csv_imports_with_high_link_rate_and_idempotent(world, tmp_path):
    out = tmp_path / "c.csv"
    assert gen.generate(out, seed=1) == world
    res = campaigns.import_campaign(out, "demo campaign")
    assert res.rows == world and res.contact_link_rate >= 0.95
    n1 = res.outcomes_added
    again = campaigns.import_campaign(out, "demo campaign")
    assert again.outcomes_added == 0
    with session_scope() as s:
        sent = s.exec(select(Outcome).where(Outcome.event == OutcomeEvent.sent)).all()
        assert len(sent) == world and n1 >= world


def test_columns_case_insensitive_and_unknown_ignored(world):
    csv_text = "EMAIL,Domain,Sent_At,Replied,Meeting,Opened,Unsubscribed,Extra\n" \
               ",co1.io,2026-10-02,yes,no,1,0,zzz\n,unknown.example,2026-10-03,0,0,0,0,\n"
    res = campaigns.import_campaign(csv_text, "csv test")
    assert res.rows == 2 and res.linked_accounts_only == 1 and res.unlinked == 1
    with session_scope() as s:
        evs = {o.event for o in s.exec(select(Outcome).where(Outcome.domain == "co1.io")).all()}
    assert evs == {OutcomeEvent.sent, OutcomeEvent.reply, OutcomeEvent.open}


def test_planted_probabilities():
    assert gen.reply_probability("series_a", "20-50", "Manager") == 0.25
    assert gen.reply_probability("series_a", "20-50", "Vice President") == pytest.approx(0.375)
    assert gen.reply_probability("series_b", "51-200", "CXO") == pytest.approx(0.09)
    assert gen.reply_probability("series_a", "20-50", "Director") == 0.03


def test_frame_has_no_nulls_in_core_dims(world, tmp_path):
    out = tmp_path / "c.csv"
    gen.generate(out, seed=2)
    c = campaigns.import_campaign(out, "x").campaign
    df = campaigns.enrich_campaign(c.id, out_dir=tmp_path)
    assert len(df) == world
    assert not df[campaigns.CORE_DIMS].isnull().any().any()
    assert set(df["seniority"]) == {"VP+", "Director", "Manager & below"}
    assert (tmp_path / f"campaign_{c.id}.parquet").exists()


def test_missing_firmographics_are_enriched_and_metered(world, tmp_path):
    crustdata.set_client(crustdata.CrustdataClient(mode="mock"))
    c = campaigns.import_campaign("domain,sent_at,replied\nretool.com,2026-10-01,1\n", "unknown co").campaign
    df = campaigns.enrich_campaign(c.id, out_dir=tmp_path)
    assert df.iloc[0]["industry"] == "Software Development"     # from the company_enrich fixture
    with session_scope() as s:
        assert s.exec(select(RunLog).where(RunLog.endpoint_or_model == "/company/enrich")).first()
