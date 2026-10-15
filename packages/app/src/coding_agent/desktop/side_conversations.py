"""User-owned side conversations, independent of the main Agent run."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Callable, cast
import uuid

from agent_core import BeforeToolCallContext, BeforeToolCallResult, SessionManager
from coding_agent.core.agent_session import AgentSession, AgentSessionConfig, _create_plan_observation_tools
from coding_agent.core.permissions import PermissionMode, requires_approval, is_approval_safe_shell_command
from coding_agent.core.plan_mode import enforce_plan_tool_policy
from coding_agent.desktop.protocol import RpcError, to_jsonable

CHILD_TOOLS = {"read", "write", "edit", "bash", "grep", "find", "ls", "git_status", "git_log", "git_diff", "git_show", "web_search"}


@dataclass
class SideConversation:
    session: AgentSession
    status: str = "idle"
    error: str | None = None
    streaming: str = ""
    task: asyncio.Task | None = None
    approval: dict[str, Any] | None = None
    decision: asyncio.Future[bool] | None = None
    approval_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    native_search_decision: bool | None = None


class SideConversations:
    def __init__(self, parent: AgentSession, storage: Path, emit: Callable, permission: Callable) -> None:
        self.parent = parent
        self.storage = storage / "chats"
        self.emit = emit
        self.permission = permission
        self.records: dict[str, SideConversation] = {}
        self.closed = False

    def _snapshot(self, live: SideConversation) -> dict[str, Any]:
        return {
            "conversationId": live.session.session_manager.header.id,
            "status": live.status, "error": live.error, "streaming": live.streaming,
            "messages": to_jsonable(live.session.state.messages), "approval": live.approval,
        }

    def _changed(self, live: SideConversation) -> None:
        if not self.closed:
            self.emit("workbench.chat.changed", self._snapshot(live))

    def _status_file(self, live: SideConversation) -> Path:
        return self.storage / f"{live.session.session_manager.header.id}.status.json"

    def _persist_status(self, live: SideConversation) -> None:
        self.storage.mkdir(parents=True, exist_ok=True)
        target = self._status_file(live)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"status": live.status, "error": live.error}), encoding="utf-8")
        temporary.replace(target)

    def list(self) -> list[dict[str, Any]]:
        saved = {item.id: {"conversationId": item.id, "title": item.first_message[:80] or "侧边对话"} for item in SessionManager.list_sessions(cwd=self.parent.cwd, sessions_dir=self.storage)}
        for identity, live in self.records.items():
            saved.setdefault(identity, {"conversationId": identity, "title": "侧边对话"})
            first = next((message for message in live.session.state.messages if getattr(message, "role", None) == "user"), None)
            if first is not None:
                content = getattr(first, "content", "")
                if isinstance(content, str):
                    saved[identity]["title"] = content[:80]
            saved[identity]["status"] = live.status
        return list(saved.values())[::-1]

    def open(self, identity: str | None = None) -> dict[str, Any]:
        if self.closed:
            raise RpcError("STALE_WORKSPACE", "会话已经切换")
        if identity is not None and identity in self.records:
            return self._snapshot(self.records[identity])
        if len(self.records) >= 16:
            raise RpcError("SIDE_CHAT_LIMIT", "最多同时打开 16 个侧边对话")
        manager = None
        if identity is not None:
            match = next((item for item in SessionManager.list_sessions(cwd=self.parent.cwd, sessions_dir=self.storage) if item.id == identity), None)
            if match is None:
                raise RpcError("SIDE_CHAT_NOT_FOUND", "侧边对话不存在")
            manager = SessionManager.open(match.path, sessions_dir=self.storage)
            if manager.header.parent_session != self.parent.session_manager.header.id:
                raise RpcError("SIDE_CHAT_NOT_FOUND", "侧边对话来源不匹配")
        manager = manager or SessionManager.create(cwd=self.parent.cwd, sessions_dir=self.storage, parent_session=self.parent.session_manager.header.id)
        # Inherit the live development surface, excluding parent controls and
        # delegation. Core's normal Plan registration has already filtered it.
        inherited = [tool for tool in self.parent.tools if tool.name in CHILD_TOOLS]
        observations = {tool.name: tool for tool in _create_plan_observation_tools(inherited, cwd=self.parent.cwd)}
        tools = [observations.get(tool.name, tool) if tool.name in {"grep", "find"} else tool for tool in inherited]
        child = AgentSession(AgentSessionConfig(
            model=self.parent.model, cwd=self.parent.cwd, tools=tools,
            session_manager=manager, no_builtin_tools=True,
            get_api_key=self.parent._config.get_api_key, reasoning=self.parent.agent.reasoning,
            context_files=self.parent._config.context_files, retry_policy=self.parent.retry_policy,
            memory_configured=False,
            permission_mode=cast(PermissionMode, self.permission()),
            append_system_prompt="This is a user-directed side conversation. Work independently. Its messages and results are not injected into the main conversation.",
        ))
        live = SideConversation(child)
        child._config.before_tool_call = lambda context, signal: self._approve(live, context, signal)
        child._config.before_native_web_search = lambda signal: self._approve_native_search(live, signal)
        child.on_event(lambda event: self._on_event(live, event))
        try:
            saved_status = json.loads(self._status_file(live).read_text(encoding="utf-8"))
            live.status = saved_status.get("status", "idle")
            live.error = saved_status.get("error")
            if live.status == "running":
                live.status = "uncertain"
                live.error = "先前的运行没有可确认结果，可查看记录或发送新消息继续。"
        except (OSError, ValueError):
            pass
        self.records[manager.header.id] = live
        return self._snapshot(live)

    def _live(self, identity: Any) -> SideConversation:
        if self.closed or not isinstance(identity, str) or identity not in self.records:
            raise RpcError("SIDE_CHAT_NOT_FOUND", "请先打开侧边对话")
        return self.records[identity]

    def send(self, identity: Any, text: Any) -> dict[str, Any]:
        live = self._live(identity)
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise RpcError("INVALID_PARAMS", "侧边消息需为 1–16000 字符")
        if live.task is not None and not live.task.done():
            raise RpcError("SIDE_CHAT_RUNNING", "侧边对话正在运行")
        if sum(item.status == "running" for item in self.records.values()) >= 3:
            raise RpcError("SIDE_CHAT_CAPACITY", "最多同时运行 3 个侧边对话")
        previous = live.status, live.error, live.streaming
        live.status, live.error, live.streaming = "running", None, ""
        try:
            self._persist_status(live)
        except OSError as exc:
            live.status, live.error, live.streaming = previous
            raise RpcError("SIDE_CHAT_SAVE_FAILED", "侧边对话保存失败，未启动运行") from exc
        live.native_search_decision = None
        live.session._config.permission_mode = cast(PermissionMode, self.permission())
        live.task = asyncio.create_task(self._drive(live, text.strip()))
        self._changed(live)
        return self._snapshot(live)

    async def _drive(self, live: SideConversation, text: str) -> None:
        try:
            async with asyncio.timeout(300):
                await live.session.prompt(text)
            live.status = "cancelled" if live.session.agent.last_run_status == "cancelled" else "failed" if live.session.agent.last_run_status == "failed" else "settled"
            live.error = live.session.agent.state.error_message
        except asyncio.CancelledError:
            live.status = "cancelled"
        except TimeoutError:
            live.status, live.error = "failed", "侧边对话运行已超时，可发送新消息继续"
        except Exception as exc:  # noqa: BLE001 - side conversation boundary
            live.status, live.error = "failed", str(exc)
        finally:
            live.streaming = ""
            live.approval = None
            try:
                live.session.session_manager.flush()
                self._persist_status(live)
            except OSError as exc:
                live.status, live.error = "uncertain", f"侧边对话保存失败：{exc}"
            self._changed(live)

    def _on_event(self, live: SideConversation, event: dict) -> None:
        if event.get("type") == "message_update":
            raw = event.get("event", {})
            if raw.get("type") == "text_delta":
                live.streaming += str(raw.get("delta", ""))
        elif event.get("type") == "message_end":
            live.streaming = ""
        self._changed(live)

    async def _approve(self, live: SideConversation, context: BeforeToolCallContext, signal: asyncio.Event) -> BeforeToolCallResult | None:
        # Agent may request several tools in parallel. Each needs its own decision;
        # a second request must not overwrite the visible approval or its Future.
        async with live.approval_lock:
            if signal.is_set():
                return BeforeToolCallResult(block=True, reason="侧边对话已停止", status="cancelled")
            return await self._approve_one(live, context, signal)

    async def _parent_policy(self, context: BeforeToolCallContext, signal: asyncio.Event) -> BeforeToolCallResult | None:
        if signal.is_set():
            return BeforeToolCallResult(block=True, reason="侧边对话已停止", status="cancelled")
        if self.closed or context.tool_call.name not in {tool.name for tool in self.parent.tools}:
            return BeforeToolCallResult(block=True, reason="主会话当前不允许此工具", code="PARENT_TOOL_UNAVAILABLE", status="blocked")
        if self.parent.collaboration_mode == "plan" or self.parent.plan_state.phase in {"uncertain", "recovery_error"}:
            return await enforce_plan_tool_policy(context, self.parent.cwd, phase=self.parent.plan_state.phase)
        return None

    async def _approve_one(self, live: SideConversation, context: BeforeToolCallContext, signal: asyncio.Event) -> BeforeToolCallResult | None:
        blocked = await self._parent_policy(context, signal)
        if blocked is not None:
            return blocked
        if requires_approval(self.permission(), context.tool_call.name, context.args, Path(self.parent.cwd), is_approval_safe_shell_command):
            if not await self._request_decision(live, context.tool_call.name, context.args, signal):
                return BeforeToolCallResult(block=True, reason="用户未批准侧边对话操作", status="cancelled" if signal.is_set() else "blocked")
        # Approval cleanup awaits; the parent can enter Plan or close meanwhile.
        return await self._parent_policy(context, signal)

    def _native_search_allowed(self, signal: asyncio.Event) -> bool:
        return (
            not self.closed and not signal.is_set()
            and self.parent.collaboration_mode == "default"
            and self.parent.plan_state.phase not in {"uncertain", "recovery_error"}
            and any(tool.name == "web_search" for tool in self.parent.tools)
        )

    async def _approve_native_search(self, live: SideConversation, signal: asyncio.Event) -> bool:
        # Provider-native search never passes through before_tool_call. Share the
        # side chat's approval queue, but revalidate the parent for every request.
        async with live.approval_lock:
            if not self._native_search_allowed(signal):
                return False
            if live.native_search_decision is None:
                live.native_search_decision = (
                    await self._request_decision(live, "web_search", {"native": True}, signal)
                    if requires_approval(self.permission(), "web_search", {}, Path(self.parent.cwd), is_approval_safe_shell_command)
                    else True
                )
            return live.native_search_decision and self._native_search_allowed(signal)

    async def _request_decision(self, live: SideConversation, tool_name: str, args: dict[str, Any], signal: asyncio.Event) -> bool:
        live.decision = asyncio.get_running_loop().create_future()
        live.approval = {"approvalId": uuid.uuid4().hex, "toolName": tool_name, "args": args}
        self._changed(live)
        stopped = asyncio.create_task(signal.wait())
        try:
            done, _ = await asyncio.wait({live.decision, stopped}, timeout=120, return_when=asyncio.FIRST_COMPLETED)
            return live.decision in done and live.decision.result() and not signal.is_set()
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
            if live.decision is not None and not live.decision.done():
                live.decision.cancel()
            live.decision = None
            live.approval = None
            self._changed(live)

    def resolve(self, identity: Any, approval_id: Any, allowed: Any) -> dict[str, Any]:
        live = self._live(identity)
        if not isinstance(allowed, bool) or not live.approval or live.approval["approvalId"] != approval_id or live.decision is None or live.decision.done():
            raise RpcError("APPROVAL_NOT_FOUND", "侧边审批已失效")
        live.decision.set_result(allowed)
        return {"accepted": True}

    async def stop(self, identity: Any) -> dict[str, Any]:
        live = self._live(identity)
        if live.task is not None and not live.task.done():
            await live.session.abort()
            live.task.cancel()
            await asyncio.gather(live.task, return_exceptions=True)
            self._cancel_before_start(live)
            self._changed(live)
        return self._snapshot(live)

    def _cancel_before_start(self, live: SideConversation) -> None:
        # Cancelling a newly scheduled Task before its first step skips _drive's
        # finally block. Explicit stop must still publish/persist a terminal state.
        if live.status == "running" and live.task is not None and live.task.done():
            live.status, live.error, live.streaming = "cancelled", None, ""
            try:
                self._persist_status(live)
            except OSError as exc:
                live.status, live.error = "uncertain", f"侧边对话保存失败：{exc}"

    async def close(self) -> None:
        self.closed = True
        for live in self.records.values():
            if live.task is not None and not live.task.done():
                live.task.cancel()
        await asyncio.gather(*(live.task for live in self.records.values() if live.task is not None), return_exceptions=True)
        for live in self.records.values():
            self._cancel_before_start(live)
        await asyncio.gather(*(live.session.aclose() for live in self.records.values()), return_exceptions=True)
