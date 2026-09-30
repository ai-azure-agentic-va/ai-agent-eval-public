"""Storage facade — backend selected once at import via ``EVAL_STORAGE``.

``langsmith`` (default) or ``cosmos``. Any other value raises immediately: a
typo silently falling back to the wrong backend would split the data. The
chosen name is exported as ``BACKEND`` so health/logging report the backend
actually bound here rather than re-deriving it from the environment.

Every other module calls ``storage.<fn>`` attribute-style (never
``from service.storage import save_row``) so the functions can be monkeypatched
in tests and so the backend module stays a single seam:

    datasets   save_dataset, list_datasets, load_dataset
    runs       create_run, update_run, load_run, list_runs
    rows       save_row, load_rows

See ``service/storage/langsmith.py`` / ``service/storage/cosmos.py`` for each
backend's object mapping and the live-verified constraints it encodes.
"""

import importlib
import os

_FUNCTIONS = (
    "create_run", "list_datasets", "list_runs", "load_dataset", "load_rows",
    "load_run", "save_dataset", "save_row", "update_run",
)

BACKEND = os.getenv("EVAL_STORAGE", "langsmith").strip().lower()
if BACKEND not in ("langsmith", "cosmos"):
    raise ValueError(
        f"EVAL_STORAGE={BACKEND!r} is not a storage backend "
        "(expected 'langsmith' or 'cosmos')")

_module = importlib.import_module(f"service.storage.{BACKEND}")
globals().update({name: getattr(_module, name) for name in _FUNCTIONS})

__all__ = _FUNCTIONS + ("BACKEND",)
