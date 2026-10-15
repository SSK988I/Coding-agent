"""Queue inputs must survive normal LLM conversion and stay within their run."""
from __future__ import annotations

import asyncio

import pytest
from agent_llm import AssistantMessage, Model, ModelCost, TextContent, UserMessage

from agent_core import Agent, SessionManager


def make_agent(stream, **kwargs):
    return Agent(
        model=Model(id="test", cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0)),
        stream_fn=stream, **kwargs,
    )


class Stream:
    def __init__(self, final=None):
        self.final = final or AssistantMessage(content=[TextContent(text="done")])

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def result(self):
        return self.final


@pytest.mark.parametrize("method", ["steer", "follow_up"])
def test_string_queue_reaches_model_and_persisted_transcript(tmp_path, method):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        contexts = []

        class FirstStream(Stream):
            async def result(self):
                entered.set()
                await release.wait()
                return await super().result()

        def stream(_model, context, _options):
            contexts.append(list(context.messages))
            return FirstStream() if len(contexts) == 1 else Stream()

        manager = SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path)
        agent = make_agent(stream, session_manager=manager)
        running = asyncio.create_task(agent.prompt("original task"))
        await asyncio.wait_for(entered.wait(), 2)
        getattr(agent, method)("additional constraint")
        release.set()
        await asyncio.wait_for(running, 2)

        assert len(contexts) == 2
        assert [m.content for m in contexts[-1] if m.role == "user"] == [
            "original task", "additional constraint",
        ]
        restored = SessionManager.open(manager.path).build_session_context().messages
        assert [m.content for m in restored if m.role == "user"] == [
            "original task", "additional constraint",
        ]
        assert not agent.has_queued_messages()

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["aborted", "error", "cancelled"])
def test_unconsumed_messages_do_not_leak_into_next_run(outcome):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockedStream(Stream):
            async def result(self):
                entered.set()
                await release.wait()
                return AssistantMessage(stop_reason=outcome)

        agent = make_agent(lambda *_: BlockedStream())
        running = asyncio.create_task(agent.prompt("first task"))
        await asyncio.wait_for(entered.wait(), 2)
        agent.steer("old steering")
        agent.follow_up("old follow up")
        if outcome == "cancelled":
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        else:
            release.set()
            await asyncio.wait_for(running, 2)
        assert not agent.has_queued_messages()
        contexts = []

        def stream(_model, context, _options):
            contexts.append(list(context.messages))
            return Stream()

        before = len(agent.state.messages)
        agent.stream_fn = stream
        await agent.prompt("new task")
        assert len(contexts) == 1
        assert [m.content for m in agent.state.messages[before:] if m.role == "user"] == ["new task"]

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["reset", "load"])
def test_replacing_context_clears_pending_queues(operation):
    agent = make_agent(lambda *_: Stream())
    agent.steer(UserMessage(content="old session"))
    agent.follow_up("old session follow up")
    if operation == "reset":
        agent.reset()
    else:
        agent.load_messages([UserMessage(content="restored session")])
    assert not agent.has_queued_messages()


def test_run_stops_accepting_input_before_end_listeners_finish():
    async def scenario():
        agent = make_agent(lambda *_: Stream())
        observed = []

        async def listener(event, _signal):
            if event["type"] == "turn_start":
                observed.append(agent.is_accepting_messages)
            if event["type"] == "agent_end":
                observed.append(agent.is_accepting_messages)

        agent.subscribe(listener)
        assert not agent.is_accepting_messages
        await agent.prompt("task")
        assert observed == [True, False]
        assert not agent.is_accepting_messages

    asyncio.run(scenario())
