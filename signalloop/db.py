"""SQLite engine/session helpers."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import event, inspect
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from signalloop import models  # noqa: F401  (registers tables on SQLModel.metadata)
from signalloop.config import get_settings

_engine: Engine | None = None


def _make_engine(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30}
    )

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return eng


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _make_engine(get_settings().db_path)
    return _engine


def set_engine(engine: Engine | None) -> None:
    """Override the engine (tests) or reset it (None)."""
    global _engine
    _engine = engine


def init_db(engine: Engine | None = None) -> Engine:
    eng = engine or get_engine()
    SQLModel.metadata.create_all(eng)
    return eng


def drop_db(engine: Engine | None = None) -> None:
    SQLModel.metadata.drop_all(engine or get_engine())


def table_names(engine: Engine | None = None) -> list[str]:
    return inspect(engine or get_engine()).get_table_names()


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as s:
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
