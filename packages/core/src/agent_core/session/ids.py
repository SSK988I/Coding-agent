"""ID generation and cwd encoding.

Two distinct IDs:
  - Session ID: full UUIDv7 (time-ordered, monotonic). Python 3.13 has no
    ``uuid.uuid7`` (added in 3.14), so we implement a minimal UUIDv7 with a
    module-level monotonic counter for same-millisecond ordering.
  - Entry ID: first 8 hex chars of a random uuid, collision-checked against
    the current session's known ids (retries up to 100, then full uuid).

``encode_cwd`` produces stable, filesystem-safe directory names for session files.
"""
from __future__ import annotations

import os
import random
import re
import threading
import time
import uuid

__all__ = [
    "create_session_id",
    "generate_entry_id",
    "encode_cwd",
    "is_valid_session_id",
]

# ─── UUIDv7 (session id, time-ordered + monotonic) ─────────────────────
#
# UUIDv7 layout:
#   bits  0-47  : unix ms timestamp
#   bits 48-51  : version (0x7)
#   bits 52-63  : monotonic sequence (rand_a, 12 bits)
#   bit     64  : variant (0b10)
#   bits 65-127 : rand_b (62 bits)

_last_ts_ms: int = -1
_sequence: int = 0
_lock = threading.Lock()

_VARIANT_10 = 0b10

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


def create_session_id() -> str:
    """Return a monotonic UUIDv7 string (uuidv7()).

    Uses a module-level last-timestamp + sequence so that calls within the
    same millisecond are strictly ordered. Thread-safe via a lock.
    """
    global _last_ts_ms, _sequence
    with _lock:
        now_ms = int(time.time() * 1000)
        if now_ms == _last_ts_ms:
            _sequence = (_sequence + 1) & 0xFFF  # 12-bit wrap
        else:
            _last_ts_ms = now_ms
            _sequence = random.getrandbits(12)

        ts = now_ms
        version = 0x7
        # 48-bit timestamp | 4-bit version | 12-bit sequence
        high = (ts << 16) | (version << 12) | _sequence
        # variant 0b10 in top 2 bits of octet 8, rest random (62 bits)
        rand_b = random.getrandbits(62)
        low = (_VARIANT_10 << 62) | rand_b

        # Compose as a 128-bit integer and format as canonical uuid.
        val = (high << 64) | low
        return str(uuid.UUID(int=val))


def is_valid_session_id(sid: str) -> bool:
    """Validate a session id against the regex."""
    return bool(_SESSION_ID_RE.match(sid))


# ─── Entry id (short, collision-checked) ──────────────────────────────

def generate_entry_id(existing: set[str] | dict | None = None) -> str:
    """Return an 8-hex entry id, collision-checked against ``existing``.

    Uses the first 8 hex characters of a random UUID; on collision it retries
    up to 100 times, then falls back to the full UUID.
    """
    for _ in range(100):
        eid = uuid.uuid4().hex[:8]
        if not existing or eid not in existing:
            return eid
    return uuid.uuid4().hex  # fallback: full 32 hex


# ─── cwd → directory name ─

def encode_cwd(cwd: str) -> str:
    """Encode a cwd path into a safe directory name.

    Strips a leading slash or backslash, replaces slashes and colons with dashes,
    and wraps the result in ``--...--``.
    Example: ``E:\\code\\foo`` -> ``--E-code-foo--``.
    """
    resolved = os.path.normpath(cwd)
    # Strip one leading path separator before replacing reserved characters.
    stripped = re.sub(r"^[/\\]", "", resolved)
    safe = re.sub(r"[/\\:]", "-", stripped)
    return f"--{safe}--"
