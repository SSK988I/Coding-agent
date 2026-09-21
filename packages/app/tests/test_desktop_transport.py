"""Exercise the actual NDJSON serving loop, not the renderer's fixture bridge."""
from __future__ import annotations

import asyncio
import json
import queue
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent_core import SessionManager
from agent_llm import AssistantMessage, Model, ModelCost, TextContent, UserMessage

from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.desktop import __main__ as sidecar
from coding_agent.desktop.runtime import DesktopRuntime


class Wire:
    def __init__(self):
        self.input = queue.Queue()
        self.responses = {}
        self.changed = asyncio.Event()

    def readline(self):
        return self.input.get(timeout=10)

    def write(self, line):
        payload = json.loads(line)
        if "id" in payload:
            self.responses[payload["id"]] = payload
            self.changed.set()

    def flush(self):
        pass

    def send(self, request_id, method, **params):
        self.input.put(json.dumps({"v": 1, "id": request_id, "method": method, "params": params}) + "\n")

    async def response(self, request_id):
        async with asyncio.timeout(2):
            while request_id not in self.responses:
                self.changed.clear()
                await self.changed.wait()
        return self.responses[request_id]

    def start(self, monkeypatch, runtime):
        monkeypatch.setattr(sidecar, "sys", SimpleNamespace(stdin=self, stdout=self, stderr=sys.stderr))
        monkeypatch.setattr(sidecar, "DesktopRuntime", lambda _emit: runtime)
        return asyncio.create_task(sidecar.serve())

    async def close(self, serving):
        self.input.put("")
        await asyncio.wait_for(serving, 3)


def test_stdio_stop_and_ping_interrupt_real_compaction(monkeypatch, tmp_path):
    async def scenario():
        wire = Wire()
        runtime = DesktopRuntime(lambda _: None)
        manager = SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path)
        manager.append_message(UserMessage(content="Keep this original context"))
        manager.append_message(AssistantMessage(content=[TextContent(text="Original answer")]))
        session = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), session_manager=manager,
            model=Model(id="test", cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0)),
        ))
        runtime._session = session
        original = list(session.state.messages)
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class BlockedStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def result(self):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        session.agent.stream_fn = lambda *_: BlockedStream()
        serving = wire.start(monkeypatch, runtime)
        try:
            wire.send("compact", "session.compact", direction="next phase")
            await asyncio.wait_for(entered.wait(), 2)
            wire.send("ping", "runtime.ping")
            assert "result" in await wire.response("ping")
            assert "compact" not in wire.responses
            approval = asyncio.get_running_loop().create_future()
            runtime._approvals["approval-1"] = approval
            wire.send("approve", "approval.resolve", approvalId="approval-1", approved=True)
            assert "result" in await wire.response("approve")
            assert approval.result() is True
            wire.send("stop", "run.abort")
            assert (await wire.response("stop"))["result"] == {"aborted": True}
            outcome = (await wire.response("compact"))["result"]
            assert not outcome["performed"] and "aborted" in outcome["error"].lower()
            assert cancelled.is_set()
            assert session.state.messages == original
            assert manager.get_latest_compaction_entry() is None
        finally:
            session.abort_compaction()
            await wire.close(serving)

    asyncio.run(scenario())


def test_sidecar_process_accepts_stop_over_real_pipes():
    # Only the slow operation is replaced. Requests pass through OS pipes, the
    # real process entrypoint, JSON parsing, dispatch and the real abort handler.
    bootstrap = textwrap.dedent('''
        import asyncio
        from types import SimpleNamespace
        from coding_agent.desktop import __main__ as sidecar
        from coding_agent.desktop.runtime import DesktopRuntime

        class Runtime(DesktopRuntime):
            def __init__(self, emit):
                super().__init__(emit)
                self.emit = emit
                self.stopped = asyncio.Event()
                self._session = SimpleNamespace(
                    is_compacting=False, compact=self.compact,
                    abort_compaction=self.stopped.set, aclose=self.close,
                )

            async def compact(self, *args, **kwargs):
                self._session.is_compacting = True
                self.emit({"v": 1, "test": "entered"})
                try:
                    await self.stopped.wait()
                    return {"performed": False, "error": "Compaction aborted"}
                finally:
                    self._session.is_compacting = False

            async def close(self):
                self.stopped.set()

        sidecar.DesktopRuntime = Runtime
        raise SystemExit(sidecar.main())
    ''')

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", bootstrap,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

        async def send(request_id, method):
            process.stdin.write((json.dumps({"v": 1, "id": request_id, "method": method}) + "\n").encode())
            await process.stdin.drain()

        async def receive():
            return json.loads(await asyncio.wait_for(process.stdout.readline(), 10))

        try:
            await send("compact", "session.compact")
            assert (await receive())["test"] == "entered"
            await send("ping", "runtime.ping")
            assert (await receive())["id"] == "ping"
            await send("stop", "run.abort")
            responses = [await receive(), await receive()]
            by_id = {response["id"]: response for response in responses}
            assert by_id["stop"]["result"] == {"aborted": True}
            assert by_id["compact"]["result"]["performed"] is False
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 5) == 0
            assert await process.stderr.read() == b""
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(scenario())


def test_stdio_subagent_wait_does_not_block_cancel_or_ordinary_requests(monkeypatch):
    async def scenario():
        wire = Wire()
        runtime = DesktopRuntime(lambda _: None)
        entered, stopped = asyncio.Event(), asyncio.Event()

        async def wait(_task_id, _timeout):
            entered.set()
            await stopped.wait()
            return {"status": "cancelled"}

        async def cancel(_task_id):
            stopped.set()
            return {"status": "cancelled"}

        runtime._session = SimpleNamespace(
            subagents=SimpleNamespace(wait=wait, cancel=cancel), aclose=AsyncMock(),
        )
        serving = wire.start(monkeypatch, runtime)
        try:
            wire.send("wait", "subagent.wait", taskId="child")
            await asyncio.wait_for(entered.wait(), 2)
            wire.send("commands", "command.list")
            assert "result" in await wire.response("commands")
            assert "wait" not in wire.responses
            wire.send("stop", "subagent.cancel", taskId="child")
            assert (await wire.response("stop"))["result"]["status"] == "cancelled"
            assert (await wire.response("wait"))["result"]["status"] == "cancelled"
        finally:
            stopped.set()
            await wire.close(serving)

    asyncio.run(scenario())


@pytest.mark.parametrize("shutdown", ["eof", "runtime.dispose"])
def test_stdio_serializes_mutations_and_cleans_up_before_dispose(monkeypatch, shutdown):
    async def scenario():
        wire = Wire()
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def dispatch(method, _params):
            calls.append(method)
            if method == "workspace.open":
                entered.set()
                try:
                    await release.wait()
                finally:
                    cancelled.set()
            if method == "runtime.dispose":
                await dispose()
            return {"ok": True}

        async def dispose():
            assert cancelled.is_set()
            calls.append("disposed")

        runtime = SimpleNamespace(dispatch=dispatch, dispose=dispose)
        serving = wire.start(monkeypatch, runtime)
        try:
            wire.send("open", "workspace.open")
            await asyncio.wait_for(entered.wait(), 2)
            wire.send("new", "session.new")
            wire.send("ping", "runtime.ping")
            await wire.response("ping")
            assert calls == ["workspace.open", "runtime.ping"]
            if shutdown == "runtime.dispose":
                wire.send("dispose", shutdown)
                await wire.response("dispose")
            else:
                wire.input.put("")
            await asyncio.wait_for(serving, 2)
            assert "session.new" not in calls
            assert calls.count("disposed") == 1
        finally:
            release.set()
            await wire.close(serving)

    asyncio.run(scenario())


def test_stdio_preserves_request_ids_errors_and_mutation_order(monkeypatch):
    async def scenario():
        wire = Wire()
        runtime = DesktopRuntime(lambda _: None)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        original_dispatch = runtime.dispatch

        async def dispatch(method, params):
            if method in {"workspace.open", "session.new"}:
                calls.append(method)
                if method == "workspace.open":
                    entered.set()
                    await release.wait()
                return {"method": method}
            if method == "test.error":
                raise ValueError("test failure")
            return await original_dispatch(method, params)

        monkeypatch.setattr(runtime, "dispatch", dispatch)
        serving = wire.start(monkeypatch, runtime)
        try:
            wire.send("open", "workspace.open")
            await asyncio.wait_for(entered.wait(), 2)
            wire.send("new", "session.new")
            wire.input.put("invalid json\n")
            assert (await wire.response(None))["error"]["code"] == "INVALID_JSON"
            wire.send("ping", "runtime.ping")
            await wire.response("ping")
            assert calls == ["workspace.open"]
            release.set()
            assert (await wire.response("new"))["result"] == {"method": "session.new"}
            assert calls == ["workspace.open", "session.new"]
            wire.send("missing", "not.a.method")
            assert "error" in await wire.response("missing")
            wire.send("error", "test.error")
            assert (await wire.response("error"))["error"]["code"] == "INTERNAL_ERROR"
        finally:
            release.set()
            await wire.close(serving)

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["run.steer", "run.followUp"])
@pytest.mark.parametrize("bind_ids", [True, False])
def test_stdio_queued_text_reaches_model_and_session(monkeypatch, tmp_path, method, bind_ids):
    async def scenario():
        wire = Wire()
        runtime = DesktopRuntime(lambda _: None)
        manager = SessionManager.create(cwd=str(tmp_path), agent_dir=tmp_path)
        session = AgentSession(AgentSessionConfig(
            cwd=str(tmp_path), session_manager=manager, tools=[],
            model=Model(id="test", cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0)),
        ))
        runtime._session = session
        entered, release = asyncio.Event(), asyncio.Event()
        ending, release_end = asyncio.Event(), asyncio.Event()
        contexts = []

        async def hold_end(event, _signal):
            if event["type"] == "agent_end":
                ending.set()
                await release_end.wait()

        session.agent.subscribe(hold_end)

        class Stream:
            def __init__(self, first):
                self.first = first

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def result(self):
                if self.first:
                    entered.set()
                    await release.wait()
                return AssistantMessage(content=[TextContent(text="done")])

        def stream(_model, context, _options):
            contexts.append(list(context.messages))
            return Stream(len(contexts) == 1)

        session.agent.stream_fn = stream
        serving = wire.start(monkeypatch, runtime)
        try:
            wire.send("idle", method, text="do not queue before a run")
            assert (await wire.response("idle"))["error"]["code"] == "NO_ACTIVE_RUN"
            wire.send("start", "run.start", text="original task")
            run_id = (await wire.response("start"))["result"]["runId"]
            await asyncio.wait_for(entered.wait(), 2)
            for index, (params, code) in enumerate([
                ({"runId": "previous-run"}, "STALE_RUN"),
                ({"sessionId": "previous-session"}, "STALE_RUN"),
                ({"runId": 1}, "INVALID_PARAMS"),
                ({"sessionId": ""}, "INVALID_PARAMS"),
            ]):
                request_id = f"invalid-{index}"
                wire.send(request_id, method, text="must not reach model", **params)
                assert (await wire.response(request_id))["error"]["code"] == code
            target = {"runId": run_id, "sessionId": manager.header.id} if bind_ids else {}
            wire.send("queue", method, text="added instruction", **target)
            assert (await wire.response("queue"))["result"]["queued"]
            running = runtime._run_task
            release.set()
            await asyncio.wait_for(ending.wait(), 2)
            wire.send("ending", method, text="too late for the model", runId=run_id)
            assert (await wire.response("ending"))["error"]["code"] == "RUN_NOT_ACCEPTING_MESSAGES"
            release_end.set()
            await asyncio.wait_for(running, 2)
            assert len(contexts) == 2
            assert [m.content for m in contexts[-1] if m.role == "user"] == [
                "original task", "added instruction",
            ]
            restored = SessionManager.open(manager.path).build_session_context().messages
            assert [m.content for m in restored if m.role == "user"] == ["original task", "added instruction"]
            wire.send("late", method, text="do not send to the next task", runId=run_id)
            assert (await wire.response("late"))["error"]["code"] == "NO_ACTIVE_RUN"
        finally:
            release.set()
            release_end.set()
            await wire.close(serving)

    asyncio.run(scenario())
