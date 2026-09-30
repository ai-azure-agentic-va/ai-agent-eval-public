"""Shared machinery for every LLM-as-judge.

A judge is a small class with a ``metric`` name, a ``prompt(**inputs)`` method
and a ``parse(raw)`` method. ``Judge.__call__`` does the rest: one JSON-mode
chat completion, parsing, error capture, logging and (when tracing is on) a
``judge-<metric>`` span under the current case trace.

Every judge returns the SAME shape, so the runner, storage, CSV export and UI
never need per-judge knowledge::

    {"score": <0-1 number>, "reasoning": "<judge rationale>", ...extras}
    {"error": "<metric> judge failed: <why>"}          # no score, ever

An error entry never carries a score: a judge outage is a blank cell with a
recorded reason, not a grade.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from langsmith import traceable
from openai import AzureOpenAI

from service.azure_credentials import openai_token_provider
from service.config import env_int
from service.judges.request_policy import request_gate
from service.judges.rubrics import normalized_template
from service.metrics import SCORING_VERSION, is_valid_score
from service.tracing import wrap_judge_client

log = logging.getLogger(__name__)


def build_judge_client(model_config: dict) -> AzureOpenAI:
    """The one AzureOpenAI client all judges share (traced when enabled)."""
    auth = {"api_key": model_config.get("api_key")}
    if model_config.get("use_managed_identity"):
        auth = {
            "azure_ad_token_provider": openai_token_provider(
                model_config.get("managed_identity_client_id"),
                managed_identity_only=bool(os.getenv("IDENTITY_ENDPOINT")),
            ),
        }
    client = AzureOpenAI(
        **auth,
        api_version=model_config["api_version"],
        azure_endpoint=model_config["azure_endpoint"],
        max_retries=0,  # The shared gate owns retries, including Azure cooldowns.
        timeout=env_int("EVAL_JUDGE_TIMEOUT_S", 60, minimum=1),
    )
    return wrap_judge_client(client)


def _trace_inputs(inputs: dict) -> dict:
    """Trace inputs for a judge span: drop ``self``, cap the long fields."""
    out = {}
    for key, value in inputs.items():
        if key == "self":
            continue
        out[key] = value[:2000] if isinstance(value, str) else value
    return out


class Judge:
    metric: str = ""            # key in service.metrics.METRIC_KEYS
    score_key: str = "score"    # where the model's JSON puts the number
    reason_key: str = "reasoning"

    def __init__(self, model_config: dict, client: AzureOpenAI | None = None) -> None:
        version = model_config.get("score_version", SCORING_VERSION)
        if version != SCORING_VERSION:
            raise ValueError(
                f"Judge scoring version {version!r} is unsupported. Start a new evaluation "
                "with the current 0–1 prompts; saved historical results remain readable.")
        self.client = client if client is not None else build_judge_client(model_config)
        self.deployment = model_config["azure_deployment"]
        self.score_version = SCORING_VERSION
        self.request_gate = request_gate(model_config)

    def prompt_template(self) -> str:
        return normalized_template(self.metric)

    # --- to implement per judge ------------------------------------------------

    def prompt(self, **inputs: Any) -> str:
        raise NotImplementedError

    def parse(self, raw: dict) -> dict:
        """Normalize the model's JSON into ``{"score", "reasoning"}``."""
        score = raw.get(self.score_key)
        if not is_valid_score(score, self.score_version):
            raise ValueError(f"judge returned no numeric score in the 0–1 range: {json.dumps(raw)[:200]}")
        return {"score": score, "reasoning": str(raw.get(self.reason_key) or "")}

    # --- shared behavior ----------------------------------------------------------

    def ask(self, prompt: str) -> dict:
        """One JSON-mode completion, parsed. Raises on any failure."""
        if len(prompt.encode("utf-8")) > env_int("EVAL_JUDGE_MAX_PROMPT_BYTES", 128000, minimum=1):
            raise ValueError("Judge input exceeds the configured prompt budget; narrow the "
                             "agent's evidence or increase EVAL_JUDGE_MAX_PROMPT_BYTES. "
                             "No evidence was silently truncated.")
        res = self.request_gate.call(lambda timeout: self.client.chat.completions.create(
            model=self.deployment,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_completion_tokens=env_int("EVAL_JUDGE_MAX_OUTPUT_TOKENS", 2048, minimum=1),
            timeout=timeout,
        ))
        return json.loads(res.choices[0].message.content)

    def _invoke(self, **inputs: Any) -> dict:
        return self.parse(self.ask(self.prompt(**inputs)))

    def __call__(self, **inputs: Any) -> dict:
        try:
            traced = traceable(run_type="chain", name=f"judge-{self.metric}",
                               process_inputs=_trace_inputs)(self._invoke)
            return traced(**inputs)
        except Exception as exc:
            log.warning("%s judge failed: %s", self.metric, exc)
            return {"error": f"{self.metric} judge failed: {exc}"}
