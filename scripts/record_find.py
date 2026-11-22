"""Run the FIND flow step by step (live or mock) and print what each step produced.

    python scripts/record_find.py seeds       # F7  import fixtures/seeds.csv
    python scripts/record_find.py traits      # F8  extract traits -> data/traits.json
    python scripts/record_find.py icp "<description>"   # F1 build + approve ICP
    python scripts/record_find.py lookalike   # F9
    python scripts/record_find.py contacts    # F11
    python scripts/record_find.py export      # api_cache -> fixtures/crustdata

Set SIGNALLOOP_MODE=live to spend real credits (each step runs in its own metered run).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from signalloop.config import get_settings  # noqa: E402
from signalloop.db import init_db  # noqa: E402
from signalloop.schemas import Traits  # noqa: E402
from signalloop.services import contacts, icp, lookalike, seeds, traits  # noqa: E402
from signalloop.tools import meter  # noqa: E402

TRAITS_FILE = ROOT / "data" / "traits.json"


def report(run) -> None:
    s = meter.summary(run.run_id)
    print(f"[{run.name}] credits={s['credits']} billable={s['billable_calls']} cached={s['cached_calls']} "
          f"llm_calls={s['llm_calls']} tokens={s['input_tokens']}/{s['output_tokens']} errors={s['errors']}")


def main() -> int:
    init_db()
    step = sys.argv[1]
    print(f"mode={get_settings().SIGNALLOOP_MODE}")
    if step == "seeds":
        with meter.run("find.seeds") as r:
            res = seeds.import_seeds(seeds.parse_csv(str(get_settings().fixtures_dir / "seeds.csv")))
        for a in res.accounts:
            n = a.firmographics_json["normalized"]
            print(f"  {a.domain:22} {n['industry']} | {n['headcount']} staff ({n['headcount_band']}) | "
                  f"{n['funding_stage']} {n['last_funding_date']} | {n['hq_country']} | growth6m={n['headcount_growth_6m_pct']}")
        print("  unresolved:", res.unresolved)
        report(r)
    elif step == "traits":
        with meter.run("find.traits") as r:
            t, stats = traits.extract_traits()
        TRAITS_FILE.parent.mkdir(exist_ok=True)
        TRAITS_FILE.write_text(t.model_dump_json(indent=2), encoding="utf-8")
        for tr in t.traits:
            print(f"  {tr.dimension:22} {tr.values} {tr.coverage:.0%}  {tr.explanation}")
        print("  persona:", t.persona_hypothesis.model_dump(), "| missing:", t.missing)
        report(r)
    elif step == "icp":
        t = Traits.model_validate_json(TRAITS_FILE.read_text(encoding="utf-8")) if TRAITS_FILE.exists() else None
        with meter.run("find.icp") as r:
            row = icp.build_icp(sys.argv[2], t)
            icp.approve_icp(row.id)
        for f in icp.describe_filters(row):
            print(f"  {f['scope']:8} {f['field']:50} {f['type']:4} {f['value']}")
        print("  warnings:", row.filters_json["warnings"])
        print("  preview_count:", row.filters_json["preview_count"], "| rationale:", row.rationale)
        report(r)
    elif step == "lookalike":
        with meter.run("find.lookalike") as r:
            res = lookalike.run_lookalike(icp.latest_approved().id)
        for it in res.iterations:
            print(f"  iter {it['iteration']}: count={it['count']} patch={it.get('patch')} {it.get('error', '')}")
        print(f"  converged={res.converged} fetched={res.fetched} upserted={res.upserted} "
              f"skipped={res.skipped_suppressed}")
        for a in lookalike.list_accounts(10):
            print(f"  {a.fit_score:.2f} {a.domain:28} {a.similarity_reason}")
        report(r)
    elif step == "contacts":
        ids = None
        if len(sys.argv) > 2 and sys.argv[2] == "all":   # staff every account that has no contacts yet
            staffed = {c.account_id for c in contacts.list_contacts()}
            ids = [a.id for a in lookalike.list_accounts() if a.id not in staffed]
            print(f"  staffing {len(ids)} unstaffed accounts")
        with meter.run("find.contacts", budget=400) as r:
            found = contacts.find_contacts(account_ids=ids)
        n = sum(len(v) for v in found.values())
        print(f"  {n} contacts across {sum(1 for v in found.values() if v)}/{len(found)} accounts")
        for c in contacts.list_contacts()[:10]:
            print(f"  {c.role.value:15} {c.name:28} {c.title} ({c.seniority})")
        report(r)
    elif step == "export":
        from record_fixtures import export
        return export(get_settings().fixtures_dir / "crustdata", overwrite=False)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "scripts"))
    raise SystemExit(main())
