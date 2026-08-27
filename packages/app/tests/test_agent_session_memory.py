from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_core import SessionManager
from agent_llm import AssistantMessage, Model, ModelCost, TextContent, UserMessage

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.memory.types import MemoryContext, MemoryIdentity


def _model() -> Model:
    return Model(
        id="memory-test",
        provider="test",
        context_window=64_000,
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )


class _MemoryService:
    def __init__(self) -> None:
        self.enabled = True
        self.queries: list[str] = []
        self.tasks: list[object] = []
        self.pending_tasks: list[object] = []
        self.processed_tasks: list[object] = []
        self.flush_count = 0
        self.stop_requested = False
        self.closed = False
        self.worker_task: asyncio.Task[None] | None = None

    async def before_task(self, identity, query: str) -> MemoryContext:
        await self.flush_pending(identity)
        self.queries.append(query)
        return MemoryContext(
            records=[],
            prompt_block='<long_term_memory>response.language = "zh-CN"</long_term_memory>',
            estimated_tokens=10,
        )

    async def after_task(self, task) -> SimpleNamespace:
        self.tasks.append(task)
        self.pending_tasks.append(task)
        return SimpleNamespace(queued=1)

    async def flush_pending(self, _identity, *, max_jobs=None) -> SimpleNamespace:
        self.flush_count += 1
        limit = len(self.pending_tasks) if max_jobs is None else max_jobs
        selected = self.pending_tasks[:limit]
        del self.pending_tasks[:limit]
        self.processed_tasks.extend(selected)
        return SimpleNamespace(accepted=len(selected))

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def request_stop(self) -> None:
        self.stop_requested = True
        if self.worker_task is not None and not self.worker_task.done():
            self.worker_task.cancel()

    async def close(self, *, grace_seconds: float = 0.25) -> None:
        del grace_seconds
        self.closed = True
        self.request_stop()
        if self.worker_task is not None:
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass


def test_prompt_retrieves_transient_context_and_queues_user_evidence(tmp_path: Path) -> None:
    manager = SessionManager.create(cwd=str(tmp_path), in_memory=True)
    memory = _MemoryService()
    session = AgentSession(AgentSessionConfig(
        model=_model(),
        cwd=str(tmp_path),
        session_manager=manager,
        memory_service=memory,
        memory_identity=MemoryIdentity("local-user", "project-a"),
    ))
    prompts_seen: list[str] = []

    async def fake_prompt(message: str) -> None:
        prompts_seen.append(session.state.system_prompt)
        manager.append_message(UserMessage(content=message))
        assistant = AssistantMessage(content=[TextContent(text="完成")], stop_reason="stop")
        manager.append_message(assistant)
        session._last_assistant_message = assistant

    async def no_compaction() -> SimpleNamespace:
        return SimpleNamespace(need_retry=False)

    session._agent.prompt = fake_prompt  # type: ignore[method-assign]
    session._compaction_orchestrator.check_compaction = no_compaction  # type: ignore[method-assign]

    asyncio.run(session.prompt("以后回答使用中文"))

    assert memory.queries == ["以后回答使用中文"]
    assert "<long_term_memory>" in prompts_seen[0]
    assert "<long_term_memory>" not in session.state.system_prompt
    assert len(memory.tasks) == 1
    task = memory.tasks[0]
    assert task.evidence[0].text == "以后回答使用中文"
    assert task.final_response == "完成"


def test_failed_task_does_not_enqueue_memory(tmp_path: Path) -> None:
    manager = SessionManager.create(cwd=str(tmp_path), in_memory=True)
    memory = _MemoryService()
    session = AgentSession(AgentSessionConfig(
        model=_model(), cwd=str(tmp_path), session_manager=manager,
        memory_service=memory, memory_identity=MemoryIdentity("u", "p"),
    ))

    async def fake_prompt(message: str) -> None:
        manager.append_message(UserMessage(content=message))
        assistant = AssistantMessage(
            content=[TextContent(text="")], stop_reason="error", error_message="boom",
        )
        manager.append_message(assistant)
        session._last_assistant_message = assistant

    async def no_compaction() -> SimpleNamespace:
        return SimpleNamespace(need_retry=False)

    session._agent.prompt = fake_prompt  # type: ignore[method-assign]
    session._compaction_orchestrator.check_compaction = no_compaction  # type: ignore[method-assign]
    asyncio.run(session.prompt("remember me"))
    assert memory.tasks == []


def test_prompt_returns_without_waiting_for_slow_memory_worker(tmp_path: Path) -> None:
    manager = SessionManager.create(cwd=str(tmp_path), in_memory=True)
    memory = _MemoryService()
    session = AgentSession(AgentSessionConfig(
        model=_model(), cwd=str(tmp_path), session_manager=manager,
        memory_service=memory, memory_identity=MemoryIdentity("u", "p"),
    ))

    async def fake_prompt(message: str) -> None:
        manager.append_message(UserMessage(content=message))
        assistant = AssistantMessage(content=[TextContent(text="完成")], stop_reason="stop")
        manager.append_message(assistant)
        session._last_assistant_message = assistant

    async def no_compaction() -> SimpleNamespace:
        return SimpleNamespace(need_retry=False)

    session._agent.prompt = fake_prompt  # type: ignore[method-assign]
    session._compaction_orchestrator.check_compaction = no_compaction  # type: ignore[method-assign]

    async def scenario() -> None:
        extraction_started = asyncio.Event()
        allow_extraction = asyncio.Event()
        enqueue = memory.after_task

        async def slow_worker() -> None:
            extraction_started.set()
            await allow_extraction.wait()

        async def enqueue_then_start_worker(task) -> SimpleNamespace:
            result = await enqueue(task)
            memory.worker_task = asyncio.create_task(slow_worker())
            return result

        memory.after_task = enqueue_then_start_worker  # type: ignore[method-assign]
        await session.prompt("first")
        await extraction_started.wait()

        assert session.is_processing is False
        assert memory.worker_task is not None
        assert not memory.worker_task.done()
        assert len(memory.pending_tasks) == 1

        allow_extraction.set()
        await memory.worker_task

    asyncio.run(scenario())
    assert session.is_processing is False


def test_next_prompt_flushes_memory_queued_by_previous_task(tmp_path: Path) -> None:
    manager = SessionManager.create(cwd=str(tmp_path), in_memory=True)
    memory = _MemoryService()
    session = AgentSession(AgentSessionConfig(
        model=_model(), cwd=str(tmp_path), session_manager=manager,
        memory_service=memory, memory_identity=MemoryIdentity("u", "p"),
    ))

    async def fake_prompt(message: str) -> None:
        manager.append_message(UserMessage(content=message))
        assistant = AssistantMessage(content=[TextContent(text="完成")], stop_reason="stop")
        manager.append_message(assistant)
        session._last_assistant_message = assistant

    async def no_compaction() -> SimpleNamespace:
        return SimpleNamespace(need_retry=False)

    session._agent.prompt = fake_prompt  # type: ignore[method-assign]
    session._compaction_orchestrator.check_compaction = no_compaction  # type: ignore[method-assign]

    async def scenario() -> None:
        await session.prompt("first")
        assert len(memory.pending_tasks) == 1
        assert memory.processed_tasks == []

        await session.prompt("second")

    asyncio.run(scenario())

    assert memory.queries == ["first", "second"]
    assert len(memory.processed_tasks) == 1
    assert memory.processed_tasks[0].evidence[0].text == "first"
    assert len(memory.pending_tasks) == 1
    assert memory.pending_tasks[0].evidence[0].text == "second"


def test_dispose_requests_worker_stop_and_aclose_waits_for_it(tmp_path: Path) -> None:
    memory = _MemoryService()
    session = AgentSession(AgentSessionConfig(
        model=_model(), cwd=str(tmp_path),
        memory_service=memory, memory_identity=MemoryIdentity("u", "p"),
    ))

    async def scenario() -> None:
        worker_started = asyncio.Event()

        async def worker() -> None:
            worker_started.set()
            await asyncio.Event().wait()

        memory.worker_task = asyncio.create_task(worker())
        await worker_started.wait()
        await session.aclose(memory_grace_seconds=0)

        assert memory.closed is True
        assert memory.stop_requested is True
        assert memory.worker_task.done()

    asyncio.run(scenario())

    other_memory = _MemoryService()
    other_session = AgentSession(AgentSessionConfig(
        model=_model(), cwd=str(tmp_path),
        memory_service=other_memory, memory_identity=MemoryIdentity("u", "p"),
    ))
    other_session.dispose()
    assert other_memory.stop_requested is True


def test_concurrent_prompt_is_rejected_while_memory_is_loading(tmp_path: Path) -> None:
    manager = SessionManager.create(cwd=str(tmp_path), in_memory=True)
    memory = _MemoryService()
    retrieval_started = asyncio.Event()
    allow_retrieval = asyncio.Event()
    session = AgentSession(AgentSessionConfig(
        model=_model(), cwd=str(tmp_path), session_manager=manager,
        memory_service=memory, memory_identity=MemoryIdentity("u", "p"),
    ))

    async def blocking_before_task(_identity, query: str) -> MemoryContext:
        memory.queries.append(query)
        retrieval_started.set()
        await allow_retrieval.wait()
        return MemoryContext(records=[], prompt_block="", estimated_tokens=0)

    async def fake_prompt(message: str) -> None:
        manager.append_message(UserMessage(content=message))
        assistant = AssistantMessage(content=[TextContent(text="完成")], stop_reason="stop")
        manager.append_message(assistant)
        session._last_assistant_message = assistant

    async def no_compaction() -> SimpleNamespace:
        return SimpleNamespace(need_retry=False)

    memory.before_task = blocking_before_task  # type: ignore[method-assign]
    session._agent.prompt = fake_prompt  # type: ignore[method-assign]
    session._compaction_orchestrator.check_compaction = no_compaction  # type: ignore[method-assign]

    async def scenario() -> None:
        first = asyncio.create_task(session.prompt("first"))
        await retrieval_started.wait()
        with pytest.raises(RuntimeError, match="already processing"):
            await session.prompt("second")
        allow_retrieval.set()
        await first

    asyncio.run(scenario())
    assert memory.queries == ["first"]


def test_plan_ready_is_not_memory_but_exact_execution_is(tmp_path: Path) -> None:
    memory = _MemoryService()
    session = AgentSession(AgentSessionConfig(
        model=_model(), cwd=str(tmp_path),
        memory_service=memory, memory_identity=MemoryIdentity("u", "p"),
    ))
    session.enter_plan_mode()
    plan_text = """<proposed_plan>
# Memory plan

## Summary
Use files.

## Implementation Changes
Add YAML and JSONL.

## Public Interfaces
Add /memory.

## Test Plan
Run tests.

## Assumptions
Local only.
</proposed_plan>"""
    session._capture_plan_revision(AssistantMessage(content=[TextContent(text=plan_text)]))
    latest = session.plan_state.latest_revision
    assert latest is not None
    assert memory.tasks == []

    async def fake_execute_prompt(_message: str) -> None:
        session._last_assistant_message = AssistantMessage(
            content=[TextContent(text="implemented")], stop_reason="stop",
        )

    session.prompt = fake_execute_prompt  # type: ignore[method-assign]
    asyncio.run(session.execute_plan(latest.plan_id, latest.revision, latest.digest))

    assert [task.mode for task in memory.tasks] == ["plan_accepted", "plan_completed"]
    assert memory.tasks[0].evidence[0].source_kind == "accepted_plan"
    assert memory.tasks[1].evidence[0].source_kind == "plan_completed"


def test_aborted_plan_execution_does_not_create_completed_memory(tmp_path: Path) -> None:
    memory = _MemoryService()
    session = AgentSession(AgentSessionConfig(
        model=_model(), cwd=str(tmp_path),
        memory_service=memory, memory_identity=MemoryIdentity("u", "p"),
    ))
    session.enter_plan_mode()
    plan_text = """<proposed_plan>
# Abort plan

## Summary
Try a change.

## Implementation Changes
Change one file.

## Public Interfaces
No changes.

## Test Plan
Run tests.

## Assumptions
Local only.
</proposed_plan>"""
    session._capture_plan_revision(AssistantMessage(content=[TextContent(text=plan_text)]))
    latest = session.plan_state.latest_revision
    assert latest is not None

    async def fake_execute_prompt(_message: str) -> None:
        session._last_assistant_message = AssistantMessage(
            content=[TextContent(text="aborted")], stop_reason="aborted",
        )

    session.prompt = fake_execute_prompt  # type: ignore[method-assign]
    asyncio.run(session.execute_plan(latest.plan_id, latest.revision, latest.digest))

    assert [task.mode for task in memory.tasks] == ["plan_accepted"]
    assert session.plan_state.phase == "aborted"
