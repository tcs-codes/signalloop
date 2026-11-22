import pytest
from pydantic import BaseModel

from signalloop import config, llm


class Echo(BaseModel):
    answer: str


@pytest.fixture
def prompt_dir(tmp_path, monkeypatch):
    (tmp_path / "echo.md").write_text("Q: {{q}}")
    monkeypatch.setattr(llm, "PROMPTS_DIR", tmp_path)
    return tmp_path


def test_record_then_replay_is_deterministic(prompt_dir, tmp_path, fake_llm):
    fake_llm.responses["echo"] = [{"answer": "first"}, {"answer": "second"}]
    cache = tmp_path / "llm"
    llm.set_backend(llm.ReplayBackend(fake_llm, cache, "replay"))
    a = llm.call_llm("echo", {"q": "x"}, Echo)          # miss -> inner, recorded
    b = llm.call_llm("echo", {"q": "x"}, Echo)          # hit -> same output, inner not called
    c = llm.call_llm("echo", {"q": "y"}, Echo)          # different prompt -> miss
    assert a.answer == b.answer == "first" and c.answer == "second"
    assert len(fake_llm.calls) == 2 and len(list(cache.glob("echo__*.json"))) == 2


def test_record_mode_always_calls_inner(prompt_dir, tmp_path, fake_llm):
    fake_llm.responses["echo"] = [{"answer": "1"}, {"answer": "2"}]
    llm.set_backend(llm.ReplayBackend(fake_llm, tmp_path / "llm", "record"))
    assert llm.call_llm("echo", {"q": "x"}, Echo).answer == "1"
    assert llm.call_llm("echo", {"q": "x"}, Echo).answer == "2"      # re-recorded


def test_replayed_calls_cost_nothing(prompt_dir, tmp_path, fake_llm):
    from signalloop.tools import meter
    fake_llm.responses["echo"] = {"answer": "z"}
    llm.set_backend(llm.ReplayBackend(fake_llm, tmp_path / "llm", "replay"))
    llm.call_llm("echo", {"q": "x"}, Echo)
    with meter.run("replay") as r:
        llm.call_llm("echo", {"q": "x"}, Echo)
    s = meter.summary(r.run_id)
    assert s["llm_calls"] == 1 and s["input_tokens"] == 0 and s["output_tokens"] == 0


@pytest.mark.parametrize("mode,setting,wrapped", [
    ("mock", "auto", True), ("live", "auto", False), ("live", "record", True), ("mock", "off", False)])
def test_default_backend_selection(monkeypatch, mode, setting, wrapped):
    monkeypatch.setenv("SIGNALLOOP_MODE", mode)
    monkeypatch.setenv("LLM_REPLAY", setting)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config.reset_settings()
    llm.set_backend(None)
    assert isinstance(llm.get_backend(), llm.ReplayBackend) == wrapped
