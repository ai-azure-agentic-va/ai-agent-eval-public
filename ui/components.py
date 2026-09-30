"""Shared Streamlit styles and presentation helpers."""
from __future__ import annotations

import html
from typing import Any, Mapping, Sequence

import streamlit as st

from ui import auth, client, logic

CHIP_CLASS = {"green": "ae-chip-green", "amber": "ae-chip-amber",
              "red": "ae-chip-red", "gray": "ae-chip-gray"}
APP_TITLE = "Evaluations Framework"

_GLOBAL_CSS = """
<style>
@import url('https://api.fontshare.com/v2/css?f[]=satoshi@400,500,700,900&display=swap');

/* Satoshi everywhere; restore the icon + mono fonts the wildcard clobbers. */
* { font-family: "Satoshi", system-ui, -apple-system, BlinkMacSystemFont,
    "Segoe UI", sans-serif; }
[data-testid="stIconMaterial"] { font-family: "Material Symbols Rounded" !important; }
code, pre, kbd, samp, .ae-link {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
    "Liberation Mono", monospace !important; }

/* Hide default Streamlit chrome; center the content column. */
#MainMenu, footer, [data-testid="stDecoration"], [data-testid="stStatusWidget"],
[data-testid="stToolbar"] { display: none !important; }
/* The header is a fixed transparent overlay ON TOP of the nav row — without
   pointer-events:none it swallows hover/clicks over the pills (only the
   sliver below the header edge stays clickable). */
[data-testid="stHeader"] { background: transparent; pointer-events: none; }
/* Streamlit fades "stale" elements to ~30% opacity during (fragment) reruns —
   with a 3 s live poll the page spends most of its time washed out and
   looks blank while processing. Keep prior content
   fully visible; the refreshed data simply replaces it in place. */
[data-stale="true"], [data-stale="true"] *,
.stale-element, .stale-element * { opacity: 1 !important; }
[data-testid="stMainBlockContainer"] {
  max-width: 1400px; margin: 0 auto;
  padding-top: 1.25rem; padding-bottom: 3rem; }
[data-testid="stMainBlockContainer"]:has(.st-key-ae_results) {
  max-width: none; padding-left: 1.5rem; padding-right: 1.5rem; }
.st-key-ae_results .ae-case { min-width: 240px; overflow-wrap: anywhere; }
.st-key-ae_results .ae-tablewrap { max-height: 70vh; overflow: auto; }
.ae-chip { overflow-wrap: anywhere; }
.ae-tile-value { overflow-wrap: anywhere; max-width: 20ch; }
.st-key-ae_threshold_editor [data-testid="stHorizontalBlock"] { flex-wrap: nowrap; }
.st-key-ae_threshold_editor [data-testid="stColumn"] {
  min-width: 0 !important; flex: 1 1 0 !important; width: 0 !important; }
@media (max-width: 768px) {
  [data-testid="stMainBlockContainer"]:has(.st-key-ae_results) {
    padding-left: .75rem; padding-right: .75rem; }
  .ae-panel-grid { grid-template-columns: 1fr !important; }
}

/* --- top nav: brand + pills (active = solid black rounded-full) ------------ */
.st-key-ae_nav { border-bottom: 1px solid #e5e7eb; padding-bottom: 10px; }
.ae-brand { font-weight: 700; font-size: 16px; color: #111827;
  margin-right: 10px; white-space: nowrap; }
.st-key-ae_nav button[data-testid="stBaseButton-primary"] {
  background: #111827 !important; color: #fff !important; border: none;
  border-radius: 9999px; padding: 2px 16px; min-height: 32px;
  font-size: 14px; font-weight: 500; box-shadow: none; }
.st-key-ae_nav button[data-testid="stBaseButton-primary"]:hover {
  background: #1f2937 !important; }
.st-key-ae_nav button[data-testid="stBaseButton-tertiary"] {
  color: #4b5563; border-radius: 9999px; padding: 2px 16px; min-height: 32px;
  font-size: 14px; font-weight: 500; }
.st-key-ae_nav button[data-testid="stBaseButton-tertiary"]:hover {
  background: #f3f4f6; color: #111827; }

/* --- cards ------------------------------------------------------------------ */
div[class*="st-key-ae_card"] {
  background: #fff; border: 1px solid #e5e7eb; border-radius: 12px;
  padding: 20px; box-shadow: 0 1px 2px rgba(16, 24, 40, .04); }
div[class*="st-key-ae_card_upload"] { border: 2px dashed #d1d5db; }
.ae-card-title { font-size: 17px; font-weight: 700; color: #111827;
  margin: 0 0 2px; }
.ae-help { font-size: 13.5px; color: #6b7280; margin: 0 0 6px; }
.ae-ok { color: #15803d; font-size: 14px; margin: 4px 0; }

/* --- buttons / inputs -------------------------------------------------------- */
button[data-testid="stBaseButton-secondary"] {
  border-radius: 8px; border: 1px solid #d1d5db; background: #fff;
  font-weight: 600; }
button[data-testid="stBaseButton-secondary"]:hover {
  border-color: #9ca3af; color: #111827; }
button[data-testid="stBaseButton-primary"] { border-radius: 8px; font-weight: 600; }
[data-baseweb="select"] > div, [data-testid="stTextInput"] input,
[data-baseweb="input"] { border-radius: 8px; }
/* Inputs must be visible at rest, not only on focus: the light theme leaves
   them white-on-white without an explicit resting border. */
[data-testid="stTextInput"] [data-baseweb="input"],
[data-baseweb="select"] > div {
  border: 1px solid #d1d5db !important;
  background: #ffffff !important;
}
[data-testid="stTextInput"] [data-baseweb="input"]:focus-within,
[data-baseweb="select"] > div:focus-within {
  border-color: #003b75 !important;
}
.st-key-ae_card_upload [data-testid="stFileUploaderDropzone"] {
  background: transparent; border: none; padding: 4px 0;
  /* Streamlit opens the picker on its full-width dropzone, including whitespace.
     Keep click/drop targets on the visible buttons (including remove-file). */
  pointer-events: none; }
.st-key-ae_card_upload [data-testid="stFileUploaderDropzone"] button {
  pointer-events: auto; }

/* --- tables: small gray UPPERCASE headers, thin row dividers ----------------- */
.ae-tablewrap { overflow-x: auto; width: 100%; }
.ae-tablewrap-scroll { max-height: 280px; overflow: auto; width: 100%;
  border: 1px solid #e5e7eb; border-radius: 8px; }
table.ae-table { width: 100%; border-collapse: collapse; text-align: left;
  font-size: 14px; }
.ae-table thead th { position: sticky; top: 0; background: #f3f4f6;
  color: #6b7280; font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: .04em; padding: 10px 12px; white-space: nowrap; }
.ae-table.ae-table-plain thead th { text-transform: none; letter-spacing: 0;
  font-size: 12px; }
.ae-table tbody td { border-top: 1px solid #f3f4f6; padding: 10px 12px;
  vertical-align: top; color: #111827; }
.ae-table tbody tr:hover td { background: #f9fafb; }
.ae-table tbody tr.ae-row-failed td { background: #fef2f2; }
.ae-table tbody tr.ae-row-failed:hover td { background: #fee2e2; }
.ae-table tbody tr.ae-pending td { color: #9ca3af; }
.ae-caret { color: #9ca3af; font-size: 9px; }
.ae-info { color: #9ca3af; cursor: help; font-style: normal; }
.ae-empty { color: #9ca3af; padding: 20px 12px !important; }
.ae-muted { color: #6b7280; }
.ae-dim { color: #9ca3af; }
.ae-nowrap { white-space: nowrap; }
.ae-center { text-align: center; }
.ae-right { text-align: right; }
.ae-upper { text-transform: uppercase; color: #4b5563; }
.ae-status { font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: .03em; }
.ae-link { color: #1d4ed8; text-decoration: underline; font-size: 13px; }
.ae-case { min-width: 280px; max-width: 640px; }
.ae-case-link { color: inherit; text-decoration: none; display: block; }
.ae-case-link:hover { color: #003b75; }
.ae-retried { font-size: 12px; color: #9ca3af; margin-left: 4px; }

/* --- chips / pills ------------------------------------------------------------ */
.ae-chip { display: inline-block; border-radius: 6px; padding: 2px 6px;
  font-size: 12px; font-weight: 600; min-width: 44px; text-align: center;
  font-variant-numeric: tabular-nums; }
.ae-chip-green { background: #dcfce7; color: #166534; }
.ae-chip-amber { background: #fef3c7; color: #92400e; }
.ae-chip-red { background: #fee2e2; color: #991b1b; }
.ae-chip-gray { background: transparent; color: #9ca3af; font-weight: 400; }
.ae-pill { display: inline-flex; align-items: center; gap: 6px;
  border-radius: 9999px; padding: 3px 10px; font-size: 12px; font-weight: 500;
  white-space: nowrap; }
.ae-pill-green { background: #ecfdf5; color: #047857;
  box-shadow: inset 0 0 0 1px #a7f3d0; }
.ae-pill-blue { background: #eff6ff; color: #1d4ed8;
  box-shadow: inset 0 0 0 1px #bfdbfe; }
.ae-pill-red { background: #fef2f2; color: #b91c1c;
  box-shadow: inset 0 0 0 1px #fecaca; }
.ae-pill-gray { background: #f3f4f6; color: #4b5563; }
.ae-pill-indigo { background: #eef2ff; color: #4338ca; }
.ae-dot { width: 6px; height: 6px; border-radius: 9999px; display: inline-block;
  background: currentColor; }
.ae-dot-pulse { animation: ae-pulse 1.6s ease-in-out infinite; }
@keyframes ae-pulse { 50% { opacity: .35; } }

/* --- run-detail header + stat tiles ------------------------------------------- */
.ae-run-title { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.ae-run-title h2 { margin: 0; font-size: 20px; font-weight: 600;
  letter-spacing: -.01em; }
.ae-runid { font-family: ui-monospace, Menlo, monospace !important;
  font-size: 12px; color: #9ca3af; }
.ae-tiles { display: flex; flex-wrap: wrap; gap: 14px 36px; padding: 8px 0 0; }
.ae-tile-label { font-size: 12px; font-weight: 500; color: #6b7280;
  white-space: nowrap; }
.ae-tile-value { font-size: 24px; font-weight: 600; letter-spacing: -.01em;
  color: #111827; font-variant-numeric: tabular-nums; }
.ae-tile-divider { width: 1px; background: #e5e7eb; align-self: stretch; }
.ae-text-green { color: #059669; } .ae-text-amber { color: #d97706; }
.ae-text-red { color: #dc2626; } .ae-text-gray { color: #9ca3af; }

/* --- run form key/value panel --------------------------------------------------- */
.ae-kv { background: #f9fafb; border-radius: 8px; padding: 14px 16px;
  display: grid; grid-template-columns: auto 1fr; gap: 10px 24px;
  font-size: 14px; }
.ae-kv-k { color: #6b7280; white-space: nowrap; }
.ae-kv-v { color: #111827; overflow-wrap: anywhere; }

/* --- case-detail dialog panels --------------------------------------------------- */
[data-testid="stDialog"] div[role="dialog"] { border-radius: 16px; }
.ae-label { font-size: 13.5px; font-weight: 700; color: #111827;
  margin: 8px 0 4px; }
.ae-panel { background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px;
  padding: 10px 12px; max-height: 260px; overflow: auto;
  white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13.5px;
  color: #1f2937; }
.ae-panel-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.ae-reason { border: 1px solid #e5e7eb; border-radius: 8px; padding: 8px 10px;
  margin: 6px 0; font-size: 13.5px; }
.ae-reason .ae-chip { margin-right: 6px; }
.ae-reason-text { color: #4b5563; }
</style>
"""


def inject_css() -> None:
    """Install the shared stylesheet for the current rerun."""
    st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)


def render_nav(pages: Mapping[str, Any], current: Any) -> None:
    """Render the application header and page navigation."""
    with st.container(horizontal=True, vertical_alignment="center",
                      key="ae_nav"):
        st.markdown(f'<span class="ae-brand">{APP_TITLE}</span>',
                    unsafe_allow_html=True)
        for key, page in pages.items():
            active = page.url_path == current.url_path
            clicked = st.button(page.title, key=f"ae_nav_{key}",
                                type="primary" if active else "tertiary")
            if clicked and not active:
                st.switch_page(page)


@st.cache_data(ttl=15, show_spinner=False)
def cached_health() -> dict | None:
    """Health probe shared by the sidebar and the run-detail badge; None when
    the service is unreachable (never raises)."""
    try:
        return client.health()
    except Exception:
        return None


def bounce_unauthorized(exc: Exception) -> None:
    """Force a new login after an authenticated service call returns 401."""
    if (auth.sso_enabled() and isinstance(exc, client.ApiError)
            and exc.status == 401):
        auth.force_login()


def show_service_error(exc: Exception) -> None:
    """Friendly, stack-trace-free error banner for any client failure."""
    bounce_unauthorized(exc)
    if isinstance(exc, client.ServiceUnreachable):
        st.error(str(exc))
    elif isinstance(exc, client.ApiError):
        detail = exc.detail if isinstance(exc.detail, str) else f"HTTP {exc.status}"
        st.error(f"Eval service error — {detail}")
    else:  # pragma: no cover - defensive
        st.error("Something went wrong talking to the eval service — is it running?")


def bearer_token() -> str | None:
    """The bearer for agent-bound calls (runs, resume, dataset uploads).
    SSO mode: always the session cookie's access token — the real caller
    identity stamps triggered_by/uploader. DISABLE_AUTH mode: the manual
    sidebar field, as before."""
    if auth.sso_enabled():
        return auth.access_token()
    return st.session_state.get("bearer_token") or None


def sort_control(state_key: str, columns: Sequence[tuple[str, str]],
                 default_key: str, default_dir: int = 1,
                 offset_key: str | None = None) -> dict:
    """Sort the current snapshot with one widget rerun, resetting pagination."""
    state = st.session_state.setdefault(
        state_key, {"key": default_key, "dir": default_dir})
    labels = {k: lbl for k, lbl in columns}
    keys = [k for k, _ in columns]
    if state["key"] not in keys:
        state = {"key": default_key, "dir": default_dir}
        st.session_state[state_key] = state
    # Session state owns the selection, including resets and restored sorts.
    # Supplying an index default as well makes Streamlit log a duplicate-value warning.
    st.session_state[f"{state_key}_col"] = state["key"]

    def select_column() -> None:
        st.session_state[state_key] = {"key": st.session_state[f"{state_key}_col"], "dir": 1}
        if offset_key:
            st.session_state[offset_key] = 0

    def toggle_direction() -> None:
        current = st.session_state[state_key]
        st.session_state[state_key] = {**current, "dir": -current["dir"]}
        if offset_key:
            st.session_state[offset_key] = 0

    c1, c2, _ = st.columns([3, 1, 4])
    with c1:
        st.selectbox(
            "Sort by", keys, index=None,
            format_func=lambda k: labels[k], key=f"{state_key}_col",
            label_visibility="collapsed", on_change=select_column)
    with c2:
        asc = state["dir"] == 1
        st.button("↑ asc" if asc else "↓ desc", key=f"{state_key}_dir",
                  help="Toggle sort direction", width="stretch", on_click=toggle_direction)
    return state


def pager(offset_key: str, total: int, size: int) -> int:
    """Reference table footer: "1–10 of 13" left, outlined Previous/Next
    buttons right; returns the clamped page start."""
    offset = st.session_state.get(offset_key, 0)
    start = logic.clamp_page_start(offset, total, size) if total else 0
    if total <= size:
        return start
    c1, _, c2, c3 = st.columns([3, 3, 1, 1], vertical_alignment="center")
    c1.markdown(f'<span style="font-size:13px;color:#4b5563">'
                f'{start + 1}–{min(start + size, total)} of {total}</span>',
                unsafe_allow_html=True)
    c2.button("Previous", key=f"{offset_key}_prev", disabled=start == 0,
              width="stretch", on_click=st.session_state.update,
              kwargs={offset_key: max(0, start - size)})
    c3.button("Next", key=f"{offset_key}_next", disabled=start + size >= total,
              width="stretch", on_click=st.session_state.update,
              kwargs={offset_key: start + size})
    return start


def table_html(columns: Sequence[Mapping[str, Any]], rows: Sequence[str],
               sort: Mapping[str, Any] | None = None, empty: str = "",
               plain_head: bool = False, scroll: bool = False) -> str:
    """One HTML table, reference-styled (small gray UPPERCASE headers with a
    ▲/▼ caret on the active sort column, thin row dividers).

    columns: [{label, key?, tip?, class?}] — `key` matches the sort state for
    the caret; `tip` renders a ⓘ with a title tooltip.
    rows: prebuilt <tr>…</tr> strings — CALLERS MUST html.escape all data.
    """
    ths = []
    for col in columns:
        label = html.escape(str(col["label"]))
        tip = col.get("tip")
        info = (f' <span class="ae-info" title="{html.escape(tip, quote=True)}">'
                f'ⓘ</span>') if tip else ""
        caret = ""
        if sort is not None and col.get("key") and sort.get("key") == col["key"]:
            caret = (' <span class="ae-caret">▲</span>' if sort.get("dir", 1) == 1
                     else ' <span class="ae-caret">▼</span>')
        cls = f' class="{col["class"]}"' if col.get("class") else ""
        ths.append(f"<th{cls}>{label}{info}{caret}</th>")
    body = "".join(rows) if rows else (
        f'<tr><td class="ae-empty" colspan="{len(columns)}">'
        f"{html.escape(empty)}</td></tr>")
    table_cls = "ae-table ae-table-plain" if plain_head else "ae-table"
    wrap_cls = "ae-tablewrap-scroll" if scroll else "ae-tablewrap"
    return (f'<div class="{wrap_cls}"><table class="{table_cls}">'
            f'<thead><tr>{"".join(ths)}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


def score_chip(v: float | None, label: str | None = None,
               tip: str | None = None, *, threshold: float = 4, version: int = 1) -> str:
    """Small rounded score chip: soft green ≥4, soft amber ≥3, soft red <3,
    plain gray em dash for structural blanks — reference scoreClass."""
    band = logic.score_band(v, threshold, version)
    text = logic.score_display(v, threshold, version)
    if band != "gray":
        precision_tip = f"Raw score: {v}. Display rounded to 2 decimal places; verdict uses the raw score."
        tip = f"{tip} {precision_tip}" if tip else precision_tip
    if label:
        text = f"{label}: {text}"
    title = f' title="{html.escape(tip, quote=True)}"' if tip else ""
    return (f'<span class="ae-chip {CHIP_CLASS[band]}"{title}>'
            f"{html.escape(text)}</span>")


def status_pill(status: str | None) -> str:
    """Rounded status pill with a leading dot: green "• Completed", blue
    pulsing "• In Progress", red "• Failed" — reference detail-header pill."""
    label = logic.status_label(status)
    cls = {"running": "ae-pill-blue", "queued": "ae-pill-blue",
           "failed": "ae-pill-red", "completed": "ae-pill-green"}.get(
        status or "", "ae-pill-gray")
    pulse = " ae-dot-pulse" if status in ("running", "queued") else ""
    return (f'<span class="ae-pill {cls}"><span class="ae-dot{pulse}"></span>'
            f"{html.escape(label)}</span>")


def stat_tiles(tiles: Sequence[Mapping[str, Any]]) -> None:
    """Metrics strip: small gray label (+ ⓘ tooltip) over a BIG value. Values
    wear the score-band colour; identity stays in neutral ink. An entry of
    {"divider": True} renders the thin vertical separator."""
    cells = []
    for t in tiles:
        if t.get("divider"):
            cells.append('<div class="ae-tile-divider"></div>')
            continue
        tip = t.get("tooltip") or ""
        info = (f' <span class="ae-info" title="{html.escape(tip, quote=True)}">'
                f'ⓘ</span>') if tip else ""
        band = t.get("band")
        vcls = f"ae-tile-value ae-text-{band}" if band else "ae-tile-value"
        cells.append(
            f'<div><div class="ae-tile-label">{html.escape(str(t["label"]))}'
            f'{info}</div><div class="{vcls}">{html.escape(str(t["value"]))}'
            f"</div></div>")
    st.markdown(f'<div class="ae-tiles">{"".join(cells)}</div>',
                unsafe_allow_html=True)
