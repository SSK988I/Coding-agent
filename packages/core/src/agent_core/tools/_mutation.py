"""Shared per-file serialization for file-mutating tools.

``edit`` and ``write`` (and any future tool that mutates a file) must hold a
lock keyed on the file's realpath so concurrent tool calls targeting the same
file are serialized. Different files run in parallel.

Mirrors ``withFileMutationQueue`` in pi: a missing file (ENOENT) falls back to
the resolved path as the key, so an edit and a write racing to *create* the
same new file also serialize.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

# Process-global lock table. One asyncio.Lock per realpath key.
# Lazily initialized per running loop: asyncio.Lock() at import time fails on
# Python 3.13 when no loop is running, so we create the guard/table on first
# use inside an async context.
_file_locks: dict[str, asyncio.Lock] | None = None
_locks_guard: asyncio.Lock | None = None


async def _get_locks_guard() -> asyncio.Lock:
    global _file_locks, _locks_guard
    if _locks_guard is None:
        _file_locks = {}
        _locks_guard = asyncio.Lock()
    return _locks_guard


async def _get_lock_table() -> "dict[str, asyncio.Lock]":
    await _get_locks_guard()
    assert _file_locks is not None
    return _file_locks


async def _mutation_key(file_path: str) -> str:
    """Return the realpath of ``file_path``, or the resolved path if missing.

    A not-yet-existing file has no realpath, so we fall back to the absolute
    normalized path. This keeps creation races (two tools writing a new file)
    serialized under the same key.
    """
    abs_path = os.path.abspath(file_path)
    try:
        return os.path.realpath(abs_path)
    except OSError:
        return abs_path


@asynccontextmanager
async def file_mutation_lock(file_path: str) -> AsyncIterator[None]:
    """Serialize mutations to ``file_path``.

    Acquires the per-realpath lock, runs the body, and releases. Different
    realpaths run concurrently. The lock table entry is left in place (locks
    are cheap and reused); Python's asyncio has no GC concern here since the
    table is bounded by the number of distinct files touched.
    """
    key = await _mutation_key(file_path)
    table = await _get_lock_table()
    guard = await _get_locks_guard()
    async with guard:
        lock = table.get(key)
        if lock is None:
            lock = asyncio.Lock()
            table[key] = lock
    async with lock:
        yield

