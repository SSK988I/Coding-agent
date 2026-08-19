"""Tests for Windows console output-mode negotiation and restoration."""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import agent_tui.terminal as terminal_module
from agent_tui.terminal import (
    _WIN_REQUIRED_OUTPUT_MODE,
    _WIN_VT_OUTPUT_MODE,
    ProcessTerminal,
)


class _FakeKernel32:
    stdin_handle = 101
    stdout_handle = 202

    def __init__(
        self,
        *,
        input_mode: int = 0x0017,
        output_mode: int = 0x0420,
        input_mode_read: bool = True,
        output_mode_read: bool = True,
        output_mode_set: bool = True,
        fallback_output_mode_set: bool = True,
    ) -> None:
        self.input_mode = input_mode
        self.output_mode = output_mode
        self.input_mode_read = input_mode_read
        self.output_mode_read = output_mode_read
        self.output_mode_set = output_mode_set
        self.fallback_output_mode_set = fallback_output_mode_set
        self.get_handle_calls: list[int] = []
        self.get_mode_calls: list[int] = []
        self.set_mode_calls: list[tuple[int, int]] = []

    def GetStdHandle(self, kind: int) -> int:
        self.get_handle_calls.append(kind)
        return self.stdin_handle if kind == -10 else self.stdout_handle

    def GetConsoleMode(self, handle: int, mode_pointer: object) -> int:
        self.get_mode_calls.append(handle)
        if handle == self.stdin_handle:
            ok = self.input_mode_read
            value = self.input_mode
        else:
            ok = self.output_mode_read
            value = self.output_mode
        if ok:
            pointer = ctypes.cast(mode_pointer, ctypes.POINTER(ctypes.c_uint32))
            pointer.contents.value = value
        return int(ok)

    def SetConsoleMode(self, handle: int, mode: int) -> int:
        self.set_mode_calls.append((handle, mode))
        is_enabling_output = (
            handle == self.stdout_handle and mode != self.output_mode
        )
        if is_enabling_output:
            if mode & terminal_module._WIN_DISABLE_NEWLINE_AUTO_RETURN:
                return int(self.output_mode_set)
            return int(self.fallback_output_mode_set)
        return 1


def _install_windows(monkeypatch, kernel32: _FakeKernel32) -> None:
    monkeypatch.setattr(terminal_module, "_is_windows", lambda: True)
    monkeypatch.setattr(
        terminal_module.ctypes,
        "windll",
        SimpleNamespace(kernel32=kernel32),
        raising=False,
    )


def test_windows_output_mode_preserves_flags_and_restores(monkeypatch):
    kernel32 = _FakeKernel32(output_mode=0x0420)
    _install_windows(monkeypatch, kernel32)
    terminal = ProcessTerminal()

    terminal._enable_windows_vt()

    expected_output_mode = 0x0420 | _WIN_REQUIRED_OUTPUT_MODE
    assert kernel32.set_mode_calls == [
        (kernel32.stdin_handle, 0x0008),
        (kernel32.stdout_handle, expected_output_mode),
    ]
    assert terminal.delayed_wrap_supported is True

    terminal._disable_windows_vt()

    assert kernel32.set_mode_calls[-2:] == [
        (kernel32.stdin_handle, kernel32.input_mode),
        (kernel32.stdout_handle, kernel32.output_mode),
    ]
    assert kernel32.get_handle_calls == [-10, -11]
    assert terminal.delayed_wrap_supported is False


def test_windows_output_get_failure_uses_safe_width_capability(monkeypatch):
    kernel32 = _FakeKernel32(output_mode_read=False)
    _install_windows(monkeypatch, kernel32)
    terminal = ProcessTerminal()

    terminal._enable_windows_vt()

    assert terminal.delayed_wrap_supported is False
    assert kernel32.set_mode_calls == [(kernel32.stdin_handle, 0x0008)]
    assert terminal._old_output_mode is None


def test_windows_output_set_failure_uses_safe_width_and_restores(monkeypatch):
    kernel32 = _FakeKernel32(output_mode_set=False)
    _install_windows(monkeypatch, kernel32)
    terminal = ProcessTerminal()

    terminal._enable_windows_vt()

    assert terminal.delayed_wrap_supported is False
    assert kernel32.set_mode_calls[-2:] == [
        (
            kernel32.stdout_handle,
            kernel32.output_mode | _WIN_REQUIRED_OUTPUT_MODE,
        ),
        (
            kernel32.stdout_handle,
            kernel32.output_mode | _WIN_VT_OUTPUT_MODE,
        ),
    ]

    terminal._disable_windows_vt()

    assert kernel32.set_mode_calls[-1] == (
        kernel32.stdout_handle,
        kernel32.output_mode,
    )


def test_non_windows_terminals_report_delayed_wrap(monkeypatch):
    monkeypatch.setattr(terminal_module, "_is_windows", lambda: False)

    assert ProcessTerminal().delayed_wrap_supported is True
