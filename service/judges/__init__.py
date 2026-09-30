"""LLM-as-judge evaluators (Azure OpenAI), one module per metric.

    from service.judges import JudgePanel        # all eight metrics, gated by row category
    from service.judges.categories import is_adversarial_category

Each judge module holds exactly one prompt and one small class; see
``service/judges/base.py`` for the shared call/parse/error contract and
``service/judges/panel.py`` for which judges run on which rows.
"""

from service.judges.base import Judge, build_judge_client
from service.judges.categories import is_adversarial_category, is_honesty_category
from service.judges.context_precision_recall import ContextPrecisionRecallJudge
from service.judges.correctness import CorrectnessJudge
from service.judges.fallback import FallbackJudge
from service.judges.groundedness import GroundednessJudge
from service.judges.jailbreak import JailbreakJudge
from service.judges.panel import JudgePanel
from service.judges.relevance import RelevanceJudge
from service.judges.retrieval import RetrievalJudge

__all__ = (
    "Judge", "JudgePanel", "build_judge_client",
    "is_adversarial_category", "is_honesty_category",
    "CorrectnessJudge", "RelevanceJudge", "GroundednessJudge", "RetrievalJudge",
    "ContextPrecisionRecallJudge", "FallbackJudge", "JailbreakJudge",
)
