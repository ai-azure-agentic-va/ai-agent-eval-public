"""Judge prompt files loaded once as UTF-8 string constants.

Resolve paths relative to this package, so callers can use any working directory.
Keep template whitespace intact to preserve existing judge inputs.
"""

from pathlib import Path

_PROMPT_DIR = Path(__file__).resolve().parent


def _read(relative_path: str) -> str:
    return (_PROMPT_DIR / relative_path).read_text(encoding="utf-8")


FALLBACK_REFERENCE_BLOCK = _read("fallback_reference.txt")
NORMALIZED_INSTRUCTIONS = _read("instructions.txt")
CORRECTNESS_PROMPT = _read("correctness.txt")
RELEVANCE_PROMPT = _read("relevance.txt")
GROUNDEDNESS_PROMPT = _read("groundedness.txt")
BIAS_PROMPT = _read("bias.txt")
RETRIEVAL_PROMPT = _read("retrieval.txt")
CONTEXT_PRECISION_RECALL_PROMPT = _read("context_precision_recall.txt")
FALLBACK_PROMPT = _read("fallback.txt")
JAILBREAK_PROMPT = _read("jailbreak.txt")
