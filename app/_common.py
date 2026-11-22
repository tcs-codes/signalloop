"""Shared page bootstrap: import path, DB init, mode banner, error helper."""
from __future__ import annotations

import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from signalloop import db  # noqa: E402
from signalloop.config import get_settings  # noqa: E402


def setup(title: str) -> None:
    st.set_page_config(page_title=f"SignalLoop · {title}", layout="wide")
    db.init_db()
    s = get_settings()
    st.title(title)
    st.caption(f"Mode: **{s.SIGNALLOOP_MODE}** · credit budget per run: {s.CREDIT_BUDGET_PER_RUN:g}")


def link(page: str, label: str) -> None:
    """st.page_link, falling back to plain text when the page isn't registered
    (e.g. a page script run on its own instead of via Home.py)."""
    try:
        st.page_link(page, label=label)
    except Exception:
        name = Path(page).stem.split("_", 1)[-1].replace("_", " ")
        st.markdown(f"{label} (open **{name}** in the sidebar)")


@contextmanager
def guarded(action: str):
    """Show errors in the page instead of crashing it."""
    try:
        yield
    except Exception as exc:  # pragma: no cover - UI only
        st.error(f"{action} failed: {type(exc).__name__}: {exc}")
        with st.expander("Details"):
            st.code(traceback.format_exc())
