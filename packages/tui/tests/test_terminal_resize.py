"""Tests for the Windows resize watcher (terminal.py:_win_resize_loop).

The watcher polls ``GetConsoleScreenBufferInfo`` on the OUTPUT handle
(``STD_OUTPUT_HANDLE``) and fires ``_on_resize`` when the visible window
size changes. This decouples resize detection from the stdin input queue
(the previous implementation used ``ReadConsoleInputW`` on the input handle,
which competed with ``os.read(0)`` for the same queue — corrupting both key
delivery and resize detection).

These tests run on any platform by monkeypatching ``ctypes.windll.kernel32``
with a fake whose ``GetConsoleScreenBufferInfo`` writes scripted size
sequences. ``time.sleep`` is patched to a no-op so poll iterations are
instant; the fake stops the loop when its sequence is exhausted (no
threading, no sleep races).
"""
from __future__ import annotations

import ctypes
import time
import types

import pytest

from agent_tui.terminal import (
    ProcessTerminal,
    _CONSOLE_SCREEN_BUFFER_INFO,
    _WIN_RESIZE_POLL_SEC,
)


class _FakeKernel32:
    """Fake kernel32 exposing only what _win_resize_loop calls.

    ``get_info_seq`` is a list of (width, height, return_ok) tuples — one per
    ``GetConsoleScreenBufferInfo`` call. The fake writes the size into the
    caller's info struct (via its srWindow fields) and returns ``ok``. When
    the sequence is exhausted, the fake sets ``terminal._running = False`` so
    the poll loop exits cleanly (no threading needed).
    """

    def __init__(self, get_info_seq, terminal):
        self._seq = list(get_info_seq)
        self._terminal = terminal
        self.call_count = 0
        self.get_std_handle_returns = 4242  # opaque handle value
        # Cache the function objects so .argtypes/.restype assignments by the
        # loop persist (the loop sets them then calls the same attribute).
        self.GetConsoleScreenBufferInfo = self._make_get_info_fn()
        self.GetStdHandle = self._make_get_std_handle_fn()

    def _make_get_info_fn(self):
        outer = self

        class _Fn:
            argtypes = None
            restype = None

            def __call__(self_inner, handle, info_ptr):
                outer.call_count += 1
                if not outer._seq:
                    # Exhausted → stop the loop (the loop's while-check exits).
                    outer._terminal._running = False
                    return 0
                w, h, ok = outer._seq.pop(0)
                if ok:
                    info = info_ptr._obj  # type: ignore[attr-defined]
                    info.srWindow.Left = 0
                    info.srWindow.Top = 0
                    info.srWindow.Right = w - 1
                    info.srWindow.Bottom = h - 1
                return 1 if ok else 0

        return _Fn()

    def _make_get_std_handle_fn(self):
        outer = self

        class _Fn:
            restype = None

            def __call__(self_inner, which):
                return outer.get_std_handle_returns

        return _Fn()


def _install_fake(fake, monkeypatch):
    """Install ``fake`` as ctypes.windll.kernel32 for the duration of a test."""
    windll = types.SimpleNamespace(kernel32=fake)
    monkeypatch.setattr(ctypes, "windll", windll)


def _make_terminal():
    t = ProcessTerminal()
    t._stdin_handle = 999  # unused by the new polling loop
    return t


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """The poll loop calls ``time.sleep`` via its own ``import time``.

    Patch the real ``time`` module's attribute (not the string path, which
    only patches the test module's import) so the loop's ``time.sleep`` is a
    no-op and iterations run instantly.
    """
    monkeypatch.setattr(time, "sleep", lambda *_: None)


# ── struct sanity ─────────────────────────────────────────────────────


def test_console_screen_buffer_info_size():
    """CONSOLE_SCREEN_BUFFER_INFO must be exactly 22 bytes (Win32 ABI)."""
    assert ctypes.sizeof(_CONSOLE_SCREEN_BUFFER_INFO) == 22


def test_poll_interval_reasonable():
    """Poll interval is sub-second (imperceptible latency) and non-zero."""
    assert 0 < _WIN_RESIZE_POLL_SEC <= 0.5


# ── resize triggers _on_resize on size change ─────────────────────────


def test_resize_fires_on_change(monkeypatch):
    """When the polled width/height changes, _on_resize fires once per
    distinct size (the initial read always differs from the -1 sentinel)."""
    t = _make_terminal()
    fired = []
    t._on_resize = lambda: fired.append(True)
    t._running = True

    # Sequence: 80x24 → 100x30 → then exhausted (loop stops).
    fake = _FakeKernel32([(80, 24, True), (100, 30, True)], t)
    _install_fake(fake, monkeypatch)
    t._win_resize_loop()

    # First poll: 80x24 (differs from sentinel) → fire. Second: 100x30
    # (differs from 80x24) → fire. Both transitions fire.
    assert len(fired) == 2, f"expected 2 resize fires, got {len(fired)}"


def test_no_fire_when_size_unchanged(monkeypatch):
    """Repeated identical sizes fire only once (the initial transition)."""
    t = _make_terminal()
    fired = []
    t._on_resize = lambda: fired.append(True)
    t._running = True

    fake = _FakeKernel32([(80, 24, True), (80, 24, True), (80, 24, True)], t)
    _install_fake(fake, monkeypatch)
    t._win_resize_loop()

    # Only the first 80x24 differs from the sentinel; the next two are
    # identical → no further fires.
    assert len(fired) == 1, f"expected 1 fire (initial only), got {len(fired)}"


def test_shrink_then_grow_fires_each_change(monkeypatch):
    """Both shrink and grow fire resize (big↔small both need re-layout)."""
    t = _make_terminal()
    fired = []
    t._on_resize = lambda: fired.append(True)
    t._running = True

    fake = _FakeKernel32([(120, 40, True), (40, 10, True), (80, 24, True)], t)
    _install_fake(fake, monkeypatch)
    t._win_resize_loop()

    # Three distinct sizes → three fires.
    assert len(fired) == 3


def test_handle_error_backs_off(monkeypatch):
    """When GetConsoleScreenBufferInfo returns 0 (output handle unavailable,
    e.g. stdout redirected), the loop backs off and does not crash or fire."""
    t = _make_terminal()
    fired = []
    t._on_resize = lambda: fired.append(True)
    t._running = True

    # First call: ok=False (handle error). Second: real size (recovers).
    fake = _FakeKernel32([(0, 0, False), (80, 24, True)], t)
    _install_fake(fake, monkeypatch)
    t._win_resize_loop()

    # The ok=False call is skipped (back off); the ok=True 80x24 fires once.
    assert len(fired) == 1


def test_on_resize_optional(monkeypatch):
    """The loop runs without _on_resize set (no crash)."""
    t = _make_terminal()
    t._on_resize = None
    t._running = True

    fake = _FakeKernel32([(80, 24, True)], t)
    _install_fake(fake, monkeypatch)
    t._win_resize_loop()  # must not raise


def test_does_not_touch_input_queue(monkeypatch):
    """The polling watcher never reads from the console INPUT handle. It only
    calls GetStdHandle(STD_OUTPUT_HANDLE) + GetConsoleScreenBufferInfo. This
    is the whole point of the rewrite — stdin's input queue is untouched.

    The fake only implements those two functions; if the loop tried to call
    ReadConsoleInputW it would raise AttributeError, failing the test."""
    t = _make_terminal()
    t._on_resize = lambda: None
    t._running = True

    fake = _FakeKernel32([(80, 24, True), (80, 24, True)], t)
    _install_fake(fake, monkeypatch)
    t._win_resize_loop()

    assert fake.call_count >= 1
