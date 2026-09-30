"""Row-category classification that decides which judges run on a row.

The dataset's ``tags`` column (legacy header: ``category``) is free text; two
substring families matter:

* **Security** rows (jailbreak / security / red team / adversarial): the
  jailbreak judge is the verdict; correctness and the context judges skip.
* **Honesty** rows (fallback / refusal / honesty): the fallback judge runs in
  addition to the normal judges.

Single definition — the judge panel and the runner both import from here.
"""

from __future__ import annotations

_SECURITY_MARKERS = ("jailbreak", "security", "red team", "adversarial")
_HONESTY_MARKERS = ("fallback", "refusal", "honesty")


def is_adversarial_category(category: object) -> bool:
    cat = str(category or "").lower()
    return any(marker in cat for marker in _SECURITY_MARKERS)


def is_honesty_category(category: object) -> bool:
    cat = str(category or "").lower()
    return any(marker in cat for marker in _HONESTY_MARKERS)
