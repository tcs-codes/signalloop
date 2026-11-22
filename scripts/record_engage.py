"""Run WATCH -> ENGAGE step by step and print what each step produced.

    python scripts/record_engage.py scan        # F12 baseline scan (holds back day-2 events)
    python scripts/record_engage.py day2        # F12 day-2 scan (releases them) + F14 top 10
    python scripts/record_engage.py engage [n]  # F15-F17 for the top n accounts (default 3)
    python scripts/record_engage.py show        # print queued drafts with critic results

Set SIGNALLOOP_MODE=live for real Crustdata calls (LLM calls are always real outside tests).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from signalloop.config import get_settings  # noqa: E402
from signalloop.db import init_db  # noqa: E402
from signalloop.services import approvals, outreach, scoring, watch  # noqa: E402
from signalloop.tools import meter  # noqa: E402


def report(run) -> None:
    s = meter.summary(run.run_id)
    print(f"[{run.name}] credits={s['credits']} billable={s['billable_calls']} cached={s['cached_calls']} "
          f"llm_calls={s['llm_calls']} tokens={s['input_tokens']}/{s['output_tokens']} errors={s['errors']}")


def show_top(n: int = 10) -> None:
    for b in scoring.this_week(n):
        print(f"  {b.priority:.3f} {b.domain:24} {b.summary.split(' · ', 1)[-1]}")


def show_drafts() -> None:
    for a in approvals.list_pending("outreach"):
        p = a.payload_json
        status = "PASS" if p["critic_passed"] else "NEEDS HUMAN"
        print(f"\n=== #{a.id} {p['account']} -> {p['contact']} ({p['title']}, {p['role']}) "
              f"[{p['channel']}] critic={status} rewrites={p['rewrites']}")
        if p.get("subject"):
            print("Subject:", p["subject"])
        print(p["body"])
        for v in p.get("violations") or []:
            print("  ! ", v)


def main() -> int:
    init_db()
    step = sys.argv[1]
    print(f"mode={get_settings().SIGNALLOOP_MODE} as_of={watch.as_of_date()}")
    if step in ("scan", "day2"):
        with meter.run(f"watch.{step}") as r:
            res = watch.daily_scan(day2=(step == "day2"))
        types: dict[str, int] = {}
        for s in res.new_signals:
            types[s.type.value] = types.get(s.type.value, 0) + 1
        print(f"  watched={res.watched} new_signals={len(res.new_signals)} {types} held_back={res.held_back} "
              f"new_accounts={res.new_accounts} errors={res.errors}")
        report(r)
        show_top()
    elif step == "engage":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        with meter.run("engage") as r:
            out = outreach.draft_this_week(n)
        passed = sum(o.critic.passed for o in out)
        print(f"  drafts={len(out)} passed={passed} needs_human={len(out) - passed} "
              f"rewrites={sum(o.critic.rewrites for o in out)}")
        report(r)
    elif step == "show":
        show_drafts()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
