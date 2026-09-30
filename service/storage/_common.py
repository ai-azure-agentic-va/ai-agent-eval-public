"""Helpers every storage backend must share byte-for-byte.

``slug`` defines a dataset's identity: if two backends ever slugged
differently, switching backends would silently re-key every dataset. ``now``
defines the timestamp format that makes started_at/finished_at/uploaded_at
comparable across backends. Neither may be copied into a backend module.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone


def slug(name: str) -> str:
    """Stable dataset id for a display name."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "dataset"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
