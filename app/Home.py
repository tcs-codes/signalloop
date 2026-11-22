"""SignalLoop home page (placeholder until Phase 5)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402

from signalloop import db  # noqa: E402
from signalloop.config import get_settings  # noqa: E402
from signalloop.models import ALL_TABLES  # noqa: E402

st.set_page_config(page_title="SignalLoop", layout="wide")
settings = get_settings()

st.title("SignalLoop")
st.caption("FIND → WATCH → ENGAGE → LEARN → update the ICP")

col1, col2, col3 = st.columns(3)
col1.metric("Mode", settings.SIGNALLOOP_MODE)
col2.metric("Strong model", settings.MODEL_STRONG)
col3.metric("Fast model", settings.MODEL_FAST)

try:
    db.init_db()
    present = set(db.table_names())
    missing = [t for t in ALL_TABLES if t not in present]
    if missing:
        st.error(f"DB at `{settings.db_path}` is missing tables: {missing}")
    else:
        st.success(f"DB ready at `{settings.db_path}` ({len(ALL_TABLES)} tables)")
except Exception as exc:  # pragma: no cover - UI only
    st.error(f"DB error: {exc}")

keys = {
    "Anthropic key": bool(settings.ANTHROPIC_API_KEY),
    "Crustdata key": bool(settings.CRUSTDATA_API_KEY),
}
st.write({k: ("set" if v else "missing") for k, v in keys.items()})
