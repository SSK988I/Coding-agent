"""Shared Plan Mode state-machine, policy, and persistence regression tests."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_core import AgentContext, BeforeToolCallContext
from agent_core.session import SessionManager
from agent_core.session.storage import read_header
from agent_core.session.types import PlanRevisionEntry, PlanRunEntry
from agent_llm import AssistantMessage, Model, ModelCost, TextContent, ToolCall, UserMessage

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.plan_mode import (
    PlanModeError,
    PlanQuestion,
    PlanQuestionOption,
    is_plan_safe_shell_command,
    reduce_plan_state,
    validate_proposed_plan,
)


def _model() -> Model:
    return Model(
        id="plan-test", provider="test", context_window=64_000,
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )


def _plan_text(title: str = "Plan") -> str:
    return f"""<proposed_plan>
# {title}

## Summary
Summary.

## Implementation Changes
Changes.

## Public Interfaces
Interfaces.

## Test Plan
Tests.

## Assumptions
None.
</proposed_plan>"""


def _bare_bilingual_plan_text() -> str:
    return """我已经完成检查，下面是详细计划。

# 运行时性能优化计划

## Summary / 摘要
减少会话热路径上的重复工作。

## 变更 1：缓存 entry id
为 SessionManager 维护集合缓存，并在分支切换时重建。

## 变更 2：合并事件分支
消除 message_end 的重复处理。

## Public Interfaces / 公开接口
不修改公开 API。

## Test Plan / 测试计划
运行单元测试和完整回归。

## Assumptions / 假设
不修改 JSONL 字段语义。

需要我按这个计划开始实现吗？"""


def test_plan_spec_is_strict_and_digest_is_stable() -> None:
    revision = validate_proposed_plan(
        _plan_text().replace("\n", "\r\n"),
        plan_id="p", revision=1, source_message_id="m",
    )
    assert revision.title == "Plan"
    assert len(revision.digest) == 64
    assert "\r" not in revision.markdown

    with pytest.raises(PlanModeError, match="块外"):
        validate_proposed_plan(
            "prefix\n" + _plan_text(), plan_id="p", revision=2,
            source_message_id="m2",
        )


def test_plan_spec_accepts_bilingual_section_headings() -> None:
    text = """<proposed_plan>
# 双语计划

## Summary / 摘要
S

## Implementation Changes / 实现变更
I

## Public Interfaces / 公开接口
P

## Test Plan / 测试计划
T

## Assumptions / 假设
A
</proposed_plan>"""

    revision = validate_proposed_plan(text, plan_id="p", revision=1, source_message_id="m")

    assert revision.title == "双语计划"


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("git status --short", True),
        ("rg --files packages/app | head -20", True),
        ("uv run pytest -q", True),
        ("pnpm typecheck", True),
        ("git checkout -- file.py", False),
        ("python scripts/mutate.py", False),
        ("cat file > copy", False),
        ("rg token ../private", False),
    ],
)
def test_plan_shell_policy(command: str, allowed: bool, tmp_path: Path) -> None:
    assert is_plan_safe_shell_command(command, str(tmp_path)) is allowed


def test_mode_only_session_is_not_materialized(tmp_path: Path) -> None:
    manager = SessionManager.create(cwd="G:\\repo", sessions_dir=tmp_path / "sessions")
    session = AgentSession(AgentSessionConfig(model=_model(), session_manager=manager, cwd=str(tmp_path)))
    session.enter_plan_mode()
    session.cancel_plan_mode(session.plan_state.active_plan_id)
    session.dispose()
    assert manager.path is not None
    assert not manager.path.exists()


def test_first_v4_entry_atomically_upgrades_legacy_header(tmp_path: Path) -> None:
    manager = SessionManager.create(cwd="G:\\repo", sessions_dir=tmp_path / "sessions")
    manager.set_name("legacy")
    manager.header.version = 3
    manager.flush()
    assert manager.path is not None
    assert read_header(manager.path).version == 3  # type: ignore[union-attr]

    reopened = SessionManager.open(manager.path, sessions_dir=tmp_path / "sessions")
    reopened.append_collaboration_mode_change("plan", plan_id="plan-1")
    assert read_header(manager.path).version == 4  # type: ignore[union-attr]
    lines = manager.path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[1])["type"] == "session_info"


def test_agent_session_switches_prompt_tools_and_captures_revision(tmp_path: Path) -> None:
    session = AgentSession(AgentSessionConfig(model=_model(), cwd=str(tmp_path)))
    assert session.collaboration_mode == "default"
    assert all(tool.name != "request_user_input" for tool in session.tools)

    session.enter_plan_mode()
    assert session.collaboration_mode == "plan"
    assert session.tools[-1].name == "request_user_input"
    assert "<collaboration_mode_policy mode=\"plan\">" in session.state.system_prompt

    session._capture_plan_revision(AssistantMessage(content=[TextContent(text=_plan_text())]))
    latest = session.plan_state.latest_revision
    assert latest is not None
    assert session.plan_state.phase == "ready"

    with pytest.raises(PlanModeError) as stale:
        asyncio.run(session.execute_plan(latest.plan_id, latest.revision, "bad"))
    assert stale.value.code == "STALE_PLAN_REVISION"


def test_bare_complete_plan_is_normalized_into_ready_revision(tmp_path: Path) -> None:
    session = AgentSession(AgentSessionConfig(model=_model(), cwd=str(tmp_path)))
    session.enter_plan_mode()
    events: list[dict] = []
    session.on_event(events.append)

    session._capture_plan_revision(
        AssistantMessage(content=[TextContent(text=_bare_bilingual_plan_text())]),
    )

    latest = session.plan_state.latest_revision
    assert latest is not None
    assert latest.title == "运行时性能优化计划"
    assert "## Implementation Changes / 实现变更" in latest.markdown
    assert session.plan_state.phase == "ready"
    assert any(event.get("type") == "plan_ready" for event in events)


def test_incomplete_markdown_discussion_does_not_become_ready(tmp_path: Path) -> None:
    session = AgentSession(AgentSessionConfig(model=_model(), cwd=str(tmp_path)))
    session.enter_plan_mode()

    session._capture_plan_revision(AssistantMessage(content=[TextContent(text="# 一个想法\n\n还需要继续讨论。")]))

    assert session.plan_state.phase == "drafting"
    assert session.plan_state.latest_revision is None


def test_resume_recovers_latest_complete_bare_plan(tmp_path: Path) -> None:
    manager = SessionManager.create(cwd=str(tmp_path), in_memory=True)
    manager.append_collaboration_mode_change("plan", plan_id="plan-resume")
    manager.append_message(
        AssistantMessage(content=[TextContent(text=_bare_bilingual_plan_text())]),
    )

    session = AgentSession(AgentSessionConfig(
        model=_model(), cwd=str(tmp_path), session_manager=manager,
    ))

    assert session.plan_state.phase == "ready"
    assert session.plan_state.latest_revision is not None
    assert any(isinstance(entry, PlanRevisionEntry) for entry in manager.entries)


def test_resume_does_not_revive_bare_plan_after_user_feedback(tmp_path: Path) -> None:
    manager = SessionManager.create(cwd=str(tmp_path), in_memory=True)
    manager.append_collaboration_mode_change("plan", plan_id="plan-resume")
    manager.append_message(
        AssistantMessage(content=[TextContent(text=_bare_bilingual_plan_text())]),
    )
    manager.append_message(UserMessage(content="还需要增加失败回滚设计"))

    session = AgentSession(AgentSessionConfig(
        model=_model(), cwd=str(tmp_path), session_manager=manager,
    ))

    assert session.plan_state.phase == "drafting"
    assert session.plan_state.latest_revision is None


def test_user_feedback_after_ready_revision_returns_to_drafting(tmp_path: Path) -> None:
    session = AgentSession(AgentSessionConfig(model=_model(), cwd=str(tmp_path)))
    session.enter_plan_mode()
    session._capture_plan_revision(AssistantMessage(content=[TextContent(text=_plan_text())]))
    latest = session.plan_state.latest_revision
    assert latest is not None

    async def fake_agent_prompt(message: str) -> None:
        session.session_manager.append_message(UserMessage(content=message))

    async def no_compaction() -> SimpleNamespace:
        return SimpleNamespace(need_retry=False)

    session._agent.prompt = fake_agent_prompt  # type: ignore[method-assign]
    session._compaction_orchestrator.check_compaction = no_compaction  # type: ignore[method-assign]

    asyncio.run(session.prompt("请补充失败恢复方案"))

    assert session.plan_state.phase == "drafting"
    assert session.plan_state.latest_revision == latest
    restored = reduce_plan_state(session.session_manager.get_branch())
    assert restored.phase == "drafting"
    assert restored.latest_revision == latest
    with pytest.raises(PlanModeError) as not_ready:
        asyncio.run(session.execute_plan(latest.plan_id, latest.revision, latest.digest))
    assert not_ready.value.code == "PLAN_NOT_READY"


def test_plan_policy_runs_before_external_approval(tmp_path: Path) -> None:
    approval_calls: list[str] = []

    async def approval(context, _signal):
        approval_calls.append(context.tool_call.name)
        return None

    unknown_tool = SimpleNamespace(
        name="custom", label="custom", description="custom", parameters={},
        execute=lambda *_args, **_kwargs: None,
    )
    session = AgentSession(AgentSessionConfig(
        model=_model(), cwd=str(tmp_path), tools=[unknown_tool],
        before_tool_call=approval, collaboration_mode="plan",
    ))
    call = ToolCall(id="t", name="custom", arguments={})
    context = BeforeToolCallContext(
        assistant_message=AssistantMessage(content=[call]), tool_call=call,
        args={}, context=AgentContext(tools=session.tools),
    )
    result = asyncio.run(session._before_tool_call(context, asyncio.Event()))
    assert result is not None and result.block is True
    assert "PLAN_POLICY_BLOCKED" in (result.reason or "")
    assert approval_calls == []


def test_deferred_question_reduces_and_resumes_same_episode(tmp_path: Path) -> None:
    manager = SessionManager.create(cwd=str(tmp_path), in_memory=True)
    session = AgentSession(AgentSessionConfig(
        model=_model(), cwd=str(tmp_path), session_manager=manager,
        collaboration_mode="plan", question_behavior="deferred",
    ))
    question = PlanQuestion(
        question_id="q1", header="范围", question="选择范围？",
        options=(
            PlanQuestionOption("核心", "只改核心"),
            PlanQuestionOption("双端", "覆盖双端"),
        ),
    )
    assert asyncio.run(session._request_plan_question(question, None)) is None
    assert session.plan_state.phase == "awaiting_answer"
    restored = reduce_plan_state(manager.get_branch())
    assert restored.active_plan_id == session.plan_state.active_plan_id
    assert restored.pending_question is not None


def test_exact_revision_execution_records_started_and_completed(tmp_path: Path) -> None:
    session = AgentSession(AgentSessionConfig(model=_model(), cwd=str(tmp_path)))
    session.enter_plan_mode()
    session._capture_plan_revision(AssistantMessage(content=[TextContent(text=_plan_text())]))
    latest = session.plan_state.latest_revision
    assert latest is not None

    async def fake_prompt(_message) -> None:
        session._last_assistant_message = AssistantMessage(content=[TextContent(text="done")])

    session.prompt = fake_prompt  # type: ignore[method-assign]
    asyncio.run(session.execute_plan(latest.plan_id, latest.revision, latest.digest, run_id="run-1"))

    runs = [entry for entry in session.session_manager.entries if isinstance(entry, PlanRunEntry)]
    assert [entry.status for entry in runs] == ["started", "completed"]
    assert session.collaboration_mode == "default"
    assert session.plan_state.phase == "completed"
