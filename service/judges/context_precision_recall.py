"""Context precision AND context recall from ONE judge call.

The two metrics share every input (query, retrieved context, reference
answer), so a single prompt halves the per-row judge cost versus two calls.

* Precision (RAGAS-style, 0–1): signal-to-noise — of everything retrieved, how
  much is relevant to this query?
* Recall (0–1): coverage — does the retrieved context contain every fact the
  reference answer needs? Unlike the retrieval judge (query-vs-context
  sufficiency), recall is anchored on the reference answer's claims, so it is
  blank when the row has no reference.

Because it yields two metrics, ``evaluate`` returns ``{metric: result}`` for
both keys instead of the single-result shape of ``Judge.__call__``.
"""

from __future__ import annotations

import json
import logging

from langsmith import traceable

from service.judges.base import Judge, _trace_inputs
from service.metrics import is_valid_score

log = logging.getLogger(__name__)


_NO_SCORE = "judge returned no numeric score in the declared range"


class ContextPrecisionRecallJudge(Judge):
    metric = "context_precision_recall"
    metrics = ("context_precision", "context_recall")

    def prompt(self, *, question: str, context: str, reference: str | None, **_) -> str:
        return self.prompt_template().format(question=question, reference=reference or "None provided",
                              context=context or "None retrieved")

    def parse(self, raw: dict) -> dict:
        """Split the combined payload into one normalized result per metric."""
        out = {}
        for metric in self.metrics:
            score = raw.get(f"{metric}_score")
            if is_valid_score(score, self.score_version):
                out[metric] = {"score": score,
                               "reasoning": str(raw.get(f"{metric}_reasoning") or "")}
            else:
                out[metric] = {"error": f"{_NO_SCORE}: {json.dumps(raw)[:200]}"}
        return out

    def evaluate(self, *, question: str, context: str, reference: str | None) -> dict:
        """``{"context_precision": {...}, "context_recall": {...}}`` — recall
        is omitted entirely when the row carries no reference answer."""
        try:
            traced = traceable(run_type="chain", name="judge-context-precision-recall",
                               process_inputs=_trace_inputs)(self._invoke)
            results = traced(question=question, context=context, reference=reference)
        except Exception as exc:
            log.warning("context precision/recall judge failed: %s", exc)
            error = {"error": f"context precision/recall judge failed: {exc}"}
            results = {m: dict(error) for m in self.metrics}
        if not reference:
            results.pop("context_recall", None)
        return results
