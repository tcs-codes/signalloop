"""Seed fixtures/llm with the LLM outputs of the recorded FIND run, keyed by exact prompt.

Runs on a throwaway temp DB in mock mode (never the dev DB), then verifies on a second
fresh temp DB that the whole FIND flow replays with NO live LLM call.

    python scripts/seed_llm_replay.py

Uses fixtures/demo/find_recording.json (traits, ICP spec, tuning patches, description).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="sl_seed_llm_"))
os.environ["DB_PATH"] = str(TMP / "seed.db")          # never the dev DB
os.environ["SIGNALLOOP_MODE"] = "mock"
os.environ["LLM_REPLAY"] = "off"                     # we install the backend ourselves
sys.path.insert(0, str(ROOT))

from signalloop import config, db, llm  # noqa: E402
from signalloop.models import IcpStatus  # noqa: E402
from signalloop.services import contacts, icp as icp_svc, lookalike, seeds, traits  # noqa: E402
from signalloop.tools import crustdata  # noqa: E402

REC = ROOT / "fixtures" / "demo" / "find_recording.json"
LLM_DIR = ROOT / "fixtures" / "llm"


class Refuse:
    def complete(self, **kw):
        raise RuntimeError(f"replay miss for prompt '{kw['prompt_name']}' (would call the live LLM)")


def run_find(desc: str) -> dict:
    s = config.get_settings()
    res = seeds.import_seeds(seeds.parse_csv(str(s.fixtures_dir / "seeds.csv")))
    t, _ = traits.extract_traits(res.accounts)
    row = icp_svc.build_icp(desc, t)
    icp_svc.approve_icp(row.id)
    la = lookalike.run_lookalike(row.id)
    found = contacts.find_contacts()
    return {"seeds": len(res.accounts), "preview": row.filters_json["preview_count"], "count": la.count,
            "fetched": la.fetched, "contacts": sum(len(v) for v in found.values())}


def fresh_db(name: str) -> None:
    os.environ["DB_PATH"] = str(TMP / name)
    config.reset_settings()
    db.set_engine(None)
    db.init_db()
    crustdata.set_client(crustdata.CrustdataClient(mode="mock"))


def main() -> int:
    rec = json.loads(REC.read_text(encoding="utf-8"))
    desc = rec["icp_description"]
    fake = llm.FakeLLM({"extract_traits": rec["traits"], "icp_to_filters": rec["icp"],
                        "tune_filters": rec["tune_patches"]})

    fresh_db("seed.db")
    llm.set_backend(llm.ReplayBackend(fake, LLM_DIR, "record"))
    seeded = run_find(desc)
    print("seeded :", seeded, "| prompts recorded:", sorted({c["prompt_name"] for c in fake.calls}))

    fresh_db("verify.db")
    llm.set_backend(llm.ReplayBackend(Refuse(), LLM_DIR, "replay"))
    verified = run_find(desc)
    print("replay :", verified)
    exp = rec["expected"]
    ok = (verified == seeded and verified["preview"] == exp["preview_count"]
          and verified["count"] == exp["tuned_count"] and verified["fetched"] == exp["fetched"])
    print("OK: FIND replays with no live LLM call" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
