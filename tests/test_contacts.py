import pytest

from signalloop.models import AccountStatus, ContactRole
from signalloop.services import contacts, lookalike, store, suppression
from signalloop.tools import crustdata
from tests.fakes import FakeCrustdata, company, person


@pytest.fixture
def setup_accounts():
    f = FakeCrustdata()
    accs = []
    for i in range(35):
        d = f"acct{i}.com"
        a = store.upsert_account(company(d, 500 + i))
        a.fit_score = 1 - i / 100
        accs.append(a)
        f.people[d] = [
            person(9000 + i * 10, f"Cee {i}", "Co-Founder & CEO", "CXO", d),
            person(9001 + i * 10, f"Vee {i}", "VP Sales", "Vice President", d),
            person(9002 + i * 10, f"Mia {i}", "Account Executive", "Senior", d),
        ]
    from signalloop.db import session_scope
    from signalloop.models import Account
    with session_scope() as s:
        for a in accs:
            s.get(Account, a.id).fit_score = a.fit_score
    crustdata.set_client(f)
    return f, accs


def test_top_30_get_one_or_two_contacts(setup_accounts):
    fake, accs = setup_accounts
    found = contacts.find_contacts()
    assert len(found) == 30 and fake.count("person_search") == 60   # champion + economic buyer
    assert all(len(cs) == 2 for cs in found.values())
    for cs in found.values():
        assert {(c.role, c.title) for c in cs} == {(ContactRole.champion, "VP Sales"),
                                                   (ContactRole.economic_buyer, "Co-Founder & CEO")}
    top = lookalike.list_accounts()[0]
    assert top.status == AccountStatus.watching
    # lower-ranked accounts beyond top 30 were not searched
    assert accs[34].id not in found


def test_suppressed_contacts_excluded(setup_accounts):
    fake, accs = setup_accounts
    suppression.add("person_id", str(9001), "asked not to be contacted")   # VP at acct0
    found = contacts.find_contacts(account_ids=[accs[0].id])
    names = [c.name for c in found[accs[0].id]]
    assert "Vee 0" not in names and len(names) == 2   # CEO + fill-in AE


def test_searches_are_scoped_to_domain(setup_accounts):
    fake, accs = setup_accounts
    contacts.find_contacts(account_ids=[accs[3].id], per_account=1)
    champ, econ = [c[1]["filters"] for c in fake.calls if c[0] == "person_search"]
    for flt in (champ, econ):
        assert flt["conditions"][0] == {"field": contacts.F_DOMAIN, "type": "=", "value": "acct3.com"}
        assert flt["conditions"][1]["op"] == "or"
    assert {c["value"] for c in champ["conditions"][1]["conditions"]} >= {"Sales", "Revenue"}
    assert econ["conditions"][1]["conditions"][0]["value"] == ["CXO", "Owner / Partner"]


def test_non_gtm_people_are_never_champions(setup_accounts):
    fake, accs = setup_accounts
    d = accs[0].domain
    fake.people[d] = [person(1, "Pat", "Director of Product & Growth Engineering", "Director", d),
                      person(2, "Dee", "Director of Engineering", "Director", d),
                      person(3, "Cee", "CTO", "CXO", d)]
    cs = contacts.find_contacts(account_ids=[accs[0].id])[accs[0].id]
    roles = {c.name: c.role for c in cs}
    assert roles["Pat"] == ContactRole.champion          # 'Growth' is a GTM keyword
    assert "Dee" not in roles and roles["Cee"] == ContactRole.economic_buyer


def test_ranking():
    assert contacts.econ_rank({"title": "Co-founder & CEO"}) > contacts.econ_rank({"title": "CRO"}) > \
        contacts.econ_rank({"title": "CTO", "seniority": "CXO"})
    assert contacts.champion_rank({"title": "VP Sales", "seniority": "Vice President"}) > \
        contacts.champion_rank({"title": "Sales Manager", "seniority": "Manager"})


@pytest.mark.parametrize("title,sen,role", [
    ("Co-Founder & CEO", "CXO", ContactRole.economic_buyer),
    ("VP Sales", "Vice President", ContactRole.champion),
    ("Head of Revenue Operations", "", ContactRole.champion),
    ("Account Executive", "Senior", ContactRole.other),
])
def test_role_rules(title, sen, role):
    assert contacts.role_for(title, sen) == role
