"""Shared report snapshots and CSV export for the results shown in the UI."""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from typing import Any

from service.metrics import (
    METRIC_KEYS,
    REPORT_METRIC_KEYS,
    case_result,
    metric_result,
    score_value,
    scoring_policy,
    summarize_rows,
)
from service.results import reconcile_rows


def _csv_safe(value: Any) -> Any:
    """Prevent untrusted text from being interpreted as spreadsheet formulas."""
    if isinstance(value, str) and value.lstrip(" \t\r\n")[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def report_snapshot(meta: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Resolve case visibility, metric scope, and totals once for every format."""
    rows = reconcile_rows(meta, rows)
    policy = scoring_policy(meta)
    selected = policy.get("selected_metrics", METRIC_KEYS)
    subset = policy["version"] == 2 and set(selected) != set(METRIC_KEYS)
    # Keep the established compact columns for historical/default runs. A custom
    # selection must expose every chosen judge, including category-only checks.
    metrics = [key for key in (selected if subset else REPORT_METRIC_KEYS)
               if key in policy["thresholds"]]
    summary = summarize_rows(rows, (meta.get("summary") or {}).get("total", len(rows)), policy)
    return {"rows": rows, "policy": policy, "selected": selected, "subset": subset,
            "metrics": metrics, "summary": summary}


def run_csv(run_id: str, meta: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> bytes:
    """Export full-precision quality scores and one saved threshold per metric."""
    snapshot = report_snapshot(meta, rows)
    rows, policy = snapshot["rows"], snapshot["policy"]
    selected, subset = snapshot["selected"], snapshot["subset"]
    metrics, summary = snapshot["metrics"], snapshot["summary"]
    buf = io.StringIO()
    writer = csv.writer(buf)

    def write(values: Sequence[Any]) -> None:
        writer.writerow([_csv_safe(value) for value in values])

    write(["# Run summary"])
    write(["run_id", run_id])
    write(["dataset", meta.get("dataset_name"), f"v{meta.get('dataset_version')}"])
    write(["application", meta.get("application_key", "")])
    write(["execution_mode", meta.get("execution_mode", "parallel")])
    write(["judge_model", meta.get("judge_model", "")])
    write(["status", meta.get("status")])
    write(["scoring_version", policy["version"], "scale_min", policy["scale_min"],
           "scale_max", policy["scale_max"], "comparison", policy["comparison"],
           "case_rule", policy["case_rule"]])
    if policy["version"] == 2:
        write(["selected_metrics", *(selected if subset else ["all"])])
    write(["pass_rate_percent", summary["pass_rate"], "passed", summary["verdicts"]["PASS"],
           "quality_failed", summary["verdicts"]["FAIL"], "errors", summary["verdicts"]["ERROR"]])
    write(["started_at", meta.get("started_at", ""), "finished_at", meta.get("finished_at", "")])
    write(["cases", summary["total"], "completed", summary["completed"], "failed", summary["failed"]])
    if summary["missing"] or summary["not_run"]:
        write(["missing_results", summary["missing"], "not_run", summary["not_run"]])
    for name in metrics:
        write([f"{name} mean", summary["means"].get(name),
               "scored_count", summary["metric_counts"][name],
               "threshold", policy["thresholds"][name],
               "pass_rate_percent", summary["metric_pass_rates"].get(name)])
    write([])
    write(["index", "name", "question", "expected_response", "actual_response", "status", *metrics,
           "latency_s", "agent_latency_s", "attempts", "usage_tokens", "expected_agent",
           "routing_observed", "routing_match", "error", "evaluation_result",
           *(f"{key}_result" for key in metrics)])
    for row in rows:
        results = {key: metric_result(row, key, policy) for key in metrics}
        values = row.get("metric_values") or {}
        write([row.get("index"), row.get("name"), row.get("question"),
               row.get("reference_answer", ""), row.get("response", ""), row.get("status"),
               *(score_value(values.get(key), policy["version"])
                 if results[key] in ("PASS", "FAIL") else None for key in metrics),
               row.get("latency_s"), row.get("agent_latency_s"), row.get("attempts", 1),
               row.get("usage_tokens"), row.get("expected_agent", ""), row.get("routing_observed"),
               row.get("routing_match"), row.get("error", ""), case_result(row, policy),
               *(results[key] for key in metrics)])
    write([])
    metric_scope = (
        "The report shows all selected metrics; unselected metrics do not affect results. "
        if subset else
        "The report shows core quality metrics; the overall case verdict still includes "
        "all applicable category checks. "
    )
    write(["# Legend: PASS means score >= saved threshold. Scores and means are not rounded. "
           "Thresholds appear once per metric in the summary. Blank = N/A, PENDING, MISSING, NOT RUN or ERROR; "
           "see each metric_result. Means use valid applicable scores only, with each metric's "
           "own denominator; zero is a valid v2 score. Case pass rate = PASS / (PASS + FAIL + ERROR). "
           "Pending, missing, not-run and N/A cases are excluded. MISSING means the result is "
           "unavailable and the outcome is unknown; NOT RUN means the case was not attempted. "
           "v1 excludes errors from its pass-rate denominator. "
           + metric_scope + "Full diagnostics remain in the results page. "
           "v2 requires all selected, applicable metrics to pass; v1 retains accuracy-only results."])
    write(["# routing_match: True when every agent named in expected_agent was actually invoked; "
           "blank when the dataset row declares no expectation."])
    return buf.getvalue().encode("utf-8-sig")
