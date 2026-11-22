from sqlmodel import select

from signalloop.db import session_scope
from signalloop.models import Account, Contact, ContactRole
from signalloop.services import store


def _accounts():
    with session_scope() as s:
        return s.exec(select(Account)).all()


def _contacts():
    with session_scope() as s:
        return s.exec(select(Contact)).all()


def test_same_company_two_domain_spellings_gives_one_row():
    store.upsert_account({"basic_info": {"name": "Acme", "primary_domain": "https://www.Acme.com/"}})
    store.upsert_account({"basic_info": {"name": "Acme Inc", "primary_domain": "acme.com"}})
    rows = _accounts()
    assert len(rows) == 1 and rows[0].domain == "acme.com" and rows[0].name == "Acme Inc"


def test_dedupe_by_crustdata_id_first():
    store.upsert_account({"crustdata_company_id": 7, "basic_info": {"name": "A", "primary_domain": "a.com"}})
    store.upsert_account({"crustdata_company_id": 7, "basic_info": {"name": "A", "primary_domain": "a-new.com"}})
    rows = _accounts()
    assert len(rows) == 1 and rows[0].crustdata_company_id == "7"


def test_merge_keeps_enriched_sections_when_thinner_payload_arrives():
    store.upsert_account({"crustdata_company_id": 1, "basic_info": {"primary_domain": "x.com"},
                          "funding": {"last_round_type": "series_a", "last_round_amount_usd": 8e6}})
    acc = store.upsert_account({"crustdata_company_id": 1, "basic_info": {"primary_domain": "x.com"}})
    assert acc.firmographics_json["funding"]["last_round_amount_usd"] == 8e6
    assert acc.firmographics_json["normalized"]["funding_stage"] == "series_a"


def test_normalize_company_from_enrich_shape():
    raw = {"crustdata_company_id": 633593,
           "basic_info": {"name": "Retool", "primary_domain": "retool.com"},
           "headcount": {"total": 42, "growth_percent": {"six_months": 18.5, "yoy": 30}},
           "funding": {"last_round_type": "series_a", "last_fundraise_date": "2026-10-01T00:00:00",
                       "last_round_amount_usd": 8000000.0},
           "locations": {"country": "USA"},
           "taxonomy": {"professional_network_industry": "Software Development"}}
    n = store.normalize_company(raw)
    assert n["headcount_band"] == "20-50"
    assert n["headcount_growth_6m_pct"] == 18.5 and n["headcount_growth_pct"]["12m"] == 30
    assert n["funding_stage"] == "series_a" and n["last_funding_date"] == "2026-10-01"
    assert n["industry"] == "Software Development" and n["hq_country"] == "USA"


def test_normalize_company_from_search_shape():
    raw = {"crustdata_company_id": 5, "basic_info": {"primary_domain": "b.com"},
           "headcount": {"total": 300, "growth_percent": {"6m": -2.0}},
           "taxonomy": {"professional_network_industries": ["Fintech", "Software"]}}
    n = store.normalize_company(raw)
    assert n["headcount_band"] == "201-500" and n["headcount_growth_band"] == "shrinking"
    assert n["industry"] == "Fintech"


def test_contact_dedupe_by_id_then_email_then_profile_url():
    acc = store.upsert_account({"basic_info": {"primary_domain": "acme.com"}})
    p = {"crustdata_person_id": 11, "basic_profile": {"name": "Jo"},
         "social_handles": {"professional_network_identifier": {"profile_url": "https://www.linkedin.com/in/jo/"}},
         "experience": {"employment_details": {"current": [
             {"title": "VP Sales", "seniority_level": "Vice President", "function_category": "Sales"}]}}}
    store.upsert_contact(p, acc.id, role=ContactRole.champion)
    store.upsert_contact({**p, "crustdata_person_id": 11}, acc.id)                       # same id
    store.upsert_contact({"basic_profile": {"name": "Jo"}, "social_handles":            # same URL
                          {"professional_network_identifier": {"profile_url": "linkedin.com/in/jo"}}}, acc.id)
    c = _contacts()
    assert len(c) == 1
    assert c[0].title == "VP Sales" and c[0].seniority == "Vice President" and c[0].role == ContactRole.champion

    store.upsert_contact({"basic_profile": {"name": "Sam"}}, acc.id, email="Sam@Acme.com")
    store.upsert_contact({"crustdata_person_id": 22, "basic_profile": {"name": "Sam"}}, acc.id,
                         email="sam@acme.com")                                              # same email
    assert len(_contacts()) == 2


def test_headcount_band_edges():
    assert [store.headcount_band(n) for n in (5, 19, 20, 50, 51, 2000)] == \
        ["1-19", "1-19", "20-50", "20-50", "51-200", "1001+"]
    assert store.headcount_band(None) is None
