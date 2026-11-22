"""SYNTHETIC campaign outcomes over contacts already in the DB (demo only; see scripts/gen_campaign.py).

Every outcome is simulated with a seeded RNG, with a planted pattern so LEARN has something to find:
    * base reply rate                                         ~6%
    * accounts at Series A with 20-50 employees               ~25%
    * VP+ contacts (CXO, Owner / Partner, Vice President)     x1.5 on top (capped)
    * Directors                                                ~3%
Meetings ~40% of replies, opens ~45% (always when replied), unsubscribes ~2% of non-replies.
"""
from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import select

from signalloop.config import PROJECT_ROOT
from signalloop.db import init_db, session_scope
from signalloop.models import Account, Contact
from signalloop.services.campaigns import seniority_band

BASE, SEGMENT, VP_MULT, DIRECTOR, CAP = 0.06, 0.25, 1.5, 0.03, 0.6
FIELDS = ["email", "domain", "profile_url", "sent_at", "opened", "replied", "meeting", "unsubscribed"]
DEFAULT_CSV = PROJECT_ROOT / "fixtures" / "campaign_results.csv"
DEFAULT_NAME = "Q4 outbound (synthetic demo)"
# Planted pattern was the top insight for seeds 1-10 on the recorded 326 contacts (lift
# 2.6-4.5x, n=39); seed 9 (3.62x, Directors 0.24x) is a typical one and the recorded demo.
DEFAULT_SEED = 9


def reply_probability(stage: str | None, headcount_band: str | None, seniority: str | None) -> float:
    band = seniority_band(seniority)
    if band == "Director":
        return DIRECTOR
    p = SEGMENT if (stage == "series_a" and headcount_band == "20-50") else BASE
    if band == "VP+":
        p = min(p * VP_MULT, CAP)
    return p


def _person_key(contact: Contact) -> str:
    return contact.crustdata_person_id or contact.profile_url or contact.email or f"id:{contact.id}"


def generate(out: Path = DEFAULT_CSV, seed: int = DEFAULT_SEED, start: str = "2026-10-01") -> int:
    """Each person's outcome comes from an RNG seeded by (seed, their Crustdata person id), and
    rows are sorted by that id, so the same people give the same CSV whatever order the DB
    created them in (the demo DB staffs accounts in a different order than the recording)."""
    init_db()
    t0 = datetime.fromisoformat(start)
    with session_scope() as s:
        rows = s.exec(select(Contact, Account).join(Account, Contact.account_id == Account.id)
                      .where(Account.is_seed == False)).all()  # noqa: E712
    rows = sorted(rows, key=lambda r: _person_key(r[0]))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for contact, account in rows:
            rng = random.Random(f"{seed}:{_person_key(contact)}")
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
