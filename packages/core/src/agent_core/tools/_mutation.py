"""Per-realpath locks shared by edit and write, including other Agent processes."""
from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import stat
import threading
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import AsyncIterator

from agent_core.types import ToolExecutionError


def resolved_mutation_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def edit_conflict(path: str, reason: str) -> ToolExecutionError:
    return ToolExecutionError(
        f"{path}: {reason}. The current file was preserved. Read it before starting a new edit.",
        details={"code": "EDIT_CONFLICT"},
    )


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel.CreateMutexW.restype = wintypes.HANDLE
    _kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel.WaitForSingleObject.restype = wintypes.DWORD
    _kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    # Win32 mutexes are recursive per thread. Coroutines on the same event
    # loop therefore need a local reservation even while the OS mutex is held.
    _reserved: set[str] = set()
    _reservation_lock = threading.Lock()

    @contextmanager
    def _path_lock(key: str):
        namespace_key = os.path.normcase(str(Path.home())) + "\0" + key
        name = "Global\\CodingAgentEdit-" + hashlib.sha256(os.fsencode(namespace_key)).hexdigest()
        handle = _kernel.CreateMutexW(None, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = False

        def try_acquire() -> bool:
            nonlocal acquired
            with _reservation_lock:
                if key in _reserved:
                    return False
                outcome = _kernel.WaitForSingleObject(handle, 0)
                if outcome == 258:  # WAIT_TIMEOUT
                    return False
                if outcome not in (0, 128):  # WAIT_OBJECT_0, WAIT_ABANDONED
                    raise ctypes.WinError(ctypes.get_last_error())
                _reserved.add(key)
                acquired = True
                return True

        try:
            yield try_acquire
        finally:
            if acquired:
                with _reservation_lock:
                    _kernel.ReleaseMutex(handle)
                    _reserved.remove(key)
            _kernel.CloseHandle(handle)

else:
    import fcntl

    @contextmanager
    def _path_lock(key: str):
        # A stable per-user location also coordinates clients with different
        # working directories, session roots and TMPDIR values.
        directory = Path("/tmp") / f"coding-agent-path-locks-{os.getuid()}"
        directory.mkdir(mode=0o700, exist_ok=True)
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise PermissionError(f"Unsafe path lock directory: {directory}")
        name = hashlib.sha256(os.fsencode(key)).hexdigest()
        fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)

        def try_acquire() -> bool:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return False
                raise
            return True

        try:
            yield try_acquire
        finally:
            os.close(fd)  # Closing the last open description releases flock.


@asynccontextmanager
async def resolved_file_lock(key: str) -> AsyncIterator[str]:
    """Lock an already identified target, including its name after a move or deletion."""
    with _path_lock(key) as try_acquire:
        while not try_acquire():
            await asyncio.sleep(0.025)
        yield key


@asynccontextmanager
async def file_mutation_lock(file_path: str) -> AsyncIterator[str]:
    key = resolved_mutation_path(file_path)
    async with resolved_file_lock(key):
        if resolved_mutation_path(file_path) != key:
            raise edit_conflict(file_path, "target path changed while waiting for the file lock")
        yield key
