"""Setup: seeds -> traits -> ICP -> approve (F7, F8, F1)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import streamlit as st

from _common import guarded, setup
from signalloop.models import IcpStatus
from signalloop.schemas import Traits
from signalloop.services import icp as icp_svc
from signalloop.services import seeds as seeds_svc
from signalloop.services import traits as traits_svc
from signalloop.tools import meter

setup("Setup: seeds → ICP")

# ---------------------------------------------------------------- 1. seeds
st.header("1 · Best customers")
default_seeds = ""
try:
    default_seeds = "\n".join(seeds_svc.parse_csv(str(traits_svc.get_settings().fixtures_dir / "seeds.csv")))
except Exception:
    pass
text = st.text_area("Paste 5–10 customer domains (one per line or comma-separated)",
                    value=default_seeds, height=140)
upload = st.file_uploader("…or upload a CSV with a `domain` column", type=["csv"])
if st.button("Import seeds", type="primary"):
    with guarded("Seed import"), meter.run("find.seeds"):
        domains = seeds_svc.parse_csv(upload.getvalue()) if upload else seeds_svc.parse_domains(text)
        res = seeds_svc.import_seeds(domains)
        st.success(f"Imported {len(res.accounts)} seed accounts.")
        if res.unresolved:
            st.warning("Could not resolve: " + ", ".join(res.unresolved))

seed_rows = traits_svc.seed_accounts()
if seed_rows:
    st.dataframe(pd.DataFrame([{**{"domain": a.domain, "name": a.name},
                                **{k: a.firmographics_json.get("normalized", {}).get(k) for k in
                                   ("industry", "headcount", "funding_stage", "last_funding_date", "hq_country")}}
                               for a in seed_rows]), use_container_width=True, hide_index=True)

# ---------------------------------------------------------------- 2. traits
st.header("2 · Shared traits")
if st.button("Extract traits", disabled=not seed_rows):
    with guarded("Trait extraction"), meter.run("find.traits"):
        t, stats = traits_svc.extract_traits(seed_rows)
        st.session_state["traits"] = t.model_dump()
        st.session_state["trait_stats"] = stats

traits_data = st.session_state.get("traits")
if traits_data:
    t = Traits.model_validate(traits_data)
    st.dataframe(pd.DataFrame([{"dimension": tr.dimension, "values": ", ".join(tr.values),
                                "coverage": f"{tr.coverage:.0%}", "explanation": tr.explanation}
                               for tr in t.traits]), use_container_width=True, hide_index=True)
    st.caption("Persona hypothesis: " + ", ".join(t.persona_hypothesis.titles) +
               " · seniority: " + ", ".join(t.persona_hypothesis.seniority))

# ---------------------------------------------------------------- 3. ICP
st.header("3 · Ideal customer profile")
prefill = ""
if traits_data:
    t = Traits.model_validate(traits_data)
    prefill = "; ".join(f"{tr.dimension.replace('_', ' ')}: {', '.join(tr.values)}" for tr in t.traits)
description = st.text_area("Describe the ICP in plain English", value=prefill, height=100)
if st.button("Generate ICP", type="primary", disabled=not description.strip()):
    with guarded("ICP generation"), meter.run("find.icp"):
        t = Traits.model_validate(traits_data) if traits_data else None
        row = icp_svc.build_icp(description, t)
        st.session_state["draft_icp_id"] = row.id

versions = icp_svc.list_versions()
if versions:
    labels = {f"v{v.version} · {v.status.value} · {v.source.value}": v.id for v in versions}
    default = next((k for k, i in labels.items() if i == st.session_state.get("draft_icp_id")), list(labels)[0])
    choice = st.selectbox("ICP version", list(labels), index=list(labels).index(default))
    row = icp_svc.get_icp(labels[choice])
    c1, c2 = st.columns([3, 1])
    with c1:
        st.dataframe(pd.DataFrame(icp_svc.describe_filters(row)), use_container_width=True, hide_index=True)
        st.write(row.rationale)
    with c2:
        st.metric("Matching companies", row.filters_json.get("preview_count") or "n/a")
        if row.status != IcpStatus.approved and st.button("Approve this ICP", type="primary"):
            icp_svc.approve_icp(row.id)
            st.success(f"ICP v{row.version} approved. Next: Accounts → Run lookalike search.")
            st.rerun()
    for w in row.filters_json.get("warnings", []):
        st.warning(w)
    edit = st.text_input("Revise in plain English (e.g. 'exclude agencies', 'only US')")
    if st.button("Revise", disabled=not edit.strip()):
        with guarded("ICP revision"), meter.run("find.icp"):
            new = icp_svc.revise_icp(row.id, edit)
            st.session_state["draft_icp_id"] = new.id
            st.rerun()
