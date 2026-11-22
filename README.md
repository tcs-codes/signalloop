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

_Recording fixtures and the demo walkthrough are documented as those phases land._
