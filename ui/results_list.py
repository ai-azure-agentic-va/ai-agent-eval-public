"""Results list: the paginated, sortable runs table.

Run ids render as links to ?run=<id> — navigation stays query-param driven,
like the reference app's URLs (see ui/page_results.py for the routing).
"""
from __future__ import annotations

import html
from typing import Any
from urllib.parse import quote

import streamlit as st

from ui import client, components, logic

RUNS_FETCH_LIMIT = 200
RUNS_PAGE_SIZE = 10
POLL_SECONDS = 5.0

_RUN_ACCESSORS = {
    "run": lambda r: r.get("run_id", ""),
    "dataset": lambda r: f"{r.get('dataset_name', '')} v{r.get('dataset_version', '')}",
    "application": lambda r: r.get("application_key") or "",
    "status": lambda r: r.get("status", ""),
    "progress": lambda r: (r.get("summary") or {}).get("completed", 0),
    "failed": lambda r: (r.get("summary") or {}).get("failed", 0),
    "by": lambda r: r.get("triggered_by") or "",
    "started": lambda r: r.get("started_at") or "",
    "ended": lambda r: r.get("finished_at") or "",
    "time": lambda r: logic.duration_seconds(r.get("started_at"), r.get("finished_at")),
}
_RUN_COLUMNS = [("run", "Run"), ("dataset", "Dataset"), ("application", "Application"),
                ("status", "Status"), ("progress", "Progress"), ("failed", "Failed"),
                ("by", "By"), ("started", "Started At"), ("ended", "Ended At"),
                ("time", "Total Time")]


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


@st.fragment(run_every=POLL_SECONDS)
def render_runs_list() -> None:
    with st.container(key="ae_card_runs"):
        st.markdown('<div class="ae-card-title">Runs</div>',
                    unsafe_allow_html=True)
        try:
            data = client.runs(RUNS_FETCH_LIMIT, 0)
        except (client.ServiceUnreachable, client.ApiError) as exc:
            components.show_service_error(exc)
            return
        runs = data.get("runs", [])
        _render_runs_table(runs)


@st.fragment
def _render_runs_table(runs: list[dict]) -> None:
    """Sort and page the fetched snapshot without another service request."""
    sort = None
    page: list[dict] = []
    total = 0
    if runs:
        sort = components.sort_control("runs_sort", _RUN_COLUMNS,
                                       default_key="started", default_dir=-1,
                                       offset_key="runs_offset")
        ordered = logic.sort_records(runs, _RUN_ACCESSORS, sort, "started")
        total = len(ordered)
        start = logic.clamp_page_start(st.session_state.get("runs_offset", 0),
                                       total, RUNS_PAGE_SIZE)
        page = ordered[start:start + RUNS_PAGE_SIZE]

    cols = [{"label": lbl, "key": k} for k, lbl in _RUN_COLUMNS]
    trs = [_run_row_html(run) for run in page]
    st.markdown(components.table_html(cols, trs, sort=sort, empty="No runs yet."),
                unsafe_allow_html=True)
    if runs:
        components.pager("runs_offset", total, RUNS_PAGE_SIZE)


def _run_row_html(run: dict) -> str:
    summary = run.get("summary") or {}
    run_id = str(run.get("run_id") or "")
    href = html.escape(f"?run={quote(run_id, safe='')}", quote=True)
    triggered_by = run.get("triggered_by") or "—"
    application = (run.get("application_key") or "—").upper()
    status = logic.status_label(run.get("status"))
    started_at = run.get("started_at")
    finished_at = run.get("finished_at")
    return (
        "<tr>"
        f'<td><a class="ae-link" target="_self" href="{href}">'
        f"{_esc(run_id)}</a></td>"
        f"<td>{_esc(run.get('dataset_name'))} "
        f"v{_esc(run.get('dataset_version'))}</td>"
        f'<td class="ae-upper">{_esc(application)}</td>'
        f'<td><span class="ae-status">{_esc(status)}</span></td>'
        f"<td>{_esc(summary.get('completed', 0))}/"
        f"{_esc(summary.get('total', 0))}</td>"
        f"<td>{_esc(summary.get('failed', 0))}</td>"
        f'<td title="{html.escape(str(triggered_by), quote=True)}">'
        f"{_esc(triggered_by.split('@')[0])}</td>"
        f'<td class="ae-muted ae-nowrap">{_esc(logic.fmt_ts(started_at))}</td>'
        f'<td class="ae-muted ae-nowrap">{_esc(logic.fmt_ts(finished_at))}</td>'
        f'<td class="ae-muted ae-nowrap">'
        f"{_esc(logic.duration_label(started_at, finished_at))}</td>"
        "</tr>"
    )
