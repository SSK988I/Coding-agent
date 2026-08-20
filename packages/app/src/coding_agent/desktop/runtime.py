"""Long-running application runtime exposed to the Electron desktop shell."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import shlex
import time
from typing import Any, Awaitable, Callable, cast
import uuid

from agent_core import BeforeToolCallContext, BeforeToolCallResult, SessionManager
from agent_llm import ThinkingLevel, create_models

from coding_agent.cli.main import _resolve_api_key_for
from coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from coding_agent.core.config import get_agent_dir, get_auth_path, get_sessions_dir
from coding_agent.core.context_files import load_project_context_files
from coding_agent.core.credentials import CredentialStore
from coding_agent.core.providers import _all_providers, get_configured_models
from coding_agent.core.retry import RetryPolicy
from coding_agent.core.settings import SettingsManager
from coding_agent.core.slash_commands import get_active_commands
from coding_agent.desktop.protocol import PROTOCOL_VERSION, RpcError, to_jsonable

EventEmitter = Callable[[dict[str, Any]], None]
_APPROVAL_TOOLS = {"bash", "write", "edit"}
_READ_ONLY_COMMANDS = {
    "cat", "du", "file", "find", "grep", "head", "ls", "pwd", "rg",
    "stat", "tail", "wc",
}
_READ_ONLY_GIT_SUBCOMMANDS = {
    "diff", "grep", "log", "ls-files", "rev-parse", "show", "status",
}
_DANGEROUS_FIND_FLAGS = {
    "-delete", "-exec", "-execdir", "-fls", "-fprint", "-fprintf", "-ok", "-okdir",
}
_SHELL_SPLIT_RE = re.compile(r"\s*(?:&&|\|)\s*")
_DESKTOP_COMMANDS = {
    "help": ("帮助", "查看桌面端可用命令"),
    "new": ("新会话", "创建一个新的会话"),
    "model": ("模型", "选择或切换当前模型"),
    "compact": ("压缩上下文", "立即压缩较早的会话内容"),
    "clear": ("清空对话", "清空当前窗口和 Agent 上下文"),
    "session": ("会话状态", "查看当前会话统计信息"),
}


class DesktopRuntime:
    """Own one live workspace/session and translate RPC commands to AgentSession."""

    def __init__(self, emit: EventEmitter, *, approval_timeout_seconds: float = 120.0) -> None:
        self._emit_raw = emit
        self._session: AgentSession | None = None
        self._workspace: Path | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._run_id: str | None = None
        self._seq = 0
        self._approvals: dict[str, asyncio.Future[bool]] = {}
        self._approval_timeout_seconds = approval_timeout_seconds

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
            "runtime.ping": self._ping,
            "workspace.open": self._workspace_open,
            "session.list": self._session_list,
            "session.new": self._session_new,
            "session.open": self._session_open,
            "session.snapshot": self._session_snapshot,
            "session.clear": self._session_clear,
            "command.list": self._command_list,
            "model.list": self._model_list,
            "model.select": self._model_select,
            "run.start": self._run_start,
            "run.abort": self._run_abort,
            "run.steer": self._run_steer,
            "run.followUp": self._run_follow_up,
            "approval.resolve": self._approval_resolve,
            "session.compact": self._session_compact,
            "runtime.dispose": self._dispose_command,
        }
        handler = handlers.get(method)
        if handler is None:
            raise RpcError("METHOD_NOT_FOUND", f"未知方法：{method}")
        return await handler(params)

    async def _ping(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        return {"protocolVersion": PROTOCOL_VERSION, "status": "ready"}

    async def _workspace_open(self, params: dict[str, Any]) -> dict[str, Any]:
        raw_path = params.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise RpcError("INVALID_PARAMS", "workspace.open 需要 path")
        workspace = Path(raw_path).expanduser().resolve()
        if not workspace.is_dir():
            raise RpcError("WORKSPACE_NOT_FOUND", f"目录不存在：{workspace}")
        session_id = params.get("sessionId")
        resume = bool(params.get("resume", True))
        await self._replace_session(workspace, session_id=session_id, resume=resume)
        return self._workspace_payload()

    async def _replace_session(
        self,
        workspace: Path,
        *,
        session_id: str | None = None,
        resume: bool = False,
    ) -> None:
        await self._stop_active_run()
        self._dispose_session()

        settings_manager = SettingsManager()
        settings = settings_manager.load()
        provider_id = settings.default_provider or "deepseek"
        providers = _all_providers()
        provider = next((item for item in providers if item.id == provider_id), None)
        if provider is None:
            provider = next((item for item in providers if item.id == "deepseek"), None)
        if provider is None and providers:
            provider = providers[0]
        if provider is None:
            raise RpcError("NO_PROVIDER", "没有可用的模型 Provider")

        available_models = provider.get_models()
        model = next((item for item in available_models if item.id == settings.default_model), None)
        if model is None and available_models:
            model = available_models[0]
        if model is None:
            raise RpcError("NO_MODEL", f"Provider {provider.id} 没有可用模型")
        models = create_models()
        models.set_provider(provider)

        manager = self._select_session_manager(workspace, session_id=session_id, resume=resume)
        persisted = manager.build_session_context()
        if persisted.model:
            persisted_provider = persisted.model.get("provider")
            persisted_model_id = persisted.model.get("model_id")
            persisted_provider_obj = next((item for item in providers if item.id == persisted_provider), None)
            if persisted_provider_obj is not None:
                persisted_model = next(
                    (item for item in persisted_provider_obj.get_models() if item.id == persisted_model_id),
                    None,
                )
                if persisted_model is not None:
                    provider = persisted_provider_obj
                    model = persisted_model
                    models.set_provider(provider)

        reasoning_value = persisted.thinking_level or settings.thinking_level
        reasoning: ThinkingLevel | None = None
        if reasoning_value and reasoning_value != "off":
            reasoning = cast(ThinkingLevel, reasoning_value)

        agent_dir = get_agent_dir()
        try:
            context_files = load_project_context_files(str(workspace), agent_dir) or None
        except Exception:
            context_files = None

        config = AgentSessionConfig(
            model=model,
            cwd=str(workspace),
            session_manager=manager,
            get_api_key=lambda current_provider: _resolve_api_key_for(current_provider),
            reasoning=reasoning,
            context_files=context_files,
            before_tool_call=self._before_tool_call,
            retry_policy=RetryPolicy(
                enabled=settings.auto_retry,
                max_retries=settings.max_retries,
                initial_delay=settings.retry_initial_delay,
                max_delay=settings.retry_max_delay,
            ),
            settings_manager=settings_manager,
            theme_name=settings.theme,
        )
        self._workspace = workspace
        self._session = AgentSession(config)
        self._unsubscribe = self._session.on_event(self._on_session_event)

    def _select_session_manager(
        self,
        workspace: Path,
        *,
        session_id: str | None,
        resume: bool,
    ) -> SessionManager:
        sessions_dir = get_sessions_dir()
        available = SessionManager.list_sessions(cwd=str(workspace), sessions_dir=sessions_dir)
        if session_id:
            match = next((item for item in available if item.id == session_id), None)
            if match is None:
                raise RpcError("SESSION_NOT_FOUND", f"找不到会话：{session_id}")
            return SessionManager.open(match.path, sessions_dir=sessions_dir)
        if resume:
            recent = SessionManager.continue_recent(cwd=str(workspace), sessions_dir=sessions_dir)
            if recent is not None:
                return recent
        return SessionManager.create(cwd=str(workspace), sessions_dir=sessions_dir)

    async def _session_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        del params
        workspace = self._require_workspace()
        infos = SessionManager.list_sessions(cwd=str(workspace), sessions_dir=get_sessions_dir())
        return [to_jsonable(item) for item in infos]

    async def _session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        workspace = self._require_workspace()
        await self._replace_session(workspace, resume=False)
        self._publish("session.changed", self._workspace_payload())
        return self._workspace_payload()

    async def _session_open(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise RpcError("INVALID_PARAMS", "session.open 需要 sessionId")
        workspace = self._require_workspace()
        await self._replace_session(workspace, session_id=session_id)
        self._publish("session.changed", self._workspace_payload())
        return self._workspace_payload()

    async def _session_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        session = self._require_session()
        return {
            "sessionId": session.session_manager.header.id,
            "messages": to_jsonable(session.state.messages),
            "stats": to_jsonable(session.get_stats()),
        }

    async def _session_clear(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        if self._run_task is not None and not self._run_task.done():
            raise RpcError("RUN_IN_PROGRESS", "任务运行时不能清空会话")
        self._require_session().agent.reset()
        payload = self._workspace_payload()
        self._publish("session.changed", payload)
        return payload

    async def _command_list(self, params: dict[str, Any]) -> list[dict[str, str]]:
        del params
        return [
            {
                "name": command.name,
                "label": _DESKTOP_COMMANDS[command.name][0],
                "description": _DESKTOP_COMMANDS[command.name][1],
            }
            for command in get_active_commands()
            if command.name in _DESKTOP_COMMANDS
        ]

    def _available_models(self) -> list[Any]:
        credentials = CredentialStore(get_auth_path()).load()
        available = get_configured_models(stored_keys=credentials, env=dict(os.environ))
        session = self._require_session()
        if session.model and not any(
            model.id == session.model.id and model.provider == session.model.provider
            for model in available
        ):
            available.insert(0, session.model)
        return available

    async def _model_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        del params
        session = self._require_session()
        return [
            {
                "id": model.id,
                "name": model.name or model.id,
                "provider": model.provider,
                "current": (
                    model.id == session.model.id
                    and model.provider == session.model.provider
                ),
            }
            for model in self._available_models()
        ]

    async def _model_select(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._run_task is not None and not self._run_task.done():
            raise RpcError("RUN_IN_PROGRESS", "任务运行时不能切换模型")
        model_id = params.get("modelId")
        provider_id = params.get("provider")
        if not isinstance(model_id, str) or not isinstance(provider_id, str):
            raise RpcError("INVALID_PARAMS", "model.select 需要 modelId 和 provider")
        chosen = next(
            (
                model for model in self._available_models()
                if model.id == model_id and model.provider == provider_id
            ),
            None,
        )
        if chosen is None:
            raise RpcError("MODEL_NOT_FOUND", "模型不存在或 Provider 尚未配置凭据")
        self._require_session().set_model(chosen)
        payload = {
            "id": chosen.id,
            "name": chosen.name or chosen.id,
            "provider": chosen.provider,
        }
        self._publish("model.changed", payload)
        return payload

    async def _run_start(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session()
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RpcError("INVALID_PARAMS", "run.start 需要非空 text")
        if text.lstrip().startswith("/"):
            raise RpcError("SLASH_COMMAND", "斜杠命令需要由桌面端处理，不能作为提示词发送给模型")
        if self._run_task is not None and not self._run_task.done():
            raise RpcError("RUN_IN_PROGRESS", "当前已有任务正在运行")
        run_id = uuid.uuid4().hex
        self._run_id = run_id
        self._run_task = asyncio.create_task(self._drive_run(session, text, run_id))
        return {"accepted": True, "runId": run_id}

    async def _drive_run(self, session: AgentSession, text: str, run_id: str) -> None:
        self._publish("run.started", {"text": text}, run_id=run_id)
        try:
            await session.prompt(text)
        except asyncio.CancelledError:
            self._publish("run.cancelled", {}, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - runtime boundary
            self._publish("run.failed", {"message": str(exc)}, run_id=run_id)
        else:
            self._publish(
                "run.completed",
                {"stats": to_jsonable(session.get_stats())},
                run_id=run_id,
            )
        finally:
            if self._run_id == run_id:
                self._run_id = None
                self._run_task = None

    async def _run_abort(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        if self._session is None or self._run_task is None or self._run_task.done():
            return {"aborted": False}
        await self._abort_active_run()
        return {"aborted": True}

    async def _run_steer(self, params: dict[str, Any]) -> dict[str, Any]:
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RpcError("INVALID_PARAMS", "run.steer 需要非空 text")
        session = self._require_session()
        session.agent.steer(text)
        return {"queued": True}

    async def _run_follow_up(self, params: dict[str, Any]) -> dict[str, Any]:
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RpcError("INVALID_PARAMS", "run.followUp 需要非空 text")
        session = self._require_session()
        session.agent.follow_up(text)
        return {"queued": True}

    async def _before_tool_call(
        self,
        context: BeforeToolCallContext,
        signal: asyncio.Event,
    ) -> BeforeToolCallResult | None:
        if context.tool_call.name not in _APPROVAL_TOOLS:
            return None
        if context.tool_call.name == "bash" and _is_read_only_bash_command(
            str(context.args.get("command", "")),
        ):
            return None
        approval_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._approvals[approval_id] = future
        self._publish(
            "approval.requested",
            {
                "approvalId": approval_id,
                "toolCallId": context.tool_call.id,
                "toolName": context.tool_call.name,
                "args": context.args,
            },
        )
        signal_task = asyncio.create_task(signal.wait())
        try:
            done, _ = await asyncio.wait(
                {future, signal_task},
                timeout=self._approval_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                self._publish(
                    "approval.expired",
                    {"approvalId": approval_id, "toolCallId": context.tool_call.id},
                )
                return BeforeToolCallResult(block=True, reason="工具审批已超时")
            approved = future in done and future.result()
            if approved:
                return None
            reason = "运行已停止" if signal.is_set() else "用户拒绝了工具执行"
            return BeforeToolCallResult(block=True, reason=reason)
        finally:
            signal_task.cancel()
            self._approvals.pop(approval_id, None)

    async def _approval_resolve(self, params: dict[str, Any]) -> dict[str, Any]:
        approval_id = params.get("approvalId")
        approved = params.get("approved")
        if not isinstance(approval_id, str) or not isinstance(approved, bool):
            raise RpcError("INVALID_PARAMS", "approval.resolve 需要 approvalId 和 approved")
        future = self._approvals.get(approval_id)
        if future is None or future.done():
            raise RpcError("APPROVAL_NOT_FOUND", "审批已失效或不存在")
        future.set_result(approved)
        return {"resolved": True}

    async def _session_compact(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        return await self._require_session().compact("manual")

    async def _dispose_command(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        await self.dispose()
        return {"disposed": True}

    async def dispose(self) -> None:
        await self._stop_active_run()
        for future in self._approvals.values():
            if not future.done():
                future.set_result(False)
        self._approvals.clear()
        self._dispose_session()

    async def _stop_active_run(self) -> None:
        if self._session is not None and self._run_task is not None and not self._run_task.done():
            await self._abort_active_run()
        self._run_task = None
        self._run_id = None

    async def _abort_active_run(self) -> None:
        """Prefer a balanced agent shutdown, then hard-cancel as a fallback.

        The grace window lets an approval hook return an aborted ToolResult so
        the persisted transcript never ends at a bare assistant tool call.
        """
        if self._session is None or self._run_task is None:
            return
        task = self._run_task
        await self._session.abort()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.75)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _dispose_session(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._session is not None:
            self._session.dispose()
            self._session = None

    def _on_session_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", "agent.event"))
        if event_type == "message_update":
            raw = event.get("event") or {}
            payload = {
                "kind": raw.get("type"),
                "contentIndex": raw.get("content_index"),
                "delta": raw.get("delta"),
            }
        else:
            payload = {key: value for key, value in event.items() if key != "type"}
        self._publish(event_type, payload)

    def _publish(self, event_type: str, payload: Any, *, run_id: str | None = None) -> None:
        self._seq += 1
        session_id = None
        if self._session is not None:
            session_id = self._session.session_manager.header.id
        self._emit_raw({
            "v": PROTOCOL_VERSION,
            "type": "event",
            "seq": self._seq,
            "timestamp": int(time.time() * 1000),
            "sessionId": session_id,
            "runId": run_id if run_id is not None else self._run_id,
            "event": {"type": event_type, "payload": to_jsonable(payload)},
        })

    def _workspace_payload(self) -> dict[str, Any]:
        session = self._require_session()
        return {
            "path": str(self._require_workspace()),
            "sessionId": session.session_manager.header.id,
            "model": to_jsonable(session.model),
            "thinkingLevel": session.thinking_level,
            "tools": [tool.name for tool in session.tools],
            "messages": to_jsonable(session.state.messages),
        }

    def _require_workspace(self) -> Path:
        if self._workspace is None:
            raise RpcError("WORKSPACE_REQUIRED", "请先打开工作区")
        return self._workspace

    def _require_session(self) -> AgentSession:
        if self._session is None:
            raise RpcError("WORKSPACE_REQUIRED", "请先打开工作区")
        return self._session


def _is_read_only_bash_command(command: str) -> bool:
    """Conservatively recognize shell pipelines that cannot mutate state.

    This is intentionally an allowlist, not a complete shell parser. Anything
    involving redirection, substitution, alternate control flow, absolute
    paths, parent traversal, or an unknown executable still requires approval.
    """
    command = command.strip()
    if not command or any(
        token in command
        for token in (";", "||", "`", "$(", ">", "<", "\n", "\r")
    ):
        return False
    if re.search(r"(?:^|\s)\.\.(?:[\\/]|(?:\s|$))", command):
        return False

    segments = _SHELL_SPLIT_RE.split(command)
    if not segments or any(not segment.strip() for segment in segments):
        return False

    for segment in segments:
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            return False
        if not tokens:
            return False
        executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if executable.endswith(".exe"):
            executable = executable[:-4]

        if executable == "cd":
            if len(tokens) != 2 or not _is_workspace_relative_token(tokens[1]):
                return False
            continue
        if executable == "git":
            if len(tokens) < 2 or tokens[1].lower() not in _READ_ONLY_GIT_SUBCOMMANDS:
                return False
            if any(
                token in {"-c", "-o", "--paginate"}
                or token.startswith(("--exec-path", "--output", "--open-files-in-pager"))
                for token in tokens[2:]
            ):
                return False
        elif executable not in _READ_ONLY_COMMANDS:
            return False

        if executable == "find" and any(token.lower() in _DANGEROUS_FIND_FLAGS for token in tokens[1:]):
            return False
        if executable == "rg" and any(token == "--pre" or token.startswith("--pre=") for token in tokens[1:]):
            return False
        if any(not _is_workspace_relative_token(token) for token in tokens[1:] if _looks_like_path(token)):
            return False
    return True


def _looks_like_path(token: str) -> bool:
    if token.startswith("-"):
        return False
    return "/" in token or "\\" in token or token in {".", ".."} or bool(re.match(r"^[A-Za-z]:", token))


def _is_workspace_relative_token(token: str) -> bool:
    normalized = token.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return False
    return ".." not in normalized.split("/")
