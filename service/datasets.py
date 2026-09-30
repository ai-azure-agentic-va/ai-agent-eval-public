"""Upload parsing and validation: .xlsx/.xls/.csv → canonical rows or errors.

Canonical schema per row: question, reference_answer, expected_agent, tags.
Legacy prompt-set headers (prompt / expected_subagent / category / name) are
accepted as aliases so existing golden sets upload unchanged. Validation
failures are reported per row/field BEFORE anything persists — a bad dataset
silently producing bad scores is the worst outcome.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterable

import pandas as pd

log = logging.getLogger(__name__)

_ALIASES = {
    "prompt": "question",
    "expected_subagent": "expected_agent",
    "category": "tags",
}
_REQUIRED = ("question", "reference_answer")
_OPTIONAL = ("expected_agent", "tags", "name")


def _canonical_columns(columns: Iterable[object]) -> tuple[list[str], list[str]]:
    normalized_columns: list[str] = []
    seen_columns: set[str] = set()
    duplicate_columns: list[str] = []
    for column in columns:
        normalized = str(column).strip().lower()
        canonical = _ALIASES.get(normalized, normalized)
        if canonical in seen_columns and canonical not in duplicate_columns:
            duplicate_columns.append(canonical)
        seen_columns.add(canonical)
        normalized_columns.append(canonical)
    return normalized_columns, duplicate_columns


def parse_upload(filename: str, payload: bytes) -> tuple[list[dict], list[dict]]:
    """Return ``(rows, errors)``; rows are canonical, errors are per-row dicts."""
    try:
        if filename.lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(payload))
        elif filename.lower().endswith(".csv"):
            df = pd.read_csv(io.BytesIO(payload), encoding="utf-8-sig")
        else:
            return [], [{
                "row": None,
                "field": "file",
                "error": f"Unsupported file type: {filename} (use .xlsx, .xls, or .csv)",
            }]
    except Exception as exc:
        return [], [{"row": None, "field": "file", "error": f"Could not parse file: {exc}"}]

    normalized_columns, duplicate_columns = _canonical_columns(df.columns)
    if duplicate_columns:
        joined = ", ".join(duplicate_columns)
        return [], [{
            "row": None,
            "field": ",".join(duplicate_columns),
            "error": f"Duplicate column(s) after alias normalization: {joined}",
        }]

    df.columns = normalized_columns

    errors: list[dict] = []
    missing = [c for c in _REQUIRED if c not in df.columns]
    if missing:
        errors.append({
            "row": None,
            "field": ",".join(missing),
            "error": (
                f"Missing required column(s): {', '.join(missing)}. "
                f"Found: {', '.join(df.columns)}"
            ),
        })
        return [], errors

    rows: list[dict] = []
    columns = _REQUIRED + _OPTIONAL
    records = df.reindex(columns=columns).itertuples(index=False, name=None)
    for i, values in enumerate(records, start=2):  # row 1 = header
        row = {
            key: "" if pd.isna(value) else str(value).strip()
            for key, value in zip(columns, values)
        }
        if not row["question"] and not row["reference_answer"]:
            errors.append({"row": i, "field": "question", "error": "Empty row"})
            continue
        for field in _REQUIRED:
            if not row[field]:
                errors.append({
                    "row": i,
                    "field": field,
                    "error": f"'{field}' is empty",
                })
        row["name"] = row["name"] or f"case-{i - 1:03d}"
        rows.append(row)

    if not rows:
        errors.append({"row": None, "field": "file", "error": "No data rows found"})
    log.debug("parsed %r: %s rows, %s error(s)", filename, len(rows), len(errors))
    return rows, errors
