"""demo_reset on temp DBs only (never the dev DB); LLM misses are refused, not called live."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from signalloop import db, llm

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("demo_reset", ROOT / "scripts" / "demo_reset.py")
demo_reset = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo_reset)


class Refuse:
    def complete(self, **kw):
        raise RuntimeError(f"replay miss: {kw['prompt_name']}")


def _replay_only():
    return llm.ReplayBackend(Refuse(), ROOT / "fixtures" / "llm", "replay")


def test_setup_stage_gives_empty_db(tmp_path):
    out = demo_reset.reset(tmp_path / "demo.db", "setup", llm_backend=_replay_only())
    assert out["stage"] == "setup"
    con = sqlite3.connect(str(tmp_path / "demo.db"))
    assert con.execute("select count(*) from account").fetchone()[0] == 0
    con.close()


def test_watch_stage_replays_find_and_baseline_scan(tmp_path):
    out = demo_reset.reset(tmp_path / "demo.db", "watch", llm_backend=_replay_only())
    assert out["seeds"] == 5 and out["icp_version"] == 1 and out["accounts"] == 150
    assert out["contacts"] == 60 and out["held_back"] == 3 and out["scan_errors"] == []
    assert out["baseline_signals"] > 100
    # day 2 then releases exactly the 3 held-back events
    from signalloop.services import watch
    assert len(watch.daily_scan(day2=True).new_signals) == 3


def test_refuses_db_with_unexported_cache_rows(tmp_path):
    p = tmp_path / "rec.db"
    con = sqlite3.connect(str(p))
    con.execute("create table api_cache (key varchar primary key)")
    con.execute("insert into api_cache values ('ffffffffffffffffdeadbeef')")
    con.commit()
    con.close()
    with pytest.raises(SystemExit, match="refusing to wipe"):
        demo_reset.reset(p, "setup", llm_backend=_replay_only())
    assert p.exists()                                           # untouched
    demo_reset.reset(p, "setup", force=True, llm_backend=_replay_only())


@pytest.fixture(autouse=True)
def _restore_engine():
    yield
    if db._engine is not None:
        db.get_engine().dispose()
