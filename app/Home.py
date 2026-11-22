"""SignalLoop home: loop status, daily scan, and this week's in-market accounts."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from _common import guarded, link, setup  # noqa: E402
from signalloop.config import get_settings  # noqa: E402
from signalloop.services import outreach, scoring, status, watch  # noqa: E402
from signalloop.tools import meter  # noqa: E402

setup("SignalLoop")
st.caption("FIND → WATCH → ENGAGE → LEARN → update the ICP. Nothing is sent without your approval.")
cfg = get_settings()

with st.sidebar.expander("Demo controls"):
    st.caption("Mock mode replays recorded Crustdata data; no credits are used.")
    if st.button("Reset demo", help="Empty all data and start again from the Setup page"):
        from signalloop import db as _db
        _db.reset_db()
        st.session_state.clear()
        st.success("Demo data cleared. Start on the Setup page.")

ls = status.loop_status()
m = st.columns(5)
m[0].metric("ICP", f"v{ls.icp_version} ({ls.icp_source})" if ls.icp_version else "none")
m[1].metric("Accounts found", ls.accounts, help=f"{ls.seeds} seed customers excluded")
m[2].metric("In market this week", ls.in_market)
m[3].metric("Pending approvals", ls.pending_outreach + ls.pending_icp,
            help=f"{ls.pending_outreach} outreach, {ls.pending_icp} ICP changes")
m[4].metric("Contacts", ls.contacts)
if ls.last_insight:
    st.info(f"**Last insight:** {ls.last_insight}")

if ls.icp_version is None:
    st.warning("Start on the Setup page: import seed customers and approve an ICP.")
    st.stop()

# ---------------------------------------------------------------- WATCH
st.header("WATCH")
day2_ready = bool(watch.load_day2()) and ls.signals > 0 and cfg.DEMO_MODE
c1, c2 = st.columns([1, 3])
if c1.button("Run daily scan now", type="primary"):
    with guarded("Daily scan"), meter.run("watch.daily_scan") as run, st.spinner("Scanning watched accounts…"):
        res = watch.daily_scan(day2=day2_ready)
        new_ids = {sg.account_id for sg in res.new_signals}
        st.session_state["new_signal_accounts"] = sorted(new_ids)
        st.success(f"{len(res.new_signals)} new signal(s) on {len(new_ids)} account(s); "
                   f"{res.new_accounts} new account(s) discovered. Credits: {run.credits:g}")
        for e in res.errors:
            st.warning(e)
c2.caption(f"Scans run as of **{watch.as_of_date()}**" + (" (demo date)" if cfg.DEMO_MODE else "") +
           (". The next scan releases the demo day-2 events." if day2_ready else "."))

# ---------------------------------------------------------------- this week
top = scoring.this_week(10)  # computed after the scan button so fresh signals show immediately
new_ids = set(st.session_state.get("new_signal_accounts", []))
st.subheader("In market this week")
if top:
    st.dataframe(pd.DataFrame([{
        "new": "🆕" if b.account_id in new_ids else "", "account": b.name, "domain": b.domain,
        "priority": round(b.priority, 3), "fit": round(b.fit, 2), "timing": round(b.timing, 2),
        "signals": ", ".join(f"{s['type']} ({s['days']:.0f}d)" for s in b.signals[:3]),
    } for b in top]), hide_index=True, width="stretch",
        column_config={"priority": st.column_config.ProgressColumn("priority", min_value=0, max_value=1)})
else:
    st.caption("No signals yet. Run the daily scan.")

# ---------------------------------------------------------------- ENGAGE
st.header("ENGAGE")
n = st.slider("Draft outreach for the top N in-market accounts", 1, 10, 3)
st.caption("About 30 seconds per account: why-now brief, then email + LinkedIn drafts per contact, each "
           "fact-checked by the critic. Drafts go to the Inbox.")
if st.button("Draft outreach", disabled=not top):
    with guarded("Drafting"), meter.run("engage") as run, st.spinner("Researching, drafting and fact-checking…"):
        out = outreach.draft_this_week(n)
        passed = sum(o.critic.passed for o in out)
        st.success(f"{len(out)} draft(s): {passed} passed the critic, {len(out) - passed} need a human. "
                   f"{sum(o.critic.rewrites for o in out)} rewrite(s). Credits: {run.credits:g}")
    link("pages/3_Inbox.py", "Open the Inbox →")
