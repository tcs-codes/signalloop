"""Current run context, shared by the meter (F5), the Crustdata client and llm.py."""
from __future__ import annotations

from contextvars import ContextVar

current_run_id: ContextVar[str | None] = ContextVar("current_run_id", default=None)
current_agent: ContextVar[str] = ContextVar("current_agent", default="")
