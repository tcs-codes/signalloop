# SignalLoop (MVP)

Closed-loop agentic GTM engine on Crustdata APIs:
**FIND** lookalikes → **WATCH** for buying signals → **ENGAGE** with fact-checked drafts →
**LEARN** which segments responded → propose an **ICP diff** → human approves → repeat.

Nothing is sent or written externally. Every draft and ICP change goes through the approval
inbox; approved drafts are only exported to `exports/approved_outreach.csv`.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
copy .env.example .env   # then fill in ANTHROPIC_API_KEY and CRUSTDATA_API_KEY
```

## Modes

| `SIGNALLOOP_MODE` | Crustdata calls | LLM calls | Use for |
|---|---|---|---|
| `mock` (default) | `api_cache`, else `fixtures/crustdata/*.json`; no network | replay cache `fixtures/llm/` (identical prompt → recorded output), otherwise live and recorded | development, demos |
| `live` | real API calls, responses written to `api_cache` | live (`LLM_REPLAY=record` also saves them) | recording fixtures |

Tests always run in mock mode with network access blocked and a `FakeLLM`.

**Why an LLM replay cache:** Crustdata fixtures are keyed by the exact request. If the LLM
proposes even slightly different ICP filters than in the recording, the next search has no
fixture. Replaying recorded LLM outputs for identical prompts keeps a mock-mode demo
deterministic. `LLM_REPLAY=auto` replays in mock mode and is off in live mode.

## Run

```powershell
.\.venv\Scripts\python.exe -m pytest                 # mock mode, no network
.\.venv\Scripts\streamlit.exe run app/Home.py        # uses DB_PATH (default data/signalloop.db)
```

Pages: **Home** (loop status, daily scan, in-market list, draft outreach) · **Setup** (seeds →
traits → ICP → approve) · **Accounts** (lookalikes, decision-makers) · **Inbox** (outreach and
ICP changes) · **Learn** (campaign → segment lift → ICP change) · **Credits** (credits, tokens,
cost per run and agent).

CLI: `python -m signalloop.jobs.daily_scan --icp latest [--day2]`. Programmatic entry points:
`signalloop/orchestrator.py` (`run_find`, `run_find_after_approval`, `run_watch_engage`,
`run_learn`, `approve_icp_diff`).

## Demo (5 minutes, mock mode, no credits)

```powershell
.\.venv\Scripts\python.exe scripts\demo_reset.py                 # fresh data/demo.db, Setup stage
# or: scripts\demo_reset.py --stage watch                        # FIND already done + baseline scan
$env:DB_PATH="data/demo.db"; .\.venv\Scripts\streamlit.exe run app/Home.py
```

`demo_reset` never wipes a database whose cache holds responses that are not exported to
fixtures (for example the recording DB) unless `--force`.

1. **Setup:** the 5 seed domains (Series A software companies found with a live Crustdata
   search) are pre-filled → *Import seeds* → *Extract traits* (coverage %) → the recorded ICP
   description is pre-filled → *Generate ICP* (filters validated by autocomplete, 3,204
   matching companies) → *Approve*.
2. **Accounts:** *Run lookalike search* (tuning log: 3,204 → 802 → 652 → 575; 150 accounts with
   fit scores and reasons) → *Find decision-makers* (champion + economic buyer per account).
3. **Home:** *Run daily scan now* → baseline signals (funding, headcount growth, real job posts).
   Run it again → the **3 day-2 accounts** (Pulley $41M Series B, Infisical $40M, Simplismart
   $14M; real rounds held back by `fixtures/demo/signals_day2.json`) become in-market.
   *Draft outreach* for the top N (about 30 s per account, live LLM).
4. **Inbox:** open a draft → why-now bullets with evidence → critic badge and claim table
   (rewrites shown) → *Approve* / *Edit & approve* / *Reject*.
5. **Learn:** *Generate demo campaign* (SYNTHETIC outcomes with a planted pattern) → *Run LEARN*
   → segment lift, narrative with n, proposed ICP change. _(Recorded numbers: pending.)_
6. **Inbox → ICP changes:** before/after diff + evidence → *Approve* → ICP v2, lookalike
   re-run, the Accounts top 10 changes. _(Pending the LEARN recording.)_
7. **Credits:** credits per run and agent, cache hit rate, LLM cost.

## Recording fixtures (live → mock)

```powershell
$env:SIGNALLOOP_MODE="live"
.\.venv\Scripts\python.exe scripts\record_fixtures.py credits      # free balance check
.\.venv\Scripts\python.exe scripts\record_fixtures.py endpoints    # free: access, rate limits, prices
.\.venv\Scripts\python.exe scripts\record_find.py seeds            # F7
.\.venv\Scripts\python.exe scripts\record_find.py traits           # F8
.\.venv\Scripts\python.exe scripts\record_find.py icp "B2B software companies, 20-200 employees, Seed to Series B, US or India."
.\.venv\Scripts\python.exe scripts\record_find.py lookalike        # F9
.\.venv\Scripts\python.exe scripts\record_find.py contacts [all]   # F11 (top 30, or every account)
.\.venv\Scripts\python.exe scripts\record_engage.py scan|day2|engage|show
.\.venv\Scripts\python.exe scripts\record_learn.py campaign|analyze|propose|approve
.\.venv\Scripts\python.exe scripts\record_find.py export           # api_cache -> fixtures/crustdata
.\.venv\Scripts\python.exe scripts\seed_llm_replay.py              # FIND LLM outputs -> fixtures/llm
```

Each step runs in a metered run (`CREDIT_BUDGET_PER_RUN`). Exported fixtures keep the credits
the live call cost, so the Credits page shows a realistic cost in mock mode. The account's
Crustdata rate limit (5 RPM on search/enrich/identify on the Starter tier) is respected via
`CRUSTDATA_RPM`. `docs/crustdata_api_notes.md` documents the API and where it differs from
the original build plan.

Replay tests: `tests/test_replay_find.py` (FIND + WATCH from fixtures) and `tests/test_e2e.py`
(the plan's end-to-end flow; the full-loop part runs once LEARN is recorded).

> **Privacy:** `fixtures/crustdata/` contains real business-contact data (names, titles,
> profile URLs) returned by Crustdata. Keep this repository private.

> **Synthetic data:** campaign outcomes (`fixtures/campaign_results.csv`, generated by
> `signalloop/services/demo_campaign.py`) are simulated. Company, people and signal data are real
> Crustdata data; the day-2 "new" events are real rounds held back by the baseline scan.

## Project map

```
signalloop/   config, db, models, schemas, llm (structured calls, FakeLLM, replay cache)
  tools/      crustdata (F2 client), meter (F5)
  services/   store F3 · suppression F23 · seeds F7 · traits F8 · icp F1 · lookalike F9 ·
              contacts F11 · watch F12 · scoring F14 · evidence · briefs F15 · outreach F16 ·
              critic F17 · approvals F4 · campaigns F24/F25 · analysis F26 · icp_diff F28 ·
              demo_campaign · status
  prompts/    one .md per LLM call
  orchestrator.py, jobs/daily_scan.py
app/          Streamlit: Home + pages 1-5
scripts/      recording, export, demo_reset, gen_campaign, seed_llm_replay
fixtures/     crustdata/ (recorded API), llm/ (recorded LLM), demo/ (recordings, day-2 events),
              seeds.csv, product.md (fictional demo product), suppression.csv
tests/        pytest, mock mode, no network
```
