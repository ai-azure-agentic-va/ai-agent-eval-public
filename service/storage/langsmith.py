"""LangSmith storage backend — the ONLY backend in this repo
(``service/storage/__init__.py`` re-exports its public contract).

Mapping onto LangSmith objects:

    dataset          -> Dataset (dataset_name = our slug id; display name, the
                        application_key/uploader scope and a per-version ledger
                        live in the dataset's metadata)
    dataset version  -> LangSmith dataset versioning: each save_dataset DELETES
                        the current examples and recreates the new set (that is
                        a new LangSmith version), then tags that state "v{N}".
                        Historical loads use list_examples(as_of="v{N}").
    case             -> Example (inputs={"question"}, outputs={"reference_answer"},
                        metadata carries ordinal + every other row field)
    run              -> Project/experiment (project_name = our 12-hex run_id;
                        ALL run meta lives in project metadata, plus the marker
                        "eval_run": True so list_runs can filter our projects)
    row              -> one root run PER SAVE (fresh uuid4 — LangSmith allows
                        only one update per run, so upserting is impossible;
                        load_rows keeps the newest saved_at per index). The
                        FULL row record is stored losslessly under
                        outputs["_eval_record"].
    judgment         -> Feedback per populated metric under the Title-case
                        keys in service.metrics.FEEDBACK_KEYS ("Accuracy",
                        "Relevance", ...) with the judge rationale as comment;
                        routing_match posts as "Routing Match" score 1/0.
    token usage      -> extra.metadata.usage_metadata {input/output/total} on
                        the row run — what LangSmith's token columns read.

Hard-won integration constraints:
  * NEVER pass feedback_config to create_feedback — a feedback key's config is
    locked workspace-wide at first registration. Bare lowercase metric names
    (correctness, relevance, ...) may already be registered 0-1 by other tools,
    so 1-5 scores live under their own Title-case keys.
  * Run ingestion is asynchronous; reads-after-writes within one process may
    lag. load_rows therefore prefers the embedded record over re-derivation.

Auth: LANGSMITH_EVALS_KEY (fallback LANGSMITH_API_KEY), read lazily on first
use. No key is ever hardcoded.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from langsmith import Client
from langsmith import utils as ls_utils

from service.metrics import (
    FEEDBACK_KEYS,
    LEGACY_FEEDBACK_KEYS,
    LEGACY_ROUTING_FEEDBACK_KEY,
    METRIC_KEYS,
    NORMALIZED_FEEDBACK_KEYS,
    ROUTING_FEEDBACK_KEY,
    is_valid_score,
)
from service.results import record_index
from service.storage._common import now as _now
from service.storage._common import slug as _slug

log = logging.getLogger(__name__)


_NOT_FOUND = (ls_utils.LangSmithNotFoundError, FileNotFoundError)
_EVAL_RUN_MARKER = "eval_run"

_client_obj: Client | None = None

# run_id -> {ordinal: example_id}; resolved once per run so save_row does not
# re-list the dataset's examples for every row.
_example_maps: dict[str, dict[int, uuid.UUID]] = {}


def _client() -> Client:
    """Lazy module-level client, built once on first use."""
    global _client_obj
    if _client_obj is None:
        key = os.getenv("LANGSMITH_EVALS_KEY") or os.getenv("LANGSMITH_API_KEY")
        _client_obj = Client(api_key=key)
    return _client_obj


def _rationale(scores: dict, metric: str) -> str | None:
    """Judge reasoning for a metric (every judge returns ``reasoning``)."""
    obj = scores.get(metric) or {}
    if isinstance(obj, dict) and obj.get("reasoning"):
        return str(obj["reasoning"])[:4000]
    return None


# --- datasets ------------------------------------------------------------------

def _write_dataset_metadata(client: Client, dataset_id, metadata: dict) -> None:
    """Persist the version ledger into the dataset's metadata.

    SDK 0.11 has no update_dataset, so PATCH the REST endpoint directly.
    The DatasetUpdate schema takes TOP-LEVEL
    "metadata" (an "extra"-shaped body returns 200 but is silently ignored).
    The offline fake implements request_with_retries with this exact shape."""
    client.request_with_retries(
        "PATCH", f"/datasets/{dataset_id}",
        headers={**getattr(client, "_headers", {}), "Content-Type": "application/json"},
        data=json.dumps({"metadata": metadata}, default=str),
    )


def _example_to_row(ex) -> dict:
    """Invert the save_dataset field mapping back into a canonical row dict.

    'ordinal' is ours; 'dataset_split' is injected server-side on read — both
    are storage plumbing, not row fields."""
    row = {k: v for k, v in (ex.metadata or {}).items()
           if k not in ("ordinal", "dataset_split")}
    if ex.inputs and "question" in ex.inputs:
        row["question"] = ex.inputs["question"]
    if ex.outputs and "reference_answer" in ex.outputs:
        row["reference_answer"] = ex.outputs["reference_answer"]
    return row


def save_dataset(name: str, rows: list[dict], uploader: str, application_key: str = "va") -> dict:
    """Persist rows as a new tagged version of `name` (new dataset on first upload)."""
    ds_id = _slug(name)
    client = _client()
    try:
        ds = client.read_dataset(dataset_name=ds_id)
        ledger = dict(ds.metadata or {})
    except _NOT_FOUND:
        ds, ledger = None, {}
    # A re-upload under the same slug is a new version. Changing the application
    # would silently re-scope the dataset's history, so reject it (main.py -> 409).
    existing_key = ledger.get("application_key")
    if existing_key and existing_key != application_key:
        raise ValueError(
            f"dataset '{name}' already belongs to application "
            f"'{existing_key}'; upload under that application or rename")
    version = int(ledger.get("latest_version", 0)) + 1

    if ds is None:
        ds = client.create_dataset(dataset_name=ds_id, description=name)
    else:
        # New version = delete the live examples and recreate the new set —
        # LangSmith records that as a new dataset version we then tag.
        stale = [ex.id for ex in client.list_examples(dataset_id=ds.id)]
        if stale:
            client.delete_examples(stale)
    for i, row in enumerate(rows):
        client.create_example(
            inputs={"question": row["question"]} if "question" in row else {},
            outputs=({"reference_answer": row["reference_answer"]}
                     if "reference_answer" in row else None),
            dataset_id=ds.id,
            metadata={"ordinal": i,
                      **{k: v for k, v in row.items()
                         if k not in ("question", "reference_answer")}},
        )
    client.update_dataset_tag(dataset_id=ds.id, as_of=datetime.now(timezone.utc),
                              tag=f"v{version}")

    vmeta = {"dataset_id": ds_id, "name": name, "version": version,
             "uploader": uploader, "uploaded_at": _now(), "case_count": len(rows),
             "application_key": application_key}
    ledger.update({
        "name": name, "application_key": application_key, "uploader": uploader,
        "latest_version": version,
        "versions": {**(ledger.get("versions") or {}), str(version): vmeta},
    })
    _write_dataset_metadata(client, ds.id, ledger)
    log.info("dataset saved: %s v%s (%s cases, application=%s, uploader=%s)",
             ds_id, version, len(rows), application_key, uploader)
    return vmeta


def list_datasets() -> list[dict]:
    out = []
    for ds in _client().list_datasets():
        md = ds.metadata or {}
        if "latest_version" in md and "versions" in md:  # ours, not a foreign dataset
            out.append(md["versions"][str(md["latest_version"])])
    return sorted(out, key=lambda m: m["dataset_id"])


def load_dataset(dataset_id: str, version: int | None = None) -> tuple[dict, list[dict]]:
    client = _client()
    try:
        ds = client.read_dataset(dataset_name=dataset_id)
    except _NOT_FOUND:
        raise FileNotFoundError(dataset_id)
    ledger = ds.metadata or {}
    latest = int(ledger.get("latest_version", 0))
    version = version or latest
    vmeta = (ledger.get("versions") or {}).get(str(version))
    if not vmeta:
        raise FileNotFoundError(f"{dataset_id} v{version}")
    # Latest = the live examples; historical = the state tagged v{N} at upload.
    kwargs = {"dataset_id": ds.id}
    if version != latest:
        kwargs["as_of"] = f"v{version}"
    examples = sorted(client.list_examples(**kwargs),
                      key=lambda ex: (ex.metadata or {}).get("ordinal", 0))
    return dict(vmeta), [_example_to_row(ex) for ex in examples]


# --- runs ----------------------------------------------------------------------

def _read_project(client: Client, run_id: str):
    try:
        return client.read_project(project_name=run_id)
    except _NOT_FOUND:
        raise FileNotFoundError(run_id)


def _project_meta(proj) -> dict:
    """Run meta dict (file-backend shape) from a project's metadata."""
    meta = dict(((proj.extra or {}).get("metadata")) or {})
    meta.pop(_EVAL_RUN_MARKER, None)
    return meta


def create_run(meta: dict) -> str:
    run_id = uuid.uuid4().hex[:12]
    client = _client()
    ref_dataset_id = None
    try:  # link the experiment to its dataset when it exists in LangSmith
        ref_dataset_id = client.read_dataset(dataset_name=meta.get("dataset_id") or "").id
    except Exception:  # noqa: BLE001 — an unlinked experiment is still a valid run
        pass
    client.create_project(
        project_name=run_id,
        reference_dataset_id=ref_dataset_id,
        metadata={**meta, "run_id": run_id, "started_at": _now(),
                  "status": "running", _EVAL_RUN_MARKER: True},
    )
    log.info("run created: %s (dataset=%s v%s, application=%s, linked_dataset=%s)",
             run_id, meta.get("dataset_id"), meta.get("dataset_version"),
             meta.get("application_key"), bool(ref_dataset_id))
    return run_id


def update_run(run_id: str, **fields) -> dict:
    client = _client()
    proj = _read_project(client, run_id)
    stored = dict(((proj.extra or {}).get("metadata")) or {})
    stored.update(fields)
    # Stamp terminal statuses and clear the timestamp when a run resumes.
    if fields.get("status") in ("completed", "failed"):
        stored["finished_at"] = _now()
    elif fields.get("status") == "running":
        stored.pop("finished_at", None)
    client.update_project(proj.id, metadata=stored)
    if fields.get("status") in ("completed", "failed"):
        _example_maps.pop(run_id, None)
        log.info("run %s -> %s", run_id, fields["status"])
    else:
        log.debug("run %s updated: %s", run_id, sorted(fields))
    meta = dict(stored)
    meta.pop(_EVAL_RUN_MARKER, None)
    return meta


def _reference_example_id(client: Client, run_id: str, index: int):
    """Example id for a row's ordinal, cached per run (one listing per run).

    Looked up against the run's own dataset VERSION (examples are recreated on
    newer uploads, so the live set may not be the one this run executed)."""
    cached = _example_maps.get(run_id)
    if cached is None:
        cached = {}
        try:
            proj = _read_project(client, run_id)
            if proj.reference_dataset_id:
                meta = _project_meta(proj)
                kwargs = {"dataset_id": proj.reference_dataset_id}
                latest = None
                try:
                    ds = client.read_dataset(dataset_id=proj.reference_dataset_id)
                    latest = (ds.metadata or {}).get("latest_version")
                except _NOT_FOUND:
                    pass
                version = meta.get("dataset_version")
                if version and latest and int(version) != int(latest):
                    kwargs["as_of"] = f"v{version}"
                for ex in client.list_examples(**kwargs):
                    ordinal = (ex.metadata or {}).get("ordinal")
                    if ordinal is not None:
                        cached[int(ordinal)] = ex.id
        except Exception:  # noqa: BLE001 — a row without its example link still saves
            cached = {}
        _example_maps[run_id] = cached
    return cached.get(index)


def save_row(run_id: str, index: int, record: dict) -> None:
    client = _client()
    # LangSmith accepts only one update for a run, so every save needs a fresh
    # ID. load_rows resolves repeated saves by their saved_at timestamp.
    rid = uuid.uuid4()
    started = record.get("started_at")
    start_time = (datetime.fromtimestamp(started, tz=timezone.utc)
                  if isinstance(started, (int, float)) else datetime.now(timezone.utc))
    latency = record.get("latency_s")
    end_time = (start_time + timedelta(seconds=latency)
                if isinstance(latency, (int, float)) else datetime.now(timezone.utc))
    # The full record rides in OUTPUTS, not metadata: LangSmith silently strips
    # oversized metadata values (a big retrieved context would vaporize the
    # record), while outputs carry large trace payloads
    # routinely. "_eval_record" is the system of record for load_rows.
    outputs = {"answer": record.get("response"),
               "retrieved_context": record.get("context"),
               "_eval_record": record}
    extra = {"metadata": {"index": index,
                          "saved_at": time.time(),  # newest-wins key for load_rows
                          "latency_s": record.get("latency_s"),
                          "agent_latency_s": record.get("agent_latency_s"),
                          "attempts": record.get("attempts", 1),
                          "usage_tokens": record.get("usage_tokens"),
                          "routing_observed": record.get("routing_observed"),
                          "routing_match": record.get("routing_match")}}
    extra["metadata"]["scoring_version"] = record.get("scoring_version", 1)
    usage = _usage_metadata(record)
    if usage:
        extra["metadata"]["usage_metadata"] = usage  # LangSmith's token columns
    client.create_run(
        id=rid, name=record.get("name") or f"case-{index:05d}", run_type="chain",
        inputs={"question": record.get("question")},
        outputs=outputs, extra=extra, error=record.get("error"),
        project_name=run_id,
        reference_example_id=_reference_example_id(client, run_id, index),
        start_time=start_time, end_time=end_time,
    )
    mv = record.get("metric_values") or {}
    scores = record.get("scores") or {}
    version = record.get("scoring_version", 1)
    feedback_keys = NORMALIZED_FEEDBACK_KEYS if version == 2 else FEEDBACK_KEYS
    for metric in METRIC_KEYS:
        if is_valid_score(mv.get(metric), version):
            # NEVER pass feedback_config, NEVER use the bare lowercase metric
            # name — see the module docstring (workspace feedback keys are locked).
            try:
                client.create_feedback(rid, feedback_keys[metric], score=mv[metric],
                                       comment=_rationale(scores, metric))
            except Exception as exc:
                # The lossless record above is already stored. A failure in
                # its redundant experiment-column feedback must not replay an
                # agent conversation or turn the saved answer into an error.
                log.warning("feedback save failed: run=%s index=%s metric=%s: %s",
                            run_id, index, metric, exc)
    if isinstance(record.get("routing_match"), bool):
        try:
            client.create_feedback(rid, ROUTING_FEEDBACK_KEY,
                                   score=1 if record["routing_match"] else 0)
        except Exception as exc:
            log.warning("routing feedback save failed: run=%s index=%s: %s", run_id, index, exc)
    log.debug("row saved: run=%s index=%s status=%s ls_run=%s feedback=%s",
              run_id, index, record.get("status"), rid,
              sum(1 for m in METRIC_KEYS if mv.get(m) is not None))


def _usage_metadata(record: dict) -> dict | None:
    """LangSmith-shaped token usage for a row: input/output/total when the
    agent reported them, total alone for records that only carry usage_tokens."""
    usage = record.get("usage") or {}
    out = {k: int(usage[k]) for k in ("input_tokens", "output_tokens", "total_tokens")
           if isinstance(usage.get(k), (int, float))}
    if "total_tokens" not in out and isinstance(record.get("usage_tokens"), (int, float)):
        out["total_tokens"] = int(record["usage_tokens"])
    return out if out.get("total_tokens") else None


def load_run(run_id: str) -> dict:
    return _project_meta(_read_project(_client(), run_id))


def _reconstruct_row(client: Client, run) -> dict:
    """Fallback for rows saved without the embedded record (foreign writers):
    rebuild what the run + its feedback can still tell us."""
    md = (run.extra or {}).get("metadata") or {}
    rec = {"index": md.get("index"), "name": run.name,
           "question": (run.inputs or {}).get("question"),
           "response": (run.outputs or {}).get("answer"),
           "context": (run.outputs or {}).get("retrieved_context"),
           "status": "failed" if run.error else "scored", "error": run.error}
    metric_values = {}
    version = md.get("scoring_version", 1)
    if version == 2:
        rec["scoring_version"] = version
    feedback_keys = NORMALIZED_FEEDBACK_KEYS if version == 2 else FEEDBACK_KEYS
    key_to_metric = {**(LEGACY_FEEDBACK_KEYS if version == 1 else {}),
                     **{v: k for k, v in feedback_keys.items()}}
    for fb in client.list_feedback(run_ids=[run.id]):
        key = getattr(fb, "key", "") or ""
        if key in (ROUTING_FEEDBACK_KEY, LEGACY_ROUTING_FEEDBACK_KEY):
            rec["routing_match"] = bool(fb.score)
        elif key in key_to_metric:
            metric_values[key_to_metric[key]] = fb.score
    if metric_values:
        rec["metric_values"] = metric_values
    return {k: v for k, v in rec.items() if v is not None}


def load_rows(run_id: str) -> list[dict]:
    client = _client()
    try:
        _read_project(client, run_id)
    except FileNotFoundError:
        return []  # contract: an unknown run yields [] (main.py 404s on load_run)
    # client.list_runs is deprecated upstream, but it is the only project-run
    # listing SDK 0.11 offers — use it deliberately until an SDK bump.
    # A resumed row can have multiple saves; keep the latest saved_at.
    best: dict = {}
    for run in client.list_runs(project_name=run_id, is_root=True):
        md = (run.extra or {}).get("metadata") or {}
        record = (run.outputs or {}).get("_eval_record") or md.get("record") or {}
        idx = record_index(record.get("index"))
        if idx is None:
            idx = record_index(md.get("index"))
        if idx is None:
            continue  # an unrelated root trace is not a dataset case
        stamp = md.get("saved_at") or 0
        if idx in best and best[idx][0] >= stamp:
            continue
        best[idx] = (stamp, run)

    # Resolve only winning checkpoints: superseded rows need no feedback reads.
    rows = []
    for index, (_, run) in best.items():
        md = (run.extra or {}).get("metadata") or {}
        record = ((run.outputs or {}).get("_eval_record")
                  or md.get("record"))
        row = dict(record) if record is not None else _reconstruct_row(client, run)
        rows.append({**row, "index": index})
    return sorted(rows, key=lambda r: (r.get("index") is None, r.get("index") or 0))


def list_runs() -> list[dict]:
    projects = _client().list_projects(metadata={_EVAL_RUN_MARKER: True})
    return sorted(
        (_project_meta(project) for project in projects),
        key=lambda meta: meta.get("started_at", ""),
        reverse=True,
    )
