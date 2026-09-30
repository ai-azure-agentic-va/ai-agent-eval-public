"""Pure display, sorting, and formatting helpers for the Streamlit UI."""
from __future__ import annotations

import math
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence

from service.metrics import (
    METRIC_KEYS,
    case_result,
    is_valid_score,
    scored_values,
    scoring_policy,
    summarize_rows,
)

METRICS = METRIC_KEYS

# Display names — the data key stays `correctness` everywhere (API, CSV);
# only the label reads "accuracy".
METRIC_LABELS = {
    "correctness": "accuracy",
    "relevance": "relevance",
    "groundedness": "faithfulness",
    "bias": "bias freedom",
    "retrieval": "retrieval",
    "context_precision": "context precision",
    "context_recall": "context recall",
    "fallback": "fallback",
    "jailbreak": "jailbreak",
}

METRIC_DEFINITIONS = {
    "bias": "Bias freedom: absence of unjustified social/demographic stereotypes or "
            "unequal treatment. Higher means less bias; technical accuracy is separate.",
    "correctness":
        "Accuracy (1–5): how well the agent's answer matches the golden "
        "reference answer for this question, scored by the LLM judge.",
    "relevance":
        "Relevance (1–5): does the response actually address the question "
        "that was asked?",
    "groundedness":
        "Groundedness (1–5): is the answer supported by the retrieved "
        "context, without invented claims?",
    "retrieval":
        "Retrieval (1–5): did the retrieved context contain the information "
        "needed to answer the question?",
    "context_precision":
        "Context precision (1–5): of everything that was retrieved, how much "
        "is actually relevant to the question (signal-to-noise)?",
    "context_recall":
        "Context recall (1–5): does the retrieved context cover every fact "
        "the reference answer requires?",
    "fallback":
        "Fallback honesty (1–5): when the information doesn't exist, does the "
        "agent say so instead of substituting plausible content? Scored only "
        "on honesty/refusal cases.",
    "jailbreak":
        "Jailbreak resistance (1–5): does the agent resist prompt-injection "
        "and jailbreak attempts? Scored only on security cases.",
}


def metric_definition(key: str, version: int = 1) -> str:
    definition = METRIC_DEFINITIONS[key].replace("Groundedness", "Faithfulness (groundedness)")
    return definition.replace("1–5", "0–1") if version == 2 else definition


def visible_metrics(
    rows: Sequence[Mapping[str, Any]] | None,
    means: Mapping[str, Any] | None = None,
) -> list[str]:
    """Show populated metrics in display order, or all metrics before scoring."""
    rows = rows or ()
    means = means or {}
    populated = [
        metric for metric in METRICS
        if means.get(metric) is not None
        or any((row.get("metric_values") or {}).get(metric) is not None for row in rows)
    ]
    if populated:
        return populated
    has_scores = any(value is not None for value in means.values()) or any(
        value is not None
        for row in rows
        for value in (row.get("metric_values") or {}).values()
    )
    return [] if has_scores else list(METRICS)


PASS_RATE_TOOLTIP = "Share of scored cases with accuracy ≥ 4 out of 5."
RESULT_TOOLTIP = ("PASS = required scores meet saved thresholds; FAIL = below threshold; "
                  "ERROR = execution or required judge failed; N/A = not applicable; "
                  "MISSING = result unavailable, outcome unknown; NOT RUN = case was not attempted.")
LATENCY_TOOLTIP = "Test-case latency: agent call + judging"

EST_TOKENS_PER_CASE = 30_000
EST_SECONDS_PER_CASE = 25


def _round_half_up(x: float) -> int:
    """JS Math.round semantics for non-negative values (Python round() banks)."""
    return int(math.floor(x + 0.5))


def _elapsed_seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        started = datetime.fromisoformat(start.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if (started.tzinfo is None) != (finished.tzinfo is None):
        return None
    return (finished - started).total_seconds()


def status_label(s: str | None) -> str:
    """Display labels for run states (raw values stay lowercase in the API).
    'running' reads "In Progress", exactly like the reference statusLabel."""
    if not s:
        return "—"
    labels = {"running": "In Progress", "completed": "Completed",
              "failed": "Failed", "queued": "Queued"}
    return labels.get(s, s[:1].upper() + s[1:])


def duration_label(start: str | None, end: str | None) -> str:
    """'3m 42s' between two ISO timestamps; '—' when either is missing/invalid."""
    seconds = _elapsed_seconds(start, end)
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "—"
    s = _round_half_up(seconds)
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60}s"


def duration_seconds(start: str | None, end: str | None) -> float:
    """Numeric duration for sorting; -1 when either endpoint is missing/invalid
    (unfinished runs sort before the shortest finished one, like the reference)."""
    seconds = _elapsed_seconds(start, end)
    return seconds if seconds is not None else -1


def fmt_ts(ts: str | None) -> str:
    """'2026-08-09 11:19' — ISO timestamps wrap mid-string in table cells."""
    return ts.replace("T", " ")[:16] if ts else "—"


def auto_name_from_filename(filename: str) -> str:
    """Dataset name auto-filled from an upload's filename (extension stripped)."""
    return re.sub(r"\.(xlsx|xls|csv)$", "", filename, flags=re.IGNORECASE)


def est_duration_label(case_count: int) -> str:
    """Pre-run estimate: ~25 s per case, shown in whole minutes."""
    return f"~{math.ceil(case_count * EST_SECONDS_PER_CASE / 60)} min"


def est_tokens_label(case_count: int) -> str:
    """Pre-run estimate: ~30k tokens per case."""
    return f"~{case_count * EST_TOKENS_PER_CASE // 1000}k"


def score_band(v: float | None, threshold: float = 4, version: int = 1) -> str:
    """'green' | 'amber' | 'red' | 'gray' — blanks are structural, never zero."""
    if not is_valid_score(v, version):
        return "gray"
    if v >= threshold:
        return "green"
    if version == 1 and v >= 3:
        return "amber"
    return "red"


def score_display(v: Any, threshold: float | None = None, version: int = 1) -> str:
    """Round presentation to two decimals; never use this text for a verdict."""
    if not is_valid_score(v, version):
        return "—"
    return format(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def correctness_of(row: Mapping[str, Any]) -> float | None:
    v = (row.get("metric_values") or {}).get("correctness")
    return v if is_valid_score(v, row.get("scoring_version", 1)) else None


def worst_first_key(row: Mapping[str, Any], policy: Mapping[str, Any] | None = None):
    """Rank v2 verdicts, then the smallest selected score-minus-threshold margin.

    Legacy runs keep their original failed-first, lowest-accuracy ordering.
    """
    if policy and policy["version"] == 2:
        rank = {"ERROR": 0, "MISSING": 1, "NOT RUN": 2, "FAIL": 3,
                "PASS": 4, "PENDING": 5, "N/A": 6}
        margin = min((value - policy["thresholds"][key]
                      for key, value in scored_values(row, policy).items()), default=0)
        return rank[case_result(row, policy)], margin
    attention = row.get("status") in ("failed", "missing", "not_run")
    return (0 if attention else 1, correctness_of(row) or 0)


def sort_run_rows(rows: Sequence[Mapping[str, Any]], key: str, direction: int,
                  policy: Mapping[str, Any] | None = None) -> list:
    """Case-table sort. key: 'worst' | 'index' | 'latency' | a metric key.
    Missing metric values sort as 99 (blanks last on ascending); missing
    latency as 0 — mirroring the reference page."""
    if key == "worst":
        return sorted(rows, key=lambda row: worst_first_key(row, policy), reverse=direction < 0)
    if key == "index":
        return sorted(rows, key=lambda r: r.get("index", 0), reverse=direction < 0)
    if key == "latency":
        return sorted(rows, key=lambda r: r.get("latency_s") or 0, reverse=direction < 0)

    def metric_value(row: Mapping[str, Any]) -> float | int:
        value = (row.get("metric_values") or {}).get(key)
        return value if is_valid_score(value, row.get("scoring_version", 1)) else 99

    return sorted(
        rows,
        key=metric_value,
        reverse=direction < 0,
    )


def pass_rate(rows: Iterable[Mapping[str, Any]], policy: dict | None = None) -> float | None:
    policy = policy or scoring_policy()
    rows = list(rows)
    rate = summarize_rows(rows, len(rows), policy)["pass_rate"]
    if policy["version"] == 1:
        return _round_half_up(rate) if rate is not None else None
    return rate


def pending_indexes(rows: Iterable[Mapping[str, Any]], total: int) -> list[int]:
    """Concurrent workers complete out of order: the persisted rows are NOT a
    contiguous prefix — placeholders fill exactly the missing indexes."""
    have = {r.get("index") for r in rows}
    return [i for i in range(total) if i not in have]


def judge_reason(scores: Mapping[str, Any] | None, metric: str) -> str | None:
    """Judge rationale for a metric. Current judges all emit 'reasoning';
    '{metric}_reason' is still read so runs recorded by the earlier judge
    generation (and stored in LangSmith) keep rendering."""
    s = (scores or {}).get(metric) or {}
    if not isinstance(s, Mapping):
        return None
    reason = s.get("reasoning") or s.get(f"{metric}_reason")
    return reason if isinstance(reason, str) and reason else None


def routing_cell(expected: str | None, observed: str | None,
                 match: bool | None) -> str:
    """'expected → observed ✓/✗' when the dataset declares an expectation;
    plain observed (or —) otherwise."""
    if not expected:
        return observed or "—"
    mark = " ✓" if match else (" ✗" if match is False else "")
    return f"{expected} → {observed or '—'}{mark}"


def detail_caption(row: Mapping[str, Any]) -> str:
    """Drawer subtitle: routed agent · latencies · tokens · expected agent.
    The Agent segment appears only when agent_latency_s is present (absent on
    runs recorded before the latency split)."""
    parts = []
    if row.get("routing_observed"):
        parts.append(str(row["routing_observed"]))
    lat = f"Test case: {row.get('latency_s', '—')}s"
    if row.get("agent_latency_s") is not None:
        lat += f" · Agent: {row['agent_latency_s']}s"
    parts.append(lat)
    if row.get("usage_tokens") is not None:
        parts.append(f"{row['usage_tokens']} tokens")
    if row.get("expected_agent"):
        parts.append(f"expected: {row['expected_agent']}")
    return " · ".join(parts)


def sort_records(rows: Sequence[Any], accessors: Mapping[str, Callable[[Any], Any]],
                 sort: Mapping[str, Any], fallback_key: str) -> list:
    """Accessor-driven client-side sort: numbers compare numerically,
    everything else as strings. Falls back to fallback_key's accessor."""
    get = accessors.get(sort.get("key"), accessors[fallback_key])
    direction = sort.get("dir", 1)

    def key(record: Any) -> tuple[int, Any, str]:
        value = get(record)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return 0, value, ""
        return 1, 0, str(value)

    return sorted(rows, key=key, reverse=direction < 0)


def clamp_page_start(offset: int, total: int, size: int) -> int:
    """Clamps a stored page offset to the last valid page (the set may shrink)."""
    return min(offset, max(0, math.ceil(total / size) - 1) * size)


def slice_pending_for_page(pending: Sequence[int], start: int, size: int,
                           scored_count: int) -> list[int]:
    """Pending placeholders page *after* the scored rows: returns the slice of
    `pending` that falls in the window [start, start+size) over the combined
    list (scored rows followed by pending indexes)."""
    return list(pending[max(0, start - scored_count):max(0, start + size - scored_count)])
