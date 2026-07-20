"""Tests for the ! bash passthrough (AgentSession.run_bash + interactive dispatch).

Covers:
  - run_bash executes a command, records a BashExecutionMessage, emits an event.
  - exclude_from_context flag propagates to the recorded message.
  - error paths (no bash tool, spawn failure).
  - the interactive _on_submit dispatch routes !-prefixed input to bash.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_llm import Model, ModelCost

from agent_core import SessionManager

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.messages import BashExecutionMessage


def _run(coro):
    return asyncio.run(coro)


def _model() -> Model:
    return Model(
        id="m", context_window=1000,
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )


def _make_session(cwd=None):
    cwd = cwd or str(Path.cwd())
    sm = SessionManager.create(cwd=cwd, in_memory=True)
    config = AgentSessionConfig(model=_model(), cwd=cwd, session_manager=sm)
    return AgentSession(config)


# ─── run_bash core ────────────────────────────────────────────────────


def test_run_bash_records_message_and_emits_event():
    session = _make_session()
    events: list[dict] = []
    session.on_event(events.append)

    result = _run(session.run_bash("echo hello_world"))

    assert result["exit_code"] == 0
    assert "hello_world" in result["output"]
    assert result["exclude_from_context"] is False

    # BashExecutionMessage recorded in agent state.
    bash_msgs = [m for m in session.agent.state.messages if isinstance(m, BashExecutionMessage)]
    assert len(bash_msgs) == 1
    assert bash_msgs[0].command == "echo hello_world"
    assert bash_msgs[0].exit_code == 0
    assert bash_msgs[0].exclude_from_context is False

    # Event emitted.
    bash_events = [e for e in events if e.get("type") == "bash_execution"]
    assert len(bash_events) == 1
    assert bash_events[0]["command"] == "echo hello_world"


def test_run_bash_exclude_from_context_flag():
    session = _make_session()
    _run(session.run_bash("echo x", exclude_from_context=True))
    bash_msgs = [m for m in session.agent.state.messages if isinstance(m, BashExecutionMessage)]
    assert bash_msgs[0].exclude_from_context is True


def test_run_bash_nonzero_exit_recorded():
    session = _make_session()
    result = _run(session.run_bash("exit 3"))
    assert result["exit_code"] == 3
    bash_msgs = [m for m in session.agent.state.messages if isinstance(m, BashExecutionMessage)]
    assert bash_msgs[0].exit_code == 3


def test_run_bash_returns_error_when_no_bash_tool():
    session = _make_session()
    session._bash_tool = None
    result = _run(session.run_bash("echo hi"))
    assert "error" in result
    assert "No bash tool" in result["error"]


def test_run_bash_handles_spawn_failure():
    session = _make_session()
    # Stub the bash tool's run_raw to raise.
    session._bash_tool = MagicMock()
    session._bash_tool.run_raw = AsyncMock(side_effect=RuntimeError("spawn failed"))
    result = _run(session.run_bash("whatever"))
    assert "error" in result
    assert "spawn failed" in result["error"]


def test_run_bash_timed_out_marked_cancelled():
    session = _make_session()
    from agent_core import BashRawResult
    session._bash_tool = MagicMock()
    session._bash_tool.run_raw = AsyncMock(
        return_value=BashRawResult(output="timed out", exit_code=124, timed_out=True)
    )
    result = _run(session.run_bash("sleep 999"))
    assert result["timed_out"] is True
    bash_msgs = [m for m in session.agent.state.messages if isinstance(m, BashExecutionMessage)]
    assert bash_msgs[0].cancelled is True


# ─── interactive _on_submit ! dispatch ────────────────────────────────


def _make_interactive_stub():
    """Build a minimal object with the _on_submit surface bound."""
    from coding_agent.modes.interactive.interactive_mode import InteractiveMode

    obj = SimpleNamespace()
    obj._is_responding = False
    obj._session = MagicMock()
    obj._session.run_bash = AsyncMock(return_value={
        "output": "stub-out", "exit_code": 0, "truncated": False,
        "timed_out": False, "exclude_from_context": False,
    })
    obj.tui = MagicMock()
    obj.theme = MagicMock()
    obj.theme.fg = lambda *a: a[1] if len(a) > 1 else ""
    obj._active_status_indicator = None

    calls: list[str] = []

    def _add_assistant_text(text):
        calls.append(text)

    def _add_system_message(text):
        calls.append(f"[sys] {text}")

    def _show_status_indicator(ind):
        pass

    def _clear_status_indicator(*a, **kw):
        pass

    def _refresh_footer():
        pass

    obj._add_assistant_text = _add_assistant_text
    obj._add_system_message = _add_system_message
    obj._show_status_indicator = _show_status_indicator
    obj._clear_status_indicator = _clear_status_indicator
    obj._refresh_footer = _refresh_footer
    obj._on_submit = InteractiveMode._on_submit.__get__(obj, SimpleNamespace)
    obj._handle_bash_command = InteractiveMode._handle_bash_command.__get__(obj, SimpleNamespace)
    return obj, calls


def test_on_submit_routes_bang_to_run_bash():
    obj, calls = _make_interactive_stub()
    _run(obj._on_submit("!echo hi"))
    # run_bash was called with the command (stripped of !).
    obj._session.run_bash.assert_awaited_once()
    kwargs = obj._session.run_bash.await_args.kwargs
    assert obj._session.run_bash.await_args.args[0] == "echo hi"
    assert kwargs.get("exclude_from_context") is False


def test_on_submit_double_bang_sets_exclude():
    obj, calls = _make_interactive_stub()
    _run(obj._on_submit("!!echo secret"))
    assert obj._session.run_bash.await_args.args[0] == "echo secret"
    assert obj._session.run_bash.await_args.kwargs["exclude_from_context"] is True


def test_on_submit_bang_with_leading_whitespace():
    obj, calls = _make_interactive_stub()
    _run(obj._on_submit("   !ls"))
    assert obj._session.run_bash.await_args.args[0] == "ls"


def test_on_submit_bang_while_responding_does_not_run():
    """When responding, _on_submit short-circuits before the ! branch (no bash)."""
    obj, calls = _make_interactive_stub()
    obj._is_responding = True
    _run(obj._on_submit("!ls"))
    obj._session.run_bash.assert_not_awaited()


def test_on_submit_bare_bang_falls_through_to_message():
    """A bare '!' with no command should not trigger bash."""
    obj, calls = _make_interactive_stub()
    # Stub _add_user_message and _respond so the fall-through doesn't crash.
    obj._add_user_message = lambda t: calls.append(f"[user] {t}")

    async def _respond(t):
        calls.append(f"[respond] {t}")
    obj._respond = _respond
    obj.editor = MagicMock()

    _run(obj._on_submit("!"))
    obj._session.run_bash.assert_not_awaited()
