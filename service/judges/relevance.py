"""Relevance: does the response address the question that was asked?

Judges ONLY whether the response answers what was asked — factual correctness
is a different judge. Runs on every row, Security rows included.
"""

from __future__ import annotations

from service.judges.base import Judge


class RelevanceJudge(Judge):
    metric = "relevance"
    score_key = "relevance"
    reason_key = "relevance_reason"

    def prompt(self, *, question: str, response: str, **_) -> str:
        return self.prompt_template().format(question=question, response=response)
