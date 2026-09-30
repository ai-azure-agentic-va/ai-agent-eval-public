"""Run endpoints: start/resume, listing, per-row records, SSE progress and the
CSV and PDF exports.

`runner` and `storage` are always called attribute-style (never `from
service.runner import start_run`) so tests can monkeypatch the modules.
"""

from __future__ import annotations

import json
import logging
import queue
import threading

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from service import config, registry, runner, storage
from service.config import caller_identity, judge_model_config
from service.execution import ExecutionMode
from service.metrics import (
    SCORING_VERSION,
    new_scoring_policy,
    normalize_selected_metrics,
    retryable_judge_row,
    scoring_policy,
)
from service.pdf_report import run_pdf
from service.reports import run_csv
from service.results import reconcile_rows

log = logging.getLogger(__name__)
router = APIRouter()
_RETRY_LOCK = threading.Lock()


def _require_current_judge_prompts(meta: dict) -> None:
    if scoring_policy(meta)["version"] != SCORING_VERSION:
        raise HTTPException(status_code=409, detail=(
            "Historical 1–5 runs are read-only. Start a new evaluation "
            "to use the current 0–1 judge prompts."))


def _require_allowed_metrics(selected: list[str] | None, *, saved: bool = False) -> list[str]:
    """Resolve new-run defaults or reject selections the operator disabled."""
    try:
        allowed = config.allowed_metrics()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    selected = allowed if selected is None else selected
    disallowed = [key for key in selected if key not in allowed]
    if disallowed:
        detail = f"Metrics disabled by EVAL_ALLOWED_METRICS: {', '.join(disallowed)}."
        if saved:
            detail += " Start a new evaluation with allowed metrics or restore the configuration."
        raise HTTPException(status_code=409 if saved else 422, detail=detail)
    return selected


class RunRequest(BaseModel):
    dataset_id: str
    version: int | None = None
    application: str = "va"
    thresholds: dict[str, float] = Field(default_factory=dict)
    selected_metrics: list[str] | None = None
    execution_mode: ExecutionMode = "sequential"

    @field_validator("thresholds", mode="before")
    @classmethod
    def validate_thresholds(cls, value):
        new_scoring_policy(value)
        return value

    @field_validator("selected_metrics", mode="before")
    @classmethod
    def validate_selected_metrics(cls, value):
        return None if value is None else normalize_selected_metrics(value)


@router.post("/runs")
def start_run(body: RunRequest, authorization: str | None = Header(default=None)):
    selected_metrics = _require_allowed_metrics(body.selected_metrics)
    try:
        app_row = registry.resolve(body.application)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except registry.RegistryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    try:
        meta, rows = storage.load_dataset(body.dataset_id, body.version)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="dataset not found")
    run_id = storage.create_run({
        "dataset_id": meta["dataset_id"], "dataset_name": meta["name"],
        "dataset_version": meta["version"], "case_count": meta["case_count"],
        "application_key": app_row["key"], "agent_endpoint": app_row["deployment_url"],
        "judge_model": judge_model_config()["azure_deployment"],
        "scoring": new_scoring_policy(body.thresholds, selected_metrics),
        "execution_mode": body.execution_mode,
        "triggered_by": caller_identity(authorization),
        "summary": {"completed": 0, "total": len(rows), "failed": 0, "means": {}},
    })
    log.info("run %s requested by %s: dataset=%s v%s application=%s (%s rows)",
             run_id, caller_identity(authorization), meta["dataset_id"],
             meta["version"], app_row["key"], len(rows))
    runner.start_run(run_id, rows, bearer=authorization,
                     agent_base_url=app_row["deployment_url"])
    return storage.load_run(run_id)


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: str, authorization: str | None = Header(default=None)):
    """Re-run a run's unfinished rows (crash / token-expiry recovery).

    Rows with a persisted 'scored' record are kept verbatim; failed or missing
    rows re-execute using the CALLER'S bearer from this request — which is the
    point: the original token may be the reason they failed.
    """
    try:
        run = storage.load_run(run_id)
        _, rows = storage.load_dataset(run["dataset_id"], run["dataset_version"])
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="run or its dataset not found")
    if run.get("status") in ("running", "queued"):
        raise HTTPException(status_code=409, detail="run is already in progress")
    _require_current_judge_prompts(run)
    _require_allowed_metrics(scoring_policy(run)["selected_metrics"], saved=True)
    if run.get("execution_mode") == "conversation":
        raise HTTPException(status_code=409, detail=(
            "Shared-conversation runs cannot replay individual turns safely. "
            "Start a new evaluation from the first row, or retry judges on saved answers."))
    done = [r for r in reconcile_rows(run, storage.load_rows(run_id), rows)
            if r.get("status") == "scored"]
    storage.update_run(run_id, status="running")
    log.info("run %s resumed by %s: %s kept, %s to re-run", run_id,
             caller_identity(authorization), len(done), len(rows) - len(done))
    runner.start_run(run_id, rows, bearer=authorization, done_records=done,
                     agent_base_url=run.get("agent_endpoint"))
    return {**storage.load_run(run_id), "kept": len(done), "rerun": len(rows) - len(done)}


@router.get("/runs")
def get_runs(limit: int = 50, offset: int = 0):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    runs = storage.list_runs()
    return {"runs": runs[offset:offset + limit], "total": len(runs)}


@router.post("/runs/{run_id}/retry-judges")
def retry_run_judges(run_id: str):
    with _RETRY_LOCK:
        try:
            meta = storage.load_run(run_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="run not found")
        if meta.get("status") in ("running", "queued"):
            raise HTTPException(status_code=409, detail="run is already in progress")
        _require_current_judge_prompts(meta)
        policy = scoring_policy(meta)
        _require_allowed_metrics(policy["selected_metrics"], saved=True)
        records = _result_rows(run_id, meta)
        count = sum(retryable_judge_row(row, policy["version"], policy["selected_metrics"])
                    for row in records)
        if not count:
            raise HTTPException(status_code=409, detail="no failed judges with a saved answer to retry")
        storage.update_run(run_id, status="running")
        runner.retry_judges(run_id, records)
    return {"run_id": run_id, "rows_to_retry": count}


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    try:
        return storage.load_run(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="run not found")


@router.get("/runs/{run_id}/rows")
def get_run_rows(run_id: str):
    try:
        meta = storage.load_run(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="run not found")
    return _result_rows(run_id, meta)


def _result_rows(run_id: str, meta: dict) -> list[dict]:
    dataset_rows = None
    if meta.get("dataset_id"):
        try:
            _, dataset_rows = storage.load_dataset(meta["dataset_id"], meta.get("dataset_version"))
        except FileNotFoundError:
            pass  # still show absent indices when the original dataset was removed
        except Exception as exc:
            # Dataset lookup only enriches absent result identities. Saved
            # answers and diagnostics must remain available when that lookup
            # is temporarily inaccessible, including during judge recovery.
            log.warning("dataset unavailable for run %s result enrichment: %s", run_id, exc)
    return reconcile_rows(meta, storage.load_rows(run_id), dataset_rows)


@router.get("/runs/{run_id}/export.csv")
def export_csv(run_id: str):
    try:
        meta = storage.load_run(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="run not found")
    rows = _result_rows(run_id, meta)
    return StreamingResponse(iter([run_csv(run_id, meta, rows)]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename=run-{run_id}.csv"})


@router.get("/runs/{run_id}/export.pdf")
def export_pdf(run_id: str):
    try:
        meta = storage.load_run(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="run not found")
    rows = _result_rows(run_id, meta)
    return StreamingResponse(iter([run_pdf(run_id, meta, rows)]), media_type="application/pdf",
                             headers={"Content-Disposition": f"attachment; filename=run-{run_id}.pdf"})


@router.get("/runs/{run_id}/events")
def run_events(run_id: str):
    """SSE stream of per-row progress; ends when the run completes.

    A client connecting to an already-finished run (or reconnecting after
    another consumer drained the terminal event — the per-run queue is
    single-consumer) gets a synthetic terminal event instead of hanging on
    keepalives forever."""
    try:
        meta = storage.load_run(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="run not found")

    def gen():
        yield f"data: {json.dumps({'type': 'meta', **meta})}\n\n"
        if meta.get("status") in ("completed", "failed"):
            yield f"data: {json.dumps({'type': 'run', 'status': meta['status']})}\n\n"
            return
        q = runner.events_for(run_id)
        while True:
            try:
                event = q.get(timeout=30)
            except queue.Empty:
                # Re-check persisted status: the terminal event may have been
                # consumed by a previous connection of this same client.
                current = storage.load_run(run_id)
                if current.get("status") in ("completed", "failed"):
                    yield f"data: {json.dumps({'type': 'run', 'status': current['status']})}\n\n"
                    return
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") == "run":
                return
    return StreamingResponse(gen(), media_type="text/event-stream")
