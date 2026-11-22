"""Approval inbox (F4): outreach drafts and ICP changes. Nothing is sent from here."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from _common import guarded, setup  # noqa: E402
from signalloop.db import session_scope  # noqa: E402
from signalloop.models import ApprovalKind, Touch  # noqa: E402
from signalloop.services import approvals, icp_diff, status  # noqa: E402
from signalloop.services.briefs import brief_model  # noqa: E402
from signalloop.tools import meter  # noqa: E402

setup("Inbox")
st.caption("Every draft and ICP change waits here for a human decision. "
           "Approved drafts are exported to `exports/approved_outreach.csv`; nothing is sent.")

outreach = approvals.list_pending(ApprovalKind.outreach)
diffs = approvals.list_pending(ApprovalKind.icp_diff)
tab_out, tab_icp, tab_done = st.tabs([f"Outreach ({len(outreach)})", f"ICP changes ({len(diffs)})", "Decided"])


# ---------------------------------------------------------------- outreach
def critic_badge(p: dict) -> str:
    if p.get("needs_human"):
        return f"⚠️ needs human ({p.get('rewrites', 0)} rewrites)"
    r = p.get("rewrites", 0)
    return "✅ critic passed" + (f" after {r} rewrite{'s' if r != 1 else ''}" if r else "")


with tab_out:
    if not outreach:
        st.info("No drafts waiting. Run the daily scan and draft outreach from Home.")
    only_flagged = st.toggle("Show flagged drafts first", value=True)
    items = sorted(outreach, key=lambda a: (not a.payload_json.get("needs_human"), a.id)) if only_flagged else outreach
    for appr in items:
        p = appr.payload_json
        with st.container(border=True):
            top = st.columns([4, 2])
            top[0].markdown(f"**{p['account']}** · {p['contact']} — {p['title']} ({p['role'].replace('_', ' ')})"
                            f" · `{p['channel']}`")
            top[1].markdown(critic_badge(p))
            brief_row = status.brief_by_id(p.get("brief_id"))
            if brief_row is not None:
                b = brief_model(brief_row)
                with st.expander(f"Why now: {b.headline}"):
                    for bullet in b.bullets:
                        st.markdown(f"- {bullet.text}")
                    st.caption("Evidence")
                    st.dataframe(pd.DataFrame([r.model_dump() for r in b.evidence_refs]),
                                 hide_index=True, width="stretch")
            subject = st.text_input("Subject", p.get("subject", ""), key=f"s{appr.id}") \
                if p["channel"] == "email" else None
            body = st.text_area("Body" if p["channel"] == "email" else "LinkedIn note", p.get("body", ""),
                                key=f"b{appr.id}", height=200 if p["channel"] == "email" else 100)
            if p.get("violations"):
                for v in p["violations"]:
                    st.warning(v)
            with session_scope() as s:
                t = s.get(Touch, appr.ref_id)
            claims = (t.critic_json or {}).get("claims", []) if t else []
            if claims:
                cdf = pd.DataFrame(claims)
                with st.expander(f"Critic: {int((cdf['verdict'] == 'supported').sum())} supported claims"):
                    st.dataframe(cdf[[c for c in ("claim", "verdict", "evidence", "reason", "source") if c in cdf]],
                                 hide_index=True, width="stretch")
            note = st.text_input("Note (optional)", key=f"n{appr.id}")
            c1, c2, c3, _ = st.columns([1, 1.3, 1, 3])
            edited = (subject is not None and subject != p.get("subject")) or body != p.get("body")
            if c1.button("Approve", key=f"a{appr.id}", type="primary", disabled=edited):
                approvals.decide_outreach(appr.id, "approve", note=note)
                st.rerun()
            if c2.button("Edit & approve", key=f"e{appr.id}", disabled=not edited):
                approvals.decide_outreach(appr.id, "edit", subject=subject, body=body, note=note)
                st.rerun()
            if c3.button("Reject", key=f"r{appr.id}"):
                approvals.decide_outreach(appr.id, "reject", note=note)
                st.rerun()


# ---------------------------------------------------------------- ICP changes
with tab_icp:
    if not diffs:
        st.info("No ICP changes proposed. Run LEARN on a campaign to get one.")
    for appr in diffs:
        p = appr.payload_json
        with st.container(border=True):
            st.markdown(f"### ICP v{p['current_version']} → v{p['current_version'] + 1}  ·  from *{p['campaign']}*")
            st.write(p.get("expected_effect", ""))
            diff = pd.DataFrame(p["diff"])
            st.dataframe(diff[["scope", "field", "before", "after", "changed"]], hide_index=True,
                         width="stretch",
                         column_config={"changed": st.column_config.CheckboxColumn("changed")})
            for ch in p["changes"]:
                st.markdown(f"- **{ch['op']}** `{ch['field']}`: {ch['reason']}")
            st.caption("Evidence")
            st.dataframe(pd.DataFrame(p["evidence"]).assign(
                rate=lambda d: (d["rate"] * 100).round(1).astype(str) + "%",
                lift=lambda d: d["lift"].round(2).astype(str) + "x"),
                hide_index=True, width="stretch")
            for w in p.get("warnings", []):
                st.warning(w)
            note = st.text_input("Note (optional)", key=f"in{appr.id}")
            c1, c2, _ = st.columns([1.6, 1, 4])
            if c1.button("Approve → create ICP v2 + rerun FIND", key=f"ia{appr.id}", type="primary"):
                with guarded("ICP update"), meter.run("learn.approve") as run, st.spinner("Creating new ICP and re-running lookalike search…"):
                    v2, res = icp_diff.approve(appr.id, note=note)
                    st.success(f"ICP v{v2.version} approved. Lookalike re-run: {res.count} matches, "
                               f"{res.upserted} accounts. Credits: {run.credits:g}. See Accounts.")
            if c2.button("Reject", key=f"ir{appr.id}"):
                icp_diff.reject(appr.id, note)
                st.rerun()

# ---------------------------------------------------------------- history
with tab_done:
    done = approvals.list_decided(limit=100)
    if done:
        st.dataframe(pd.DataFrame([{
            "id": a.id, "kind": a.kind.value, "status": a.status.value, "decided": a.decided_at,
            "what": f"{a.payload_json.get('account', '')} {a.payload_json.get('contact', '')} "
                    f"{a.payload_json.get('channel', '')}".strip() or f"ICP v{a.payload_json.get('new_version', '?')}",
            "note": a.note} for a in done]), hide_index=True, width="stretch")
    else:
        st.caption("Nothing decided yet.")
