"""Structured LLM calls.

Every call:
  1. loads ``signalloop/prompts/{prompt_name}.md`` and fills ``{{variable}}`` placeholders,
  2. asks for JSON only (the schema's JSON Schema goes in the system prompt),
  3. validates the reply with the Pydantic ``schema``,
  4. on a validation error retries once with the error message, then raises,
  5. logs model, tokens and latency to ``run_log``.

Tests swap the backend for ``FakeLLM`` (canned JSON keyed by prompt name).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from signalloop.config import get_settings
from signalloop.runctx import current_agent, current_run_id

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
T = TypeVar("T", bound=BaseModel)

# Models that accept Anthropic's server-side refusal fallback (`fallbacks: "default"`).
_FALLBACK_MODELS = {
    "claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5",
    "claude-fable-5-2", "claude-fable-5-1",
}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_TEMPLATE = """You are a component of SignalLoop, a B2B go-to-market agent.
Return ONLY a single JSON object that matches the JSON Schema below. No prose, no markdown fences.
Use only facts present in the inputs. If information is missing, say so in a `missing` field
(when the schema has one) instead of inventing it.

JSON Schema:
{schema}"""


class LLMError(RuntimeError):
    pass


class LLMValidationError(LLMError):
    def __init__(self, prompt_name: str, error: str, raw: str):
        super().__init__(f"{prompt_name}: output failed validation after retry: {error}")
        self.prompt_name, self.error, self.raw = prompt_name, error, raw


class LLMRefusal(LLMError):
    pass


class PromptError(LLMError):
    pass


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


class LLMBackend(Protocol):
    def complete(
        self, *, prompt_name: str, model: str, system: str,
        messages: list[dict[str, Any]], variables: dict[str, Any],
    ) -> LLMResult: ...


# ---------------------------------------------------------------- backends
class AnthropicBackend:
    def __init__(self, api_key: str | None = None):
        import anthropic

        settings = get_settings()
        key = api_key or settings.ANTHROPIC_API_KEY or None
        self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        self._max_tokens = settings.LLM_MAX_TOKENS
        self._fallback = settings.LLM_REFUSAL_FALLBACK

    def complete(self, *, prompt_name, model, system, messages, variables) -> LLMResult:
        kwargs: dict[str, Any] = dict(
            model=model, max_tokens=self._max_tokens, system=system, messages=messages
        )
        if self._fallback and model in _FALLBACK_MODELS:
            resp = self._client.beta.messages.create(
                betas=[_FALLBACK_BETA], fallbacks="default", **kwargs
            )
        else:
            resp = self._client.messages.create(**kwargs)

        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMRefusal(f"{prompt_name}: model declined ({details})")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if resp.stop_reason == "max_tokens":
            log.warning("%s hit max_tokens; output may be truncated", prompt_name)
        return LLMResult(
            text=text,
            input_tokens=resp.usage.input_tokens or 0,
            output_tokens=resp.usage.output_tokens or 0,
            model=resp.model,
        )


Canned = Any  # dict | str | BaseModel | list[...] | Callable[[dict], Any]


@dataclass
class FakeLLM:
    """Test backend. ``responses[prompt_name]`` may be a dict/str/BaseModel, a list
    (consumed in order, last item repeats), or a callable ``f(variables) -> value``."""

    responses: dict[str, Canned] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    _cursor: dict[str, int] = field(default_factory=dict)

    def complete(self, *, prompt_name, model, system, messages, variables) -> LLMResult:
        self.calls.append(
            {"prompt_name": prompt_name, "model": model, "messages": messages,
             "variables": variables}
        )
        if prompt_name not in self.responses:
            raise LLMError(f"FakeLLM has no canned response for prompt '{prompt_name}'")
        value = self.responses[prompt_name]
        if isinstance(value, list):
            i = self._cursor.get(prompt_name, 0)
            self._cursor[prompt_name] = i + 1
            value = value[min(i, len(value) - 1)]
        if callable(value) and not isinstance(value, BaseModel):
            value = value(variables)
        if isinstance(value, BaseModel):
            text = value.model_dump_json()
        elif isinstance(value, str):
            text = value
        else:
            text = json.dumps(value)
        return LLMResult(text=text, input_tokens=len(str(messages)) // 4,
                         output_tokens=len(text) // 4, model=f"fake:{model}")


_backend: LLMBackend | None = None


def get_backend() -> LLMBackend:
    global _backend
    if _backend is None:
        _backend = AnthropicBackend()
    return _backend


def set_backend(backend: LLMBackend | None) -> None:
    global _backend
    _backend = backend


# ---------------------------------------------------------------- prompts
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _fmt(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json(indent=2)
    return json.dumps(value, indent=2, default=str)


def render_prompt(prompt_name: str, variables: dict[str, Any]) -> str:
    path = PROMPTS_DIR / f"{prompt_name}.md"
    if not path.exists():
        raise PromptError(f"prompt file not found: {path}")
    template = path.read_text(encoding="utf-8")
    missing = sorted({m for m in _PLACEHOLDER.findall(template) if m not in variables})
    if missing:
        raise PromptError(f"{prompt_name}: missing variables {missing}")
    return _PLACEHOLDER.sub(lambda m: _fmt(variables[m.group(1)]), template)


def extract_json(text: str) -> str:
    """Strip markdown fences / surrounding prose and return the outermost JSON object."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if fence:
        t = fence.group(1).strip()
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        return t[start : end + 1]
    return t


# ---------------------------------------------------------------- run_log
def _log_call(*, agent: str, model: str, inp: int, out: int, latency_ms: int,
              ok: bool, error: str = "") -> None:
    try:
        from signalloop.db import session_scope
        from signalloop.models import RunLog

        with session_scope() as s:
            s.add(RunLog(run_id=current_run_id.get(), agent=agent, endpoint_or_model=model,
                         input_tokens=inp, output_tokens=out, latency_ms=latency_ms,
                         ok=ok, error=error[:2000]))
    except Exception as exc:  # logging must never break the call
        log.warning("run_log write failed: %s", exc)


# ---------------------------------------------------------------- main entry
def call_llm(
    prompt_name: str,
    variables: dict[str, Any],
    schema: type[T],
    model: str | None = None,
    agent: str | None = None,
    validator: Callable[[T], None] | None = None,
) -> T:
    """Render prompt, call the model, validate against ``schema``; retry once on failure.

    ``validator`` may raise ``ValueError`` for semantic checks beyond the schema
    (e.g. "every number must appear in the table"); it gets the same single retry.
    """
    model = model or get_settings().MODEL_STRONG
    agent = agent or current_agent.get() or prompt_name
    system = SYSTEM_TEMPLATE.format(schema=json.dumps(schema.model_json_schema(), indent=1))
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": render_prompt(prompt_name, variables)}
    ]
    backend = get_backend()

    last_error, last_raw = "", ""
    for attempt in range(2):
        t0 = time.perf_counter()
        try:
            result = backend.complete(prompt_name=prompt_name, model=model, system=system,
                                      messages=messages, variables=variables)
        except Exception as exc:
            _log_call(agent=agent, model=model, inp=0, out=0,
                      latency_ms=int((time.perf_counter() - t0) * 1000), ok=False,
                      error=f"{type(exc).__name__}: {exc}")
            raise
        latency = int((time.perf_counter() - t0) * 1000)
        last_raw = result.text
        try:
            parsed = schema.model_validate_json(extract_json(result.text))
            if validator is not None:
                validator(parsed)
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)
            _log_call(agent=agent, model=result.model or model, inp=result.input_tokens,
                      out=result.output_tokens, latency_ms=latency, ok=False,
                      error=f"validation (attempt {attempt + 1}): {last_error}")
            messages = messages + [
                {"role": "assistant", "content": result.text or "(empty)"},
                {"role": "user", "content": (
                    "Your previous reply failed validation:\n" + last_error[:4000] +
                    "\n\nReturn ONLY the corrected JSON object.")},
            ]
            continue
        _log_call(agent=agent, model=result.model or model, inp=result.input_tokens,
                  out=result.output_tokens, latency_ms=latency, ok=True)
        return parsed

    raise LLMValidationError(prompt_name, last_error, last_raw)
