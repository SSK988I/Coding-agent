"""Windows shell resolution regression tests."""
from __future__ import annotations

import agent_core.shell as shell_module


def test_bash_on_path_rejects_legacy_wsl(monkeypatch) -> None:
    legacy = r"C:\Windows\System32\bash.exe"
    monkeypatch.setattr(shell_module.shutil, "which", lambda _name: legacy)
    monkeypatch.setattr(shell_module.os.path, "isfile", lambda _path: True)

    assert shell_module._find_bash_on_path() is None


def test_windows_resolution_uses_fixed_git_bash_fallback(monkeypatch) -> None:
    expected = r"C:\Program Files\Git\usr\bin\bash.exe"
    monkeypatch.setattr(shell_module.sys, "platform", "win32")
    monkeypatch.setattr(
        shell_module,
        "_windows_git_bash_candidates",
        lambda: [expected],
    )
    monkeypatch.setattr(shell_module.os.path, "isfile", lambda path: path == expected)

    config = shell_module.get_shell_config()

    assert config.shell == expected
    assert config.command_transport == "argv"
    assert config.shell_kind == "bash"
    assert config.args == ("--noprofile", "--norc", "-c")


def test_explicit_legacy_wsl_launcher_is_rejected(monkeypatch) -> None:
    legacy = r"C:\Windows\System32\bash.exe"
    monkeypatch.setattr(shell_module.os.path, "isfile", lambda _path: True)

    try:
        shell_module.get_shell_config(legacy)
    except ValueError as exc:
        assert "legacy" in str(exc)
    else:
        raise AssertionError("legacy WSL launcher should be rejected")
