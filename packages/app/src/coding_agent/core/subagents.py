"""Bounded, session-owned read-only investigation and review tasks.

Child transcripts are private to the task. Only explicit task briefs and
bounded final reports cross the boundary; no Plan authorization does.
"""
from __future__ import annotations

import asyncio
import copy
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from agent_core import Agent, AgentToolResult, SessionManager
from agent_core.session.types import SubagentTaskEntry
from agent_core.types import PlanAccess
from agent_llm import TextContent

ACTIVE = {"queued", "running"}
MAX_REPORT_CHARS = 16000


class SubagentError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class _LiveTask:
    snapshot: dict[str, Any]
    manager: SessionManager
    anchor: str
    agent: Agent
    task: asyncio.Task[None] | None = None
    budget_hit: bool = False


class SubagentManager:
    def __init__(
        self, *, session: Callable[[], SessionManager],
        make_agent: Callable[[], Agent], validate_spawn: Callable[[], None],
        emit: Callable[[dict], Any], max_active: int = 3,
        max_turns: int = 12, timeout_seconds: float = 180,
    ) -> None:
        self._session = session
        self._make_agent = make_agent
        self._validate_spawn = validate_spawn
        self._emit = emit
        self.max_active = max_active
        self.max_turns = max_turns
        self.timeout_seconds = timeout_seconds
        self._live: dict[str, _LiveTask] = {}

    def _owns(self, live: _LiveTask) -> bool:
        return self._session() is live.manager and any(
            entry.id == live.anchor for entry in live.manager.get_branch()
        )

    def snapshot(self) -> list[dict[str, Any]]:
        records: dict[str, dict] = {}
        for entry in self._session().get_branch():
            if isinstance(entry, SubagentTaskEntry):
                records[entry.task["taskId"]] = copy.deepcopy(entry.task)
        for task_id, record in records.items():
            live = self._live.get(task_id)
            if live is not None and self._owns(live):
                records[task_id] = copy.deepcopy(live.snapshot)
            elif record["status"] in ACTIVE:
                record["status"] = "uncertain"
                record["error"] = "当前 Runtime 不拥有该任务；请查看记录，不会自动重试。"
        return list(records.values())

    @property
    def has_active(self) -> bool:
        return any(item["status"] in ACTIVE for item in self.snapshot())

    def get(self, task_id: str) -> dict[str, Any]:
        result = next((item for item in self.snapshot() if item["taskId"] == task_id), None)
        if result is None:
            raise SubagentError("SUBAGENT_NOT_FOUND", "当前会话分支没有这个子代理任务")
        return result

    def spawn(self, prompt: str, purpose: str = "investigate") -> dict[str, Any]:
        self._validate_spawn()
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
            raise SubagentError("INVALID_SUBAGENT_TASK", "任务说明需要 1–12000 个字符")
        if purpose not in {"investigate", "review"}:
            raise SubagentError("INVALID_SUBAGENT_PURPOSE", "任务类型必须为 investigate 或 review")
        records = self.snapshot()
        if sum(record["status"] in ACTIVE for record in records) >= self.max_active:
            raise SubagentError("SUBAGENT_LIMIT", f"最多同时运行 {self.max_active} 个只读子代理")
        if len(records) >= 32:
            raise SubagentError("SUBAGENT_LIMIT", "当前分支已达到 32 个任务的上限")
        agent = self._make_agent()
        manager = self._session()
        snapshot = {
            "taskId": uuid.uuid4().hex, "sessionId": manager.header.id,
            "purpose": purpose, "prompt": prompt.strip(), "status": "queued",
            "turns": 0, "lastTool": None, "output": "", "error": None,
            "maxTurns": self.max_turns, "timeoutSeconds": self.timeout_seconds,
        }
        # Durable ownership precedes launch. No task starts if storage fails.
        manager.flush()
        anchor = manager.append_subagent_task(snapshot).id
        live = _LiveTask(snapshot=snapshot, manager=manager, anchor=anchor, agent=agent)
        self._live[snapshot["taskId"]] = live
        live.task = asyncio.create_task(self._drive(live))
        live.task.add_done_callback(lambda task: self._task_done(live, task))
        self._notify(live)
        return copy.deepcopy(snapshot)

    def _task_done(self, live: _LiveTask, task: asyncio.Task) -> None:
        if task.cancelled() and live.snapshot["status"] in ACTIVE:
            live.snapshot.update(status="cancelled", error="任务启动前已停止")
            try:
                self._record(live)
            except Exception:
                pass

    def _notify(self, live: _LiveTask) -> None:
        if self._owns(live):
            self._emit({
                "type": "subagent.stateChanged", "sessionId": live.manager.header.id,
                "tasks": self.snapshot(),
            })

    def _record(self, live: _LiveTask) -> None:
        if not self._owns(live):
            return
        try:
            live.manager.append_subagent_task(live.snapshot)
        except Exception as exc:
            live.snapshot["status"] = "uncertain"
            live.snapshot["error"] = f"子代理记录保存失败：{exc}"
            raise
        finally:
            self._notify(live)

    async def _drive(self, live: _LiveTask) -> None:
        def on_event(event, signal):
            if not self._owns(live):
                signal.set()
                return
            if event["type"] == "turn_start":
                if live.snapshot["turns"] >= self.max_turns:
                    live.budget_hit = True
                    signal.set()
                    raise SubagentError("SUBAGENT_TURN_LIMIT", "子代理已达到最大调查轮数")
                live.snapshot["turns"] += 1
                try:
                    self._record(live)
                except Exception:
                    signal.set()
            elif event["type"] == "tool_execution_running":
                live.snapshot["lastTool"] = event.get("tool_name")
                self._notify(live)

        unsubscribe = live.agent.subscribe(on_event)
        try:
            live.snapshot["status"] = "running"
            self._record(live)
            async with asyncio.timeout(self.timeout_seconds):
                await live.agent.prompt(
                    f"Task type: {live.snapshot['purpose']}\n\n{live.snapshot['prompt']}"
                )
            final = next((m for m in reversed(live.agent.state.messages) if getattr(m, "role", None) == "assistant"), None)
            output = "".join(getattr(block, "text", "") for block in getattr(final, "content", []) if getattr(block, "type", None) == "text")
            if len(output) > MAX_REPORT_CHARS:
                output = output[:MAX_REPORT_CHARS] + "\n[Report truncated at 16000 characters]"
            live.snapshot["output"] = output
            if live.snapshot["status"] == "uncertain":
                pass
            elif live.budget_hit:
                live.snapshot.update(status="failed", error="子代理已达到最大调查轮数；结果可能不完整")
            elif live.agent.last_run_status == "cancelled":
                live.snapshot.update(status="cancelled", error="任务已停止，结果可能不完整")
            elif live.agent.last_run_status != "completed" or getattr(final, "stop_reason", None) != "stop" or not output.strip():
                live.snapshot.update(status="failed", error=getattr(final, "error_message", None) or "子代理未生成完整报告")
            else:
                live.snapshot["status"] = "completed"
        except TimeoutError:
            live.snapshot.update(status="timed_out", error="子代理已达到时间上限")
        except asyncio.CancelledError:
            live.snapshot.update(status="cancelled", error="用户停止或来源会话已切换")
        except Exception as exc:
            if live.snapshot["status"] != "uncertain":
                live.snapshot.update(status="failed", error=str(exc))
        finally:
            unsubscribe()
            try:
                self._record(live)
            except Exception:
                pass  # _record already projects the storage failure as uncertain.

    async def wait(self, task_id: str, timeout: float = 20) -> dict[str, Any]:
        self.get(task_id)  # Enforce the active-branch boundary before waiting.
        if not isinstance(timeout, (int, float)) or not 0 <= timeout <= 30:
            raise SubagentError("INVALID_WAIT_TIMEOUT", "等待时间需要为 0–30 秒")
        live = self._live.get(task_id)
        if live is not None and live.task is not None and not live.task.done():
            await asyncio.wait({live.task}, timeout=timeout)
        return self.get(task_id)

    async def cancel(self, task_id: str) -> dict[str, Any]:
        snapshot = self.get(task_id)
        live = self._live.get(task_id)
        if snapshot["status"] in ACTIVE and live is not None and live.task is not None:
            await live.agent.abort()
            live.task.cancel()
            await asyncio.gather(live.task, return_exceptions=True)
            if live.snapshot["status"] in ACTIVE:  # cancelled before coroutine start
                live.snapshot.update(status="cancelled", error="用户停止了任务")
                self._record(live)
        return self.get(task_id)

    def reconcile(self) -> None:
        """Cancel work whose source branch/session is no longer active."""
        for live in self._live.values():
            if not self._owns(live) and live.task is not None and not live.task.done():
                live.task.cancel()

    async def aclose(self) -> None:
        for snapshot in self.snapshot():
            if snapshot["status"] in ACTIVE:
                await self.cancel(snapshot["taskId"])
        remaining = [live.task for live in self._live.values() if live.task is not None and not live.task.done()]
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


class SubagentTool:
    effect = "control"
    plan_access: PlanAccess = "control"
    execution_mode = "sequential"

    def __init__(self, manager: SubagentManager, action: str) -> None:
        self._manager, self._action = manager, action
        self.name = f"subagent_{action}"
        self.label = self.name
        descriptions = {
            "spawn": "Start an independent read-only investigation or review. Supply a self-contained brief, not full chat history. No shell, writes, or nested delegation. Collect its report using subagent_wait/status before relying on it.",
            "status": "List this branch's subagent tasks, or read one task's status and final report. Completed means the report ended, not that findings were verified.",
            "wait": "Wait up to 30 seconds for one read-only subagent; returns running if still active. Waiting never restarts a task.",
            "cancel": "Stop one read-only subagent owned by this session branch.",
        }
        self.description = descriptions[action]
        properties: dict[str, Any] = {"taskId": {"type": "string", "minLength": 1}}
        required = ["taskId"] if action in {"wait", "cancel"} else []
        if action == "spawn":
            properties = {
                "task": {"type": "string", "minLength": 1, "maxLength": 12000},
                "purpose": {"type": "string", "enum": ["investigate", "review"]},
            }
            required = ["task"]
        if action == "wait":
            properties["timeoutSeconds"] = {"type": "number", "minimum": 0, "maximum": 30}
        self.parameters = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    async def execute(self, tool_call_id: str, params: dict, signal=None, on_update=None) -> AgentToolResult:
        del tool_call_id, on_update
        if signal is not None and signal.is_set():
            return AgentToolResult(content=[TextContent(text="Operation cancelled")], status="cancelled")
        if self._action == "spawn":
            result = self._manager.spawn(params["task"], params.get("purpose", "investigate"))
        elif self._action == "status":
            result = self._manager.get(params["taskId"]) if params.get("taskId") else self._manager.snapshot()
        elif self._action == "wait":
            result = await self._manager.wait(params["taskId"], params.get("timeoutSeconds", 20))
        else:
            result = await self._manager.cancel(params["taskId"])
        if isinstance(result, list):
            result = [{**item, "prompt": item["prompt"][:200], "output": item["output"][:200]} for item in result]
        elif result["status"] in ACTIVE:
            result = {**result, "prompt": result["prompt"][:200]}
        return AgentToolResult(content=[TextContent(text=json.dumps(result, ensure_ascii=False))], details=result)
