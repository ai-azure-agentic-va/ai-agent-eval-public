"""Applications registry: which orchestrator a run targets.

Config-based (no database): reads EVAL_APPLICATIONS — a JSON list of
application dicts — falling back to the default entries below when the variable
is unset. Their active endpoint comes from LANGGRAPH_BASE_URL in .env or the
process environment (a local agent is the fallback). Each entry has these fields:

    key, display_name, deployment_url, assistant_id, auth_profile, is_active

Example:
    EVAL_APPLICATIONS='[{"key": "my-agent", "display_name": "My agent",
        "deployment_url": "http://127.0.0.1:2026", "assistant_id": "chat",
        "auth_profile": "caller-passthrough", "is_active": true}]'
"""
from __future__ import annotations

import json
import os

from service.config import langgraph_base_url, loopback_rewrite


class RegistryUnavailable(RuntimeError):
    """EVAL_APPLICATIONS is set but is not valid JSON / not a list of app dicts.

    Deliberately NOT a silent fallback to the built-in seed: a configured but
    malformed registry is a misconfiguration the caller must see (503)."""


_REQUIRED = ("key", "deployment_url")
_DEFAULTS = {"display_name": "", "assistant_id": "chat",
             "auth_profile": "caller-passthrough", "is_active": True}


def _seed_applications() -> list[dict]:
    endpoint = langgraph_base_url()
    return [
        {"key": "va", "display_name": "Default LangGraph agent",
         "deployment_url": endpoint, "assistant_id": "chat",
         "auth_profile": "caller-passthrough", "is_active": True},
        # Inactive entries stay valid dataset scopes but cannot start new runs.
        {"key": "legacy", "display_name": "Legacy agent (inactive)",
         "deployment_url": "http://127.0.0.1:2027", "assistant_id": "chat",
         "auth_profile": "caller-passthrough", "is_active": False},
        {"key": "va-dev", "display_name": "Default LangGraph agent (legacy alias)",
         "deployment_url": endpoint, "assistant_id": "chat",
         "auth_profile": "caller-passthrough", "is_active": False},
    ]


def get_applications() -> list[dict]:
    """Registered applications; loopback URLs are re-targeted in Docker
    (see config.loopback_rewrite) so the registry shows the URL actually used."""
    return [_with_reachable_url(app) for app in _configured_applications()]


def _with_reachable_url(app: dict) -> dict:
    return {**app, "deployment_url": loopback_rewrite(app["deployment_url"])}


def _configured_applications() -> list[dict]:
    raw = os.getenv("EVAL_APPLICATIONS")
    if not raw or not raw.strip():
        return _seed_applications()
    try:
        apps = json.loads(raw)
        if not isinstance(apps, list):
            raise ValueError("EVAL_APPLICATIONS must be a JSON list")
        out = []
        for i, app in enumerate(apps):
            if not isinstance(app, dict):
                raise ValueError(f"entry {i} is not an object")
            missing = [k for k in _REQUIRED if not app.get(k)]
            if missing:
                raise ValueError(f"entry {i} missing {', '.join(missing)}")
            out.append({**_DEFAULTS, "display_name": app["key"], **app})
        return out
    except (ValueError, TypeError) as exc:
        raise RegistryUnavailable(f"EVAL_APPLICATIONS is malformed: {exc}") from exc


def resolve(key: str) -> dict:
    for app in get_applications():
        if app["key"] == key and app["is_active"]:
            return app
    raise KeyError(f"unknown or inactive application '{key}'")
