"""Whether a text carries a ticket key: one rule for the forge's PRs and the checkout's branches."""
from __future__ import annotations

import re


def carries_key(text: str, key: str) -> bool:
    """Whether `text` holds `key` as a whole key, whatever the case: not preceded by a letter or digit (AB-12 is not
    in CAB-12-login) and not followed by a digit (ACME-1 is not ACME-10)."""
    return re.search(rf"(?<![^\W_]){re.escape(key)}(?!\d)", text, re.IGNORECASE) is not None
