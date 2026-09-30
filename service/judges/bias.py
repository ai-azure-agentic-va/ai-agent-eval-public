"""Bias freedom: a higher score means less unjustified demographic/social bias."""

from service.judges.base import Judge


class BiasJudge(Judge):
    metric = "bias"
    score_key = "bias_score"

    def prompt(self, *, question: str, response: str, **_) -> str:
        return self.prompt_template().format(question=question, response=response)
