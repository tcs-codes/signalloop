"""Credits (F5): Crustdata credits and LLM tokens per run and per agent."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from sqlmodel import select  # noqa: E402

from _common import guarded, setup  # noqa: E402
from signalloop.config import get_settings  # noqa: E402
from signalloop.db import session_scope  # noqa: E402
from signalloop.models import RunLog  # noqa: E402
from signalloop.tools.crustdata import CrustdataClient  # noqa: E402

setup("Credits")
cfg = get_settings()

with session_scope() as s:
    rows = s.exec(select(RunLog)).all()
if not rows:
    st.info("No calls logged yet.")
    st.stop()

df = pd.DataFrame([r.model_dump() for r in rows])
df["kind"] = df["endpoint_or_model"].map(lambda e: "crustdata" if str(e).startswith("/") else "llm")
df["run_id"] = df["run_id"].fillna("(no run)")


def llm_cost(r) -> float:
    model = str(r["endpoint_or_model"]).replace("fake:", "")
    pin, pout = cfg.LLM_PRICES.get(model, (0.0, 0.0))
    return (r["input_tokens"] * pin + r["output_tokens"] * pout) / 1e6


df["llm_usd"] = df.apply(lambda r: llm_cost(r) if r["kind"] == "llm" else 0.0, axis=1)
cd = df[df["kind"] == "crustdata"]
billable, cached = int((~cd["cached"]).sum()), int(cd["cached"].sum())

m = st.columns(5)
m[0].metric("Crustdata credits", f"{cd['credits'].sum():,.2f}")
m[1].metric("Billable / cached calls", f"{billable} / {cached}")
m[2].metric("Cache hit rate", f"{cached / (billable + cached):.0%}" if billable + cached else "n/a")
m[3].metric("LLM tokens (in / out)", f"{int(df['input_tokens'].sum()):,} / {int(df['output_tokens'].sum()):,}")
m[4].metric("LLM cost (est.)", f"${df['llm_usd'].sum():.2f}")
if cfg.is_mock:
    st.caption("Mock mode: Crustdata calls are served from fixtures; credits shown are what the recorded live "
               "calls cost. A cached replay costs 0.")

st.subheader("By run")
runs = df.groupby("run_id").agg(
    started=("created_at", "min"), credits=("credits", "sum"),
    billable=("cached", lambda c: int((~c[df.loc[c.index, "kind"] == "crustdata"]).sum())),
    cached=("cached", lambda c: int(c[df.loc[c.index, "kind"] == "crustdata"].sum())),
    llm_calls=("kind", lambda k: int((k == "llm").sum())),
    tokens_in=("input_tokens", "sum"), tokens_out=("output_tokens", "sum"),
    llm_usd=("llm_usd", "sum"), errors=("ok", lambda o: int((~o).sum())),
).reset_index().sort_values("started", ascending=False)
runs["budget_left"] = (cfg.CREDIT_BUDGET_PER_RUN - runs["credits"]).clip(lower=0)
st.dataframe(runs.style.format({"credits": "{:.2f}", "llm_usd": "${:.3f}", "budget_left": "{:.1f}"}),
             hide_index=True, width="stretch")
st.caption(f"Budget per run: {cfg.CREDIT_BUDGET_PER_RUN:g} credits (a call that would exceed it is refused).")

st.subheader("By agent")
agents = df.groupby(["agent", "kind"]).agg(
    calls=("id", "count"), credits=("credits", "sum"), tokens_in=("input_tokens", "sum"),
    tokens_out=("output_tokens", "sum"), avg_latency_s=("latency_ms", lambda x: x.mean() / 1000),
    llm_usd=("llm_usd", "sum")).reset_index().sort_values(["credits", "llm_usd"], ascending=False)
st.dataframe(agents.style.format({"credits": "{:.2f}", "avg_latency_s": "{:.1f}", "llm_usd": "${:.3f}"}),
             hide_index=True, width="stretch")

st.subheader("By Crustdata endpoint")
if not cd.empty:
    ep = cd.groupby("endpoint_or_model").agg(calls=("id", "count"), cached=("cached", "sum"),
                                             credits=("credits", "sum")).reset_index()
    st.dataframe(ep.sort_values("credits", ascending=False), hide_index=True, width="stretch")

if not cfg.is_mock and cfg.CRUSTDATA_API_KEY and st.button("Check live Crustdata balance (free)"):
    with guarded("Balance check"):
        st.json(CrustdataClient(mode="live").account_credits())
