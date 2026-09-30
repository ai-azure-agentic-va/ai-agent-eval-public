"""Run page: pick a dataset + application, see the pre-run estimate, start."""
from __future__ import annotations

import html

import streamlit as st

from service.execution import EXECUTION_LABELS
from service.metrics import (
    DEFAULT_THRESHOLD,
    METRIC_KEYS,
    is_valid_score,
    normalize_selected_metrics,
)
from ui import client, components, logic


def _saved_thresholds() -> dict[str, float]:
    """Keep applied values independent of temporary editor widget state."""
    previous = st.session_state.get("run_thresholds", {})
    values = {metric: previous.get(metric, st.session_state.get(
        f"threshold_{metric}", DEFAULT_THRESHOLD)) for metric in METRIC_KEYS}
    st.session_state["run_thresholds"] = {
        metric: float(value) if is_valid_score(value) else DEFAULT_THRESHOLD
        for metric, value in values.items()
    }
    return dict(st.session_state["run_thresholds"])


def _render_metric_settings(allowed_metrics: list[str] | None = None) -> list[str]:
    allowed = list(METRIC_KEYS) if allowed_metrics is None else allowed_metrics
    previous_allowed = st.session_state.get("run_metrics_allowed")
    previous_selection = st.session_state.get("run_metrics", allowed)
    selected = [metric for metric in allowed if metric in previous_selection]
    changed = ((previous_allowed is not None and previous_allowed != allowed)
               or selected != previous_selection)
    if changed or not selected:
        _close_threshold_editor()
    if changed:
        st.info("The available metrics have changed. Review your selection before starting.")
    st.session_state["run_metrics_allowed"] = list(allowed)
    st.session_state["run_metrics"] = selected
    st.session_state["run_metrics_selection"] = list(selected)

    def save_selection() -> None:
        values = st.session_state["run_metrics_selection"]
        st.session_state["run_metrics"] = [metric for metric in allowed if metric in values]

    st.multiselect(
        "Metrics to evaluate", allowed, key="run_metrics_selection",
        format_func=logic.METRIC_LABELS.get, on_change=save_selection,
        disabled=bool(st.session_state.get("threshold_editor_open")),
        help="Choose from the metrics enabled for this environment. "
             "Only selected metrics are judged and included in pass/fail. "
             "Apply or cancel threshold edits before changing this selection.")
    rules = {"fallback": "Fallback applies to honesty cases.",
             "jailbreak": "Jailbreak applies to security cases.",
             "context_recall": "Context recall requires a reference answer."}
    if notes := [note for metric, note in rules.items() if metric in allowed]:
        st.caption(" ".join(notes))
    if not selected:
        st.warning("Select at least one metric to start a run.")
    return list(selected)


def _open_threshold_editor() -> None:
    for field in (*METRIC_KEYS, "mode", "all"):
        st.session_state.pop(f"threshold_draft_{field}", None)
    st.session_state["threshold_editor_open"] = True


def _close_threshold_editor() -> None:
    st.session_state["threshold_editor_open"] = False


def _render_threshold_editor(selected_metrics: list[str]) -> None:
    saved = {key: value for key, value in _saved_thresholds().items() if key in selected_metrics}
    uniform = len(set(saved.values())) == 1
    st.caption("Values: 0.00–1.00. Save with Apply thresholds.")
    mode = st.radio("Set thresholds", ["One value for all", "Per metric"],
                    index=0 if uniform else 1, horizontal=True, key="threshold_draft_mode")
    if mode == "One value for all":
        value = st.number_input(
            "Threshold for selected metrics", min_value=0.0, max_value=1.0,
            value=next(iter(saved.values())) if uniform else DEFAULT_THRESHOLD,
            step=0.01, format="%.2f", key="threshold_draft_all")
        thresholds = dict.fromkeys(selected_metrics, value)
        st.caption(f"Applies to the {len(selected_metrics)} selected metrics. Default: 0.80.")
    else:
        thresholds = {}
        columns = st.columns(2)
        for index, metric in enumerate(selected_metrics):
            with columns[index % 2]:
                thresholds[metric] = st.number_input(
                    logic.METRIC_LABELS[metric], min_value=0.0, max_value=1.0,
                    value=saved[metric], step=0.01, format="%.2f",
                    key=f"threshold_draft_{metric}", help=logic.metric_definition(metric, 2))

    apply, cancel = st.columns(2)
    if apply.button("Apply thresholds", type="primary", width="stretch"):
        st.session_state["run_thresholds"] = {**_saved_thresholds(), **thresholds}
        _close_threshold_editor()
        st.rerun()
    if cancel.button("Cancel", width="stretch"):
        _close_threshold_editor()
        st.rerun()


@st.fragment
def _render_threshold_settings(selected_metrics: list[str] | None = None) -> dict[str, float]:
    selected_metrics = list(METRIC_KEYS) if selected_metrics is None else selected_metrics
    saved = _saved_thresholds()
    thresholds = {key: saved[key] for key in selected_metrics}
    if not thresholds:
        st.button("Set thresholds", disabled=True, help="Select at least one metric first.")
        return thresholds
    if len(set(thresholds.values())) == 1:
        summary = f"Thresholds: {next(iter(thresholds.values())):.2f} for all selected metrics"
    else:
        summary = (f"Thresholds: custom ({min(thresholds.values()):.2f}–"
                   f"{max(thresholds.values()):.2f})")
    st.caption(summary)
    editing = bool(st.session_state.get("threshold_editor_open"))
    if st.button("Set thresholds", disabled=editing,
                 help="Set one value for selected metrics or customize each metric."):
        _open_threshold_editor()
        st.rerun()
    if editing:
        with st.container(border=True, key="ae_threshold_editor"):
            st.markdown("#### Metric thresholds")
            _render_threshold_editor(selected_metrics)
    return thresholds


def render_run() -> None:
    _, mid, _ = st.columns([1, 2.2, 1])
    with mid, st.container(key="ae_card_run"):
        st.markdown('<div class="ae-card-title">Start an evaluation run</div>',
                    unsafe_allow_html=True)
        try:
            datasets = client.datasets()
            applications = [
                application for application in client.applications()
                if application.get("is_active")
            ]
        except (client.ServiceUnreachable, client.ApiError) as exc:
            components.show_service_error(exc)
            return
        health = components.cached_health()
        if health is None:
            st.error("Metric settings are unavailable. Check the evaluation service connection.")
            return
        if error := health.get("metric_config_error"):
            st.error(f"Metric settings are unavailable: {error}")
            return
        try:
            configured_metrics = health.get("allowed_metrics", list(METRIC_KEYS))
            if configured_metrics is None:
                raise ValueError("Missing allowed metrics")
            allowed_metrics = normalize_selected_metrics(configured_metrics)
        except ValueError:
            st.error("Metric settings are unavailable. Check the evaluation service configuration.")
            return

        if not datasets:
            st.info("No datasets yet — upload one on the Datasets page.")
            return
        if not applications:
            st.warning("No active applications are registered with the eval service.")
            return

        ds = st.selectbox(
            "Dataset", datasets,
            format_func=lambda d: (
                f"{d['name']} (v{d['version']}, {d['case_count']} cases)"
            ))
        app = st.selectbox(
            "Application", applications,
            format_func=lambda a: a.get("display_name") or a["key"])

        cases = ds["case_count"]
        mode = st.radio("Query execution", list(EXECUTION_LABELS),
                        format_func=EXECUTION_LABELS.get, key="run_execution_mode")
        if mode == "conversation":
            st.info("Multi-turn: every row continues one shared conversation in spreadsheet "
                    "order. Choose this for multi-turn datasets. Use a separate run for each "
                    "conversation. An agent error stops the sequence; start a new run to retry.")
        elif mode == "sequential":
            st.caption("One question at a time, in spreadsheet order. Each question starts "
                       "a fresh conversation. For follow-up questions, select shared conversation.")
        else:
            st.caption("Independent questions run concurrently. Execution order is not guaranteed; "
                       "do not use this for context-dependent tests.")
        selected_metrics = _render_metric_settings(allowed_metrics)
        st.caption("Scores: 0–1. A case passes only when every selected, applicable metric meets its "
                   "threshold (score ≥ threshold). Higher Bias freedom means less bias.")
        thresholds = _render_threshold_settings(selected_metrics)
        info = {
            "Agent under test": app.get("deployment_url", "…"),
            "Judge model": health.get("judge", "…"),
            "Cases": str(cases),
        }
        if len(selected_metrics) == len(METRIC_KEYS):
            info.update({"Est. duration": logic.est_duration_label(cases),
                         "Est. tokens": logic.est_tokens_label(cases)})
        st.markdown(
            '<div class="ae-kv">' + "".join(
                f'<div class="ae-kv-k">{html.escape(k)}</div>'
                f'<div class="ae-kv-v">{html.escape(str(v))}</div>'
                for k, v in info.items()) + "</div>",
            unsafe_allow_html=True)

        editing = bool(st.session_state.get("threshold_editor_open"))
        if st.button("Run evaluation", type="primary", width="stretch",
                     disabled=editing or not selected_metrics,
                     help="Apply or cancel your threshold edits before starting." if editing else None):
            try:
                with st.spinner("Starting…"):
                    run = client.start_run(ds["dataset_id"], app["key"], ds["version"],
                                           token=components.bearer_token(), thresholds=thresholds,
                                           execution_mode=mode, selected_metrics=selected_metrics)
                st.session_state["view_run_id"] = run["run_id"]
                st.switch_page(st.session_state["_pages"]["results"])
            except client.ServiceUnreachable as exc:
                st.error(str(exc))
            except client.ApiError as exc:
                components.bounce_unauthorized(exc)
                st.error(f"Could not start run: {exc}")
