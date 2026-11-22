# Crustdata API notes (verified against docs, 2026-11-22)

Sources: `https://docs.crustdata.com/llms.txt` and the linked `.md` pages
(API Introduction, Company Search/Identify/Enrich/Autocomplete reference, Person
Search/Enrich/Identify/Autocomplete reference, Job Search reference, Pricing,
Credits, Rate limits). The OpenAPI YAML links in llms.txt currently return the HTML
site shell, so the `.md` pages (some embed the OpenAPI schema) are the source of truth.

## Conventions

| Item | Value |
|---|---|
| Base URL | `https://api.crustdata.com` |
| Headers | `authorization: Bearer <key>`, `x-api-version: 2025-11-01` (required; missing → 400), `content-type: application/json` |
| Method | All data endpoints are `POST` with a JSON body |
| Credits per call | Response header **`X-Credits-Used`** (exact decimal, also on errors). The meter records this value, not an estimate. |
| Errors | JSON `{"error": {"type", "message", "metadata"}}` (older pages show `{"error", "description"}`; Person API uses `reason`). |
| Status codes | 400 bad request · 401 bad/missing key · 402 out of credits / key cap · 403 no access to endpoint/field or insufficient credits · 404 not found (enrich may return 200 + empty `matches` instead) · 413 body > 10 MB · 429 rate limited (may carry `Retry-After`) · 500 server error |
| Live endpoints | `*/professional_network/*/live`, `/web/*/live`: enterprise-only and **not used** by SignalLoop |

## Endpoints used

| Client method | Path | Request | Response | Batch limit |
|---|---|---|---|---|
| `company_search` | `POST /company/search` | `filters`, `fields`, `sorts`, `limit` (1–1000, default 20), `cursor` | `{companies, next_cursor, total_count}` | n/a |
| `company_identify` | `POST /company/identify` | exactly one of `domains` / `names` / `crustdata_company_ids` / `professional_network_profile_urls`; `exact_match` | top-level array `[{matched_on, match_type, matches:[{confidence_score, company_data:{crustdata_company_id, basic_info}}]}]` | **25** |
| `company_enrich` | `POST /company/enrich` | one identifier type (as identify) + `fields` (section groups; default `basic_info` only) | same array shape, `company_data` holds requested sections | **25** |
| `person_search` | `POST /person/search` | `filters`, `fields`, `sorts`, `limit` (1–1000), `cursor`, `explain` | `{profiles, next_cursor, total_count}` | n/a |
| `person_enrich` (profile URLs) | `POST /person/enrich` | `professional_network_profile_urls` (**only** identifier accepted, `additionalProperties: false`), `fields` | array `[{matched_on, match_type, match_status, matches:[{confidence_score, person_data}]}]` | **25** |
| `person_enrich` (emails) | `POST /person/identify` | `emails` | same array shape as enrich, `person_data` = basic profile + social handles | **25**; **enabled per account** |
| `job_search` | `POST /job/search` | `filters`, `fields`, `sorts`, `limit`, `cursor`, `aggregations` | `{job_listings, next_cursor, total_count}` | n/a |
| `autocomplete` | `POST /{company,person,job}/search/autocomplete` | `field`, `query` (`""` = most frequent), `limit` (max 100) | `{suggestions:[{value}]}` | n/a |

## Filter grammar (search endpoints)

```json
{"field": "<dot.path>", "type": "<op>", "value": <scalar|array|null>}
{"op": "and" | "or", "conditions": [<condition or group>, ...]}   // groups nest
```

Operators: `=`, `!=`, `<`, `=<`, `>`, `=>`, `in`, `not_in`, `is_null`, `is_not_null`,
`(.)` (fuzzy all-words), `[.]` (exact phrase), `(!)` (fuzzy exclude), `contains`,
`not_contains`, `has_all` (technographics/SIC lists only), `geo_distance`,
`geo_exclude`, `geo_polygon` (`locations.headquarters` only).
**`>=` and `<=` are rejected**; use `=>` and `=<`. Text operators on numeric fields → 400.

Sorts: `[{"field": "<path>", "order": "asc"|"desc"}]` (some pages say `column`; use `field`
as in the pagination examples). Include `sorts` when paginating. Changing filters,
sorts or fields between pages invalidates the cursor.

### Company search fields SignalLoop uses

| Dimension | Field | Type | Notes |
|---|---|---|---|
| industry | `taxonomy.professional_network_industry` (or alias `basic_info.industries`) | string[] | values from autocomplete |
| headcount | `headcount.total` | int | |
| headcount band | `basic_info.employee_count_range` | string | e.g. `"51-200"` |
| growth | `headcount.growth_percent.6m` | number | filterable, not sortable |
| funding stage | `funding.last_round_type` | string | e.g. `"series_a"`; values from autocomplete |
| funding recency | `funding.last_fundraise_date` | date | `=> "2026-08-01"` |
| funding amount | `funding.last_round_amount_usd`, `funding.total_investment_usd` | number | |
| geography | `locations.country` | string | ISO-3 or full name both accepted |
| domain | `basic_info.primary_domain` | string | |

**Period keys differ between endpoints:** search uses `1m/3m/6m/12m`, enrich uses
`mom/qoq/six_months/yoy/two_years`. `normalize_company` maps enrich → search keys.

### Person search fields SignalLoop uses

| Purpose | Field |
|---|---|
| scope to company | `experience.employment_details.current.company_website_domain` (bare domain) or `...current.company_id` |
| title | `experience.employment_details.current.title` (`(.)` fuzzy) |
| seniority | `experience.employment_details.current.seniority_level` (values include `CXO`, `Vice President`, `Director`, `Owner / Partner`) |
| function | `experience.employment_details.current.function_category` |
| role start date (leader change) | `experience.employment_details.current.start_date` |
| location | `basic_profile.location.country` |

`experience.employment_details.current` is a **list** in responses. Profile URL is
returned at `social_handles.professional_network_identifier.profile_url` but is not filterable.

### Job search fields SignalLoop uses

`company.basic_info.primary_domain`, `job_details.title`, `job_details.category`,
`metadata.date_added` (ISO date, `=>`). Response list key is `job_listings`.

## Credit costs (standard list prices; your account's prices: `GET /account/endpoints`, free)

| Endpoint | Cost |
|---|---|
| `/company/search` | **0.03 / result** + premium groups per result: filter on taxonomy +0.1, headcount +0.2, funding +0.2, hiring +0.2, followers +0.2; *receiving* the same group bills again. Sorting is free. |
| `/company/identify` | 0.2 per matched identifier (unmatched free) |
| `/company/enrich` | 2 per record (+2 if `technographics` requested and returned) |
| `/person/search` | 0.03 / result + premium groups (experience +0.1 filter, +0.1 response; education/skills +0.1 each) |
| `/person/enrich` | 1 per record (base profile incl. experience); up to 9 with add-ons |
| `/person/identify` | 1 per match (unmatched free) |
| `/job/search` | 0.03 / result |
| `*/search/autocomplete` | free |
| `/account/credits`, `/account/endpoints` | free |

**Cost-control consequences for SignalLoop**
- An ICP search that filters on industry + headcount + funding costs about
  0.03 + 0.5 = 0.53 credits per result, plus ~0.5 more if those groups are returned.
  F9 therefore runs tuning iterations with `limit=1` and reads `total_count`
  (~1 credit each), then fetches the final list once.
- Search `fields` should list only what scoring needs.

## Rate limits

Self-serve tiers set per-endpoint RPM for search/enrich/identify: Starter (no spend)
**5 RPM**, Standard ($5+) 15, Plus ($95+) 30; overall 100–200 RPM. Autocomplete: 300 RPM.
`/account/credits`: 300 RPM. The client retries 429 and 5xx with exponential
backoff and jitter, honouring `Retry-After` when present.

## Differences from the build plan's assumptions

1. **`/person/enrich` does not accept emails.** Emails go to `/person/identify`, which
   is enabled per account on request (403 otherwise). `person_enrich(emails=...)`
   routes there and reports emails it could not resolve instead of failing.
2. **Autocomplete is per dataset** (`/company|person|job/search/autocomplete`) with
   no `dataset` parameter; the client maps `dataset` to the path.
3. **`total_count` is returned** by filter-only company search, so the F1 preview and
   the F9 tuning loop can use cheap `limit=1` calls.
4. **Enrich and search use different headcount-growth period keys** (see above).
5. **Every response reports exact credits** in `X-Credits-Used`, so F5 meters actual
   spend; the table above is only used for pre-call estimates.
