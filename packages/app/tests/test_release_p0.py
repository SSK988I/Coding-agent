"""Regression tests for first-release correctness requirements."""
from __future__ import annotations

from pathlib import Path

from agent_core import SessionManager
from agent_llm import AssistantMessage, Model, ModelCost, TextContent, UserMessage

from coding_agent.cli.args import Args
from coding_agent.cli.main import _normalize_model_options, _run_print
from coding_agent.core.agent_session import AgentSession, AgentSessionConfig


def _model() -> Model:
    return Model(
        id="m",
        provider="deepseek",
        context_window=64_000,
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )


def test_agent_session_restores_messages_and_thinking(tmp_path: Path):
    manager = SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path)
    manager.append_message(UserMessage(content="old question"))
    manager.append_message(AssistantMessage(
        content=[TextContent(text="old answer")], provider="deepseek", model="m",
    ))
    manager.append_thinking_level_change("high")
    manager.flush()

    resumed = SessionManager.open(manager.path, agent_dir=tmp_path)
    session = AgentSession(AgentSessionConfig(model=_model(), tools=[], session_manager=resumed))

    assert [message.role for message in session.state.messages] == ["user", "assistant"]
    assert session.state.messages[0].content == "old question"
    assert session.thinking_level == "high"


def test_new_session_switches_manager_and_storage(tmp_path: Path):
    old = SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path)
    old.append_message(UserMessage(content="old"))
    session = AgentSession(AgentSessionConfig(model=_model(), tools=[], session_manager=old))

    new = session.new_session()

    assert new.header.id != old.header.id
    assert new.path != old.path
    assert session.session_manager is new
    assert session.agent.session_manager is new
    assert session.state.messages == []
    assert old.path.exists()


def test_new_session_does_not_persist_empty_previous_manager(tmp_path: Path):
    old = SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path)
    session = AgentSession(AgentSessionConfig(model=_model(), tools=[], session_manager=old))

    session.new_session()

    assert old.path is not None
    assert not old.path.exists()


def test_model_shorthand_sets_provider_and_thinking():
    args = Args(model="zhipu/glm-5.2:high")
    _normalize_model_options(args)
    assert args.provider == "zhipu"
    assert args.model == "glm-5.2"
    assert args.thinking == "high"


class _PrintSession:
    def __init__(self, stop_reason: str = "stop") -> None:
        self.listeners = []
        self.stop_reason = stop_reason
        self.disposed = False

    def on_event(self, listener):
        self.listeners.append(listener)

    async def prompt(self, prompt):
        message = AssistantMessage(
            content=[TextContent(text="")],
            stop_reason=self.stop_reason,
            error_message="network error" if self.stop_reason == "error" else None,
        )
        for listener in self.listeners:
            listener({"type": "message_end", "message": message})

    def dispose(self):
        self.disposed = True


def test_print_mode_returns_nonzero_for_missing_input():
    session = _PrintSession()
    assert _run_print(session, None) == 2  # type: ignore[arg-type]
    assert session.disposed


def test_print_mode_returns_nonzero_for_model_error():
    session = _PrintSession(stop_reason="error")
    assert _run_print(session, "hello") == 1  # type: ignore[arg-type]
    assert session.disposed
