"""Environment-backed settings shared by every service module.

Loads the repo's ``.env`` once (never overriding values already in the shell),
then exposes small typed readers. Nothing here caches: values are read live so
tests can flip them with ``monkeypatch.setenv``.

Cross-cutting concerns live next door:
    service/logging_config.py — stdlib logging setup + request/run correlation ids
    service/tracing.py        — the LangSmith tracing gate and helpers
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

from service.metrics import METRIC_KEYS

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env", override=False)

_TRUTHY = ("1", "true", "yes", "on")


def env_flag(name: str, default: bool = False) -> bool:
    """Boolean env var: 1/true/yes/on (case-insensitive) is True."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUTHY


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value) if minimum is not None else value


def allowed_metrics() -> list[str]:
    """Metric keys operators permit for new runs and restarted execution.

    Empty/unset configuration retains all metrics. Malformed configuration
    fails closed rather than silently expanding the set of allowed judges.
    Saved scoring policies remain independent of this live setting.
    """
    raw = os.getenv("EVAL_ALLOWED_METRICS", "").strip()
    if not raw:
        return list(METRIC_KEYS)
    keys = [key.strip() for key in raw.split(",")]
    invalid = [key for key in keys if key not in METRIC_KEYS]
    if invalid:
        raise ValueError(
            "EVAL_ALLOWED_METRICS must be a comma-separated list of metric keys; "
            f"invalid entries: {', '.join(repr(key) for key in invalid)}")
    return [key for key in METRIC_KEYS if key in keys]


def langgraph_base_url() -> str:
    """Default agent endpoint; remote deployment URLs belong in environment config."""
    return os.getenv("LANGGRAPH_BASE_URL", "").strip() or "http://127.0.0.1:2026"


def loopback_rewrite(url: str | None) -> str | None:
    """Re-target a loopback agent URL when the service runs in a container.

    Inside Docker, ``127.0.0.1`` / ``localhost`` is the container itself — an
    agent running on the host machine is reachable as ``host.docker.internal``
    instead. The docker entrypoint sets ``AGENT_LOOPBACK_HOST`` to that name;
    natively the variable is unset and URLs pass through untouched."""
    target = os.getenv("AGENT_LOOPBACK_HOST", "").strip()
    if not url or not target:
        return url
    parts = urlsplit(url)
    if parts.hostname not in ("127.0.0.1", "localhost", "0.0.0.0"):
        return url
    netloc = f"{target}:{parts.port}" if parts.port else target
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def judge_model_config() -> dict:
    """Azure OpenAI settings for the LLM-as-judge deployment."""
    return {
        "azure_endpoint": os.getenv("AZURE_OPENAI_ENDPOINT"),
        "azure_deployment": os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-5.1"),
        "api_key": os.getenv("AZURE_OPEN_AI_KEY"),
        "api_version": os.getenv("API_VERSION", "2024-05-01-preview"),
        "use_managed_identity": env_flag(
            "AZURE_OPENAI_USE_MANAGED_IDENTITY", env_flag("AZURE_USE_MANAGED_IDENTITY", True)),
        "managed_identity_client_id": os.getenv("AZURE_CLIENT_ID") or None,
    }


def caller_identity(authorization: str | None) -> str:
    """Display identity from a bearer token's claims.

    The token is NOT validated here — the agent under test is the validator.
    This only stamps ``triggered_by`` / ``uploader`` for audit display."""
    if not authorization:
        return "anonymous"
    try:
        payload = authorization.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims.get("upn") or claims.get("preferred_username") or "unknown"
    except Exception:
        return "anonymous"
