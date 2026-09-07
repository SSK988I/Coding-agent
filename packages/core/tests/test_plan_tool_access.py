"""Plan-mode access metadata and subprocess-free search profiles."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_core.tools import (
    BashTool,
    EditTool,
    FindTool,
    GitDiffTool,
    GitLogTool,
    GitShowTool,
    GitStatusTool,
    GrepTool,
    LsTool,
    ReadTool,
    WriteTool,
)


@pytest.mark.parametrize(
    ("tool_type", "expected"),
    [
        (ReadTool, "observe"),
        (GrepTool, "observe"),
        (FindTool, "observe"),
        (LsTool, "observe"),
        (GitStatusTool, "observe"),
        (GitLogTool, "observe"),
        (GitDiffTool, "observe"),
        (GitShowTool, "observe"),
        (WriteTool, "deny"),
        (EditTool, "deny"),
        (BashTool, "deny"),
    ],
)
def test_builtin_tools_declare_plan_access(tool_type, expected: str) -> None:
    assert tool_type.plan_access == expected


def test_missing_plan_access_fails_closed_by_default() -> None:
    class CustomTool:
        pass

    assert getattr(CustomTool(), "plan_access", "deny") == "deny"


def test_grep_plan_profile_never_resolves_external_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "source.py").write_text("needle\n", encoding="utf-8")

    def unexpected_lookup(_binary: str) -> str:
        raise AssertionError("Plan-safe grep must not search PATH")

    monkeypatch.setattr("agent_core.tools.grep.find_in_path", unexpected_lookup)
    tool = GrepTool(cwd=str(tmp_path), prefer_external=False)
    result = asyncio.run(tool.execute("grep-1", {"pattern": "needle"}))
    assert "source.py:1: needle" in result.content[0].text


def test_find_plan_profile_never_resolves_external_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "source.py").write_text("content\n", encoding="utf-8")

    def unexpected_lookup(_binary: str) -> str:
        raise AssertionError("Plan-safe find must not search PATH")

    monkeypatch.setattr("agent_core.tools.find.find_in_path", unexpected_lookup)
    tool = FindTool(cwd=str(tmp_path), prefer_external=False)
    result = asyncio.run(tool.execute("find-1", {"pattern": "*.py"}))
    assert result.content[0].text == "source.py"
