"""Scriptable stand-in for CrustdataClient (same method signatures, no fixtures, no network)."""
from __future__ import annotations

from typing import Any, Callable

from signalloop.tools.crustdata import normalize_domain


def company(domain: str, cid: int, *, name: str | None = None, industry="Software Development",
            headcount=40, stage="series_a", country="USA", growth=12.0,
            last_round_date="2026-06-01", amount=8_000_000.0) -> dict:
    return {
        "crustdata_company_id": cid,
        "basic_info": {"name": name or domain.split(".")[0].title(), "primary_domain": domain},
        "headcount": {"total": headcount, "growth_percent": {"six_months": growth}},
        "funding": {"last_round_type": stage, "last_fundraise_date": last_round_date,
                    "last_round_amount_usd": amount, "total_investment_usd": amount * 1.5},
        "locations": {"country": country},
        "taxonomy": {"professional_network_industry": industry},
    }


def person(pid: int, name: str, title: str, seniority: str, domain: str,
           function="Sales", email: str | None = None) -> dict:
    p = {
        "crustdata_person_id": pid,
        "basic_profile": {"name": name},
        "social_handles": {"professional_network_identifier":
                           {"profile_url": f"https://www.linkedin.com/in/{name.lower().replace(' ', '-')}-{pid}"}},
        "experience": {"employment_details": {"current": [
            {"title": title, "seniority_level": seniority, "function_category": function,
             "company_website_domain": domain}]}},
    }
    if email:
        p["email"] = email
    return p


class FakeCrustdata:
    def __init__(self):
        self.companies: dict[str, dict] = {}            # domain -> company_data
        self.autocomplete_values: dict[tuple[str, str], list[str]] = {}
        self.search_count: Callable[[dict], int] = lambda f: len(self.companies)
        self.search_pool: list[dict] = []               # what company_search returns
        self.people: dict[str, list[dict]] = {}         # domain -> profiles
        self.jobs: dict[str, list[dict]] = {}           # domain -> job listings
        self.calls: list[tuple[str, Any]] = []

    # company
    def company_identify(self, domain_or_name: str, exact_match=None) -> list[dict]:
        d = normalize_domain(domain_or_name)
        self.calls.append(("company_identify", d))
        c = self.companies.get(d)
        matches = [] if c is None else [{"confidence_score": 1.0, "company_data": {
            "crustdata_company_id": c["crustdata_company_id"], "basic_info": c["basic_info"]}}]
        return [{"matched_on": d, "match_type": "domain", "matches": matches}]

    def company_enrich(self, domains=None, company_ids=None, fields=None) -> list[dict]:
        self.calls.append(("company_enrich", domains or company_ids))
        out = []
        by_id = {str(c["crustdata_company_id"]): c for c in self.companies.values()}
        for v in (domains or company_ids or []):
            c = self.companies.get(normalize_domain(str(v))) if domains else by_id.get(str(v))
            out.append({"matched_on": v, "matches": [] if c is None else
                        [{"confidence_score": 1.0, "company_data": c}]})
        return out

    def company_search(self, filters: dict, limit: int = 100, cursor=None, fields=None, sorts=None) -> dict:
        self.calls.append(("company_search", {"filters": filters, "limit": limit}))
        total = self.search_count(filters)
        return {"companies": self.search_pool[:limit], "next_cursor": None, "total_count": total}

    # person
    def person_search(self, filters: dict, limit: int = 25, cursor=None, fields=None, sorts=None) -> dict:
        self.calls.append(("person_search", {"filters": filters, "limit": limit}))
        domain = _find_value(filters, "experience.employment_details.current.company_website_domain")
        profiles = self.people.get(domain, [])[:limit]
        return {"profiles": profiles, "next_cursor": None, "total_count": len(profiles)}

    def person_enrich(self, profile_urls=None, emails=None, fields=None) -> list[dict]:
        self.calls.append(("person_enrich", {"profile_urls": profile_urls, "emails": emails}))
        return []

    def job_search(self, filters: dict, limit: int = 50, cursor=None, fields=None, sorts=None) -> dict:
        self.calls.append(("job_search", {"filters": filters, "limit": limit}))
        domain = _find_value(filters, "company.basic_info.primary_domain")
        jobs = self.jobs.get(domain, [])[:limit]
        return {"job_listings": jobs, "next_cursor": None, "total_count": len(jobs)}

    def autocomplete(self, dataset: str, field: str, query: str, limit: int = 20) -> list[str]:
        self.calls.append(("autocomplete", (dataset, field, query)))
        vals = self.autocomplete_values.get((dataset, field), [])
        q = (query or "").lower()
        return [v for v in vals if q in v.lower()][:limit] if q else vals[:limit]

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


def _find_value(filters: Any, field: str):
    if isinstance(filters, dict):
        if filters.get("field") == field:
            return filters.get("value")
        for c in filters.get("conditions", []):
            v = _find_value(c, field)
            if v is not None:
                return v
    return None
