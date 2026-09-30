"""Evaluations Framework — Streamlit UI entry point.

Run (from the repo root):
    .venv/bin/streamlit run ui/app.py
Configuration:
    EVAL_API_URL — eval service base URL (default http://127.0.0.1:8100)
    AUTH_SECRET  — shared HMAC secret (must match the service's)
    DISABLE_AUTH — set truthy to skip SSO entirely
    LOG_LEVEL / LOG_FORMAT / LOG_FILE — same logging knobs as the service

Streamlit puts only ``ui/`` on sys.path, so the repo root is added here to
make ``service.metrics`` (the shared metric list) importable.
"""
from __future__ import annotations

import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main() -> None:
    import streamlit as st

    from service import config as _config  # noqa: F401 — loads .env once for the UI process too
    from service.logging_config import configure_logging

    configure_logging("ui")
    log = logging.getLogger("ui.app")

    from ui import auth, client, components
    from ui.page_datasets import render_datasets
    from ui.page_results import render_results
    from ui.page_run import render_run

    st.set_page_config(page_title=components.APP_TITLE, page_icon="🧪",
                       layout="wide", initial_sidebar_state="collapsed")
    components.inject_css()

    session = None
    if auth.sso_enabled():
        status, session = auth.gate(auth._context_cookies(), False,
                                    os.getenv("AUTH_SECRET", ""))
        forced_note = auth.forced_login_note()
        if status != "ok" or forced_note:
            log.info("login card shown: gate=%s forced=%s", status, bool(forced_note))
            note = forced_note or (auth.EXPIRED_NOTE if status == "expired"
                                   else None)
            auth.render_login_card(note)
            st.stop()

    pages = {
        "datasets": st.Page(render_datasets, title="Datasets",
                            url_path="datasets", default=True),
        "run": st.Page(render_run, title="Run", url_path="run"),
        "results": st.Page(render_results, title="Results", url_path="runs"),
    }
    st.session_state["_pages"] = pages
    nav = st.navigation(list(pages.values()), position="hidden")
    components.render_nav(pages, nav)

    with st.sidebar:
        st.markdown(f"## {components.APP_TITLE}")
        if session is not None:
            st.markdown(f"**{session.get('name') or 'Signed in'}**")
            if session.get("upn"):
                st.caption(session["upn"])
            st.markdown(f'<a href="{auth.logout_url()}" target="_self">'
                        'Sign out</a>', unsafe_allow_html=True)
        else:
            st.text_input(
                "Bearer token (optional)", key="bearer_token", type="password",
                help="Forwarded as the Authorization header when starting or "
                     "resuming runs; the eval service passes it through to the "
                     "agent under test. Leave blank if the agent doesn't need it.")
        st.caption(f"Eval service: {client.base_url()}")
        if h := components.cached_health():
            st.caption(f"Judge: {h.get('judge', '—')} · "
                       f"Storage: {h.get('storage', '—')}")
        else:
            log.warning("eval service unreachable at %s", client.base_url())
            st.warning("Eval service unreachable — is it running?")

    nav.run()


if __name__ == "__main__":
    main()
