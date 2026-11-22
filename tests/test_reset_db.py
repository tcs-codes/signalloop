from sqlmodel import select

from signalloop import db
from signalloop.db import session_scope
from signalloop.models import ALL_TABLES, Account
from signalloop.services import store
from tests.fakes import company


def test_reset_db_empties_tables_in_place():
    store.upsert_account(company("a.io", 1))
    with session_scope() as s:
        assert s.exec(select(Account)).first() is not None
    db.reset_db()
    assert set(ALL_TABLES) <= set(db.table_names())
    with session_scope() as s:
        assert s.exec(select(Account)).first() is None
