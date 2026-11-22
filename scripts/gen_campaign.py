"""Generate a SYNTHETIC campaign results CSV from contacts already in the DB (demo only).

Every outcome in the file is simulated with a seeded RNG, with a planted pattern so the
LEARN step has something real to find:
    * base reply rate                                         ~6%
    * accounts at Series A with 20-50 employees               ~25%
    * VP+ contacts (CXO, Owner / Partner, Vice President)     x1.5 on top (capped)
    * Directors                                                ~3%
Meetings ~40% of replies, opens ~45% (always when replied), unsubscribes ~2% of non-replies.

    python scripts/gen_campaign.py [--out fixtures/campaign_results.csv] [--seed 42]
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlmodel import select  # noqa: E402

from signalloop.db import init_db, session_scope  # noqa: E402
from signalloop.models import Account, Contact  # noqa: E402
from signalloop.services.campaigns import seniority_band  # noqa: E402

BASE, SEGMENT, VP_MULT, DIRECTOR, CAP = 0.06, 0.25, 1.5, 0.03, 0.6
FIELDS = ["email", "domain", "profile_url", "sent_at", "opened", "replied", "meeting", "unsubscribed"]


def reply_probability(stage: str | None, headcount_band: str | None, seniority: str | None) -> float:
    band = seniority_band(seniority)
    if band == "Director":
        return DIRECTOR
    p = SEGMENT if (stage == "series_a" and headcount_band == "20-50") else BASE
    if band == "VP+":
        p = min(p * VP_MULT, CAP)
    return p


def generate(out: Path, seed: int = 42, start: str = "2026-10-01") -> int:
    init_db()
    rng = random.Random(seed)
    t0 = datetime.fromisoformat(start)
    with session_scope() as s:
        rows = s.exec(select(Contact, Account).join(Account, Contact.account_id == Account.id)
                      .where(Account.is_seed == False).order_by(Contact.id)).all()  # noqa: E712
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for contact, account in rows:
            n = (account.firmographics_json or {}).get("normalized", {})
            p = reply_probability(n.get("funding_stage"), n.get("headcount_band"), contact.seniority)
            replied = rng.random() < p
            meeting = replied and rng.random() < 0.4
            opened = replied or rng.random() < 0.45
            unsub = (not replied) and rng.random() < 0.02
            w.writerow({"email": contact.email or "", "domain": account.domain,
                        "profile_url": contact.profile_url or "",
                        "sent_at": (t0 + timedelta(days=rng.randint(0, 20))).date().isoformat(),
                        "opened": int(opened), "replied": int(replied), "meeting": int(meeting),
                        "unsubscribed": int(unsub)})
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "fixtures" / "campaign_results.csv")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    n = generate(args.out, args.seed)
    print(f"wrote {n} synthetic rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
