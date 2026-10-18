"""Sortable unique IDs (ULID-style, Crockford base32)."""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def ulid() -> str:
    millis = int(time.time() * 1000)
    return _encode(millis, 10) + _encode(int.from_bytes(os.urandom(10), "big"), 16)


def canonical_id() -> str:
    return f"atlas:{ulid().lower()}"
