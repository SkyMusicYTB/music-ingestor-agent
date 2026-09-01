from __future__ import annotations

import secrets
import time
import uuid


def uuid7() -> str:
    """Return a UUIDv7 string on Python versions with or without uuid.uuid7."""
    native = getattr(uuid, "uuid7", None)
    if native is not None:
        return str(native())
    timestamp_ms = int(time.time_ns() // 1_000_000) & ((1 << 48) - 1)
    rand = secrets.randbits(74)
    value = timestamp_ms << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))
