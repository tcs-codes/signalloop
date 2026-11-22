"""LEARN (F24-F28): campaign results -> segment lift -> ICP change proposal."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from _common import guarded, link, setup  # noqa: E402
from signalloop.models import Campaign  # noqa: E402
from signalloop.services import analysis, approvals, campaigns, demo_campaign, icp_diff  # noqa: E402
from signalloop.tools import meter  # noqa: E402

setup("Learn")

# Validated with the dataviz palette validator against both chart surfaces.
BAR = {"light": "#2a78d6", "dark": "#3987e5"}
INK_MUTED = "#898781"


def theme() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:  # pragma: no cover - older Streamlit
        return "light"


# ---------------------------------------------------------------- 1. results
st.header("1 · Campaign results")
c1, c2 = st.columns(2)
with c1:
    upload = st.file_uploader("Upload a results CSV (email, domain, sent_at, opened, replied, meeting, unsubscribed)",
                              type=["csv"])
with c2:
    st.write("…or simulate one on the contacts already in SignalLoop.")
    seed = st.number_input("Random seed", value=demo_campaign.DEFAULT_SEED, step=1)
    if st.button("Generate demo campaign"):
        from signalloop.orchestrator import staff_for_demo_campaign
        staffed = staff_for_demo_campaign()          # mock mode: free, from fixtures
        if staffed:
            st.caption(f"Found decision-makers for {staffed} more account(s) from recorded data.")
        n = demo_campaign.generate(demo_campaign.DEFAULT_CSV, seed=int(seed))
        st.success(f"Simulated outcomes for {n} contacts → `fixtures/campaign_results.csv` (synthetic).")
name = st.text_input("Campaign name", demo_campaign.DEFAULT_NAME)

if st.button("Run LEARN", type="primary"):
    with guarded("LEARN"), meter.run("learn") as run, st.status("Running LEARN…", expanded=True) as box:
        src = upload.getvalue() if upload else demo_campaign.DEFAULT_CSV
        if not upload and not Path(src).exists():
            raise FileNotFoundError("No CSV uploaded and no generated demo campaign yet.")
        st.write("Importing results…")
        imp = campaigns.import_campaign(src, name)
        st.write(f"{imp.rows} rows, {imp.contact_link_rate:.0%} linked to contacts.")
        st.write("Enriching responders and non-responders…")
        df = campaigns.enrich_campaign(imp.campaign.id)
        st.write("Computing segment lift…")
        res = analysis.analyze(imp.campaign.id)
        st.write("Proposing ICP changes…")
        appr = icp_diff.propose(imp.campaign.id)
        st.session_state["learn_campaign_id"] = imp.campaign.id
        box.update(label=f"LEARN done · credits {run.credits:g}" + (" · ICP change waiting in Inbox" if appr else ""),
                   state="complete")

# ---------------------------------------------------------------- 2. insights
all_camps = campaigns.list_campaigns()
if not all_camps:
    st.info("No campaigns yet.")
    st.stop()
ids = {f"{c.name} (#{c.id})": c.id for c in all_camps}
default_id = st.session_state.get("learn_campaign_id", all_camps[0].id)
label = st.selectbox("Campaign", list(ids), index=list(ids.values()).index(default_id) if default_id in ids.values() else 0)
camp: Campaign = next(c for c in all_camps if c.id == ids[label])
insights = analysis.insights_for(camp.id)

st.header("2 · What responded")
aj = camp.analysis_json or {}
narr = aj.get("narrative")
if narr:
    with st.container(border=True):
        st.subheader(narr["headline"])
        cols = st.columns(2)
        cols[0].markdown("**Over-performing**\n" + "\n".join(f"- {x}" for x in narr.get("positives", [])))
        cols[1].markdown("**Under-performing**\n" + "\n".join(f"- {x}" for x in narr.get("negatives", [])))
        st.write(narr.get("narrative", ""))
        st.caption(f"Baseline reply rate {aj.get('baseline', 0) * 100:.1f}% across {aj.get('total', 0)} contacted people. "
                   "Every number above is checked against the table below.")

if insights:
    df = pd.DataFrame([{"dimension": i.dimension, "segment": i.segment_value, "n": i.n,
                        "replies": i.responders, "rate": i.rate, "lift": i.lift,
                        "low_confidence": i.low_confidence} for i in insights])
    top = df[df["lift"] > 1].nlargest(8, "lift").copy()
    top["label"] = top["segment"] + top["low_confidence"].map({True: "  (low confidence)", False: ""})
    if not top.empty:
        mode = theme()
        bars = alt.Chart(top).mark_bar(size=20, cornerRadiusEnd=4, color=BAR[mode]).encode(
            x=alt.X("lift:Q", title="Lift vs baseline (×)", scale=alt.Scale(zero=True)),
            y=alt.Y("label:N", sort="-x", title=None),
            opacity=alt.condition("datum.low_confidence", alt.value(0.45), alt.value(1.0)),
            tooltip=[alt.Tooltip("dimension:N"), alt.Tooltip("segment:N"),
                     alt.Tooltip("lift:Q", format=".2f", title="lift ×"),
                     alt.Tooltip("rate:Q", format=".1%", title="reply rate"),
                     alt.Tooltip("n:Q"), alt.Tooltip("replies:Q"), alt.Tooltip("low_confidence:N")])
        rule = alt.Chart(pd.DataFrame({"x": [1.0]})).mark_rule(color=INK_MUTED, strokeWidth=1).encode(x="x:Q")
        chart = (bars + rule).properties(height=max(160, 34 * len(top)),
                                         title="Top segments by reply-rate lift (rule = baseline 1.0×)")
        st.altair_chart(chart.configure_axis(grid=True, gridWidth=1, gridColor="#e1e0d9" if mode == "light" else "#2c2c2a",
                                             domain=False, labelColor=INK_MUTED, titleColor=INK_MUTED)
                        .configure_view(stroke=None), width="stretch")

    def grey(row):
        return [f"color: {INK_MUTED}" if row["low_confidence"] else "" for _ in row]

    st.caption("All segments (n ≥ 10). Grey rows have n < 30 (low confidence).")
    st.dataframe(df.sort_values("lift", ascending=False).style.apply(grey, axis=1)
                 .format({"rate": "{:.1%}", "lift": "{:.2f}×"}), hide_index=True, width="stretch")
else:
    st.caption("No insights for this campaign yet. Click Run LEARN.")

# ---------------------------------------------------------------- 3. ICP change
st.header("3 · Proposed ICP change")
pending = [a for a in approvals.list_pending("icp_diff") if a.payload_json.get("campaign_id") == camp.id]
if pending:
    p = pending[-1].payload_json
    st.write(p.get("expected_effect", ""))
    st.dataframe(pd.DataFrame(p["diff"]).query("changed")[["scope", "field", "before", "after"]],
                 hide_index=True, width="stretch")
    link("pages/3_Inbox.py", "Review and approve in the Inbox →")
elif insights:
    eligible = icp_diff.eligible_insights(camp.id)
    st.caption(f"{len(eligible)} insight(s) meet the bar for an ICP change "
               f"(lift ≥ {icp_diff.POSITIVE_LIFT} or ≤ {icp_diff.NEGATIVE_LIFT}, n ≥ {icp_diff.min_n()}).")
