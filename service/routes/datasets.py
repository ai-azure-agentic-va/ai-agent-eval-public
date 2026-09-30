"""Dataset endpoints: preview/validate uploads, save versions, read back."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile

from service import datasets, registry, storage
from service.config import caller_identity

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/datasets/preview")
async def preview_dataset(file: UploadFile = File(...)):
    rows, errors = datasets.parse_upload(file.filename, await file.read())
    return {"case_count": len(rows), "errors": errors, "rows": rows[:200]}


@router.post("/datasets")
async def upload_dataset(
    file: UploadFile = File(...),
    name: str = Form(...),
    application: str = Form("va"),
    authorization: str | None = Header(default=None),
):
    # Any registered key is a valid scope (inactive included — datasets may be
    # curated before their application is activated); unknown keys are typos.
    try:
        known = {a["key"] for a in registry.get_applications()}
    except registry.RegistryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if application not in known:
        raise HTTPException(status_code=404, detail=f"unknown application '{application}'")
    rows, errors = datasets.parse_upload(file.filename, await file.read())
    if errors:
        log.info("upload of %r rejected: %s validation error(s)", file.filename, len(errors))
        raise HTTPException(status_code=400, detail={"errors": errors})
    try:
        return storage.save_dataset(name, rows, uploader=caller_identity(authorization),
                                    application_key=application)
    except ValueError as exc:  # slug collision with another application's dataset
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/datasets")
def get_datasets(
    limit: int | None = Query(default=None, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    sort_by: Literal["name", "application_key", "version", "case_count", "uploader", "uploaded_at"] = "uploaded_at",
    sort_dir: Literal["asc", "desc"] = "desc",
):
    records = storage.list_datasets()
    if limit is None:
        # Preserve the full-list contract used by the Run page's selector.
        return records

    def sort_value(record: dict):
        value = record.get(sort_by)
        return (value or 0) if sort_by in {"version", "case_count"} else str(value or "")

    # Keep tied values in a deterministic order across page requests.
    ordered = sorted(records, key=lambda record: record["dataset_id"])
    ordered.sort(key=sort_value, reverse=sort_dir == "desc")
    return {"datasets": ordered[offset:offset + limit], "total": len(ordered)}


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str, version: int | None = None):
    try:
        meta, rows = storage.load_dataset(dataset_id, version)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="dataset not found")
    return {"meta": meta, "rows": rows}
