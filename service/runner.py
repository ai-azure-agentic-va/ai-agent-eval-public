"""Background evaluation with persisted rows and live progress events.

New runs default to spreadsheet order. Parallel independent execution is opt-in;
conversation mode runs ordered turns on one agent thread. Independent failures
retry with backoff, but conversation failures stop without replaying a turn.
Judge recovery always reuses saved answers; low scores are never re-rolled.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass, field

from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from service import storage
from service.agent_client import LangGraphAgentClient
from service.config import env_int, judge_model_config
from service.execution import execution_mode
from service.judges import JudgePanel
from service.logging_config import run_id_var
from service.metrics import (
    METRIC_KEYS,
    applicable_metrics,
    retryable_judge_row,
    score_value,
    scoring_policy,
    summarize_rows,
)
from service.tracing import RunTrace, tracing_enabled

log = logging.getLogger(__name__)

_events: dict[str, "queue.Queue[dict]"] = {}
_lock = threading.Lock()

# Delays before the second and third agent-call attempts.
RETRY_DELAYS_S = (5, 15)


def concurrency() -> int:
    """Worker count — bounded by the Azure OpenAI quota behind agent + judges."""
    return env_int("EVAL_RUN_CONCURRENCY", 4, minimum=1)


def events_for(run_id: str) -> "queue.Queue[dict]":
    with _lock:
        if run_id not in _events:
            _events[run_id] = queue.Queue()
        return _events[run_id]


def _emit(run_id: str, event: dict) -> None:
    events_for(run_id).put(event)


def metric_values(scores: dict, version: int = 1) -> dict:
    """``{metric: score|None}`` for every surfaced metric — None for a judge
    that did not run or errored (a structural blank, never zero)."""
    return {k: score_value((scores.get(k) or {}).get("score"), version) for k in METRIC_KEYS}


def routing_observed(context: str) -> str:
    """Marker fallback for agent clients that don't report tool-call routing."""
    for marker, agent in (("[adls-agent]", "adls-agent"), ("[adf-agent]", "adf-agent")):
        if marker in context:
            return agent
    return "ai_search_or_none" if context else "none"


def routing_match(expected: str | None, observed: str | None) -> bool | None:
    """True when every expected agent was actually invoked; None when the
    dataset row declares no expectation. 'none' must match exactly."""
    expected = (expected or "").strip()
    if not expected:
        return None
    observed_set = {p.strip() for p in (observed or "").split(",") if p.strip()}
    expected_set = {p.strip() for p in expected.split(",") if p.strip()}
    if expected_set == {"none"}:
        return observed_set in ({"none"}, set())
    return expected_set <= observed_set


def call_agent_with_retry(agent: LangGraphAgentClient, question: str) -> dict:
    """Retry transient agent failures; return the last attempt's output."""
    if getattr(agent, "conversation_mode", False):
        # A timeout may still have advanced the remote conversation.
        return agent.call_agent(question)
    for delay in RETRY_DELAYS_S:
        out = agent.call_agent(question)
        if not out["response"].startswith("Run failed"):
            return out
        log.debug("agent call failed, retrying in %ss", delay)
        time.sleep(delay)
    return agent.call_agent(question)  # final attempt, returned as-is


def _case_trace_inputs(inputs: dict) -> dict:
    row = inputs.get("row") or {}
    return {"question": row.get("question"), "tags": row.get("tags")}


def _case_trace_outputs(record: dict) -> dict:
    return {
        "status": record.get("status"),
        "answer": (record.get("response") or "")[:1000],
        "metric_values": record.get("metric_values"),
        "routing_observed": record.get("routing_observed"),
        "routing_match": record.get("routing_match"),
        "latency_s": record.get("latency_s"),
        "error": record.get("error"),
    }


# @traceable no-ops when tracing is off. When on, each attempt is ONE chain
# ("case-…") whose children are the agent-call span and the judge spans.
@traceable(run_type="chain", name="case",
           process_inputs=_case_trace_inputs, process_outputs=_case_trace_outputs)
def _attempt_row(i: int, row: dict, agent: LangGraphAgentClient, judges: JudgePanel) -> dict:
    """One full execution of a case: agent call + judging.

    ``agent_latency_s`` times the agent call alone (incl. its HTTP retries);
    ``latency_s`` is the whole attempt, judging included (what the UI shows)."""
    run_tree = get_current_run_tree()  # None unless tracing is active
    if run_tree is not None:
        run_tree.name = f"case-{i:05d} {row.get('name', '')}".rstrip()
        run_tree.metadata["routing_expected"] = row.get("expected_agent", "")
    record = {**row, "index": i, "status": "failed", "attempted": True,
              "started_at": time.time()}
    record["scoring_version"] = getattr(judges, "score_version", 1)
    record["applicable_metrics"] = applicable_metrics(
        row.get("tags", ""), row.get("reference_answer", ""), record["scoring_version"],
        getattr(judges, "selected_metrics", None))
    t_case = time.perf_counter()
    try:
        t0 = time.perf_counter()
        agent_out = call_agent_with_retry(agent, row["question"])
        record["agent_latency_s"] = round(time.perf_counter() - t0, 2)
        record["response"] = agent_out["response"]
        record["agent_completed"] = not agent_out["response"].startswith("Run failed")
        record["thread_id"] = agent_out.get("thread_id")
        record["evaluation_question"] = agent_out.get("evaluation_question", row["question"])
        record["context"] = agent_out["context"]
        record["usage_tokens"] = agent_out.get("usage_tokens", 0)
        record["usage"] = agent_out.get("usage") or {"total_tokens": record["usage_tokens"]}
        record["routing_observed"] = agent_out.get("routing") or routing_observed(agent_out["context"])
        record["routing_match"] = routing_match(row.get("expected_agent", ""),
                                                record["routing_observed"])
        if run_tree is not None:
            run_tree.metadata["routing"] = record["routing_observed"]
        if agent_out["response"].startswith("Run failed"):
            record["error"] = agent_out["response"]
        else:
            scores = {}
            if record["applicable_metrics"]:
                scores = judges.evaluate(
                    question=record["evaluation_question"], response=agent_out["response"],
                    context=agent_out["context"], reference=row.get("reference_answer", ""),
                    category=row.get("tags", ""))
            record["scores"] = {key: value for key, value in scores.items()
                                if key in record["applicable_metrics"]}
            record["metric_values"] = metric_values(record["scores"], record["scoring_version"])
            judge_errors = [v["error"] for v in record["scores"].values()
                            if isinstance(v, dict) and v.get("error")]
            if judge_errors and all(v is None for v in record["metric_values"].values()):
                # Agent answered but EVERY judge errored — that is a failed row,
                # not a scored one with structurally-blank cells.
                record["error"] = "all judges failed: " + "; ".join(judge_errors[:3])
            else:
                record["status"] = "scored"
    except Exception as exc:  # a row failure must not kill the run
        log.exception("case %05d raised", i)
        record["error"] = str(exc)
    record["latency_s"] = round(time.perf_counter() - t_case, 2)
    return record


def _process_row(run_id: str, i: int, row: dict, agent: LangGraphAgentClient,
                 judges: JudgePanel, trace: RunTrace | None = None) -> dict:
    """Execute one case on a pool thread; owns no shared state.

    An EXECUTION failure gets exactly one automatic retry before the case is
    marked failed."""
    run_id_var.set(run_id)  # pool threads do not inherit the caller's contextvars

    def attempt(n: int) -> dict:
        with (trace.case(i, n) if trace is not None else nullcontext()):
            return _attempt_row(i, row, agent, judges)

    _emit(run_id, {"type": "row", "index": i, "name": row["name"], "status": "running"})
    record = attempt(1)
    record["attempts"] = 1
    if (not getattr(agent, "conversation_mode", False)
            and record["status"] == "failed" and not retryable_judge_row(
                record, getattr(judges, "score_version", 1),
                getattr(judges, "selected_metrics", None))):
        log.warning("case %05d failed (%s) — automatic retry",
                    i, (record.get("error") or "")[:120])
        first_error = record.get("error")
        record = attempt(2)
        record["attempts"] = 2
        if record["status"] == "failed" and first_error:
            record["error"] = f"{record.get('error', '')} (first attempt: {first_error})"[:1000]
    log.info("case %05d %s: status=%s attempts=%s agent_latency_s=%s latency_s=%s "
             "tokens=%s routing=%s",
             i, row.get("name", ""), record["status"], record["attempts"],
             record.get("agent_latency_s"), record.get("latency_s"),
             record.get("usage_tokens"), record.get("routing_observed"))
    return record


@dataclass
class _RunProgress:
    run_id: str
    total: int
    completed: int = 0
    failed: int = 0
    policy: dict = field(default_factory=scoring_policy)
    records: dict[int, dict] = field(default_factory=dict)
    completed_indices: set[int] = field(default_factory=set)
    unsaved_rows: dict[int, dict] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_records(cls, run_id: str, total: int, records: list[dict],
                     policy: dict | None = None) -> _RunProgress:
        records_by_index = {record["index"]: record for record in records}
        completed_indices = {index for index, record in records_by_index.items()
                             if record.get("status") in ("scored", "failed")}
        return cls(run_id=run_id, total=total, completed=len(completed_indices),
                   failed=sum(r.get("status") == "failed" for r in records_by_index.values()),
                   policy=policy or scoring_policy(), records=records_by_index,
                   completed_indices=completed_indices)

    def summary(self) -> dict:
        return {**summarize_rows(self.records.values(), self.total, self.policy),
                "completed": self.completed, "failed": self.failed}

    def finish(self, record: dict) -> bool:
        persisted = False
        save_error = None
        for _ in range(2):
            try:
                storage.save_row(self.run_id, record["index"], record)
                persisted = True
                break
            except Exception as exc:
                save_error = str(exc)
                log.warning("save_row failed (row %s): %s", record["index"], exc)
        if not persisted:
            # Keep a compact fallback in run metadata. A result-storage
            # failure must neither disappear nor masquerade as a success.
            record["persistence_error"] = True
            if record.get("error"):
                record["execution_error"] = record["error"]
            record["error"] = f"Could not save case result: {save_error}"[:1000]
            if record.get("status") != "not_run":
                record["status"] = "failed"

        with self.lock:
            self.records[record["index"]] = record
            if record.get("status") in ("scored", "failed"):
                self.completed_indices.add(record["index"])
            else:
                self.completed_indices.discard(record["index"])
            if not persisted:
                self.unsaved_rows[record["index"]] = {
                    key: record[key] for key in (
                        "index", "name", "status", "attempted", "attempts", "agent_completed",
                        "error", "execution_error", "persistence_error",
                    ) if key in record
                }
            else:
                self.unsaved_rows.pop(record["index"], None)
            self.completed = len(self.completed_indices)
            self.failed = sum(r.get("status") == "failed" for r in self.records.values())
            summary = self.summary()
            try:
                storage.update_run(self.run_id, summary=summary,
                                   unsaved_rows=list(self.unsaved_rows.values()))
            except Exception as exc:
                log.warning("update_run summary failed: %s", exc)

        _emit(self.run_id, {"type": "row", "index": record["index"], "name": record["name"],
                            "status": record["status"], "metrics": record.get("metric_values"),
                            "latency_s": record.get("latency_s"), "means": summary["means"]})
        return persisted


def start_run(run_id: str, rows: list[dict], *, bearer: str | None,
              agent_base_url: str | None, done_records: list[dict] | None = None) -> None:
    """Run (or resume) in the background. ``done_records`` are prior rows kept
    as-is on resume — their indices are skipped and their scores seed the means."""
    threading.Thread(
        target=_run, args=(run_id, rows, bearer, agent_base_url, done_records or []),
        name=f"eval-run-{run_id}", daemon=True,
    ).start()


def retry_judges(run_id: str, records: list[dict]) -> None:
    """Repair saved answers in the background without contacting the agent."""
    threading.Thread(target=_run, args=(run_id, [], None, None, [], records),
                     name=f"judge-retry-{run_id}", daemon=True).start()


def _retry_row_judges(run_id: str, original: dict, judges: JudgePanel) -> dict:
    run_id_var.set(run_id)
    record = dict(original)
    started = time.perf_counter()
    record["applicable_metrics"] = applicable_metrics(
        record.get("tags", ""), record.get("reference_answer", ""), judges.score_version,
        getattr(judges, "selected_metrics", None))
    scores = {}
    if record["applicable_metrics"]:
        scores = judges.evaluate(
            question=record.get("evaluation_question", record["question"]), response=record["response"],
            context=record.get("context", ""), reference=record.get("reference_answer", ""),
            category=record.get("tags", ""), previous_scores=record.get("scores"))
    record["scores"] = {key: value for key, value in scores.items()
                        if key in record["applicable_metrics"]}
    record["metric_values"] = metric_values(record["scores"], judges.score_version)
    record["scoring_version"] = judges.score_version
    record["judge_retry_count"] = record.get("judge_retry_count", 0) + 1
    record["judge_retry_latency_s"] = time.perf_counter() - started
    if (not record["applicable_metrics"]
            or any(value is not None for value in record["metric_values"].values())):
        record["status"] = "scored"
        record.pop("error", None)
    else:
        record["status"] = "failed"
        record["error"] = "all judges failed; saved agent answer retained for another retry"
    return record


def _run(run_id: str, rows: list[dict], bearer: str | None,
         agent_base_url: str | None, done_records: list[dict],
         judge_records: list[dict] | None = None) -> None:
    run_id_var.set(run_id)
    progress = _RunProgress(run_id, len(rows))
    progress_initialized = False
    trace = None
    final_status, error = "completed", None
    stopped_at_index = None
    conversation_complete = None
    try:
        meta = storage.load_run(run_id)
        mode = execution_mode(meta)
        policy = scoring_policy(meta)
        selected_metrics = policy.get("selected_metrics")
        if judge_records is None:
            total = len(rows)
            agent = LangGraphAgentClient(base_url=agent_base_url)
            if mode == "conversation":
                if done_records:
                    raise ValueError("Start a new shared-conversation run from the first row")
                agent.conversation_mode = True
            if bearer:
                agent.headers["Authorization"] = bearer
        else:
            total = meta.get("case_count") or (meta.get("summary") or {}).get("total") or len(judge_records)
            done_records = [row for row in judge_records
                            if not retryable_judge_row(row, policy["version"], selected_metrics)]
        progress = _RunProgress.from_records(run_id, total, done_records, policy)
        progress.unsaved_rows = {row["index"]: row for row in meta.get("unsaved_rows") or []}
        model_config = judge_model_config()
        if meta.get("judge_model"):
            model_config["azure_deployment"] = meta["judge_model"]
        judges = JudgePanel({**model_config, "score_version": policy["version"],
                             "selected_metrics": selected_metrics})
        judges.score_version = policy["version"]
        judges.selected_metrics = selected_metrics
        trace = RunTrace.start(run_id, meta) if tracing_enabled() else None
        workers = concurrency() if mode == "parallel" or judge_records is not None else 1
        progress_initialized = True
        log.info("run starting: %s rows (%s kept from a previous attempt), agent=%s, "
                 "concurrency=%s, tracing=%s",
                 len(rows), len(done_records), agent_base_url or "default", workers,
                 trace is not None)
        if judge_records is None and mode != "parallel":
            for i, row in enumerate(rows):
                if i in progress.completed_indices:
                    continue
                record = _process_row(run_id, i, row, agent, judges, trace)
                persisted = progress.finish(record)
                if mode == "conversation" and (not record.get("agent_completed") or not persisted):
                    stopped_at_index = i
                    for next_index, next_row in enumerate(rows[i + 1:], start=i + 1):
                        progress.finish({
                            **next_row, "index": next_index, "status": "not_run",
                            "attempted": False, "attempts": 0,
                            "error": f"Not attempted because the conversation stopped at case {i + 1}.",
                        })
                    raise RuntimeError(
                        f"Conversation stopped at case {i + 1}; later questions were not sent. "
                        "Start a new evaluation from the first row. " + (record.get("error") or ""))
            if mode == "conversation":
                conversation_complete = True
        else:
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix=f"eval-{run_id}") as pool:
                if judge_records is None:
                    futures = [pool.submit(_process_row, run_id, i, row, agent, judges, trace)
                               for i, row in enumerate(rows) if i not in progress.completed_indices]
                else:
                    futures = [pool.submit(_retry_row_judges, run_id, row, judges)
                               for row in judge_records if retryable_judge_row(
                                   row, policy["version"], selected_metrics)]
                for fut in as_completed(futures):
                    progress.finish(fut.result())
            if mode == "conversation" and not meta.get("conversation_complete"):
                raise RuntimeError(meta.get("error") or "Conversation is incomplete; start a new run")
    except Exception as exc:  # unexpected engine/storage failure must not wedge the run
        final_status, error = "failed", str(exc)
        log.exception("run failed")
    finally:
        summary = progress.summary()
        if progress.unsaved_rows:
            final_status = "failed"
            error = error or f"Could not save {len(progress.unsaved_rows)} case result(s)."
        if final_status == "completed" and progress.completed < progress.total:
            final_status = "failed"
            error = (f"Evaluation incomplete: {progress.completed} of {progress.total} cases "
                     "have completed results. Missing or unattempted cases remain.")
        final_fields = {"status": final_status, "error": error}
        if progress_initialized:
            final_fields.update(summary=summary, unsaved_rows=list(progress.unsaved_rows.values()))
        # An initialization failure has produced no new results. Keep the
        # persisted checkpoint: bootstrap progress can be empty, and judge
        # retries initially exclude the saved rows they plan to repair.
        if stopped_at_index is not None:
            final_fields["stopped_at_index"] = stopped_at_index
        if conversation_complete is not None:
            final_fields["conversation_complete"] = conversation_complete
        try:
            # Status and the exact final counts must be written together.
            # A separate terminal update can merge an older LangSmith read
            # and restore a stale summary such as completed=7 out of 8.
            storage.update_run(run_id, **final_fields)
        except Exception as exc:
            final_status = "failed"
            error = f"Could not finalize run results: {exc}"
            log.exception("could not persist final run status and summary")
            try:
                storage.update_run(run_id, **{**final_fields, "status": final_status, "error": error})
            except Exception:
                pass  # the terminal event still fires when storage is unavailable
        log.info("run finished: status=%s completed=%s/%s failed=%s means=%s",
                 final_status, progress.completed, progress.total, progress.failed,
                 summary["means"])
        if trace is not None:
            try:
                trace.finish(final_status, summary, error)
            except Exception as exc:
                log.warning("could not finish run trace: %s", exc)
        # Exactly one terminal event so SSE clients always terminate; then drop
        # the queue so per-run queues don't accumulate for the life of the
        # process (late consumers detect the terminal run status instead).
        _emit(run_id, {"type": "run", "status": final_status})
        with _lock:
            _events.pop(run_id, None)
