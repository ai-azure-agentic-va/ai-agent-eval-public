"""SSO endpoints (Entra ID confidential-client flow) for the eval service.

Login/callback/logout routes for a confidential-client auth-code flow. The
SERVICE owns the OAuth dance and mints
an HMAC-signed, httpOnly session cookie on localhost; the Streamlit UI (a
different localhost port — browsers scope cookies per-host, ignoring port)
reads that cookie via st.context.cookies and verifies it with the shared
AUTH_SECRET (see ui/auth.py).

Session envelope — byte-parity with a Node signSession() implementation:
    base64url( JSON.stringify({ data: JSON.stringify(payload),
                                sig: HMAC-SHA256-hex(AUTH_SECRET, data) }) )
(no base64 padding, no whitespace in the JSON). The PAYLOAD schema
({sub, name, upn, access_token, exp}) and the cookie name (`eval_session`) are
this service's own, so other apps sharing localhost cannot clobber its sessions.

Trust model: these routes only MINT the cookie for the UI. The service stays
decode-only — config.caller_identity() reads the Authorization header without
validating it; the AGENT under test is the validator (same posture as the
reference).

The login URL is built by hand (standard v2.0 authorize URL) instead of via
msal.get_authorization_request_url(): MSAL's URL builder performs live
authority discovery (a network round-trip just to build a URL). MSAL is
used where it earns its keep — the authorization-code redemption.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import time
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from service import (
    config as _config,  # noqa: F401 — loads .env (SSO settings) before any os.environ read
)

log = logging.getLogger(__name__)
router = APIRouter()

# Distinct names — other apps' session/state cookies on the same localhost host
# must not collide with ours.
SESSION_COOKIE = "eval_session"
STATE_COOKIE = "eval_oauth_state"
STATE_MAX_AGE = 600  # seconds — reference login route sets maxAge 600
DEFAULT_UI_URL = "http://localhost:8501"

_ALLOWED_NEXT_HOSTS = {"localhost", "127.0.0.1"}


def _secret() -> str:
    return os.environ["AUTH_SECRET"]


# --- session codec (byte-parity with reference signSession/verifySession) ------

def _b64url_encode(raw: bytes) -> str:
    # Node Buffer.toString("base64url") emits NO padding.
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_session(payload: dict, secret: str | None = None) -> str:
    """Sign exactly like the reference signSession(): inner JSON.stringify of
    the payload, HMAC-SHA256 hex over that string, {data, sig} wrapper,
    unpadded base64url. separators=(",", ":") + ensure_ascii=False mirror
    JSON.stringify byte-for-byte for the same key order."""
    secret = _secret() if secret is None else secret
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    sig = hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()
    wrapper = json.dumps({"data": data, "sig": sig},
                         separators=(",", ":"), ensure_ascii=False)
    return _b64url_encode(wrapper.encode())


def verify_session(token: str, secret: str | None = None) -> dict | None:
    """Reference verifySession(): None on any tamper/garbage, payload dict on
    success. No expiry semantics here (the reference verifier has none either;
    the UI-side ui/auth.verify_session adds the exp check)."""
    try:
        secret = _secret() if secret is None else secret
        wrapper = json.loads(_b64url_decode(token))
        data, sig = wrapper["data"], wrapper["sig"]
        expected = hmac.new(secret.encode(), data.encode(),
                            hashlib.sha256).hexdigest()
        if not isinstance(sig, str) or not hmac.compare_digest(sig, expected):
            return None
        return json.loads(data)
    except Exception:
        return None


# --- helpers ---------------------------------------------------------------------

def is_safe_next(url: str | None) -> bool:
    """Allow local development URLs and the explicitly configured HTTPS UI origin."""
    if not url:
        return False
    try:
        parts = urlsplit(url)
        configured = urlsplit(os.getenv("STREAMLIT_URL", DEFAULT_UI_URL))
        if parts.username or parts.password:
            return False
        if parts.scheme == configured.scheme == "https":
            if (parts.hostname, parts.port) == (configured.hostname, configured.port):
                return True
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and parts.hostname in _ALLOWED_NEXT_HOSTS


def _redirect_uri(request: Request) -> str:
    """Callback URI on THIS service instance (http://<host>:<port>/auth/callback;
    it must be registered on your Entra App Registration). Normalize 127.0.0.1 to
    localhost so a 127.0.0.1-addressed request still matches a registered URI."""
    if configured := os.getenv("AUTH_REDIRECT_URI", "").strip():
        return configured
    host = request.url.hostname or "localhost"
    if host == "127.0.0.1":
        host = "localhost"
    port = request.url.port
    netloc = f"{host}:{port}" if port else host
    return f"http://{netloc}/auth/callback"


def _authority() -> str:
    return f"https://login.microsoftonline.com/{os.environ['ENTRA_TENANT_ID']}"


def _scopes() -> list[str]:
    api_scope = (os.getenv("API_SCOPE") or "").strip()
    return [api_scope] if api_scope else ["User.Read"]


def _secure_cookies() -> bool:
    return urlsplit(os.getenv("STREAMLIT_URL", DEFAULT_UI_URL)).scheme == "https"


@router.get("/auth/verify", include_in_schema=False)
def verify_api_session(request: Request):
    """Nginx auth_request guard; in-container UI calls remain on loopback only."""
    if not _config.env_flag("AUTH_REQUIRE_API_SESSION"):
        return Response(status_code=204)
    session = verify_session(request.cookies.get(SESSION_COOKIE, ""))
    if isinstance(session, dict):
        try:
            expires = float(session.get("exp", 0))
            if math.isfinite(expires) and expires > time.time() and session.get("access_token"):
                return Response(status_code=204)
        except (TypeError, ValueError):
            pass
    return Response(status_code=401)


def _msal_app():
    """Lazy: msal's ConfidentialClientApplication performs live authority
    discovery — only construct it on the (inherently online) callback path."""
    import msal

    return msal.ConfidentialClientApplication(
        client_id=os.environ["ENTRA_CLIENT_ID"],
        client_credential=os.environ["ENTRA_CLIENT_SECRET"],
        authority=_authority(),
    )


# --- routes ----------------------------------------------------------------------

@router.get("/auth/login")
def auth_login(request: Request, next: str | None = None):
    if next and not is_safe_next(next):
        return JSONResponse(status_code=400,
                            content={"detail": "next must be a localhost URL or the configured UI origin"})
    next_url = next or os.getenv("STREAMLIT_URL", DEFAULT_UI_URL)
    state = secrets.token_hex(32)
    # Reference scope set: [API_SCOPE, openid, profile]; prompt="login" like
    # getLoginUrl(). response_mode=query matches what MSAL's URL builder emits.
    authorize_url = _authority() + "/oauth2/v2.0/authorize?" + urlencode({
        "client_id": os.environ["ENTRA_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": _redirect_uri(request),
        "response_mode": "query",
        "scope": " ".join([*_scopes(), "openid", "profile"]),
        "state": state,
        "prompt": "login",
    })
    resp = RedirectResponse(authorize_url, status_code=302)
    # Signed short-lived state cookie; also carries `next` so the callback
    # knows where to land (the Streamlit app lives on another port).
    resp.set_cookie(STATE_COOKIE,
                    sign_session({"state": state, "next": next_url,
                                  "ts": int(time.time())}),
                    max_age=STATE_MAX_AGE, httponly=True, samesite="lax",
                    secure=_secure_cookies(),
                    path="/")
    return resp


@router.get("/auth/callback")
def auth_callback(request: Request, code: str | None = None,
                  state: str | None = None, error: str | None = None,
                  error_description: str | None = None):
    if error:
        log.warning("entra sign-in failed: %s %s", error, error_description or "")
        return JSONResponse(status_code=400, content={
            "detail": f"Entra sign-in failed: {error} — {error_description or ''}".strip(" —")})
    if not code or not state:
        return JSONResponse(status_code=400,
                            content={"detail": "missing code or state"})
    stored = verify_session(request.cookies.get(STATE_COOKIE, ""))
    if (not stored or stored.get("state") != state
            or int(time.time()) - int(stored.get("ts", 0)) > STATE_MAX_AGE):
        return JSONResponse(status_code=400,
                            content={"detail": "state mismatch or expired — restart sign-in"})

    result = _msal_app().acquire_token_by_authorization_code(
        code, scopes=_scopes(), redirect_uri=_redirect_uri(request))
    if not result or "access_token" not in result:
        detail = (result or {}).get("error_description") or (result or {}).get("error") \
            or "token exchange failed"
        log.warning("token exchange failed: %s", detail)
        return JSONResponse(status_code=400, content={"detail": detail})

    claims = result.get("id_token_claims") or {}
    now = int(time.time())
    exp = now + int(result.get("expires_in") or 3600)
    session = {
        "sub": claims.get("oid") or claims.get("sub") or "",
        "name": claims.get("name") or claims.get("preferred_username") or "User",
        "upn": claims.get("preferred_username") or claims.get("upn")
               or claims.get("email") or "",
        "access_token": result["access_token"],
        "exp": exp,
    }
    next_url = stored.get("next")
    if not is_safe_next(next_url):
        next_url = os.getenv("STREAMLIT_URL", DEFAULT_UI_URL)
    log.info("session minted for %s (expires in %ss) -> %s", session["upn"] or session["sub"],
             exp - now, next_url)
    resp = RedirectResponse(next_url, status_code=302)
    resp.delete_cookie(STATE_COOKIE, path="/")
    # httpOnly: browser JS can never touch the access token; the Streamlit
    # SERVER still sees it (cookies ride the websocket handshake headers).
    # max-age tracks the token lifetime, so cookie death == token death.
    resp.set_cookie(SESSION_COOKIE, sign_session(session),
                    max_age=max(exp - now, 0), httponly=True, samesite="lax",
                    secure=_secure_cookies(),
                    path="/")
    return resp


@router.get("/auth/logout")
def auth_logout(next: str | None = None):
    post_logout = next if is_safe_next(next) else os.getenv("STREAMLIT_URL",
                                                            DEFAULT_UI_URL)
    logout_url = (f"{_authority()}/oauth2/v2.0/logout"
                  f"?client_id={os.environ['ENTRA_CLIENT_ID']}"
                  f"&post_logout_redirect_uri={quote(post_logout, safe='')}")
    resp = RedirectResponse(logout_url, status_code=302)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
