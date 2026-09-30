"""HTTP adapter for the agent under test (a LangGraph server).

Speaks the LangGraph HTTP API (``POST /threads`` then
``POST /threads/{id}/runs/wait``) and returns one flat dict the runner and
judges consume::

    {"response":     final assistant answer (or "Run failed: <why>"),
     "context":      tool messages verbatim (including earlier turns in
                     conversation mode) — evidence for the judges,
     "usage":        {"input_tokens", "output_tokens", "total_tokens"} summed
                     over the assistant messages' usage_metadata,
     "usage_tokens": the total alone (what the UI shows),
     "routing":      comma-joined sub-agents/tools invoked ("none" if no
                     tool calls), used for the routing-match check}

Failures never raise: the framework contract is that errors come back as a
``"Run failed: ..."`` response so a bad row marks itself failed instead of
killing the run.

Env: LANGGRAPH_BASE_URL (default http://127.0.0.1:2026 — the registry's
``deployment_url`` overrides it per application on every run),
LANGGRAPH_ASSISTANT_ID (default "chat"), LANGGRAPH_BEARER_TOKEN (static
bearer for non-SSO use; runs normally forward the CALLER's bearer instead),
LANGGRAPH_TIMEOUT_SECONDS (default 300).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import requests
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from service.config import env_int, langgraph_base_url, loopback_rewrite

log = logging.getLogger(__name__)

# Tool names whose targets count as "routing": the deep-agent `task` tool
# (sub-agent in args.subagent_type) and the direct AI Search tool.
_ROUTING_TOOLS = ("task", "ai_search_tool")


def _trace_inputs(inputs: dict) -> dict:
    return {"prompt": inputs.get("prompt")}


def _trace_outputs(out: dict) -> dict:
    context = out.get("context") or ""
    return {"response": out.get("response"), "routing": out.get("routing"),
            "usage_tokens": out.get("usage_tokens"),
            "context_chars": len(context), "context": context[:2000]}


def message_text(content: Any) -> str:
    """LangGraph message content is either a string or a list of text blocks."""
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict)).strip()
    return str(content or "")


def _usage(messages: list[dict]) -> dict:
    """Token usage summed over the assistant turns (LangGraph usage_metadata)."""
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for m in messages:
        if m.get("type") not in ("ai", "assistant"):
            continue
        meta = m.get("usage_metadata") or {}
        for key in usage:
            value = meta.get(key)
            if isinstance(value, (int, float)):
                usage[key] += int(value)
    if not usage["total_tokens"]:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def _routing(messages: list[dict]) -> str:
    called: list[str] = []
    for m in messages:
        if m.get("type") not in ("ai", "assistant"):
            continue
        for tc in m.get("tool_calls") or []:
            name = tc.get("name")
            target = (tc.get("args") or {}).get("subagent_type") if name == "task" else name
            if name in _ROUTING_TOOLS and target and target not in called:
                called.append(target)
    return ",".join(called) or "none"


def _final_response(messages: list[dict]) -> str:
    """Require a final answer, not an earlier plan or an unfinished tool call."""
    final = next((m for m in reversed(messages) if m.get("type") in ("ai", "assistant")), None)
    if final is None or final.get("tool_calls") or final.get("invalid_tool_calls"):
        raise RuntimeError("no final assistant message returned")
    response = message_text(final.get("content"))
    if not response.strip():
        raise RuntimeError("no final assistant message returned")
    return response


class LangGraphAgentClient:
    def __init__(self, base_url: str | None = None) -> None:
        configured = base_url or langgraph_base_url()
        self.base_url = loopback_rewrite(configured).rstrip("/")
        self.assistant_id = os.getenv("LANGGRAPH_ASSISTANT_ID", "chat")
        self.timeout = env_int("LANGGRAPH_TIMEOUT_SECONDS", 300, minimum=1)
        self.headers = {"Content-Type": "application/json"}
        self.conversation_mode = False
        self._thread_id: str | None = None
        token = os.getenv("LANGGRAPH_BEARER_TOKEN", "").strip()
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def _post(self, path: str, body: dict, timeout: int) -> dict:
        resp = requests.post(f"{self.base_url}{path}", json=body,
                             headers=self.headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    # @traceable is a no-op unless tracing is on (or an enclosing trace is
    # active); when on, each call is an "agent-call" span under the case.
    @traceable(run_type="chain", name="agent-call",
               process_inputs=_trace_inputs, process_outputs=_trace_outputs)
    def call_agent(self, prompt: str) -> dict:
        """Run one turn; only explicit conversation mode reuses the thread.

        Conversation clients must be called sequentially. A message ID separates
        the current turn from history so old answers and usage are never reused.
        """
        started = time.perf_counter()
        thread_id = self._thread_id if self.conversation_mode else None
        try:
            if thread_id is None:
                thread_id = self._post("/threads", {}, 30)["thread_id"]
                if self.conversation_mode:
                    self._thread_id = thread_id
            message = {"role": "user", "content": prompt}
            if self.conversation_mode:
                message["id"] = str(uuid.uuid4())
            run = self._post(
                f"/threads/{thread_id}/runs/wait",
                {"assistant_id": self.assistant_id,
                 "input": {"messages": [message]}},
                self.timeout,
            )
            if not isinstance(run, dict):
                raise RuntimeError("Agent returned an invalid run response")
            # /runs/wait reports execution errors inside a successful HTTP response.
            # Check that envelope before looking for a user-message boundary, or a
            # real upstream failure gets mislabeled as missing conversation history.
            if "__error__" in run:
                error = run["__error__"]
                detail = (": ".join(str(error[key]) for key in ("error", "message") if error.get(key))
                          if isinstance(error, dict) else str(error or ""))
                raise RuntimeError(detail or "Agent run failed without error details")
            if run.get("__interrupt__"):
                raise RuntimeError("Agent interrupted before completing this turn")
            messages = run.get("messages") or []
            current_messages = messages
            history = []
            if self.conversation_mode:
                boundary = next((i for i, m in enumerate(messages)
                                 if m.get("id") == message["id"]), None)
                if boundary is None:
                    raise RuntimeError("Cannot identify the current conversation turn in the agent response")
                current_messages = messages[boundary + 1:]
                history = [{"role": m.get("type"), "content": message_text(m.get("content"))}
                           for m in messages[:boundary]
                           if m.get("type") in ("human", "user", "ai", "assistant")
                           and message_text(m.get("content"))]
            response = _final_response(current_messages)
            context = "\n\n".join(
                t for m in messages
                if m.get("type") == "tool" and (t := message_text(m.get("content"))))
            usage = _usage(current_messages)
            out = {"response": response, "context": context, "usage": usage,
                   "usage_tokens": usage["total_tokens"], "routing": _routing(current_messages),
                   "thread_id": thread_id}
            if self.conversation_mode and history:
                out["evaluation_question"] = (
                    "Conversation history (context only, not a reference answer):\n"
                    + json.dumps(history, ensure_ascii=False)
                    + "\n\nEvaluate only the response to this current question:\n" + prompt)
            span = get_current_run_tree()  # the "agent-call" span, when tracing
            if span is not None and usage["total_tokens"]:
                span.set(usage_metadata=dict(usage))
            log.debug("agent call ok (%s): thread=%s tokens=%s routing=%s %.1fs",
                      self.base_url, thread_id, usage["total_tokens"], out["routing"],
                      time.perf_counter() - started)
            return out
        except Exception as exc:
            log.warning("agent call failed (%s) after %.1fs: %s",
                        self.base_url, time.perf_counter() - started, exc)
            return {"response": f"Run failed: {exc}", "context": "", "usage_tokens": 0,
                    "thread_id": thread_id}
