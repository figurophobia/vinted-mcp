"""Turn item / user references (ids, URLs, slugs) into numeric ids."""

from __future__ import annotations

import re

_ITEM_URL = re.compile(r"/items/(\d+)")
_MEMBER_URL = re.compile(r"/member/(\d+)")


def item_id(ref: str | int) -> int:
    """``1234567890``, ``"1234567890-nintendo-3ds"`` or a full item URL -> ``1234567890``."""
    s = str(ref).strip()
    m = _ITEM_URL.search(s)
    if m:
        return int(m.group(1))
    m = re.match(r"^(\d+)", s)
    if m:
        return int(m.group(1))
    raise ValueError(f"not a Vinted item reference: {ref!r}")


def user_id(ref: str | int) -> int:
    """A numeric user id, a ``/member/<id>-<login>`` path or URL -> the id."""
    s = str(ref).strip()
    m = _MEMBER_URL.search(s)
    if m:
        return int(m.group(1))
    m = re.match(r"^(\d+)", s)
    if m:
        return int(m.group(1))
    raise ValueError(f"not a Vinted user reference: {ref!r}")
