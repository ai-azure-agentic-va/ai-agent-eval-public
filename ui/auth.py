"""Verify the service-issued session cookie and render Streamlit auth UI."""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import time
from typing import Mapping
from urllib.parse import quote, urlsplit, urlunsplit

SESSION_COOKIE = "eval_session"
DEFAULT_UI_URL = "http://localhost:8501"

EXPIRED_NOTE = "Your session expired — please sign in again."


def sso_enabled() -> bool:
    return os.getenv("DISABLE_AUTH", "").strip().lower() not in (
        "1", "true", "yes", "on")


def verify_session(cookie_value: str | None, secret: str,
                   now: float | None = None,
                   check_exp: bool = True) -> dict | None:
    """None on any tamper/garbage/(expiry); payload dict on success.

    Envelope matches the reference signSession byte format: unpadded base64url
    of {"data": <payload JSON string>, "sig": <HMAC-SHA256 hex of data>}.
    `exp` (unix seconds) is enforced when present and check_exp is True.
    """
    if not cookie_value or not secret:
        return None
    try:
        padded = cookie_value + "=" * (-len(cookie_value) % 4)
        wrapper = json.loads(base64.urlsafe_b64decode(padded))
        data, sig = wrapper["data"], wrapper["sig"]
        expected = hmac.new(secret.encode(), data.encode(),
                            hashlib.sha256).hexdigest()
        if not isinstance(sig, str) or not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(data)
        if not isinstance(payload, dict):
            return None
    except Exception:
        return None
    if check_exp and "exp" in payload:
        try:
            if (time.time() if now is None else now) >= float(payload["exp"]):
                return None
        except (TypeError, ValueError):
            return None
    return payload


def gate(cookies: Mapping[str, str], disable_auth: bool, secret: str,
         now: float | None = None) -> tuple[str, dict | None]:
    """Pure auth-gate decision: ("ok", session) to render the app,
    ("login", None) to show the sign-in card, ("expired", None) to show it
    with the session-expired note (valid signature, dead exp)."""
    if disable_auth:
        return ("ok", None)
    raw = cookies.get(SESSION_COOKIE, "")
    signed_session = verify_session(raw, secret, now=now, check_exp=False)
    if signed_session is None:
        return ("login", None)
    session = verify_session(raw, secret, now=now)
    return ("ok", session) if session is not None else ("expired", None)


def _context_cookies() -> Mapping[str, str]:
    import streamlit as st

    try:
        return st.context.cookies
    except Exception:
        return {}


def current_user(cookies: Mapping[str, str] | None = None,
                 now: float | None = None) -> dict | None:
    """The verified session payload ({sub, name, upn, access_token, exp}) or
    None. `cookies` is injectable for tests; defaults to st.context.cookies."""
    secret = os.getenv("AUTH_SECRET", "")
    if cookies is None:
        cookies = _context_cookies()
    return verify_session(cookies.get(SESSION_COOKIE, ""), secret, now=now)


def access_token() -> str | None:
    session = current_user()
    return (session or {}).get("access_token") or None


def streamlit_url() -> str:
    """The browser's current URL (st.context.url when available) — used both
    as the `next` target and to pick the hostname for the login link."""
    import streamlit as st

    try:
        url = getattr(st.context, "url", None)
        if url:
            return str(url)
    except Exception:
        pass
    return os.getenv("STREAMLIT_URL", DEFAULT_UI_URL)


def _service_base_for_browser(browser_url: str | None = None) -> str:
    """EVAL_API_URL with its hostname swapped to the one the BROWSER is using
    for Streamlit. Cookies are per-host: if the user browses localhost:8501
    but EVAL_API_URL says 127.0.0.1:8100, a cookie minted on 127.0.0.1 would
    be invisible to the localhost websocket — same-host keeps them together."""
    base = os.getenv("EVAL_API_URL", "http://127.0.0.1:8100").rstrip("/")
    browser_host = urlsplit(browser_url or streamlit_url()).hostname
    parts = urlsplit(base)
    if (browser_host and browser_host != parts.hostname
            and {browser_host, parts.hostname} <= {"localhost", "127.0.0.1"}):
        netloc = f"{browser_host}:{parts.port}" if parts.port else browser_host
        base = urlunsplit((parts.scheme, netloc, parts.path, "", "")).rstrip("/")
    return base


def _same_origin() -> bool:
    """Truthy AUTH_SAME_ORIGIN means the service's /auth/* routes are served
    on the SAME host:port as the Streamlit UI (the single-port docker image,
    where nginx fronts both) — so auth links must be relative and ride the one
    exposed port instead of pointing at the unexposed service port."""
    return os.getenv("AUTH_SAME_ORIGIN", "").strip().lower() in (
        "1", "true", "yes", "on")


def _auth_url(path: str) -> str:
    target = streamlit_url()
    if _same_origin():
        return f"{path}?next={quote(target, safe='')}"
    base = _service_base_for_browser(target)
    return f"{base}{path}?next={quote(target, safe='')}"


def login_url() -> str:
    return _auth_url("/auth/login")


def logout_url() -> str:
    return _auth_url("/auth/logout")


def force_login(note: str = EXPIRED_NOTE) -> None:
    """A service call 401'd mid-session (token died before the cookie's
    max-age ran out, mirroring the reference's 401 -> login behavior): flag
    the session and rerun so app.py's gate lands on the login card."""
    import streamlit as st

    st.session_state["_auth_force_login"] = note
    st.rerun()


def forced_login_note() -> str | None:
    import streamlit as st

    return st.session_state.get("_auth_force_login")


_LOGIN_CSS = """
<style>
.ae-login-card { background: #fff; border: 1px solid #e5e7eb;
  border-radius: 12px; padding: 40px 32px; text-align: center;
  box-shadow: 0 1px 2px rgba(16, 24, 40, .04); margin-top: 14vh; }
.ae-login-title { font-size: 22px; font-weight: 700; color: #111827;
  margin: 0 0 6px; }
.ae-login-sub { font-size: 14px; color: #6b7280; margin: 0 0 20px; }
.ae-login-note { font-size: 13.5px; color: #b45309; background: #fffbeb;
  border: 1px solid #fde68a; border-radius: 8px; padding: 8px 12px;
  margin: 0 0 16px; }
.ae-login-btn { display: inline-block; background: #003b75; color: #fff
  !important; border-radius: 8px; padding: 10px 22px; font-size: 15px;
  font-weight: 600; text-decoration: none !important; }
.ae-login-btn:hover { background: #00457f; }
</style>
"""


def render_login_card(note: str | None = None) -> None:
    """Centered sign-in card; target=_self keeps the OAuth redirect in this
    tab (Streamlit links default to a new tab, which would strand the user)."""
    import streamlit as st

    note_html = (
        f'<div class="ae-login-note">{html.escape(note)}</div>' if note else ""
    )
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown(
            _LOGIN_CSS
            + '<div class="ae-login-card">'
              '<div class="ae-login-title">Evaluations Framework</div>'
              '<div class="ae-login-sub">Sign in with your organization account to '
              'run and review agent evaluations.</div>'
            + note_html
            + f'<a class="ae-login-btn" href="{login_url()}" target="_self">'
              'Sign in with Microsoft</a></div>',
            unsafe_allow_html=True)
