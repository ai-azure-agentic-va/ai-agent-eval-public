"""LangSmith tracing of the eval pipeline ITSELF.

Two different things talk to LangSmith and they are deliberately separate:

* **Storage** (service/storage/) persists datasets, runs, per-row records and
  judge feedback. It is always on and uses ``LANGSMITH_EVALS_KEY``.
* **Tracing** (this module) emits the eval app's OWN execution traces to the
  ``LANGSMITH_PROJECT`` tracing project so a run can be debugged span by span.
  It is gated by ``LANGSMITH_TRACING`` and uses ``LANGSMITH_API_KEY``. When the
  gate is off, nothing in this module touches LangSmith and the pipeline runs
  fully offline with byte-identical results.

Trace shape when enabled::

    eval-run <run_id>                 (chain; inputs: dataset/application/cases,
      |                                outputs: final summary)
      +-- case-00001 <name>           (chain; one per attempt of a case)
      |     +-- agent-call            (chain; the LangGraph round-trip)
      |     +-- judge-relevance       (chain) -> ChatCompletion LLM span
      |     +-- judge-correctness     (chain) -> ChatCompletion LLM span
      |     +-- ...                   (one chain per judge that ran)
      +-- case-00002 ...

Run-level metadata (dataset, version, application, eval_run_id) is set on the
parent and inherited by every child, so LangSmith filters like
``metadata.eval_run_id = "..."`` find a whole run.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

from langsmith.run_helpers import tracing_context
from langsmith.run_trees import RunTree
from langsmith.utils import tracing_is_enabled

from service.config import env_flag

log = logging.getLogger(__name__)

DEFAULT_PROJECT = "eval-framework"


def tracing_enabled() -> bool:
    """The LANGSMITH_TRACING gate, read live on every call."""
    return env_flag("LANGSMITH_TRACING")


def trace_project() -> str:
    return os.getenv("LANGSMITH_PROJECT", "").strip() or DEFAULT_PROJECT


def wrap_judge_client(client):
    """Make judge completions show up as LLM spans (prompt, latency, tokens)
    under the active case trace. ``wrap_openai`` patches the client in place,
    so it is only ever applied when tracing is on."""
    if not tracing_enabled():
        return client
    from langsmith.wrappers import wrap_openai

    return wrap_openai(client)


def _uploading() -> bool:
    """True when spans are actually sent to LangSmith (as opposed to the SDK's
    'local' mode, which builds run trees in memory)."""
    return tracing_is_enabled() is True


class RunTrace:
    """Parent span for a run; ``start`` returns None when tracing is unavailable."""

    def __init__(self, tree: RunTree) -> None:
        self.tree = tree

    @classmethod
    def start(cls, run_id: str, meta: dict[str, Any]) -> RunTrace | None:
        if not tracing_enabled():
            return None
        metadata = {
            "eval_run_id": run_id,
            "dataset": meta.get("dataset_name"),
            "dataset_version": meta.get("dataset_version"),
            "application": meta.get("application_key"),
        }
        try:
            tree = RunTree(
                name=f"eval-run {run_id}",
                run_type="chain",
                project_name=trace_project(),
                inputs={"dataset": meta.get("dataset_name"),
                        "dataset_version": meta.get("dataset_version"),
                        "application": meta.get("application_key"),
                        "agent_endpoint": meta.get("agent_endpoint"),
                        "cases": meta.get("case_count")},
                extra={"metadata": metadata},
                tags=["eval-run"],
            )
        except Exception as exc:
            log.warning("could not create run trace for %s: %s", run_id, exc)
            return None
        try:
            if _uploading():
                tree.post()
        except Exception as exc:  # tracing is best-effort, never load-bearing
            log.warning("could not post run trace for %s: %s", run_id, exc)
        log.info("run trace started: project=%s trace_id=%s", trace_project(), tree.id)
        return cls(tree)

    @contextmanager
    def case(self, index: int, attempt: int) -> Iterator[None]:
        """Context in which one case attempt's ``@traceable`` chain becomes a
        child of the run span (run metadata is inherited automatically)."""
        with tracing_context(parent=self.tree,
                             metadata={"case_index": index, "attempt": attempt}):
            yield

    def finish(self, status: str, summary: dict[str, Any] | None = None,
               error: str | None = None) -> None:
        try:
            self.tree.end(outputs={"status": status, **(summary or {})}, error=error)
            if _uploading():
                self.tree.patch()
        except Exception as exc:
            log.warning("could not finish run trace %s: %s", self.tree.id, exc)
