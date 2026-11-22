"""F2: Crustdata tool layer (mock/live, cache, retries, batching).

Every call goes through ``CrustdataClient._post``:

1. cache key = sha256(path + canonical JSON body)
2. ``api_cache`` hit  -> return it (0 credits, logged as cached)
3. mock mode          -> ``fixtures/crustdata/<endpoint>__<key16>.json``, else
                         ``fixtures/crustdata/<endpoint>__default.json``; stored in the cache
4. live mode          -> throttle, POST with retries/backoff, store in the cache,
                         log the exact ``X-Credits-Used`` header value

Endpoint paths and limits are documented in ``docs/crustdata_api_notes.md``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from signalloop.config import get_settings
from signalloop.runctx import current_agent, current_run_id

log = logging.getLogger(__name__)

BATCH_SIZE = 25
AUTOCOMPLETE_RPM = 300  # autocomplete has its own, much higher limit

# endpoint name -> path (names are used in fixture filenames)
PATHS: dict[str, str] = {
    "company_search": "/company/search",
    "company_identify": "/company/identify",
    "company_enrich": "/company/enrich",
    "person_search": "/person/search",
    "person_enrich": "/person/enrich",
    "person_identify": "/person/identify",
    "job_search": "/job/search",
    "company_autocomplete": "/company/search/autocomplete",
    "person_autocomplete": "/person/search/autocomplete",
    "job_autocomplete": "/job/search/autocomplete",
}
AUTOCOMPLETE_DATASETS = {"company", "person", "job"}

# `hiring` is left out: hiring.recent_openings is disabled on this account, and
# job-post signals come from /job/search instead.
DEFAULT_COMPANY_ENRICH_FIELDS = [
    "basic_info", "headcount", "funding", "locations", "taxonomy", "people",
]
DEFAULT_COMPANY_SEARCH_FIELDS = [
    "crustdata_company_id", "basic_info", "headcount", "funding", "locations", "taxonomy",
]
DEFAULT_PERSON_SEARCH_FIELDS = [
    "crustdata_person_id", "basic_profile", "social_handles",
    "experience.employment_details.current",
]


# ---------------------------------------------------------------- errors
class CrustdataError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status, self.body = status, body


class CrustdataAuthError(CrustdataError):
    """401: missing or invalid API key."""


class CrustdataForbidden(CrustdataError):
    """402/403: no access to the endpoint/field, or out of credits."""


class CrustdataBadRequest(CrustdataError):
    """400/404/413/422: the request itself is wrong."""


class CrustdataRateLimit(CrustdataError):
    """429."""

    def __init__(self, message: str, retry_after: float | None = None, **kw):
        super().__init__(message, status=429, **kw)
        self.retry_after = retry_after


class CrustdataServerError(CrustdataError):
    """5xx."""


class CrustdataFixtureMissing(CrustdataError):
    """Mock mode found neither a specific nor a default fixture."""


def _error_message(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or err)
        return str(body.get("description") or body.get("reason") or err or body.get("message") or body)
    return str(body)[:500]


def raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    msg = f"{resp.request.method} {resp.request.url.path} -> {resp.status_code}: {_error_message(body)}"
    code = resp.status_code
    if code == 401:
        raise CrustdataAuthError(msg, code, body)
    if code in (402, 403):
        raise CrustdataForbidden(msg, code, body)
    if code == 429:
        ra = resp.headers.get("retry-after")
        raise CrustdataRateLimit(msg, retry_after=float(ra) if ra else None, body=body)
    if code >= 500:
        raise CrustdataServerError(msg, code, body)
    raise CrustdataBadRequest(msg, code, body)


# ---------------------------------------------------------------- helpers
def cache_key(path: str, body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{path}|{canonical}".encode()).hexdigest()


def normalize_domain(value: str) -> str:
    v = value.strip().lower()
    for prefix in ("https://", "http://"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    v = v.split("/")[0].split("?")[0].split("#")[0]
    if v.startswith("www."):
        v = v[4:]
    return v.rstrip(".")


def looks_like_domain(value: str) -> bool:
    v = value.strip()
    return " " not in v and "." in normalize_domain(v)


def chunks(items: list[Any], size: int = BATCH_SIZE) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class _Throttle:
    """Minimum spacing between live calls to the same endpoint (requests per minute)."""

    def __init__(self, rpm: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 overrides: dict[str, float] | None = None):
        self.interval = 60.0 / rpm if rpm > 0 else 0.0
        self._overrides = {k: (60.0 / v if v > 0 else 0.0) for k, v in (overrides or {}).items()}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._clock, self._sleep = clock, sleep

    def _interval_for(self, endpoint: str) -> float:
        if not self.interval:
            return 0.0
        for suffix, iv in self._overrides.items():
            if endpoint.endswith(suffix):
                return iv
        return self.interval

    def wait(self, endpoint: str) -> None:
        interval = self._interval_for(endpoint)
        if not interval:
            return
        with self._lock:
            now = self._clock()
            due = self._last.get(endpoint, -1e18) + interval
            if due > now:
                self._sleep(due - now)
                now = self._clock()
            self._last[endpoint] = now


# ---------------------------------------------------------------- client
class CrustdataClient:
    def __init__(
        self,
        mode: str | None = None,
        api_key: str | None = None,
        fixtures_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rpm: float | None = None,
        max_attempts: int = 5,
    ):
        s = get_settings()
        self.mode = mode or s.SIGNALLOOP_MODE
        self.api_key = api_key if api_key is not None else s.CRUSTDATA_API_KEY
        self.fixtures_dir = Path(fixtures_dir or s.fixtures_dir / "crustdata")
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._throttle = _Throttle(rpm if rpm is not None else s.CRUSTDATA_RPM, sleep=sleep,
                                   overrides={"/autocomplete": AUTOCOMPLETE_RPM})
        self._transport = transport
        self._http: httpx.Client | None = None
        self.base_url = s.CRUSTDATA_BASE_URL
        self.api_version = s.CRUSTDATA_API_VERSION

    # ------------------------------------------------ plumbing
    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            if not self.api_key:
                raise CrustdataAuthError("CRUSTDATA_API_KEY is not set (needed in live mode)")
            self._http = httpx.Client(
                base_url=self.base_url, timeout=httpx.Timeout(60.0, connect=15.0),
                transport=self._transport,
                headers={
                    "authorization": f"Bearer {self.api_key}",
                    "x-api-version": self.api_version,
                    "content-type": "application/json",
                },
            )
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def _log(self, *, endpoint: str, credits: float, latency_ms: int, cached: bool,
             ok: bool, error: str = "") -> None:
        from signalloop.db import session_scope
        from signalloop.models import RunLog

        try:
            with session_scope() as s:
                s.add(RunLog(run_id=current_run_id.get(), agent=current_agent.get() or "crustdata",
                             endpoint_or_model=endpoint, credits=credits, latency_ms=latency_ms,
                             cached=cached, ok=ok, error=error[:2000]))
        except Exception as exc:  # logging must never break the call
            log.warning("run_log write failed: %s", exc)

    def _cache_get(self, key: str) -> Any | None:
        from signalloop.db import session_scope
        from signalloop.models import ApiCache

        with session_scope() as s:
            row = s.get(ApiCache, key)
            return None if row is None else row.response_json

    def _cache_put(self, key: str, endpoint: str, body: dict, response: Any) -> None:
        from signalloop.db import session_scope
        from signalloop.models import ApiCache

        with session_scope() as s:
            row = s.get(ApiCache, key)
            if row is None:
                s.add(ApiCache(key=key, endpoint=endpoint, request_json=body, response_json=response))
            else:
                row.response_json = response

    def _load_fixture(self, name: str, key: str) -> tuple[Any, float]:
        for fname in (f"{name}__{key[:16]}.json", f"{name}__default.json"):
            p = self.fixtures_dir / fname
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "response" in data and "request" in data:
                    return data["response"], float(data.get("credits") or 0)
                return data, 0.0
        raise CrustdataFixtureMissing(
            f"mock mode: no fixture {name}__{key[:16]}.json or {name}__default.json "
            f"in {self.fixtures_dir}"
        )

    def _send(self, path: str, body: dict) -> tuple[Any, float]:
        def attempt() -> tuple[Any, float]:
            self._throttle.wait(path)
            resp = self.http.post(path, json=body)
            credits = float(resp.headers.get("x-credits-used") or 0)
            if resp.status_code == 404 and path in (PATHS["company_enrich"], PATHS["company_identify"]):
                return None, credits  # per docs: treat 404 as "no match"
            raise_for_status(resp)
            return resp.json(), credits

        def wait(state) -> float:
            exc = state.outcome.exception()
            ra = getattr(exc, "retry_after", None)
            return min(float(ra), 60.0) if ra else wait_exponential_jitter(initial=1, max=30)(state)

        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts), wait=wait, sleep=self._sleep, reraise=True,
            retry=retry_if_exception_type(
                (CrustdataRateLimit, CrustdataServerError, httpx.TransportError)),
        )
        return retrying(attempt)

    def _post(self, name: str, body: dict[str, Any]) -> Any:
        from signalloop.tools import meter

        path = PATHS[name]
        key = cache_key(path, body)
        t0 = time.perf_counter()
        ms = lambda: int((time.perf_counter() - t0) * 1000)  # noqa: E731

        cached = self._cache_get(key)
        if cached is not None:
            self._log(endpoint=path, credits=0.0, latency_ms=ms(), cached=True, ok=True)
            return cached

        meter.check_budget(meter.estimate(name, body))  # raises BudgetExceeded before spending
        try:
            if self.mode == "mock":
                response, credits = self._load_fixture(name, key)
            else:
                response, credits = self._send(path, body)
        except Exception as exc:
            self._log(endpoint=path, credits=float(getattr(exc, "credits", 0) or 0),
                      latency_ms=ms(), cached=False, ok=False, error=f"{type(exc).__name__}: {exc}")
            raise

        if response is None:  # 404 no-match on enrich/identify
            response = [{"matched_on": v, "match_type": None, "matches": []}
                        for k in ("domains", "names", "crustdata_company_ids",
                                  "professional_network_profile_urls") for v in body.get(k, [])]
        self._cache_put(key, path, body, response)
        meter.add_credits(credits)
        self._log(endpoint=path, credits=credits, latency_ms=ms(), cached=False, ok=True)
        return response

    def _get_live(self, path: str) -> Any:
        """Free account endpoints (never cached, never mocked)."""
        resp = self.http.get(path)
        raise_for_status(resp)
        return resp.json()

    # ------------------------------------------------ company
    def company_search(self, filters: dict, limit: int = 100, cursor: str | None = None,
                       fields: list[str] | None = None, sorts: list[dict] | None = None) -> dict:
        body: dict[str, Any] = {"filters": filters, "limit": limit,
                                "fields": fields or DEFAULT_COMPANY_SEARCH_FIELDS}
        if sorts:
            body["sorts"] = sorts
        if cursor:
            body["cursor"] = cursor
        return self._post("company_search", body)

    def company_identify(self, domain_or_name: str, exact_match: bool | None = None) -> list[dict]:
        if looks_like_domain(domain_or_name):
            body: dict[str, Any] = {"domains": [normalize_domain(domain_or_name)]}
        else:
            body = {"names": [domain_or_name.strip()]}
        if exact_match is not None:
            body["exact_match"] = exact_match
        return self._post("company_identify", body)

    def company_enrich(self, domains: list[str] | None = None, company_ids: list[int | str] | None = None,
                       fields: list[str] | None = None) -> list[dict]:
        if bool(domains) == bool(company_ids):
            raise ValueError("company_enrich: pass exactly one of domains or company_ids")
        key, values = ("domains", [normalize_domain(d) for d in domains]) if domains else \
                      ("crustdata_company_ids", [int(c) for c in company_ids])
        out: list[dict] = []
        for batch in chunks(values):
            out.extend(self._post("company_enrich", {
                key: batch, "fields": fields or DEFAULT_COMPANY_ENRICH_FIELDS}))
        return out

    # ------------------------------------------------ person
    def person_search(self, filters: dict, limit: int = 25, cursor: str | None = None,
                      fields: list[str] | None = None, sorts: list[dict] | None = None) -> dict:
        body: dict[str, Any] = {"filters": filters, "limit": limit,
                                "fields": fields or DEFAULT_PERSON_SEARCH_FIELDS}
        if sorts:
            body["sorts"] = sorts
        if cursor:
            body["cursor"] = cursor
        return self._post("person_search", body)

    def person_enrich(self, profile_urls: list[str] | None = None, emails: list[str] | None = None,
                      fields: list[str] | None = None) -> list[dict]:
        """Profile URLs -> /person/enrich. Emails -> /person/identify (enabled per account;
        /person/enrich does not accept emails). Both are sent in batches of <= 25."""
        if not profile_urls and not emails:
            raise ValueError("person_enrich: pass profile_urls and/or emails")
        out: list[dict] = []
        for batch in chunks(list(profile_urls or [])):
            body: dict[str, Any] = {"professional_network_profile_urls": batch}
            if fields:
                body["fields"] = fields
            out.extend(self._post("person_enrich", body))
        for batch in chunks(list(emails or [])):
            out.extend(self._post("person_identify", {"emails": batch}))
        return out

    # ------------------------------------------------ jobs
    def job_search(self, filters: dict, limit: int = 50, cursor: str | None = None,
                   fields: list[str] | None = None, sorts: list[dict] | None = None) -> dict:
        body: dict[str, Any] = {"filters": filters, "limit": limit}
        if fields:
            body["fields"] = fields
        if sorts:
            body["sorts"] = sorts
        if cursor:
            body["cursor"] = cursor
        return self._post("job_search", body)

    # ------------------------------------------------ autocomplete (free)
    def autocomplete(self, dataset: str, field: str, query: str, limit: int = 20) -> list[str]:
        if dataset not in AUTOCOMPLETE_DATASETS:
            raise ValueError(f"dataset must be one of {sorted(AUTOCOMPLETE_DATASETS)}")
        resp = self._post(f"{dataset}_autocomplete", {"field": field, "query": query, "limit": limit})
        return [s.get("value") for s in (resp or {}).get("suggestions", []) if s.get("value")]

    # ------------------------------------------------ account (free, live only)
    def account_credits(self) -> dict:
        return self._get_live("/account/credits")

    def account_endpoints(self) -> dict:
        return self._get_live("/account/endpoints")


_client: CrustdataClient | None = None


def get_client() -> CrustdataClient:
    global _client
    if _client is None:
        _client = CrustdataClient()
    return _client


def set_client(client: CrustdataClient | None) -> None:
    global _client
    _client = client
