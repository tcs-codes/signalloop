"""F12 CLI: ``python -m signalloop.jobs.daily_scan --icp latest [--day2]``"""
from __future__ import annotations

import argparse

from signalloop.db import init_db
from signalloop.services import icp as icp_svc
from signalloop.services import watch
from signalloop.tools import meter


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--icp", default="latest", help="ICP version id, or 'latest' (approved)")
    ap.add_argument("--day2", action="store_true", help="release the demo day-2 events")
    args = ap.parse_args(argv)
    init_db()
    icp_id = None if args.icp == "latest" else int(args.icp)
    if icp_id is None and icp_svc.latest_approved() is None:
        print("no approved ICP")
        return 1
    with meter.run("watch.daily_scan") as run:
        res = watch.daily_scan(icp_id, day2=args.day2)
    print(f"as_of={res.as_of} watched={res.watched} new_signals={len(res.new_signals)} "
          f"held_back={res.held_back} new_accounts={res.new_accounts} credits={run.credits:g}")
    for e in res.errors:
        print("  error:", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
