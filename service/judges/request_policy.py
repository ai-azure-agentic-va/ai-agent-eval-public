"""Bound judge traffic across runs and retry transient failures without rescoring."""

from __future__ import annotations

import math
import random
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from typing import Callable, TypeVar

from openai import APIConnectionError, APIStatusError

from service.config import env_int

T = TypeVar("T")
_PERMANENT_CODES = {"insufficient_quota", "billing_hard_limit_reached", "content_filter"}


def retry_delay(exc: Exception, attempt: int) -> float | None:
    """Honor Azure Retry-After headers; never retry auth, quota, or content blocks."""
    if isinstance(exc, APIStatusError):
        body = exc.body if isinstance(exc.body, dict) else {}
        error = body.get("error", body)
        if isinstance(error, dict) and error.get("code") in _PERMANENT_CODES:
            return None
        if exc.status_code not in (408, 409, 429) and exc.status_code < 500:
            return None
        headers = exc.response.headers
        for name, factor in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
            value = headers.get(name)
            if value is None:
                continue
            try:
                delay = float(value) * factor
            except ValueError:
                if name != "retry-after":
                    continue
                try:
                    date = parsedate_to_datetime(value)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    delay = (date - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    continue
            if math.isfinite(delay) and delay >= 0:
                return delay + random.uniform(0.05, 0.25)
    elif not isinstance(exc, APIConnectionError):
        return None
    return min(2 ** (attempt - 1), 30) + random.uniform(0.05, 0.25)


class JudgeRequestGate:
    """Shared concurrency, pacing, and cooldown for one Azure deployment/process."""

    def __init__(self, concurrency: int, interval: float) -> None:
        self._slots = threading.BoundedSemaphore(concurrency)
        self._lock = threading.Lock()
        self._interval = interval
        self._next_start = 0.0
        self._cooldown = 0.0

    def _wait(self, deadline: float) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                start = max(now, self._next_start, self._cooldown)
                if start >= deadline:
                    raise TimeoutError("Judge retry budget exhausted; retry failed judges later.")
                delay = start - now
                if delay <= 0:
                    self._next_start = now + self._interval
                    return
            time.sleep(delay)

    def call(self, request: Callable[[float], T]) -> T:
        deadline = time.monotonic() + env_int("EVAL_JUDGE_RETRY_BUDGET_S", 180, minimum=1)
        attempts = env_int("EVAL_JUDGE_MAX_ATTEMPTS", 5, minimum=1)
        for attempt in range(1, attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._slots.acquire(timeout=remaining):
                raise TimeoutError("Judge request queue timed out; retry failed judges later.")
            try:
                self._wait(deadline)
                timeout = min(env_int("EVAL_JUDGE_TIMEOUT_S", 60, minimum=1),
                              deadline - time.monotonic())
                return request(timeout)
            except Exception as exc:
                delay = retry_delay(exc, attempt)
                if delay is None:
                    raise
                # Even an exhausted request makes other workers honor a 429.
                if isinstance(exc, APIStatusError) and exc.status_code == 429:
                    with self._lock:
                        self._cooldown = max(self._cooldown, time.monotonic() + delay)
                if attempt == attempts or time.monotonic() + delay >= deadline:
                    raise
            finally:
                self._slots.release()
            time.sleep(delay)
        raise AssertionError("unreachable retry state")


@lru_cache(maxsize=32)
def _shared_gate(_endpoint: str, _deployment: str, concurrency: int,
                 interval_ms: int) -> JudgeRequestGate:
    """Endpoint and deployment are cache-key fields, not constructor inputs."""
    return JudgeRequestGate(concurrency, interval_ms / 1000)


def request_gate(model_config: dict) -> JudgeRequestGate:
    return _shared_gate(
        str(model_config.get("azure_endpoint") or ""),
        str(model_config.get("azure_deployment") or ""),
        env_int("EVAL_JUDGE_CONCURRENCY", 2, minimum=1),
        env_int("EVAL_JUDGE_MIN_INTERVAL_MS", 1000, minimum=0),
    )
