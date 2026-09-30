"""Execution modes shared by the API, runner and UI."""

from collections.abc import Mapping
from typing import Literal

ExecutionMode = Literal["sequential", "conversation", "parallel"]

EXECUTION_LABELS: dict[ExecutionMode, str] = {
    "sequential": "Sequential — independent questions",
    "conversation": "Sequential — shared conversation (multi-turn)",
    "parallel": "Parallel — independent questions",
}


def execution_mode(meta: Mapping) -> ExecutionMode:
    """Legacy runs keep their original parallel, independent execution policy."""
    mode = meta.get("execution_mode", "parallel")
    if mode not in EXECUTION_LABELS:
        raise ValueError(f"Unsupported execution mode: {mode}")
    return mode
