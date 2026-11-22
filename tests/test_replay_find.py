"""Replay the recorded live FIND run from repo fixtures: fresh DB, mock mode, no network.

Proves every Crustdata request the services make is covered by a recorded fixture, and that
the demo can run without credits. LLM outputs come from fixtures/demo/find_recording.json.
"""
import json
from pathlib import Path

import pytest

from signalloop.config import get_settings
from signalloop.models import IcpStatus
from signalloop.schemas import ICP, Traits
from signalloop.services import contacts, icp as icp_svc, lookalike, seeds
from signalloop.tools import crustdata, meter

REC = Path(__file__).resolve().parents[1] / "fixtures" / "demo" / "find_recording.json"
pytestmark = pytest.mark.skipif(not REC.exists(), reason="no recording")


def test_find_flow_replays_from_fixtures(fake_llm):
    rec = json.loads(REC.read_text(encoding="utf-8"))
    crustdata.set_client(crustdata.CrustdataClient(mode="mock", fixtures_dir=get_settings().fixtures_dir / "crustdata"))
    fake_llm.responses["tune_filters"] = rec["tune_patches"]

    with meter.run("replay", budget=10_000) as run:
        res = seeds.import_seeds(seeds.parse_csv(str(get_settings().fixtures_dir / "seeds.csv")))
        assert len(res.accounts) == 5 and res.unresolved == []

        row = icp_svc.create_version(ICP.model_validate(rec["icp"]), description=rec["icp_description"],
                                     traits=Traits.model_validate(rec["traits"]).model_dump(),
                                     source="seed", status=IcpStatus.approved)
        assert row.filters_json["preview_count"] == rec["expected"]["preview_count"]

        out = lookalike.run_lookalike(row.id)
        assert out.count == rec["expected"]["tuned_count"]
        assert out.fetched == rec["expected"]["fetched"] == 150

        found = contacts.find_contacts()
        cs = [c for v in found.values() for c in v]
        assert len(cs) == 60
        champs = [c for c in cs if c.role.value == "champion"]
        assert len(champs) >= 25 and all(contacts._GTM.search(c.title) for c in champs)

    s = meter.summary(run.run_id)
    assert s["errors"] == 0
    assert s["credits"] > 150          # recorded live cost is replayed, not re-spent
