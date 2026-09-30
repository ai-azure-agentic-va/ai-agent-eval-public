"""The judge panel: runs the right judges for a row's category.

Gating (honest scoring — a blank cell means "not applicable", never zero):

    row category      | judges that run
    ------------------+---------------------------------------------------------
    Security          | relevance, bias, jailbreak
    Honesty           | relevance, bias, correctness, groundedness, retrieval,
                      | context_precision, context_recall*, fallback
    everything else   | relevance, bias, correctness, groundedness, retrieval,
                      | context_precision, context_recall*
    (* recall only when the row has a reference answer)

Security rows skip correctness and the context judges on purpose: "correct"
there means "refused properly" (the jailbreak judge measures that), a correct
refusal retrieves nothing, and quoting attack text into other judges' prompts
can trip Azure's content filter on the judge call itself.
Each run can select a subset of these metrics; category and reference gates
still apply, and excluded judges are never invoked.
"""

from __future__ import annotations

from service.judges.bias import BiasJudge
from service.judges.categories import is_adversarial_category, is_honesty_category
from service.judges.context_precision_recall import ContextPrecisionRecallJudge
from service.judges.correctness import CorrectnessJudge
from service.judges.fallback import FallbackJudge
from service.judges.groundedness import GroundednessJudge
from service.judges.jailbreak import JailbreakJudge
from service.judges.relevance import RelevanceJudge
from service.judges.retrieval import RetrievalJudge
from service.metrics import applicable_metrics, is_valid_score, normalize_selected_metrics


class JudgePanel:
    """Current 0–1 metrics behind one evaluate call and one shared client."""

    def __init__(self, model_config: dict, client=None) -> None:
        self.selected_metrics = normalize_selected_metrics(model_config.get("selected_metrics"))
        self.bias = BiasJudge(model_config, client)
        client = self.bias.client
        self.score_version = self.bias.score_version
        self.relevance = RelevanceJudge(model_config, client)
        self.correctness = CorrectnessJudge(model_config, client)
        self.groundedness = GroundednessJudge(model_config, client)
        self.retrieval = RetrievalJudge(model_config, client)
        self.context_pr = ContextPrecisionRecallJudge(model_config, client)
        self.fallback = FallbackJudge(model_config, client)
        self.jailbreak = JailbreakJudge(model_config, client)

    def evaluate(self, *, question: str, response: str, context: str = "",
                 reference: str = "", category: str = "",
                 previous_scores: dict | None = None) -> dict:
        """``{metric: {"score", "reasoning"} | {"error"}}`` for every judge
        that ran; excluded or inapplicable metrics are absent."""
        required = applicable_metrics(category, reference, self.score_version, self.selected_metrics)
        results = {key: value for key, value in (previous_scores or {}).items()
                   if key in required and isinstance(value, dict) and not value.get("error")
                   and is_valid_score(value.get("score"), self.score_version)}

        def evaluate_missing(key, judge, **inputs):
            if key in required and key not in results:
                results[key] = judge(**inputs)

        evaluate_missing("relevance", self.relevance, question=question, response=response)
        evaluate_missing("bias", self.bias, question=question, response=response)
        if is_adversarial_category(category):
            evaluate_missing("jailbreak", self.jailbreak, question=question, response=response)
            return results
        evaluate_missing("correctness", self.correctness,
                         question=question, reference=reference, response=response, context=context)
        evaluate_missing("groundedness", self.groundedness, response=response, context=context)
        evaluate_missing("retrieval", self.retrieval,
            question=question, context=context, response=response, reference=reference)
        context_keys = {"context_precision", "context_recall"} & set(required)
        if context_keys - results.keys():
            combined = self.context_pr.evaluate(question=question, context=context, reference=reference)
            results.update({key: value for key, value in combined.items()
                            if key in context_keys and key not in results})
        if is_honesty_category(category):
            evaluate_missing("fallback", self.fallback,
                question=question, response=response, reference=reference)
        return results
