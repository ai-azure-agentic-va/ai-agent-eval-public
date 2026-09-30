"""Fallback honesty: when the information does not exist (or the request is
out of scope), does the agent say so instead of substituting plausible content?

Runs ONLY on honesty/refusal/fallback rows. It is reference-aware: the
dataset's reference answer is what lets the judge tell a genuine "does not
exist" report from confident substitute content (an answer that copies rules
for a *different* entity out of real documents and presents them as
applicable is hallucination by substitution, not a fallback).
"""

from __future__ import annotations

from prompts import FALLBACK_REFERENCE_BLOCK
from service.judges.base import Judge


class FallbackJudge(Judge):
    metric = "fallback"
    score_key = "fallback_score"

    def prompt(self, *, question: str, response: str, reference: str | None = None, **_) -> str:
        block = FALLBACK_REFERENCE_BLOCK.format(reference=reference) if reference else ""
        return self.prompt_template().format(question=question, response=response, reference_block=block)
