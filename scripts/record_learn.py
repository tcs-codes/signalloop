"""Run LEARN step by step and print what each step produced.

    python scripts/record_learn.py campaign [seed]   # F24 generate synthetic CSV + import (+ F25 enrich)
    python scripts/record_learn.py analyze          # F26 segment lift + narrative (real LLM)
    python scripts/record_learn.py propose          # F28 ICP diff -> approval queue (real LLM)
    python scripts/record_learn.py approve          # F28 approve -> ICP v2 + lookalike re-run (Crustdata)

Set SIGNALLOOP_MODE=live for real Crustdata calls in the approve step.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from signalloop.config import get_settings  # noqa: E402
from signalloop.db import init_db  # noqa: E402
from signalloop.services import analysis, approvals, campaigns, icp as icp_svc, icp_diff, lookalike  # noqa: E402
from signalloop.tools import meter  # noqa: E402

from signalloop.services.demo_campaign import DEFAULT_CSV as CSV, DEFAULT_NAME as NAME  # noqa: E402


def report(run) -> None:
    s = meter.summary(run.run_id)
    print(f"[{run.name}] credits={s['credits']} billable={s['billable_calls']} cached={s['cached_calls']} "
          f"llm_calls={s['llm_calls']} tokens={s['input_tokens']}/{s['output_tokens']} errors={s['errors']}")


def campaign_id() -> int:
    return next(c.id for c in campaigns.list_campaigns() if c.name == NAME)


def main() -> int:
    init_db()
    step = sys.argv[1]
    print(f"mode={get_settings().SIGNALLOOP_MODE}")
    if step == "campaign":
        seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
        from signalloop.services import demo_campaign
        n = demo_campaign.generate(CSV, seed=seed)
        with meter.run("learn.import") as r:
            res = campaigns.import_campaign(CSV, NAME)
            df = campaigns.enrich_campaign(res.campaign.id)
        print(f"  generated {n} rows (seed {seed}); linked contacts {res.contact_link_rate:.0%}; "
              f"replies {int(df.responded.sum())}/{len(df)} ({df.responded.mean():.1%})")
        report(r)
    elif step == "analyze":
        with meter.run("learn.analyze") as r:
            res = analysis.analyze(campaign_id())
        for i in res.top(5):
            print(f"  +{i.lift:5.2f}x n={i.n:3} {i.dimension} = {i.segment_value}{' (low conf)' if i.low_confidence else ''}")
        for i in res.top(2, positive=False):
            print(f"  -{i.lift:5.2f}x n={i.n:3} {i.dimension} = {i.segment_value}")
        print("  headline:", res.narrative.headline)
        print("  narrative:", res.narrative.narrative)
        report(r)
    elif step == "propose":
        with meter.run("learn.propose") as r:
            appr = icp_diff.propose(campaign_id())
        if appr is None:
            print("  no eligible insights")
            return 1
        for row in appr.payload_json["diff"]:
            if row["changed"]:
                print(f"  {row['scope']:7} {row['field']:52} {row['before']}  ->  {row['after']}")
        for ch in appr.payload_json["changes"]:
            print(f"  - {ch['op']} {ch['field']}: {ch['reason']}")
        print("  expected:", appr.payload_json["expected_effect"])
        report(r)
    elif step == "approve":
        pending = approvals.list_pending("icp_diff")
        if not pending:
            print("  nothing pending")
            return 1
        before = [a.domain for a in lookalike.list_accounts(10)]
        with meter.run("learn.approve", budget=400) as r:
            v2, res = icp_diff.approve(pending[-1].id, note="approved during recording")
        after = lookalike.list_accounts(10)
        print(f"  ICP v{v2.version} approved; lookalike count={res.count} converged={res.converged} "
              f"fetched={res.fetched} upserted={res.upserted}")
        print(f"  top-10 changed: {len(set(before) - {a.domain for a in after})} of 10")
        for a in after:
            print(f"   {a.fit_score:.2f} {a.domain:26} {a.similarity_reason}")
        report(r)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
