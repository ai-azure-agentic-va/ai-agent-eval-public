"""Process-wide logging for the service and the UI.

One call to ``configure_logging()`` at startup; every module then simply does
``log = logging.getLogger(__name__)``.

Environment:
    LOG_LEVEL   DEBUG | INFO (default) | WARNING | ERROR
    LOG_FORMAT  text (default) | json   — json is one object per line, for
                Docker log drivers and log shippers
    LOG_FILE    optional path; a size-rotated file (10 MB x 5) is written in
                addition to stderr

Correlation ids ride on contextvars and appear on every line:
    request_id  set by RequestLogMiddleware per HTTP request (also returned in
                the X-Request-ID response header; an incoming X-Request-ID is
                honored so the UI/agent can correlate across services)
    run_id      set by the runner for the eval run a thread is working on
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import time
import uuid
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")

_TEXT_FORMAT = ("%(asctime)s %(levelname)-7s %(name)s "
                "[req=%(request_id)s run=%(run_id)s] %(message)s")
_configured = False


class ContextFilter(logging.Filter):
    """Stamps the correlation ids onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.run_id = run_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "run_id": getattr(record, "run_id", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _build_handler(handler: logging.Handler, fmt: str) -> logging.Handler:
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(_TEXT_FORMAT))
    handler.addFilter(ContextFilter())
    return handler


def configure_logging(component: str = "service") -> None:
    """Idempotent root-logger setup (safe to call from both processes)."""
    global _configured
    if _configured:
        return
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").strip().upper(), logging.INFO)
    fmt = os.getenv("LOG_FORMAT", "text").strip().lower()
    handlers: list[logging.Handler] = [_build_handler(logging.StreamHandler(), fmt)]
    log_file = os.getenv("LOG_FILE", "").strip()
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handlers.append(_build_handler(
            logging.handlers.RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024,
                                                 backupCount=5),
            fmt))
    root = logging.getLogger()
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    # Third-party chatter that drowns our own lines at INFO.
    for noisy in ("httpx", "httpx2", "httpcore", "urllib3", "openai", "msal"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
    _configured = True
    logging.getLogger(__name__).info(
        "logging configured: component=%s level=%s format=%s file=%s",
        component, logging.getLevelName(level), fmt, log_file or "-")


class RequestLogMiddleware:
    """Pure-ASGI middleware: one line per HTTP request with method, path,
    status and duration, plus request-id propagation.

    Written as raw ASGI (not BaseHTTPMiddleware) so streaming responses such
    as the SSE progress feed are passed through untouched."""

    QUIET_PATHS = {"/health"}  # polled every few seconds by the UI — DEBUG only

    def __init__(self, app) -> None:
        self.app = app
        self.log = logging.getLogger("eval.http")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or {}).get(b"x-request-id", b"").decode()
        request_id = incoming[:64] or uuid.uuid4().hex[:12]
        token = request_id_var.set(request_id)
        method, path = scope.get("method", "?"), scope.get("path", "?")
        status = {"code": 0}
        started = time.perf_counter()

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            self.log.exception("%s %s -> unhandled error", method, path)
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            level = logging.DEBUG if path in self.QUIET_PATHS else logging.INFO
            self.log.log(level, "%s %s -> %s %.1fms", method, path, status["code"], elapsed_ms)
            request_id_var.reset(token)
