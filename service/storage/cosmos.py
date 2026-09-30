"""Cosmos DB storage backend — selected via ``EVAL_STORAGE=cosmos``
(``service/storage/__init__.py`` re-exports whichever backend is active).

Mapping onto Cosmos containers (database ``AZURE_COSMOS_DATABASE``, default
``eval-framework``; all three containers must already exist — data-plane RBAC
cannot create them):

    datasets (pk /dataset_id)
        ledger doc  id="ledger"  — name, application_key, uploader,
                                   latest_version, versions{n: vmeta}
        version doc id="v{N}"    — vmeta + the full row list (kept forever, so
                                   historical loads are point reads, unlike the
                                   LangSmith backend's delete-and-retag dance)
    runs (pk /run_id)
        one doc per run, id=run_id, run meta nested under "meta" so run fields
        can never collide with Cosmos system properties.
    rows (pk /run_id)
        one doc per case, id="{index:05d}", the FULL row record verbatim under
        "record". save_row upserts, so "latest save wins" needs no saved_at
        bookkeeping. Judge feedback lives inside the record; nothing is posted
        to LangSmith (tracing.py stays a separate, storage-independent path).

Auth: AAD only via the app's managed identity (AZURE_CLIENT_ID), falling back
to DefaultAzureCredential for local use. If your account disables public network
access, run the service from inside its VNet.
"""
from __future__ import annotations

import logging
import os
import uuid

from azure.cosmos import CosmosClient, exceptions

from service.azure_credentials import build_credential
from service.storage._common import now as _now
from service.storage._common import slug as _slug

log = logging.getLogger(__name__)

_LEDGER_ID = "ledger"

_client_obj: CosmosClient | None = None
_containers: dict[str, object] = {}


def _container(name: str):
    """Lazy per-container client; the CosmosClient is built once on first use."""
    global _client_obj
    if name not in _containers:
        if _client_obj is None:
            endpoint = os.getenv("AZURE_COSMOS_ENDPOINT")
            if not endpoint:
                raise RuntimeError(
                    "EVAL_STORAGE=cosmos requires AZURE_COSMOS_ENDPOINT")
            credential = build_credential(
                os.getenv("AZURE_CLIENT_ID") or None, managed_identity_only=False)
            _client_obj = CosmosClient(endpoint, credential=credential)
        database = _client_obj.get_database_client(
            os.getenv("AZURE_COSMOS_DATABASE", "eval-framework"))
        _containers[name] = database.get_container_client(name)
    return _containers[name]


def _read_item(container_name: str, item_id: str, partition_key: str) -> dict | None:
    try:
        return _container(container_name).read_item(item_id, partition_key=partition_key)
    except exceptions.CosmosResourceNotFoundError:
        return None


# --- datasets ------------------------------------------------------------------

def save_dataset(name: str, rows: list[dict], uploader: str, application_key: str = "va") -> dict:
    """Persist rows as a new version of `name` (new dataset on first upload)."""
    ds_id = _slug(name)
    ledger = _read_item("datasets", _LEDGER_ID, ds_id) or {}
    existing_key = ledger.get("application_key")
    if existing_key and existing_key != application_key:
        raise ValueError(
            f"dataset '{name}' already belongs to application "
            f"'{existing_key}'; upload under that application or rename")
    version = int(ledger.get("latest_version", 0)) + 1

    vmeta = {"dataset_id": ds_id, "name": name, "version": version,
             "uploader": uploader, "uploaded_at": _now(), "case_count": len(rows),
             "application_key": application_key}
    _container("datasets").upsert_item({
        "id": f"v{version}", "dataset_id": ds_id, "meta": vmeta,
        "rows": [dict(row) for row in rows],
    })
    _container("datasets").upsert_item({
        "id": _LEDGER_ID, "dataset_id": ds_id,
        "name": name, "application_key": application_key, "uploader": uploader,
        "latest_version": version,
        "versions": {**(ledger.get("versions") or {}), str(version): vmeta},
    })
    log.info("dataset saved: %s v%s (%s cases, application=%s, uploader=%s)",
             ds_id, version, len(rows), application_key, uploader)
    return vmeta


def list_datasets() -> list[dict]:
    ledgers = _container("datasets").query_items(
        f"SELECT * FROM c WHERE c.id = '{_LEDGER_ID}'",
        enable_cross_partition_query=True)
    out = [led["versions"][str(led["latest_version"])] for led in ledgers]
    return sorted(out, key=lambda m: m["dataset_id"])


def load_dataset(dataset_id: str, version: int | None = None) -> tuple[dict, list[dict]]:
    if not version:  # the ledger is only needed to resolve "latest"
        ledger = _read_item("datasets", _LEDGER_ID, dataset_id)
        if ledger is None:
            raise FileNotFoundError(dataset_id)
        version = int(ledger.get("latest_version", 0))
    doc = _read_item("datasets", f"v{version}", dataset_id)
    if doc is None:
        raise FileNotFoundError(f"{dataset_id} v{version}")
    return dict(doc["meta"]), [dict(row) for row in doc["rows"]]


# --- runs ----------------------------------------------------------------------

def _read_run_doc(run_id: str) -> dict:
    doc = _read_item("runs", run_id, run_id)
    if doc is None:
        raise FileNotFoundError(run_id)
    return doc


def create_run(meta: dict) -> str:
    run_id = uuid.uuid4().hex[:12]
    _container("runs").upsert_item({
        "id": run_id, "run_id": run_id,
        "meta": {**meta, "run_id": run_id, "started_at": _now(),
                 "status": "running"},
    })
    log.info("run created: %s (dataset=%s v%s, application=%s)",
             run_id, meta.get("dataset_id"), meta.get("dataset_version"),
             meta.get("application_key"))
    return run_id


def update_run(run_id: str, **fields) -> dict:
    doc = _read_run_doc(run_id)
    meta = dict(doc.get("meta") or {})
    meta.update(fields)
    # Stamp terminal statuses and clear the timestamp when a run resumes.
    if fields.get("status") in ("completed", "failed"):
        meta["finished_at"] = _now()
    elif fields.get("status") == "running":
        meta.pop("finished_at", None)
    _container("runs").upsert_item({**doc, "meta": meta})
    if fields.get("status") in ("completed", "failed"):
        log.info("run %s -> %s", run_id, fields["status"])
    else:
        log.debug("run %s updated: %s", run_id, sorted(fields))
    return dict(meta)


def load_run(run_id: str) -> dict:
    return dict(_read_run_doc(run_id).get("meta") or {})


def list_runs() -> list[dict]:
    metas = _container("runs").query_items(
        "SELECT VALUE c.meta FROM c", enable_cross_partition_query=True)
    return sorted((dict(meta) for meta in metas),
                  key=lambda meta: meta.get("started_at", ""),
                  reverse=True)


# --- rows ----------------------------------------------------------------------

def save_row(run_id: str, index: int, record: dict) -> None:
    _container("rows").upsert_item({
        "id": f"{index:05d}", "run_id": run_id, "index": index,
        "record": dict(record),
    })
    log.debug("row saved: run=%s index=%s status=%s",
              run_id, index, record.get("status"))


def load_rows(run_id: str) -> list[dict]:
    records = _container("rows").query_items(
        "SELECT VALUE c.record FROM c", partition_key=run_id)
    return sorted((dict(record) for record in records),
                  key=lambda r: r.get("index") or 0)
