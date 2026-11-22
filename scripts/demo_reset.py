"""Reset a demo database (mock mode) so the 5-minute demo runs from recorded fixtures.

    python scripts/demo_reset.py                 # data/demo.db, stage "setup"
    python scripts/demo_reset.py --stage watch   # FIND already done + baseline scan
    python scripts/demo_reset.py --db data/other.db [--force]

Then run the app on that DB:
    $env:DB_PATH="data/demo.db"; .\\.venv\\Scripts\\streamlit.exe run app/Home.py

Stages
  setup  empty DB; open the Setup page, the seed list and ICP description are pre-filled.
  watch  FIND replayed (5 seeds, ICP v1 approved, ~150 lookalikes, contacts) and the baseline
         daily scan done; Home -> "Run daily scan now" releases the 3 day-2 accounts.

Safety: refuses to wipe a DB whose api_cache holds responses that are not exported to
fixtures/crustdata (e.g. the recording DB) unless --force. Always mock mode.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "demo.db"
STAGES = ("setup", "watch")


def unexported_cache_rows(db_path: Path, fixtures: Path) -> int:
    """How many cached Crustdata responses in ``db_path`` have no fixture file."""
    if not db_path.exists():
        return 0
    have = {p.name.split("__", 1)[1][:16] for p in fixtures.glob("*__*.json")}
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute("SELECT key FROM api_cache").fetchall()
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()
    return sum(1 for (k,) in rows if k[:16] not in have)


def wipe(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()


def reset(db_path: Path, stage: str = "setup", *, force: bool = False, llm_backend=None) -> dict:
    """Wipe ``db_path`` and prepare ``stage``. ``llm_backend`` overrides the LLM (tests)."""
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    db_path = Path(db_path).resolve()
    fixtures = ROOT / "fixtures" / "crustdata"
    n = unexported_cache_rows(db_path, fixtures)
    if n and not force:
        raise SystemExit(f"refusing to wipe {db_path}: {n} cached API responses are not exported to "
                         f"fixtures (run scripts/record_fixtures.py export first, or pass --force)")

    os.environ["DB_PATH"] = str(db_path)
    os.environ["SIGNALLOOP_MODE"] = "mock"
    sys.path.insert(0, str(ROOT))
    from signalloop import config, db, llm
    from signalloop.tools import crustdata

    config.reset_settings()
    db.get_engine().dispose() if db._engine is not None else None
    db.set_engine(None)
    wipe(db_path)
    db.init_db()
    crustdata.set_client(crustdata.CrustdataClient(mode="mock"))
    if llm_backend is not None:
        llm.set_backend(llm_backend)
    else:
        llm.set_backend(None)            # default: replay cache (mock mode)

    out = {"db": str(db_path), "stage": stage}
    if stage == "watch":
        from signalloop.services import contacts, icp as icp_svc, lookalike, seeds, traits, watch
        rec = json.loads((ROOT / "fixtures" / "demo" / "find_recording.json").read_text(encoding="utf-8"))
        s = config.get_settings()
        imp = seeds.import_seeds(seeds.parse_csv(str(s.fixtures_dir / "seeds.csv")))
        t, _ = traits.extract_traits(imp.accounts)
        row = icp_svc.build_icp(rec["icp_description"], t)
        icp_svc.approve_icp(row.id)
        la = lookalike.run_lookalike(row.id)
        found = contacts.find_contacts()
        scan = watch.daily_scan()
        out.update(seeds=len(imp.accounts), icp_version=row.version, accounts=la.upserted,
                   contacts=sum(len(v) for v in found.values()), baseline_signals=len(scan.new_signals),
                   held_back=scan.held_back, scan_errors=scan.errors)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--stage", choices=STAGES, default="setup")
    ap.add_argument("--force", action="store_true", help="wipe even if the DB has unexported cache rows")
    args = ap.parse_args()
    out = reset(args.db, args.stage, force=args.force)
    print(json.dumps(out, indent=2))
    print(f'\nNext: $env:DB_PATH="{out["db"]}"; .\\.venv\\Scripts\\streamlit.exe run app/Home.py')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
