import pytest
from pydantic import BaseModel
from sqlmodel import select

from signalloop import llm
from signalloop.db import session_scope
from signalloop.models import RunLog


class Echo(BaseModel):
    answer: str
    score: int


@pytest.fixture
def prompt_dir(tmp_path, monkeypatch):
    (tmp_path / "echo.md").write_text('Question: {{question}}\nContext: {{ctx}}\nReply as {"answer":..}')
    monkeypatch.setattr(llm, "PROMPTS_DIR", tmp_path)
    return tmp_path


def test_render_fills_variables_and_keeps_json_braces(prompt_dir):
    out = llm.render_prompt("echo", {"question": "hi", "ctx": {"a": 1}})
    assert "Question: hi" in out and '"a": 1' in out and '{"answer":..}' in out


def test_render_missing_variable_raises(prompt_dir):
    with pytest.raises(llm.PromptError):
        llm.render_prompt("echo", {"question": "hi"})


def test_call_llm_validates_and_logs(prompt_dir, fake_llm):
    fake_llm.responses["echo"] = {"answer": "ok", "score": 3}
    out = llm.call_llm("echo", {"question": "q", "ctx": ""}, Echo, model="m")
    assert out == Echo(answer="ok", score=3)
    with session_scope() as s:
        rows = s.exec(select(RunLog)).all()
    assert len(rows) == 1 and rows[0].ok and rows[0].agent == "echo"


def test_call_llm_retries_once_then_succeeds(prompt_dir, fake_llm):
    fake_llm.responses["echo"] = ["not json", '```json\n{"answer":"fixed","score":1}\n```']
    out = llm.call_llm("echo", {"question": "q", "ctx": ""}, Echo)
    assert out.answer == "fixed"
    assert len(fake_llm.calls) == 2
    # retry carries the validation error back to the model
    assert "failed validation" in fake_llm.calls[1]["messages"][-1]["content"]


def test_call_llm_raises_after_second_failure(prompt_dir, fake_llm):
    fake_llm.responses["echo"] = [{"answer": "x"}, {"answer": "y"}]  # missing score
    with pytest.raises(llm.LLMValidationError):
        llm.call_llm("echo", {"question": "q", "ctx": ""}, Echo)
    assert len(fake_llm.calls) == 2


def test_semantic_validator_triggers_retry(prompt_dir, fake_llm):
    fake_llm.responses["echo"] = [{"answer": "x", "score": 99}, {"answer": "x", "score": 5}]

    def check(e: Echo):
        if e.score > 10:
            raise ValueError("score must be <= 10")

    assert llm.call_llm("echo", {"question": "q", "ctx": ""}, Echo, validator=check).score == 5
