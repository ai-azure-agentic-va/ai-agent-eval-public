"""Select native 0–1 rubrics loaded from the top-level prompts directory."""

from prompts import (
    BIAS_PROMPT,
    CONTEXT_PRECISION_RECALL_PROMPT,
    CORRECTNESS_PROMPT,
    FALLBACK_PROMPT,
    GROUNDEDNESS_PROMPT,
    JAILBREAK_PROMPT,
    NORMALIZED_INSTRUCTIONS,
    RELEVANCE_PROMPT,
    RETRIEVAL_PROMPT,
)

RUBRICS = {
    "correctness": CORRECTNESS_PROMPT,
    "relevance": RELEVANCE_PROMPT,
    "groundedness": GROUNDEDNESS_PROMPT,
    "bias": BIAS_PROMPT,
    "retrieval": RETRIEVAL_PROMPT,
    "context_precision_recall": CONTEXT_PRECISION_RECALL_PROMPT,
    "fallback": FALLBACK_PROMPT,
    "jailbreak": JAILBREAK_PROMPT,
}


def normalized_template(metric: str) -> str:
    return NORMALIZED_INSTRUCTIONS + RUBRICS[metric]
