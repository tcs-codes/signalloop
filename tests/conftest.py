"""Shared fixtures: every test runs in mock mode on a fresh temp SQLite DB, with no network."""
from __future__ import annotations

import httpx
import pytest

from signalloop import config, db, llm

_REAL_SEND = httpx.Client.send


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALLOOP_MODE", "mock")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("CRUSTDATA_API_KEY", "")
    monkeypatch.setenv("OUTREACH_PARALLELISM", "1")   # FakeLLM response lists are order-dependent
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
def allow_mock_transport(monkeypatch):
    """Re-enable httpx.Client.send for tests that use httpx.MockTransport (still no network)."""
    monkeypatch.setattr(httpx.Client, "send", _REAL_SEND)


@pytest.fixture(autouse=True)
def _reset_crustdata_client():
    from signalloop.tools import crustdata

    crustdata.set_client(None)
    yield
    crustdata.set_client(None)


@pytest.fixture
def fake_llm():
    fake = llm.FakeLLM()
    llm.set_backend(fake)
    return fake
