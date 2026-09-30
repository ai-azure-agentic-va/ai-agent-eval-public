"""Groundedness / faithfulness: is every factual claim supported by the
retrieved evidence? Measures faithfulness to evidence, NOT correctness against
the answer key. Skipped on Security rows (a correct refusal retrieves nothing).
"""

from __future__ import annotations

from service.judges.base import Judge


class GroundednessJudge(Judge):
    metric = "groundedness"
    score_key = "groundedness"
    reason_key = "groundedness_reason"

    def prompt(self, *, response: str, context: str, **_) -> str:
        return self.prompt_template().format(context=context or "No evidence was retrieved.",
                              response=response)
