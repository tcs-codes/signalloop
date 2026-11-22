import pytest

from signalloop.services import seeds, suppression
from signalloop.tools import crustdata
from tests.fakes import FakeCrustdata, company

SEEDS = ["alpha.io", "beta.com", "gamma.ai", "delta.co", "epsilon.dev"]


@pytest.fixture
def fake():
    f = FakeCrustdata()
    for i, d in enumerate(SEEDS):
        f.companies[d] = company(d, 100 + i)
    crustdata.set_client(f)
    return f


def test_five_domains_give_five_seed_accounts(fake):
    res = seeds.import_seeds(SEEDS)
    assert len(res.accounts) == 5 and res.unresolved == []
    for a in res.accounts:
        assert a.is_seed and a.firmographics_json["normalized"]["funding_stage"] == "series_a"
        assert suppression.is_suppressed(account=a)
    assert fake.count("company_enrich") == 1  # one batched enrich call


def test_unresolved_domains_reported_not_raised(fake):
    res = seeds.import_seeds(["alpha.io", "no-such-company.xyz", "not a domain"])
    assert [a.domain for a in res.accounts] == ["alpha.io"]
    assert res.unresolved == ["no-such-company.xyz", "not a domain"]


def test_parse_pasted_list_and_csv():
    assert seeds.parse_domains("https://www.Alpha.io/\nbeta.com, gamma.ai;alpha.io") == \
        ["alpha.io", "beta.com", "gamma.ai"]
    assert seeds.parse_csv("Domain,name\nalpha.io,A\nhttps://beta.com,B\n") == ["alpha.io", "beta.com"]
    with pytest.raises(ValueError):
        seeds.parse_csv("site\nalpha.io\n")
