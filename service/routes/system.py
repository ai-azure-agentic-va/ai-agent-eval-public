"""Operational endpoints: health probe, applications registry, and the
``/spike`` single-question chain test (one agent round-trip, no judging —
the quickest way to prove the service can reach an agent)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from service import config, registry, storage
from service.agent_client import LangGraphAgentClient
from service.config import judge_model_config
from service.tracing import trace_project, tracing_enabled

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
def health():
    body = {"ok": True, "judge": judge_model_config()["azure_deployment"],
            "storage": storage.BACKEND,
            "tracing": {"enabled": tracing_enabled(),
                        "project": trace_project() if tracing_enabled() else None}}
    try:
        body["applications"] = [a["key"] for a in registry.get_applications() if a["is_active"]]
    except registry.RegistryUnavailable as exc:
        # Health must not 500 on a bad EVAL_APPLICATIONS value — report it instead.
        body["applications"] = []
        body["registry"] = f"unreachable: {str(exc)[:200]}"
    try:
        body["allowed_metrics"] = config.allowed_metrics()
    except ValueError as exc:
        # Keep probes usable while preventing new execution with bad config.
        body["allowed_metrics"] = []
        body["metric_config_error"] = str(exc)
    return body


@router.get("/applications")
def get_applications():
    try:
        return registry.get_applications()
    except registry.RegistryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


class SpikeRequest(BaseModel):
    question: str


@router.post("/spike")
def spike(body: SpikeRequest, authorization: str | None = Header(default=None)):
    client = LangGraphAgentClient()
    if authorization:
        client.headers["Authorization"] = authorization
    log.info("spike against %s", client.base_url)
    result = client.call_agent(body.question)
    if result["response"].startswith("Run failed"):
        raise HTTPException(status_code=502, detail=result["response"])
    return {"response": result["response"], "context_chars": len(result.get("context", ""))}
