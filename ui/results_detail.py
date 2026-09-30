"""Live run details, case inspection, CSV/PDF exports, and resume controls."""
from __future__ import annotations

import html
from collections.abc import Callable
from copy import deepcopy
from functools import lru_cache
from typing import Any
from urllib.parse import quote

import streamlit as st

from service.execution import EXECUTION_LABELS, execution_mode
from service.metrics import (
    applicable_metrics,
    case_result,
    metric_result,
    retryable_judge_row,
    row_metrics,
    score_value,
    scoring_policy,
    summarize_rows,
)
from service.pdf_report import run_pdf
from service.reports import run_csv
from service.results import reconcile_rows
from ui import client, components, logic
from ui.results_list import _esc

POLL_SECONDS = 3.0


def _score(value: Any, version: int = 1) -> float | None:
    return score_value(value, version)


def _pdf_download(run_id: str, meta: dict, rows: list[dict]) -> Callable[[], bytes]:
    """Generate on click, retaining the displayed snapshot through later polls."""
    saved_meta, saved_rows = deepcopy(meta), deepcopy(rows)

    @lru_cache(maxsize=1)
    def generate() -> bytes:
        return run_pdf(run_id, saved_meta, saved_rows)

    return generate


def render_run_detail(run_id: str) -> None:
    if st.button("← All runs", type="tertiary"):
        st.query_params.pop("run", None)
        st.query_params.pop("case", None)
        st.rerun()

    try:
        meta = client.run(run_id)
    except client.ServiceUnreachable:
        st.error("Run not found or eval service unreachable — is the eval service running?")
        return
    except client.ApiError:
        st.error("Run not found or eval service unreachable.")
        return

    running = meta.get("status") in ("running", "queued")
    initial_meta: dict | None = meta
    initial_rows: list[dict] | None = None

    if (case_param := st.query_params.get("case")) is not None:
        del st.query_params["case"]
        try:
            initial_rows = reconcile_rows(meta, client.run_rows(run_id))
        except (client.ServiceUnreachable, client.ApiError):
            pass
        row = next((r for r in initial_rows or []
                    if str(r.get("index")) == str(case_param)), None)
        if row:
            _show_case_dialog(row, scoring_policy(meta))

    @st.fragment(run_every=POLL_SECONDS if running else None)
    def live_body() -> None:
        nonlocal initial_meta, initial_rows
        # Consume the first-render snapshot once; fragment reruns fetch fresh data.
        m, rows = initial_meta, initial_rows
        initial_meta = initial_rows = None
        try:
            if m is None:
                m = client.run(run_id)
            if rows is None:
                rows = client.run_rows(run_id)
        except (client.ServiceUnreachable, client.ApiError):
            st.warning("Lost contact with the eval service — retrying… "
                       "(is the eval service running?)")
            return
        if running and m.get("status") not in ("running", "queued"):
            st.rerun()
        rows = reconcile_rows(m, rows)
        shown_metrics = logic.visible_metrics(rows, (m.get("summary") or {}).get("means"))
        policy = scoring_policy(m)
        if policy["version"] == 2:
            applicable = {key for row in rows for key in row_metrics(row, policy)}
            selected = policy["selected_metrics"]
            has_results = any(row.get("status") in ("scored", "failed") for row in rows)
            shown_metrics = [key for key in selected if key in applicable] if has_results else list(selected)
        _render_header_card(run_id, m, rows, shown_metrics)
        _render_case_card(run_id, m, rows, shown_metrics)

    live_body()


def _render_header_card(run_id: str, meta: dict, rows: list[dict],
                        shown_metrics: list[str]) -> None:
    running = meta.get("status") in ("running", "queued")
    summary = meta.get("summary") or {}
    policy = scoring_policy(meta)
    with st.container(key="ae_card_detail"):
        c1, c2 = st.columns([3, 2], vertical_alignment="center")
        with c1:
            st.markdown(
                '<div class="ae-run-title">'
                f"<h2>{_esc(meta.get('dataset_name') or '—')}</h2>"
                f'<span class="ae-pill ae-pill-gray">'
                f'v{_esc(meta.get("dataset_version", "?"))}</span>'
                f'<span class="ae-runid">{_esc(run_id)}</span></div>',
                unsafe_allow_html=True)
        with c2:
            with st.container(horizontal=True, horizontal_alignment="right",
                              vertical_alignment="center"):
                badge = ""
                h = components.cached_health()
                if h and h.get("storage") == "langsmith":
                    badge = (' <span class="ae-pill ae-pill-indigo">Storage: '
                             'LangSmith</span> <a href="https://smith.langchain.com" '
                             'style="font-size:12px">smith.langchain.com</a>')
                st.markdown(components.status_pill(meta.get("status")) + badge,
                            unsafe_allow_html=True)
                st.download_button("Export CSV", data=run_csv(run_id, meta, rows),
                                   file_name=f"run-{run_id}.csv", mime="text/csv",
                                   key=f"csv_{run_id}", on_click="ignore")
                st.download_button("Export PDF", data=_pdf_download(run_id, meta, rows),
                                   file_name=f"run-{run_id}.pdf", mime="application/pdf",
                                   key=f"pdf_{run_id}", on_click="ignore",
                                   help="Download all cases and full answers from this results snapshot.")
                total = summary.get("total") or 0
                needs_agent = len(rows) < total or any(
                    row.get("status") in ("missing", "not_run", "pending") or (
                        row.get("status") == "failed" and not retryable_judge_row(
                            row, policy["version"], policy.get("selected_metrics")))
                    for row in rows)
                if not running and needs_agent and execution_mode(meta) != "conversation":
                    _render_resume_button(run_id)
                if not running and any(retryable_judge_row(
                        row, policy["version"], policy.get("selected_metrics")) for row in rows):
                    _render_retry_judges_button(run_id)
        if msg := st.session_state.pop("resume_msg", None):
            st.info(msg)
        policy = scoring_policy(meta)
        if policy["version"] == 1:
            st.info("Historical run — these saved scores use the original 1–5 scale, "
                    "not the current 0–1 scale. Start a new evaluation from Run to use "
                    "the current scoring criteria; this history is unchanged.")
        _render_summary_tiles(meta, rows, shown_metrics)
        st.caption("Query execution: " + EXECUTION_LABELS[execution_mode(meta)])
        if not running and meta.get("error"):
            st.error(f"Run stopped: {meta['error']}")
        if not running and execution_mode(meta) == "conversation" and not meta.get("conversation_complete"):
            st.warning("This conversation did not finish. Start a new evaluation from the first "
                       "row to preserve context. Judge-only retries reuse saved answers.")
        if policy["version"] == 2:
            st.caption("Scoring v2 · 0–1 · PASS when score ≥ threshold · All selected, applicable metrics "
                       "must pass. Errors prevent PASS. Precision/recall measure retrieved context.")
            st.caption("Selected metrics: " + " · ".join(
                logic.METRIC_LABELS[key] for key in policy["selected_metrics"]))
            st.caption("Saved thresholds: " + " · ".join(
                f"{logic.METRIC_LABELS[key]} ≥ {logic.score_display(policy['thresholds'][key], version=2)}"
                for key in policy["selected_metrics"]))
            if rows and not shown_metrics:
                st.caption("No selected metrics apply to the returned cases. "
                           "Successfully executed cases without applicable metrics are N/A.")
        else:
            st.caption("Legacy scoring v1 · 1–5 · Accuracy pass threshold: 4.00. "
                       "Original scores and accuracy-only pass rule are preserved.")
        st.caption("Hover over a score or mean's ⓘ to see its raw value; CSV and PDF retain full precision.")


def _render_resume_button(run_id: str) -> None:
    if not st.button(
        "Resume run",
        key=f"resume_{run_id}",
        help="Re-run this run's failed or missing rows (scored rows are kept "
             "verbatim). Uses the sidebar bearer token, if provided.",
    ):
        return
    try:
        result = client.resume_run(run_id, token=components.bearer_token())
    except client.ServiceUnreachable as exc:
        st.error(str(exc))
        return
    except client.ApiError as exc:
        components.bounce_unauthorized(exc)
        st.error(f"Could not resume run — {exc}")
        return
    st.session_state["resume_msg"] = (
        f"Resume started — {result.get('kept', 0)} scored row(s) kept, "
        f"{result.get('rerun', 0)} row(s) re-running."
    )
    st.rerun()


def _render_retry_judges_button(run_id: str) -> None:
    if not st.button("Retry failed judges", key=f"retry_judges_{run_id}",
                     help="Reuse saved answers and evidence. Only missing/error scores are "
                          "retried; valid scores, including failures, are unchanged."):
        return
    try:
        result = client.retry_judges(run_id)
    except (client.ServiceUnreachable, client.ApiError) as exc:
        if isinstance(exc, client.ApiError):
            components.bounce_unauthorized(exc)
        st.error(f"Could not retry judges — {exc}")
        return
    st.session_state["resume_msg"] = f"Retrying failed judges for {result['rows_to_retry']} saved answers."
    st.rerun()


def _render_summary_tiles(meta: dict, rows: list[dict], shown_metrics: list[str]) -> None:
    policy = scoring_policy(meta)
    summary = summarize_rows(rows, (meta.get("summary") or {}).get("total", len(rows)), policy)
    means = summary.get("means") or {}
    total = summary.get("total", 0)
    failed = sum(1 for r in rows if r.get("status") == "failed")
    pr = summary["pass_rate"]
    pass_tip = (logic.PASS_RATE_TOOLTIP if policy["version"] == 1 else
                "Passed / (passed + failed + error cases). Pending, missing, not-run and N/A cases excluded. "
                "All selected, applicable metrics must meet their saved thresholds.")

    tiles = [
        {"label": "Pass rate", "value": f"{pr:.2f}%" if pr is not None else "—",
         "tooltip": pass_tip},
        {"label": "Progress", "value": f"{summary['completed']}/{total}",
         "tooltip": "Cases with a recorded outcome, including failures. Pending, not-run, "
                    "and missing results do not count as completed."},
        {"label": "Execution errors", "value": failed, "band": "red" if failed else None},
        {"label": "Quality fails", "value": summary["verdicts"]["FAIL"]},
        {"label": "Evaluation errors", "value": summary["verdicts"]["ERROR"]},
        {"divider": True},
    ]
    for m in shown_metrics:
        v = _score(means.get(m), policy["version"])
        threshold = policy["thresholds"].get(m, 4)
        tiles.append({
            "label": f"{logic.METRIC_LABELS[m]} μ",
            "value": logic.score_display(v, threshold, policy["version"]),
            "tooltip": (logic.metric_definition(m, policy["version"]) +
                        f" Mean of {summary['metric_counts'][m]} valid scores; "
                        f"threshold ≥ {logic.score_display(threshold, version=policy['version'])}. "
                        f"Raw mean: {v if v is not None else 'N/A'}. "
                        "Display rounded to 2 decimal places; calculations use raw scores."),
            "band": logic.score_band(v, threshold, policy["version"]),
        })
    components.stat_tiles(tiles)
    if summary["missing"]:
        st.warning(f"{summary['missing']} case result(s) are missing. Their outcome is unknown; "
                   "they appear as MISSING in the grid and are excluded from the pass rate. "
                   "Refresh to check for delayed results.")
    if summary["not_run"]:
        st.warning(f"{summary['not_run']} case(s) were not run after execution stopped. "
                   "They appear as NOT RUN in the grid and are excluded from the pass rate.")
    coverage = []
    for key in shown_metrics:
        eligible = sum(key in applicable_metrics(
            row.get("tags", ""), row.get("reference_answer", ""), policy["version"],
            policy.get("selected_metrics")) for row in rows)
        coverage.append(f"{logic.METRIC_LABELS[key]} {summary['metric_counts'][key]}/{eligible}")
    if coverage:
        st.caption("Scores contributing to averages (valid / applicable returned cases): "
                   + " · ".join(coverage))


@st.fragment
def _render_case_card(run_id: str, meta: dict, rows: list[dict],
                      shown_metrics: list[str]) -> None:
    policy = scoring_policy(meta)
    default_sort = "worst" if execution_mode(meta) == "parallel" else "index"
    if st.session_state.get("case_sort_run_id") != run_id:
        st.session_state.update(case_sort_run_id=run_id, case_offset=0,
                                case_sort={"key": default_sort, "dir": 1},
                                case_sort_col=default_sort)
    with st.container(key="ae_card_cases"):
        t1, t2 = st.columns([4, 1.4], vertical_alignment="center")
        with t1:
            st.markdown('<div class="ae-card-title">Cases</div>',
                        unsafe_allow_html=True)
        with t2:
            with st.container(horizontal=True, horizontal_alignment="right",
                              vertical_alignment="center"):
                st.markdown('<span class="ae-muted" style="font-size:13px">'
                            'show:</span>', unsafe_allow_html=True)
                page_size = st.selectbox(
                    "show", [10, 25, 50, 0],
                    format_func=lambda v: "All" if v == 0 else str(v),
                    key="case_page_size", label_visibility="collapsed", width=84,
                    on_change=lambda: st.session_state.update(case_offset=0))

        sort_columns = [("worst", "Severity (asc: worst first; desc: best first)"),
                        ("index", "# (case order)"),
                        *[(m, logic.METRIC_LABELS[m]) for m in shown_metrics],
                        ("latency", "latency")]
        sort = components.sort_control("case_sort", sort_columns,
                                       default_key=default_sort,
                                       offset_key="case_offset")
        if sort != {"key": "worst", "dir": 1}:
            st.button("Reset to worst first", type="tertiary",
                      on_click=st.session_state.update,
                      kwargs={"case_sort": {"key": "worst", "dir": 1},
                              "case_sort_col": "worst", "case_offset": 0})

        ordered = logic.sort_run_rows(rows, sort["key"], sort["dir"], policy)
        running = meta.get("status") in ("running", "queued")
        total = (meta.get("summary") or {}).get("total", 0)
        pending = logic.pending_indexes(rows, total) if running else []

        display_count = len(ordered) + len(pending)
        size = page_size or max(display_count, 1)
        start = logic.clamp_page_start(st.session_state.get("case_offset", 0),
                                       display_count, size) if display_count else 0
        page_rows = ordered[start:start + size]
        page_pending = logic.slice_pending_for_page(pending, start, size,
                                                    len(ordered))

        st.markdown(_case_table_html(run_id, page_rows, page_pending, sort,
                                     shown_metrics, policy),
                    unsafe_allow_html=True)
        st.caption("Click a case to inspect it. N/A means not applicable; ERROR means a "
                   "required judge or execution failed. Missing scores are never treated as zero.")
        st.caption("The # column follows spreadsheet row order. Sorting this table changes "
                   "the display only, not query execution.")
        components.pager("case_offset", display_count, size)


def _case_table_html(run_id: str, page_rows: list[dict],
                     page_pending: list[int], sort: dict,
                     shown_metrics: list[str], policy: dict | None = None) -> str:
    policy = policy or scoring_policy()
    cols = [
        {"label": "#", "key": "index"},
        {"label": "Result",
         "tip": logic.RESULT_TOOLTIP + " A failed row's error appears in the "
                "case detail. 'retried' = the errored case was auto-retried."},
        {"label": "Case"},
        *[{"label": logic.METRIC_LABELS[m], "key": m, "class": "ae-center",
           "tip": logic.metric_definition(m, policy["version"]) +
                  f" Pass threshold ≥ {logic.score_display(policy['thresholds'].get(m, 4), version=policy['version'])}."}
          for m in shown_metrics],
        {"label": "Latency", "key": "latency", "class": "ae-right",
         "tip": logic.LATENCY_TOOLTIP},
        {"label": "Routed",
         "tip": "Observed routing (expected → observed when the case declares "
                "an expectation; ✓/✗ = match)."},
    ]
    rid_q = quote(str(run_id), safe="")
    trs = [_case_row_html(rid_q, row, shown_metrics, policy) for row in page_rows]
    for i in page_pending:
        trs.append(f'<tr class="ae-pending"><td>{i + 1}</td>'
                   f'<td colspan="{len(cols) - 1}">pending / running…</td></tr>')
    return components.table_html(cols, trs, sort=sort,
                                 empty="No cases recorded yet.")


def _case_row_html(quoted_run_id: str, row: dict,
                   shown_metrics: list[str], policy: dict | None = None) -> str:
    policy = policy or scoring_policy()
    index = row.get("index", 0)
    failed = row.get("status") == "failed"
    verdict = case_result(row, policy)
    chip_class = {"PASS": "ae-chip-green", "FAIL": "ae-chip-red",
                  "ERROR": "ae-chip-red"}.get(verdict, "ae-chip-gray")
    chip_tip = str(row.get("error") or logic.RESULT_TOOLTIP)
    retried = row.get("attempts") == 2
    if retried:
        chip_tip += " (retried once)"
    retried_html = (
        '<span class="ae-retried">retried</span>' if retried else ""
    )
    href = html.escape(f"?run={quoted_run_id}&case={index}", quote=True)
    case_cell = (
        f'<a class="ae-case-link" target="_self" href="{href}">'
        f"{_esc(row.get('name') or f'Case {index + 1}')}: {_esc(row.get('question'))}</a>"
    )
    metric_cells = "".join(
        f'<td class="ae-center">{_metric_chip(row, m, policy)}</td>'
        for m in shown_metrics
    )
    latency = f"{row['latency_s']}s" if row.get("latency_s") else "—"
    row_class = ' class="ae-row-failed"' if failed else ""
    routing = logic.routing_cell(
        row.get("expected_agent"),
        row.get("routing_observed"),
        row.get("routing_match"),
    )
    return (
        f"<tr{row_class}>"
        f'<td class="ae-dim">{index + 1}</td>'
        f'<td class="ae-nowrap"><span class="ae-chip {chip_class}" '
        f'title="{html.escape(chip_tip, quote=True)}">'
        f'{verdict}</span>{retried_html}</td>'
        f'<td class="ae-case">{case_cell}</td>'
        f"{metric_cells}"
        f'<td class="ae-right ae-muted ae-nowrap">{_esc(latency)}</td>'
        f'<td class="ae-muted">{_esc(routing)}</td>'
        "</tr>"
    )


def _metric_chip(row: dict, metric: str, policy: dict, *, label: bool = False) -> str:
    result = metric_result(row, metric, policy)
    name = logic.METRIC_LABELS[metric]
    if result not in ("PASS", "FAIL"):
        text = f"{name}: {result}" if label else result
        return f'<span class="ae-chip ae-chip-gray">{html.escape(text)}</span>'
    threshold = policy["thresholds"][metric]
    return components.score_chip(
        _score((row.get("metric_values") or {}).get(metric), policy["version"]),
        label=name if label else None,
        tip=f"{result} · threshold ≥ {logic.score_display(threshold, version=policy['version'])}. "
            + logic.metric_definition(metric, policy["version"]),
        threshold=threshold, version=policy["version"])


def _show_case_dialog(row: dict, policy: dict | None = None) -> None:
    """Show the case's answers, retrieved context, and per-metric judge reasoning."""
    title = str(row.get("name") or f"Case {row.get('index', 0) + 1}")
    policy = policy or scoring_policy()

    @st.dialog(title, width="large")
    def _dialog() -> None:
        st.caption(logic.detail_caption(row))
        if row.get("status") == "failed" and row.get("error"):
            st.error(f"Case failed: {row['error']}"
                     + (" (retried once)" if row.get("attempts") == 2 else ""))
        elif row.get("status") in ("missing", "not_run", "pending"):
            st.warning(row.get("error") or "This case is pending execution.")

        def panel(label: str, text: Any) -> str:
            return (f'<div class="ae-label">{html.escape(label)}</div>'
                    f'<div class="ae-panel">{_esc(text or "—")}</div>')

        st.markdown(panel("Question", row.get("question")),
                    unsafe_allow_html=True)
        st.markdown(
            '<div class="ae-panel-grid"><div>'
            + panel("Agent answer", row.get("response") or row.get("error"))
            + "</div><div>"
            + panel("Reference answer", row.get("reference_answer"))
            + "</div></div>", unsafe_allow_html=True)
        st.markdown(panel("Retrieved context",
                          row.get("context") or "(no context retrieved)"),
                    unsafe_allow_html=True)

        st.markdown('<div class="ae-label">Judge reasoning</div>',
                    unsafe_allow_html=True)
        mv = row.get("metric_values") or {}
        applicable = row_metrics(row, policy)
        metrics = applicable if policy["version"] == 2 else logic.METRICS
        shown = False
        for m in metrics:
            v = _score(mv.get(m), policy["version"])
            reason = logic.judge_reason(row.get("scores"), m)
            entry = (row.get("scores") or {}).get(m) or {}
            if isinstance(entry, dict) and entry.get("error"):
                reason = "Judge error: " + str(entry["error"])
            if v is None and not reason and m not in applicable:
                continue
            shown = True
            chip = _metric_chip(row, m, policy, label=True)
            st.markdown(
                f'<div class="ae-reason">{chip}'
                f'<span class="ae-reason-text">{_esc(reason or "")}</span></div>',
                unsafe_allow_html=True)
        if not shown:
            st.caption("No selected metrics apply to this case." if policy["version"] == 2
                       and not applicable else "No judge output recorded for this case.")

    _dialog()
