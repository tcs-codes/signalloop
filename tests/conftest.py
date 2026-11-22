"""Shared fixtures: every test runs in mock mode on a fresh temp SQLite DB, with no network."""
from __future__ import annotations

import httpx
import pytest

from signalloop import config, db, llm


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALLOOP_MODE", "mock")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("CRUSTDATA_API_KEY", "")
    config.reset_settings()
    db.set_engine(None)
    db.init_db()
    llm.set_backend(None)
    yield
    db.get_engine().dispose()
    db.set_engine(None)
    llm.set_backend(None)
    config.reset_settings()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise RuntimeError("network access is disabled in tests")

    monkeypatch.setattr(httpx.Client, "send", _blocked)
    monkeypatch.setattr(httpx.AsyncClient, "send", _blocked)


@pytest.fixture
def fake_llm():
    fake = llm.FakeLLM()
    llm.set_backend(fake)
    return fake
