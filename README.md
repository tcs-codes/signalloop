# SignalLoop (MVP)

Closed-loop agentic GTM engine on Crustdata APIs:
**FIND** lookalikes → **WATCH** for buying signals → **ENGAGE** with fact-checked drafts →
**LEARN** which segments responded → propose an **ICP diff** → human approves → repeat.

Nothing is sent or written externally. Every draft and ICP change goes through the approval inbox.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
copy .env.example .env   # then fill in ANTHROPIC_API_KEY and CRUSTDATA_API_KEY
```

## Modes

| `SIGNALLOOP_MODE` | Crustdata calls | Use for |
|---|---|---|
| `mock` (default) | served from `api_cache` or `fixtures/crustdata/*.json`, no network | development, tests, demos |
| `live` | real API calls, responses written to the cache | recording fixtures |

## Run

```powershell
.\.venv\Scripts\python.exe -m pytest          # mock mode, no network
.\.venv\Scripts\streamlit.exe run app/Home.py
```

## Recording fixtures (live → mock)

```powershell
$env:SIGNALLOOP_MODE="live"
.\.venv\Scripts\python.exe scripts\record_fixtures.py credits      # free balance check
.\.venv\Scripts\python.exe scripts\record_find.py seeds            # F7
.\.venv\Scripts\python.exe scripts\record_find.py traits           # F8
.\.venv\Scripts\python.exe scripts\record_find.py icp "B2B software companies, 20-200 employees, Seed to Series B, US or India."
.\.venv\Scripts\python.exe scripts\record_find.py lookalike        # F9
.\.venv\Scripts\python.exe scripts\record_find.py contacts         # F11
.\.venv\Scripts\python.exe scripts\record_find.py export           # api_cache -> fixtures/crustdata
```

Each step runs inside a metered run (`CREDIT_BUDGET_PER_RUN`). Exported fixtures keep the
credits the live call cost, so the Credits page shows a realistic cost in mock mode.
`fixtures/demo/find_recording.json` holds the LLM outputs of the recorded run, and
`tests/test_replay_find.py` replays the whole FIND flow from fixtures with no network.

> **Privacy:** `fixtures/crustdata/` contains real business-contact data (names, titles,
> profile URLs) returned by Crustdata. Keep this repository private.

_The demo walkthrough is documented when Phase 6 lands._
