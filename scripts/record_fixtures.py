"""Turn live Crustdata responses into mock fixtures.

Workflow:
  1. Set SIGNALLOOP_MODE=live and run the pipeline steps you want to record (live
     responses are written to the `api_cache` table).
  2. `python scripts/record_fixtures.py export` writes every cached response to
     fixtures/crustdata/<endpoint>__<key16>.json, which mock mode then serves.

Other commands:
  credits    Show the account credit balance (free endpoint, live call).
  endpoints  Show your account's endpoint access, rate limits and prices (free).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import select  # noqa: E402

from signalloop.config import get_settings  # noqa: E402
from signalloop.db import init_db, session_scope  # noqa: E402
from signalloop.models import ApiCache  # noqa: E402
from signalloop.tools.crustdata import PATHS, CrustdataClient  # noqa: E402

PATH_TO_NAME = {v: k for k, v in PATHS.items()}


def export(out_dir: Path, overwrite: bool) -> int:
    init_db()
    out_dir.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    with session_scope() as s:
        rows = s.exec(select(ApiCache)).all()
    for row in rows:
        name = PATH_TO_NAME.get(row.endpoint)
        if name is None:
            continue
        p = out_dir / f"{name}__{row.key[:16]}.json"
        if p.exists() and not overwrite:
            skipped += 1
            continue
        p.write_text(json.dumps({"request": row.request_json, "endpoint": row.endpoint,
                                 "credits": row.credits, "response": row.response_json}, indent=2),
                     encoding="utf-8")
        written += 1
    print(f"exported {written} fixtures to {out_dir} ({skipped} already existed)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--out", type=Path, default=get_settings().fixtures_dir / "crustdata")
    e.add_argument("--overwrite", action="store_true")
    sub.add_parser("credits")
    sub.add_parser("endpoints")
    args = ap.parse_args()

    if args.cmd == "export":
        return export(args.out, args.overwrite)
    client = CrustdataClient(mode="live")
    data = client.account_credits() if args.cmd == "credits" else client.account_endpoints()
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
