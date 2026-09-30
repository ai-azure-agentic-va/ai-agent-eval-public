"""Thin HTTP client for the evaluation service.

Authenticated calls accept either the SSO session token or the optional
manual bearer used when authentication is disabled.

Errors:
- ServiceUnreachable — connection-level failure; message is user-safe
  ("is the eval service running?"), never a stack trace.
- ApiError — non-2xx response; carries .status and the parsed JSON .body.
"""
from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_BASE = "http://127.0.0.1:8100"
_TIMEOUT = 60


def base_url() -> str:
    return os.getenv("EVAL_API_URL", DEFAULT_BASE).rstrip("/")


class ServiceUnreachable(Exception):
    """Connection failure — message is safe to display as-is."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or f"Could not reach the eval service at {base_url()} — "
               "is the eval service running?"
        )


class ApiError(Exception):
    """Non-2xx response from the service."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body if isinstance(body, dict) else {}
        detail = self.body.get("detail")
        super().__init__(detail if isinstance(detail, str) else f"HTTP {status}")

    @property
    def detail(self) -> Any:
        return self.body.get("detail")


def _request(method: str, path: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", _TIMEOUT)
    try:
        return requests.request(method, base_url() + path, **kwargs)
    except requests.RequestException:
        raise ServiceUnreachable() from None


def _json(res: requests.Response) -> Any:
    if not res.ok:
        try:
            body = res.json()
        except ValueError:
            body = {}
        raise ApiError(res.status_code, body)
    return res.json()


def _auth_headers(token: str | None) -> dict[str, str]:
    token = (token or "").strip()
    if not token:
        return {}
    if not token.lower().startswith("bearer "):
        token = f"Bearer {token}"
    return {"Authorization": token}


def health() -> dict:
    return _json(_request("GET", "/health", timeout=5))


def applications() -> list[dict]:
    return _json(_request("GET", "/applications"))


def datasets() -> list[dict]:
    return _json(_request("GET", "/datasets"))


def datasets_page(limit: int = 10, offset: int = 0,
                  sort_by: str = "uploaded_at", sort_dir: str = "desc") -> dict:
    return _json(_request("GET", "/datasets", params={
        "limit": limit, "offset": offset, "sort_by": sort_by, "sort_dir": sort_dir,
    }))


def preview_dataset(filename: str, data: bytes) -> dict:
    return _json(_request("POST", "/datasets/preview",
                          files={"file": (filename, data)}))


def upload_dataset(filename: str, data: bytes, name: str, application: str,
                   token: str | None = None) -> dict:
    return _json(_request("POST", "/datasets",
                          files={"file": (filename, data)},
                          data={"name": name, "application": application},
                          headers=_auth_headers(token)))


def runs(limit: int = 25, offset: int = 0) -> dict:
    return _json(_request("GET", f"/runs?limit={limit}&offset={offset}"))


def run(run_id: str) -> dict:
    return _json(_request("GET", f"/runs/{run_id}"))


def run_rows(run_id: str) -> list[dict]:
    return _json(_request("GET", f"/runs/{run_id}/rows"))


def start_run(dataset_id: str, application: str, version: int | None = None,
              token: str | None = None, thresholds: dict[str, float] | None = None,
              execution_mode: str = "sequential", selected_metrics: list[str] | None = None) -> dict:
    return _json(_request("POST", "/runs",
                          json={"dataset_id": dataset_id, "version": version,
                                "application": application,
                                "execution_mode": execution_mode,
                                **({"selected_metrics": selected_metrics}
                                   if selected_metrics is not None else {}),
                                **({"thresholds": thresholds} if thresholds is not None else {})},
                          headers=_auth_headers(token)))


def resume_run(run_id: str, token: str | None = None) -> dict:
    return _json(_request("POST", f"/runs/{run_id}/resume",
                          headers=_auth_headers(token)))


def export_csv(run_id: str) -> bytes:
    res = _request("GET", f"/runs/{run_id}/export.csv")
    if not res.ok:
        _json(res)
    return res.content


def retry_judges(run_id: str) -> dict:
    return _json(_request("POST", f"/runs/{run_id}/retry-judges", headers=_auth_headers(None)))
