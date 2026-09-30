"""Eval service — FastAPI app assembly.

Design:
- The service NEVER holds its own identity toward the agent: every agent call
  forwards the caller's bearer token (the tester's SSO identity scopes answers).
- Storage (service/storage/) is backend-switchable via EVAL_STORAGE: datasets,
  runs and per-row records live in LangSmith (default) or Cosmos DB.
- Runs execute in the background, sequentially by default; progress streams over SSE per row.

This module only wires things together: logging, request logging + request
ids, CORS and the routers. Endpoints live in service/routes/ and
service/auth.py.

Run from the repo root:
    .venv/bin/python -m uvicorn service.main:app --port 8100
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from service import auth, storage
from service.logging_config import RequestLogMiddleware, configure_logging
from service.routes import datasets, runs, system
from service.tracing import trace_project, tracing_enabled

configure_logging("service")  # LOG_LEVEL / LOG_FORMAT / LOG_FILE — before any request
log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    log.info("eval service up: storage=%s tracing=%s project=%s",
             storage.BACKEND,
             tracing_enabled(), trace_project() if tracing_enabled() else "-")
    yield
    log.info("eval service shutting down")


app = FastAPI(title="ai-agent-eval-service", lifespan=_lifespan)
app.add_middleware(RequestLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"], allow_headers=["*"],
)
app.include_router(auth.router)      # /auth/* — mints the UI session cookie
app.include_router(system.router)    # /health, /applications, /spike
app.include_router(datasets.router)  # /datasets*
app.include_router(runs.router)      # /runs*
