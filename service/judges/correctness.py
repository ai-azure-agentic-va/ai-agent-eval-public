"""Correctness (shown as "accuracy"): the answer vs the dataset's reference.

The reference may state failure conditions ("inventing a specific number
is a failure") as well as required facts; the judge honors both. Skipped on
Security rows — there the jailbreak judge is the verdict, and quoting attack
text verbatim can trip Azure's content filter on the judge's own call.
"""

from __future__ import annotations

from service.judges.base import Judge


class CorrectnessJudge(Judge):
    metric = "correctness"
    score_key = "correctness_score"

    def prompt(self, *, question: str, reference: str, response: str,
               context: str = "", **_) -> str:
        return self.prompt_template().format(
            question=question, reference=reference, response=response,
            context=context or "No retrieved evidence supplied")
