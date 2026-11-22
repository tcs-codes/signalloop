import json

import httpx
import pytest
from sqlmodel import select

from signalloop.db import session_scope
from signalloop.models import ApiCache, RunLog
from signalloop.tools import crustdata as cd


def _logs():
    with session_scope() as s:
        return s.exec(select(RunLog).order_by(RunLog.id)).all()


# ---------------------------------------------------------------- mock mode
def test_all_methods_return_fixture_data_without_network():
    c = cd.CrustdataClient(mode="mock")
    assert c.company_search({"field": "x", "type": "=", "value": 1})["companies"]
    assert c.company_identify("https://www.Retool.com/")[0]["matches"]
    assert c.company_enrich(domains=["retool.com"])[0]["matches"][0]["company_data"]["funding"]
    assert c.person_search({"field": "x", "type": "=", "value": 1})["profiles"]
    assert c.person_enrich(profile_urls=["https://www.linkedin.com/in/a"])[0]["matches"]
    assert c.person_enrich(emails=["a@b.com"])[0]["match_type"] == "email"
    assert c.job_search({"field": "x", "type": "=", "value": 1})["job_listings"]
    assert "Software Development" in c.autocomplete("company", "basic_info.industries", "soft")
    assert c.autocomplete("person", "experience.employment_details.current.seniority_level", "")


def test_second_identical_call_is_served_from_cache():
    c = cd.CrustdataClient(mode="mock")
    first = c.company_enrich(domains=["retool.com"])
    second = c.company_enrich(domains=["retool.com"])
    assert first == second
    logs = _logs()
    assert [r.cached for r in logs] == [False, True]
    assert logs[0].credits == 2.0 and logs[1].credits == 0.0
    with session_scope() as s:
        assert len(s.exec(select(ApiCache)).all()) == 1


def test_specific_fixture_beats_default(tmp_path):
    body = {"domains": ["acme.com"], "fields": cd.DEFAULT_COMPANY_ENRICH_FIELDS}
    key = cd.cache_key(cd.PATHS["company_enrich"], body)
    (tmp_path / f"company_enrich__{key[:16]}.json").write_text(json.dumps(
        {"request": body, "credits": 2, "response": [{"matched_on": "acme.com", "matches": []}]}))
    (tmp_path / "company_enrich__default.json").write_text(json.dumps(
        {"request": None, "response": [{"matched_on": "DEFAULT", "matches": []}]}))
    c = cd.CrustdataClient(mode="mock", fixtures_dir=tmp_path)
    assert c.company_enrich(domains=["acme.com"])[0]["matched_on"] == "acme.com"
    assert c.company_enrich(domains=["other.com"])[0]["matched_on"] == "DEFAULT"


def test_missing_fixture_raises(tmp_path):
    c = cd.CrustdataClient(mode="mock", fixtures_dir=tmp_path)
    with pytest.raises(cd.CrustdataFixtureMissing):
        c.job_search({"field": "x", "type": "=", "value": 1})
    assert _logs()[-1].ok is False


def test_person_enrich_batches_of_25():
    c = cd.CrustdataClient(mode="mock")
    c.person_enrich(emails=[f"user{i}@acme.com" for i in range(60)])
    c.person_enrich(profile_urls=[f"https://www.linkedin.com/in/u{i}" for i in range(60)])
    calls = [r.endpoint_or_model for r in _logs()]
    assert calls.count("/person/identify") == 3
    assert calls.count("/person/enrich") == 3


def test_company_enrich_requires_exactly_one_identifier_type():
    c = cd.CrustdataClient(mode="mock")
    with pytest.raises(ValueError):
        c.company_enrich()
    with pytest.raises(ValueError):
        c.company_enrich(domains=["a.com"], company_ids=[1])


@pytest.mark.parametrize("raw,expected", [
    ("https://www.Acme.com/", "acme.com"), ("acme.com", "acme.com"),
    ("http://WWW.acme.io/pricing?x=1", "acme.io"), ("acme.com.", "acme.com"),
])
def test_normalize_domain(raw, expected):
    assert cd.normalize_domain(raw) == expected


def test_identify_uses_names_for_non_domains():
    c = cd.CrustdataClient(mode="mock")
    c.company_identify("Serve Robotics")
    with session_scope() as s:
        row = s.exec(select(ApiCache)).one()
    assert row.request_json == {"names": ["Serve Robotics"]}


# ---------------------------------------------------------------- live mode (mocked transport)
class Recorder:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body, headers = self.responses.pop(0)
        return httpx.Response(status, json=body, headers=headers or {})


def _live(rec, **kw):
    return cd.CrustdataClient(mode="live", api_key="test-key", rpm=0,
                              transport=httpx.MockTransport(rec), sleep=lambda s: None, **kw)


def test_live_sends_headers_and_records_credits(allow_mock_transport):
    rec = Recorder([(200, {"suggestions": [{"value": "Fintech"}]}, {"x-credits-used": "0"}),
                    (200, [{"matched_on": "a.com", "matches": []}], {"x-credits-used": "2"})])
    c = _live(rec)
    assert c.autocomplete("company", "basic_info.industries", "fin") == ["Fintech"]
    c.company_enrich(domains=["a.com"])
    req = rec.requests[0]
    assert req.url.path == "/company/search/autocomplete"
    assert req.headers["authorization"] == "Bearer test-key"
    assert req.headers["x-api-version"] == "2025-11-01"
    assert [r.credits for r in _logs()] == [0.0, 2.0]
    # cached afterwards: no new request
    c.company_enrich(domains=["a.com"])
    assert len(rec.requests) == 2


def test_live_retries_429_and_5xx(allow_mock_transport):
    rec = Recorder([(429, {"error": {"message": "slow down"}}, {"retry-after": "1"}),
                    (503, {"error": {"message": "busy"}}, None),
                    (200, {"companies": [], "next_cursor": None, "total_count": 0}, None)])
    out = _live(rec).company_search({"field": "x", "type": "=", "value": 1}, limit=1)
    assert out["total_count"] == 0 and len(rec.requests) == 3


def test_live_auth_error_is_not_retried(allow_mock_transport):
    rec = Recorder([(401, {"error": {"type": "unauthorized", "message": "Invalid API key"}}, None)])
    with pytest.raises(cd.CrustdataAuthError, match="Invalid API key"):
        _live(rec).company_identify("a.com")
    assert len(rec.requests) == 1


def test_live_bad_request_and_forbidden(allow_mock_transport):
    rec = Recorder([(400, {"error": {"message": "Unsupported columns"}}, None),
                    (403, {"error": {"message": "Access denied"}}, None)])
    c = _live(rec)
    with pytest.raises(cd.CrustdataBadRequest):
        c.company_search({"field": "nope", "type": "=", "value": 1})
    with pytest.raises(cd.CrustdataForbidden):
        c.person_enrich(emails=["a@b.com"])


def test_live_enrich_404_means_no_match(allow_mock_transport):
    rec = Recorder([(404, {"error": "Not Found"}, None)])
    out = _live(rec).company_enrich(domains=["nope.example"])
    assert out == [{"matched_on": "nope.example", "match_type": None, "matches": []}]


def test_live_without_key_raises():
    c = cd.CrustdataClient(mode="live", api_key="")
    with pytest.raises(cd.CrustdataAuthError):
        c.company_identify("a.com")


def test_throttle_spaces_calls():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    t = cd._Throttle(rpm=6, clock=lambda: now[0], sleep=sleep)  # one call per 10s
    t.wait("/x")
    t.wait("/x")
    t.wait("/y")
    assert slept == [10.0]
