from signalloop import db
from signalloop.models import ALL_TABLES


def test_init_db_adds_columns_missing_from_older_files(tmp_path):
    from sqlalchemy import create_engine, inspect

    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as c:
        c.exec_driver_sql("CREATE TABLE api_cache (key VARCHAR PRIMARY KEY, endpoint VARCHAR, "
                          "request_json JSON, response_json JSON, created_at DATETIME)")
    db.init_db(eng)
    assert "credits" in {col["name"] for col in inspect(eng).get_columns("api_cache")}


def test_init_db_creates_all_tables():
    names = set(db.table_names())
    assert set(ALL_TABLES) <= names, f"missing: {set(ALL_TABLES) - names}"
