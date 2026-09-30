"""Reconcile result snapshots without inventing execution outcomes."""

from __future__ import annotations

from collections.abc import Iterable

_IDENTITY_FIELDS = ("name", "question", "reference_answer", "tags", "expected_agent")


def record_index(value) -> int | None:
    """Accept an ordinal, including older string ordinals, but never booleans."""
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def reconcile_rows(meta: dict, records: Iterable[dict],
                   dataset_rows: list[dict] | None = None) -> list[dict]:
    """One row per case, preserving failures and exposing absent results.

    A missing persisted result is an unknown outcome, not proof the agent
    failed or never ran. Only an explicit stop boundary proves a later case
    was not attempted. ``unsaved_rows`` are compact, authoritative failure
    checkpoints written with the final run summary when row storage failed.
    """
    indexed = {}
    for record in records:
        index = record_index(record.get("index"))
        if index is not None:
            indexed[index] = {**record, "index": index}
    for record in meta.get("unsaved_rows") or []:
        index = record_index(record.get("index"))
        if index is not None:
            # Keep source identity after repeated UI/export reconciliation,
            # but never attach an older saved answer/scores to a newer failed
            # write whose actual result is unavailable.
            previous = indexed.get(index, {})
            source = {key: previous[key] for key in _IDENTITY_FIELDS if key in previous}
            indexed[index] = {**source, **record, "index": index}

    count = max(
        len(dataset_rows) if dataset_rows is not None else 0,
        record_index(meta.get("case_count")) or 0,
        record_index((meta.get("summary") or {}).get("total")) or 0,
    )
    terminal = meta.get("status") in ("completed", "failed")
    stopped_at = record_index(meta.get("stopped_at_index"))
    for index in range(count):
        source = dataset_rows[index] if dataset_rows is not None and index < len(dataset_rows) else {}
        if index in indexed:
            previous = indexed[index]
            if not (previous.get("synthetic") and previous.get("status") in (
                    "pending", "missing", "not_run")):
                indexed[index] = {**source, **previous}
                continue
            # Metadata and rows are fetched separately. Reclassify generated
            # placeholders using the newest run state instead of leaving a
            # completed run's absent result permanently labeled PENDING.
            identity = {key: previous[key] for key in _IDENTITY_FIELDS if key in previous}
            source = {**identity, **source}
        status = "pending"
        error = None
        if terminal:
            if stopped_at is not None and index > stopped_at:
                status = "not_run"
                error = f"Not attempted because the conversation stopped at case {stopped_at + 1}."
            else:
                status = "missing"
                error = ("No saved result is available for this case; its execution outcome is unknown. "
                         "The result may still be becoming available in storage.")
        indexed[index] = {
            **source, "index": index, "name": source.get("name") or f"case-{index + 1:05d}",
            "status": status, "attempts": 0 if status == "not_run" else None,
            "attempted": False if status in ("pending", "not_run") else None,
            "synthetic": True,
        }
        if error:
            indexed[index]["error"] = error
    return [indexed[index] for index in sorted(indexed)]
