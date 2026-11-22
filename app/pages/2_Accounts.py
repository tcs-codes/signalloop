"""Accounts: lookalike search results (F9) and decision-makers (F11)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from _common import guarded, setup  # noqa: E402
from signalloop.services import contacts as contacts_svc  # noqa: E402
from signalloop.services import icp as icp_svc  # noqa: E402
from signalloop.services import lookalike  # noqa: E402
from signalloop.tools import meter  # noqa: E402

setup("Accounts")

icp = icp_svc.latest_approved()
if icp is None:
    st.info("Approve an ICP on the Setup page first.")
    st.stop()

st.write(f"Current ICP: **v{icp.version}** ({icp.source.value}) · "
         f"preview count {icp.filters_json.get('preview_count') or 'n/a'}")

if st.button("Run lookalike search", type="primary"):
    with guarded("Lookalike search"), meter.run("find.lookalike") as run, st.spinner("Searching…"):
        res = lookalike.run_lookalike(icp.id)
        msg = (f"{'Converged' if res.converged else 'Stopped'} after {len(res.iterations)} "
               f"iteration(s): {res.count} matches, {res.upserted} accounts saved, "
               f"{res.skipped_suppressed} skipped (seeds/suppressed). Credits: {run.credits:g}")
        (st.success if res.converged else st.warning)(msg)

tuned = (icp_svc.get_icp(icp.id).filters_json or {}).get("tuned")
if tuned:
    with st.expander(f"Tuning log ({len(tuned['iterations'])} iterations)"):
        for it in tuned["iterations"]:
            st.write(f"**#{it['iteration']}** count = {it['count']}")
            if it.get("patch"):
                p = it["patch"]
                st.caption(f"{p['direction']} · {p['action']} · {p.get('condition') or p.get('index')} — {p['reason']}")
            for w in it.get("warnings", []):
                st.caption(f"⚠ {w}")

accounts = lookalike.list_accounts()
st.subheader(f"Lookalike accounts ({len(accounts)})")
if accounts:
    df = pd.DataFrame([{
        "name": a.name, "domain": a.domain, "fit": round(a.fit_score, 3),
        "priority": round(a.priority, 3), "status": a.status.value,
        "similarity reason": a.similarity_reason, "icp": a.icp_version_id,
    } for a in accounts])
    st.dataframe(df, width="stretch", hide_index=True,
                 column_config={"fit": st.column_config.ProgressColumn("fit", min_value=0, max_value=1)})
else:
    st.caption("No accounts yet. Run the lookalike search.")

# ---------------------------------------------------------------- F11
st.subheader("Decision-makers")
top_n = st.number_input("Accounts to staff (top by fit)", 1, 100,
                        value=contacts_svc.get_settings().CONTACTS_TOP_N)
if st.button("Find decision-makers", disabled=not accounts):
    with guarded("Decision-maker search"), meter.run("find.contacts") as run, st.spinner("Searching people…"):
        found = contacts_svc.find_contacts(top_n=int(top_n), icp_id=icp.id)
        n = sum(len(v) for v in found.values())
        st.success(f"{n} contacts across {sum(1 for v in found.values() if v)} accounts. "
                   f"Credits: {run.credits:g}")

people = contacts_svc.list_contacts()
if people:
    by_acc = {a.id: a.name for a in accounts}
    st.dataframe(pd.DataFrame([{
        "account": by_acc.get(c.account_id, c.account_id), "name": c.name, "title": c.title,
        "seniority": c.seniority, "role": c.role.value, "profile": c.profile_url,
    } for c in people if c.account_id in by_acc]), width="stretch", hide_index=True)
