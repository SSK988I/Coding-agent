"""Tests for CLI options that affect runtime safety and session handling."""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_llm import Model, ModelCost

from coding_agent.cli.args import Args, parse_args, resolve_app_mode
from coding_agent.cli.main import (
    _configure_output_encoding,
    _create_session_manager,
    _load_context_for_run,
)
from coding_agent.core.agent_session import AgentSession, AgentSessionConfig


def _model() -> Model:
    return Model(
        id="m",
        context_window=1000,
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )


def test_session_selectors_are_mutually_exclusive():
    try:
        parse_args(["--continue", "--session", "abc"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected conflicting session selectors to fail")


def test_plan_controls_are_mutually_exclusive():
    with pytest.raises(SystemExit) as exc:
        parse_args(["--cancel-plan", "--execute-plan", "1"])
    assert exc.value.code == 2


def test_agent_mode_is_distinct_from_output_mode():
    args = parse_args(["--mode", "json", "--agent-mode", "plan"])
    assert args.output_mode == "json"
    assert args.agent_mode == "plan"


def test_plan_control_forces_non_interactive_mode(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert resolve_app_mode(Args(cancel_plan=True)) == "print"


def test_project_trust_flags_are_mutually_exclusive():
    try:
        parse_args(["--approve", "--no-approve"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected conflicting trust flags to fail")


def test_web_search_flags_are_tristate_and_mutually_exclusive():
    assert parse_args([]).web_search is None
    assert parse_args(["--web-search"]).web_search is True
    assert parse_args(["--no-web-search"]).web_search is False
    with pytest.raises(SystemExit) as exc:
        parse_args(["--web-search", "--no-web-search"])
    assert exc.value.code == 2


def test_no_approve_skips_project_context(monkeypatch, tmp_path: Path):
    def unexpected_load(*_args, **_kwargs):
        raise AssertionError("project context should not be loaded")

    monkeypatch.setattr(
        "coding_agent.cli.main.load_project_context_files", unexpected_load,
    )

    context = _load_context_for_run(
        Args(project_trust_override=False), str(tmp_path), tmp_path,
    )

    assert context is None


def test_no_builtin_tools_disables_defaults_but_keeps_custom_list():
    without_builtins = AgentSession(AgentSessionConfig(
        model=_model(), no_builtin_tools=True,
    ))
    assert without_builtins._tools == []

    custom_tool_marker = SimpleNamespace(name="custom")
    with_custom = AgentSession(AgentSessionConfig(
        model=_model(),
        tools=[custom_tool_marker],  # type: ignore[list-item]
        no_builtin_tools=True,
    ))
    assert with_custom._tools == [custom_tool_marker]


def test_session_id_and_directory_are_applied(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    manager = _create_session_manager(
        Args(session_id="named-session", session_dir=str(sessions_dir)),
        str(tmp_path),
    )

    assert manager.header.id == "named-session"
    assert manager.path is not None
    assert sessions_dir in manager.path.parents


def test_unimplemented_resume_option_is_not_advertised():
    try:
        parse_args(["--resume"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected removed resume option to be rejected")


def test_output_encoding_handles_localized_help_on_legacy_windows_code_page():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")

    _configure_output_encoding(stream)
    stream.write("用法：coding-agent")
    stream.flush()

    assert stream.encoding == "utf-8"
    assert raw.getvalue().decode("utf-8") == "用法：coding-agent"
