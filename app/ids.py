import os
import secrets
import time
import uuid
from datetime import UTC, datetime


def new_id() -> str:
    """UUIDv7: time-ordered and unguessable."""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


def utcnow() -> datetime:
    return datetime.now(UTC)


_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def join_code(name: str) -> str:
    prefix = "".join(c for c in name.upper() if c.isascii() and c.isalnum())[:4] or "GRP"
    return f"{prefix}-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
