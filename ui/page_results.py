"""Results page router: runs list vs run detail, driven by query params.

Navigation mirrors the reference app's URLs: run ids render as links to
?run=<id>; case rows link to ?run=<id>&case=<index>. The actual views live in
ui/results_list.py (runs table) and ui/results_detail.py (header card, tiles,
case table, case-detail dialog); app.py registers render_results unchanged.
"""
from __future__ import annotations

import streamlit as st

from ui.results_detail import render_run_detail
from ui.results_list import render_runs_list


def render_results() -> None:
    if handoff := st.session_state.pop("view_run_id", None):
        st.query_params["run"] = handoff
    run_id = st.query_params.get("run")
    with st.container(key="ae_results"):
        if run_id:
            render_run_detail(run_id)
        else:
            render_runs_list()
