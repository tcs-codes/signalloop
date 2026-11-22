from signalloop import db
from signalloop.models import ALL_TABLES


def test_init_db_creates_all_tables():
    names = set(db.table_names())
    assert set(ALL_TABLES) <= names, f"missing: {set(ALL_TABLES) - names}"
