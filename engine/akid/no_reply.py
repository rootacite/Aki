"""Detect a NO_REPLY marker in a final reply."""

from __future__ import annotations

import unicodedata
from .constants import _NO_REPLY_MARKER_RE



def contains_no_reply_marker(text: str) -> bool:
    """Return true when text contains a tolerated NO_REPLY marker anywhere."""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return bool(_NO_REPLY_MARKER_RE.search(normalized))
