"""Retrieval quality: does the retrieved context contain what is needed to
answer the query? Judged strictly from the data shown — no assumed capability.
Skipped on Security rows.
"""

from __future__ import annotations

from service.judges.base import Judge


class RetrievalJudge(Judge):
    metric = "retrieval"
    score_key = "retrieval_score"

    def prompt(self, *, question: str, context: str, response: str,
               reference: str = "", **_) -> str:
        return self.prompt_template().format(question=question, context=context or "None retrieved",
                              response=response, reference=reference or "Not provided")
