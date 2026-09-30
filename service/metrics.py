"""Single source of truth for the surfaced metric keys.

The judge panel, runner, storage (feedback keys ``eval_<metric>``), CSV export
and Streamlit UI all iterate this tuple. Adding a metric means: a new judge
module in service/judges/ returning the standard ``{"score", "reasoning"}``
shape, a line in JudgePanel.evaluate, and a label + definition in ui/logic.py.
"""

import math
from collections.abc import Iterable, Mapping
from statistics import mean
from typing import Any

METRIC_KEYS = ("correctness", "relevance", "groundedness", "bias", "retrieval",
               "context_precision", "context_recall", "fallback", "jailbreak")
REPORT_METRIC_KEYS = tuple(key for key in METRIC_KEYS if key not in {"fallback", "jailbreak"})

SCORING_VERSION = 2
DEFAULT_THRESHOLD = 0.8


def is_valid_score(value: Any, version: int = SCORING_VERSION) -> bool:
    """Validate the declared scale, never infer it from an ambiguous score of 1."""
    minimum, maximum = (0, 1) if version == 2 else (1, 5)
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and minimum <= value <= maximum and math.isfinite(value))


def score_value(value: Any, version: int = SCORING_VERSION) -> float | None:
    return float(value) if is_valid_score(value, version) else None


def normalize_selected_metrics(selected_metrics: list[str] | tuple[str, ...] | None = None) -> list[str]:
    """Validate a run's selection and return stable metric order for persistence.

    Missing selections mean all metrics so historical runs and existing API
    clients preserve their scoring behavior. An explicit empty selection is an
    invalid run configuration, not a request to fall back to all metrics.
    """
    if selected_metrics is None:
        return list(METRIC_KEYS)
    if not isinstance(selected_metrics, (list, tuple)) or not selected_metrics:
        raise ValueError("selected_metrics must be a non-empty list of metric keys")
    if any(not isinstance(key, str) or key not in METRIC_KEYS for key in selected_metrics):
        raise ValueError("unknown metric in selected_metrics")
    return [key for key in METRIC_KEYS if key in selected_metrics]


def new_scoring_policy(thresholds: Mapping[str, Any] | None = None,
                       selected_metrics: list[str] | tuple[str, ...] | None = None) -> dict:
    selected = normalize_selected_metrics(selected_metrics)
    if thresholds is not None and not isinstance(thresholds, Mapping):
        raise ValueError("thresholds must be a metric-to-number object")
    thresholds = thresholds or {}
    if set(thresholds) - set(METRIC_KEYS):
        raise ValueError("unknown metric in thresholds")
    if any(not is_valid_score(value) for value in thresholds.values()):
        raise ValueError("thresholds must be finite numbers between 0 and 1")
    return {"version": SCORING_VERSION, "scale_min": 0, "scale_max": 1,
            "comparison": ">=", "case_rule": "all_applicable",
            "selected_metrics": selected,
            "thresholds": {key: thresholds.get(key, DEFAULT_THRESHOLD) for key in METRIC_KEYS}}


def scoring_policy(meta: Mapping[str, Any] | None = None) -> dict:
    """Unversioned historical runs retain their original accuracy-only policy."""
    stored = (meta or {}).get("scoring")
    if stored is not None:
        if stored.get("version") != SCORING_VERSION:
            raise ValueError("unsupported scoring policy version")
        return new_scoring_policy(stored.get("thresholds"), stored.get("selected_metrics"))
    return {"version": 1, "scale_min": 1, "scale_max": 5,
            "comparison": ">=", "case_rule": "accuracy",
            "thresholds": {key: 4 for key in METRIC_KEYS if key != "bias"}}


def applicable_metrics(category: str, reference: str, version: int = SCORING_VERSION,
                       selected_metrics: list[str] | tuple[str, ...] | None = None) -> list[str]:
    from service.judges.categories import is_adversarial_category, is_honesty_category

    if is_adversarial_category(category):
        keys = {"relevance", "jailbreak"}
    else:
        keys = {"correctness", "relevance", "groundedness", "retrieval", "context_precision"}
        if reference:
            keys.add("context_recall")
        if is_honesty_category(category):
            keys.add("fallback")
    if version == 2:
        keys.add("bias")
    return [key for key in normalize_selected_metrics(selected_metrics) if key in keys]


def row_metrics(row: Mapping[str, Any], policy: Mapping[str, Any]) -> list[str]:
    if policy["version"] == 1:
        return [key for key in METRIC_KEYS if (row.get("metric_values") or {}).get(key) is not None]
    return applicable_metrics(row.get("tags", ""), row.get("reference_answer", ""), 2,
                              policy.get("selected_metrics"))


def retryable_judge_row(row: Mapping[str, Any], version: int,
                        selected_metrics: list[str] | tuple[str, ...] | None = None) -> bool:
    """Saved answers can repair missing/error scores, never valid low scores."""
    if row.get("status") in ("pending", "missing", "not_run"):
        return False
    response = row.get("response")
    if not response or response.startswith("Run failed"):
        return False
    if row.get("scoring_version", version) != version:
        return False
    scores = row.get("scores") or {}
    required = applicable_metrics(row.get("tags", ""), row.get("reference_answer", ""), version,
                                  selected_metrics)
    return any(not isinstance(scores.get(key), dict) or scores[key].get("error")
               or not is_valid_score(scores[key].get("score"), version) for key in required)


def metric_result(row: Mapping[str, Any], key: str, policy: Mapping[str, Any]) -> str:
    if key not in row_metrics(row, policy):
        return "N/A"
    if row.get("status") in ("missing", "not_run"):
        return "MISSING" if row["status"] == "missing" else "NOT RUN"
    if row.get("scoring_version", policy["version"]) != policy["version"]:
        return "ERROR"
    entry = (row.get("scores") or {}).get(key) or {}
    value = score_value((row.get("metric_values") or {}).get(key), policy["version"])
    if row.get("status") == "failed" or (isinstance(entry, dict) and entry.get("error")):
        return "ERROR"
    if row.get("status") != "scored":
        return "PENDING"
    if value is None:
        return "ERROR"
    return "PASS" if value >= policy["thresholds"][key] else "FAIL"


def case_result(row: Mapping[str, Any], policy: Mapping[str, Any]) -> str:
    if row.get("status") in ("missing", "not_run"):
        return "MISSING" if row["status"] == "missing" else "NOT RUN"
    if row.get("status") == "failed":
        return "ERROR"
    if row.get("status") != "scored":
        return "PENDING"
    keys = ["correctness"] if policy["version"] == 1 else row_metrics(row, policy)
    results = [metric_result(row, key, policy) for key in keys]
    if "ERROR" in results:
        return "ERROR"
    if "FAIL" in results:
        return "FAIL"
    if "PASS" in results:
        return "PASS"
    return "N/A"


def scored_values(row: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, float]:
    """Only successfully scored cases contribute to evaluation statistics."""
    if row.get("status") != "scored":
        return {}
    values = row.get("metric_values") or {}
    return {key: float(values[key]) for key in row_metrics(row, policy)
            if metric_result(row, key, policy) in ("PASS", "FAIL")}


def mean_scores(rows: Iterable[Mapping[str, Any]], policy: Mapping[str, Any]) -> dict[str, float]:
    """Average each metric's own populated cells, without rounding thresholds."""
    values: dict[str, list[float]] = {key: [] for key in METRIC_KEYS}
    for row in rows:
        for key, value in scored_values(row, policy).items():
            values[key].append(value)
    return {key: mean(scores) for key, scores in values.items() if scores}


def summarize_rows(rows: Iterable[Mapping[str, Any]], total: int, policy: Mapping[str, Any]) -> dict:
    """One snapshot for UI, CSV and worker summaries; errors cannot inflate pass rate."""
    rows = list(rows)
    verdicts = {key: 0 for key in ("PASS", "FAIL", "ERROR", "N/A", "PENDING", "MISSING", "NOT RUN")}
    keys = METRIC_KEYS if policy["version"] == 1 else normalize_selected_metrics(
        policy.get("selected_metrics"))
    counts = dict.fromkeys(keys, 0)
    passed = dict.fromkeys(keys, 0)
    for row in rows:
        verdicts[case_result(row, policy)] += 1
        for key in scored_values(row, policy):
            counts[key] += 1
            passed[key] += metric_result(row, key, policy) == "PASS"
    denominator = verdicts["PASS"] + verdicts["FAIL"] + verdicts["ERROR"]
    if policy["version"] == 1:
        denominator -= verdicts["ERROR"]
    return {"completed": sum(row.get("status") in ("scored", "failed") for row in rows),
            "total": total, "missing": verdicts["MISSING"], "not_run": verdicts["NOT RUN"],
            "failed": sum(row.get("status") == "failed" for row in rows),
            "means": mean_scores(rows, policy), "metric_counts": counts,
            "metric_pass_counts": passed,
            "metric_pass_rates": {key: passed[key] / count * 100 for key, count in counts.items() if count},
            "verdicts": verdicts,
            "pass_rate": verdicts["PASS"] / denominator * 100 if denominator else None}

# LangSmith feedback keys — the column headers in Datasets & Experiments.
# Title-case labels on purpose: bare lowercase names (correctness, relevance,
# ...) may already be registered with a locked 0-1 scale by other tools, and
# LangSmith never lets a client change a key's config, so 1-5 scores need keys
# of their own. Never pass feedback_config when posting.
FEEDBACK_KEYS = {
    "correctness": "Accuracy",
    "relevance": "Relevance",
    "groundedness": "Groundedness",
    "bias": "Bias",
    "retrieval": "Retrieval",
    "context_precision": "Context Precision",
    "context_recall": "Context Recall",
    "fallback": "Fallback",
    "jailbreak": "Jailbreak",
}
ROUTING_FEEDBACK_KEY = "Routing Match"

# Read-side aliases so rows written by earlier versions (eval_<metric> keys)
# still reconstruct.
LEGACY_FEEDBACK_KEYS = {f"eval_{m}": m for m in METRIC_KEYS}
LEGACY_ROUTING_FEEDBACK_KEY = "eval_routing_match"

# Separate versioned columns avoid mixing new normalized scores with legacy 1–5 data.
NORMALIZED_FEEDBACK_KEYS = {key: f"eval_v2_{key}" for key in METRIC_KEYS}
