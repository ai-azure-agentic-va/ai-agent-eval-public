"""Datasets page: upload + preview + validation gate, and the dataset registry."""
from __future__ import annotations

import html
from typing import Any

import streamlit as st

from ui import client, components, logic

PAGE_SIZE = 10

_SORT_FIELDS = {
    "name": "name", "application": "application_key", "version": "version",
    "cases": "case_count", "by": "uploader", "at": "uploaded_at",
}
_COLUMNS = [("name", "Name"), ("application", "Application"), ("version", "Version"),
            ("cases", "Cases"), ("by", "Uploaded by"), ("at", "Uploaded At")]


def _mark_name_touched() -> None:
    st.session_state["ds_name_touched"] = True


def render_datasets() -> None:
    _render_upload()
    _render_registry()


def _render_upload() -> None:
    with st.container(key="ae_card_upload"):
        st.markdown(
            '<div class="ae-card-title">Upload a golden dataset</div>'
            '<p class="ae-help">Choose a .xlsx/.xls/.csv with the upload button '
            'or drop a file onto that button. '
            'Required columns: question, reference_answer (expected_agent, '
            'tags and name optional). Repeated questions are allowed; each row is a separate case. '
            'Spreadsheet row order is preserved.</p>',
            unsafe_allow_html=True)

        nonce = st.session_state.setdefault("ds_uploader_nonce", 0)
        file = st.file_uploader("Upload file", type=["csv", "xlsx", "xls"],
                                key=f"ds_file_{nonce}", label_visibility="collapsed")
        preview = _sync_preview(file)
        apps = _load_applications()
        blocked = not preview or bool(preview["errors"])

        c1, c2, c3 = st.columns([2, 2, 1], vertical_alignment="bottom")
        with c1:
            name = st.text_input("Dataset name", key="ds_name",
                                 on_change=_mark_name_touched)
        with c2:
            app_key = st.selectbox(
                "Application", [a["key"] for a in apps],
                format_func=lambda k: next(
                    (a["display_name"] or k for a in apps if a["key"] == k), k),
                key="ds_app",
                help="Application this dataset belongs to. Inactive applications are "
                     "listed too — datasets may be curated before activation.")
        with c3:
            save = st.button("Save dataset", type="primary", width="stretch",
                             disabled=blocked or not name or file is None or not apps)

        _render_preview(preview)
        if save:
            _save_dataset(file, name, app_key)

        if msg := st.session_state.get("ds_message"):
            (st.success if msg[0] == "success" else st.error)(msg[1])


def _sync_preview(file: Any) -> dict | None:
    if file is None:
        st.session_state.pop("ds_last_file", None)
        st.session_state.pop("ds_preview", None)
        return None

    file_signature = getattr(file, "file_id", None) or (file.name, file.size)
    if st.session_state.get("ds_last_file") == file_signature:
        return st.session_state.get("ds_preview")

    st.session_state["ds_last_file"] = file_signature
    st.session_state.pop("ds_preview", None)
    st.session_state.pop("ds_message", None)
    if not st.session_state.get("ds_name_touched"):
        st.session_state["ds_name"] = logic.auto_name_from_filename(file.name)
    try:
        with st.spinner("Parsing…"):
            preview = client.preview_dataset(file.name, file.getvalue())
    except (client.ServiceUnreachable, client.ApiError):
        st.session_state["ds_message"] = (
            "error", "Preview failed — is the eval service running?"
        )
        return None
    st.session_state["ds_preview"] = preview
    return preview


def _load_applications() -> list[dict]:
    try:
        return client.applications()
    except (client.ServiceUnreachable, client.ApiError):
        st.warning("Could not load applications — is the eval service running?")
        return []


def _render_preview(preview: dict | None) -> None:
    if not preview:
        return
    if errors := preview["errors"]:
        lines = [
            f"- {('Row ' + str(error['row'])) if error.get('row') else 'File'} · "
            f"{error.get('field', '')}: {error.get('error', '')}"
            for error in errors[:12]
        ]
        st.error(
            f"{len(errors)} validation error(s) — upload blocked:\n\n"
            + "\n".join(lines)
        )
    else:
        st.markdown(
            f'<p class="ae-ok">✓ {preview["case_count"]} cases parsed, '
            'no validation errors.</p>',
            unsafe_allow_html=True,
        )
    if preview["rows"]:
        st.markdown(_preview_table(preview["rows"][:50]),
                    unsafe_allow_html=True)


def _save_dataset(file: Any, name: str, application_key: str) -> None:
    try:
        with st.spinner("Working…"):
            meta = client.upload_dataset(
                file.name,
                file.getvalue(),
                name,
                application_key,
                token=components.bearer_token(),
            )
    except client.ApiError as exc:
        components.bounce_unauthorized(exc)
        st.session_state["ds_message"] = ("error", _upload_error_message(exc))
        return
    except client.ServiceUnreachable:
        st.session_state["ds_message"] = (
            "error", "Upload failed — is the eval service running?"
        )
        return

    st.session_state["ds_message"] = (
        "success",
        f'Saved "{meta["name"]}" v{meta["version"]} '
        f'({meta["case_count"]} cases) — dataset id `{meta["dataset_id"]}`. '
        "Re-uploading the same name creates a new version.",
    )
    st.session_state["ds_uploader_nonce"] += 1
    st.session_state["ds_offset"] = 0
    st.session_state.pop("ds_preview", None)
    st.session_state.pop("ds_last_file", None)
    st.rerun()


def _upload_error_message(exc: client.ApiError) -> str:
    detail = exc.detail
    if exc.status == 400 and isinstance(detail, dict) and detail.get("errors"):
        return (f"Upload rejected — {len(detail['errors'])} validation error(s). "
                "Fix the file and retry.")
    if exc.status == 409:
        return f"Upload failed — {detail}"
    if isinstance(detail, str):
        return f"Upload failed — {detail}"
    return f"Upload failed — HTTP {exc.status}"


def _preview_table(rows: list[dict]) -> str:
    """Numbered preview table: #, question, reference_answer (muted),
    expected_agent, tags — lowercase headers."""
    cols = [{"label": "#"}, {"label": "question"},
            {"label": "reference_answer"}, {"label": "expected_agent"},
            {"label": "tags"}]
    trs = []
    for i, r in enumerate(rows, start=1):
        trs.append(
            "<tr>"
            f'<td class="ae-dim">{i}</td>'
            f"<td>{html.escape(str(r.get('question') or ''))}</td>"
            f'<td class="ae-muted">{html.escape(str(r.get("reference_answer") or ""))}</td>'
            f"<td>{html.escape(str(r.get('expected_agent') or ''))}</td>"
            f"<td>{html.escape(str(r.get('tags') or ''))}</td>"
            "</tr>")
    return components.table_html(cols, trs, plain_head=True, scroll=True)


@st.fragment
def _render_registry() -> None:
    with st.container(key="ae_card_datasets"):
        st.markdown('<div class="ae-card-title">Datasets</div>',
                    unsafe_allow_html=True)
        sort = components.sort_control("ds_sort", _COLUMNS, default_key="at",
                                       default_dir=-1, offset_key="ds_offset")
        offset = max(0, st.session_state.get("ds_offset", 0))
        request = {"limit": PAGE_SIZE, "sort_by": _SORT_FIELDS[sort["key"]],
                   "sort_dir": "asc" if sort["dir"] == 1 else "desc"}
        try:
            result = client.datasets_page(offset=offset, **request)
            start = logic.clamp_page_start(offset, result["total"], PAGE_SIZE)
            if start != offset:
                result = client.datasets_page(offset=start, **request)
            st.session_state["ds_offset"] = start
        except (client.ServiceUnreachable, client.ApiError) as exc:
            components.show_service_error(exc)
            return

        cols = [
            {"label": "Name", "key": "name"},
            {"label": "Application", "key": "application"},
            {"label": "Version", "key": "version"},
            {"label": "Cases", "key": "cases"},
            {"label": "Uploaded by", "key": "by"},
            {"label": "Uploaded At", "key": "at",
             "tip": "When this dataset's latest version was uploaded."},
        ]
        trs = []
        for d in result["datasets"]:
            trs.append(
                "<tr>"
                f'<td style="font-weight:500">{html.escape(str(d.get("name") or ""))}</td>'
                f'<td class="ae-upper">'
                f'{html.escape((d.get("application_key") or "—").upper())}</td>'
                f"<td>v{html.escape(str(d.get('version') or ''))}</td>"
                f"<td>{html.escape(str(d.get('case_count') or 0))}</td>"
                f"<td>{html.escape(str(d.get('uploader') or ''))}</td>"
                f'<td class="ae-muted ae-nowrap">'
                f'{html.escape(logic.fmt_ts(d.get("uploaded_at")))}</td>'
                "</tr>")
        st.markdown(components.table_html(
            cols, trs, sort=sort, empty="No datasets yet — upload one above."),
            unsafe_allow_html=True)
        components.pager("ds_offset", result["total"], PAGE_SIZE)
