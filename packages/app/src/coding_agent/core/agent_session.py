"""AgentSession — central application class.

Wraps agent_core.Agent with session persistence, tool management, compaction,
model switching, and event forwarding. Shared between interactive and
non-interactive run modes.

Responsibilities:
  - Prompt lifecycle (prompt / abort)
  - Tool registry (built-in 7 tools + filtering)
  - Auto compaction after each turn (delegates to CompactionOrchestrator)
  - Manual compaction (/compact)
  - Model switching
  - Session stats (token counts, cost, message counts)
  - Event forwarding (AgentEvent -> AgentSessionEvent)

"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal, cast

from agent_llm import (
    AssistantMessage,
    Model,
    ThinkingLevel,
)
from agent_core import (
    Agent,
    AgentEvent,
    AgentTool,
    BashTool,
    BeforeToolCallContext,
    BeforeToolCallResult,
    CompactionOrchestrator,
    EditTool,
    FindTool,
    GrepTool,
    LsTool,
    ReadTool,
    SessionManager,
    WriteTool,
)
from agent_core.prompts import build_system_prompt
from coding_agent.core.retry import RetryPolicy, retrying_stream
from coding_agent.core.plan_mode import (
    CollaborationMode,
    PLAN_MODE_OVERLAY,
    PlanModeError,
    PlanQuestion,
    PlanRevision,
    PlanState,
    QuestionBehavior,
    RequestUserInputTool,
    call_hook,
    enforce_plan_tool_policy,
    is_plan_safe_shell_command,
    new_plan_id,
    prepare_plan_reply,
    reduce_plan_state,
    validate_proposed_plan,
)

# ─── Session event types (extending AgentEvent) ──────────────────────────

#: Compaction trigger reason.
CompactionReason = Literal["manual", "threshold", "overflow"]


class AgentSessionEvent(dict):
    """Base for session-level events. Passed as dicts matching AgentEvent shape."""
    pass


# ─── Data structs ─────────────────────────────────────────────────────────


@dataclass
class AgentSessionConfig:
    """Configuration for creating an AgentSession.

    Holds the model, credentials, tools, session manager, and runtime options.
    """

    # Required
    model: Model
    cwd: str = "."

    # Optional
    system_prompt: str | None = None
    tools: list[AgentTool] | None = None
    session_manager: SessionManager | None = None
    get_api_key: "Callable[[str], str | None] | None" = None
    reasoning: ThinkingLevel | None = None

    # Tool filtering uses a simple allowlist/denylist.
    allowed_tool_names: list[str] | None = None
    excluded_tool_names: list[str] | None = None
    no_tools: bool = False
    no_builtin_tools: bool = False

    # System prompt assembly
    #: Text appended after the assembled system prompt (--append-system-prompt).
    append_system_prompt: str | None = None
    #: Discovered AGENTS.md/CLAUDE.md entries, injected as <project_context>.
    context_files: list | None = None  # list[ContextFile]
    #: Loaded skills injected as ``<available_skills>``.
    skills: list | None = None  # list[Skill]
    #: Loaded prompt templates, expanded when the user types ``/name args``.
    prompt_templates: list | None = None  # list[PromptTemplate]
    # Optional runtime hooks. Desktop and other embedding frontends use these
    # to implement approval gates and audit logging without changing tools.
    before_tool_call: Any = None
    after_tool_call: Any = None
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    settings_manager: Any = None
    theme_name: str = "dark"
    collaboration_mode: CollaborationMode = "default"
    question_behavior: QuestionBehavior = "interactive"


@dataclass
class SessionStats:
    """Statistics about the current session."""

    session_id: str = ""
    session_file: str | None = None
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    tool_results: int = 0
    total_messages: int = 0
    tokens: _TokenStats = field(default_factory=lambda: _TokenStats())
    cost: float = 0.0


@dataclass
class _TokenStats:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total: int = 0


# ─── Built-in tool factory ────────────────────────────────────────────────

def _create_default_tools(cwd: str, shell_kind: str = "bash", platform: str = "") -> list[AgentTool]:
    """Create the standard 7 built-in tools.

    Returns ``AgentTool`` instances directly without additional wrappers.
    """
    bash_tool = BashTool(cwd=cwd)
    return cast(list[AgentTool], [
        ReadTool(cwd=cwd),
        WriteTool(cwd=cwd),
        EditTool(cwd=cwd),
        bash_tool,
        GrepTool(cwd=cwd),
        FindTool(cwd=cwd),
        LsTool(cwd=cwd),
    ])


def _filter_tools(
    tools: list[AgentTool],
    allowed: list[str] | None,
    excluded: list[str] | None,
    no_tools: bool,
) -> list[AgentTool]:
    """Apply tool allowlist/denylist filtering."""
    if no_tools:
        return []
    result = list(tools)
    if allowed:
        allowed_set = set(allowed)
        result = [t for t in result if t.name in allowed_set]
    if excluded:
        excluded_set = set(excluded)
        result = [t for t in result if t.name not in excluded_set]
    return result


# ─── AgentSession ─────────────────────────────────────────────────────────


class AgentSession:
    """Central application class wrapping an Agent with session management.

    Usage:
        session = AgentSession(config)
        # Subscribe to events before starting
        session.on_event(my_listener)
        # Send a prompt
        await session.prompt("hello")
        # Get stats
        stats = session.get_stats()
    """

    def __init__(self, config: AgentSessionConfig) -> None:
        self._config = config
        self.cwd = config.cwd
        self.retry_policy = config.retry_policy
        self.settings_manager = config.settings_manager
        self.theme_name = config.theme_name
        self._retry_abort_event = asyncio.Event()

        # ── Model ────────────────────────────────────────────────────────
        self._model = config.model

        # ── Tools ─────────────────────────────────────────────────────────
        if config.tools is not None:
            raw_tools = list(config.tools)
        elif config.no_builtin_tools:
            raw_tools = []
        else:
            raw_tools = _create_default_tools(
                config.cwd,
                shell_kind=getattr(config, "shell_kind", "bash"),
                platform=getattr(config, "platform", ""),
            )
        self._development_tools = _filter_tools(
            raw_tools,
            config.allowed_tool_names,
            config.excluded_tool_names,
            config.no_tools,
        )
        self._bash_tool = cast(
            BashTool | None,
            next((tool for tool in self._development_tools if tool.name == "bash"), None),
        )

        # ── Session persistence ──────────────────────────────────────────
        if config.session_manager is not None:
            self.session_manager = config.session_manager
        else:
            self.session_manager = SessionManager.create(
                cwd=config.cwd, in_memory=True,
            )

        # Event/lifecycle state must exist before persisted Plan recovery can
        # emit or append a recovered revision during Agent construction.
        self._listeners: list[Callable[[Any], Any]] = []
        self._is_processing = False
        self._turn_index = 0
        self._last_assistant_message: AssistantMessage | None = None
        self._background_tasks: set = set()

        # ── Plan state + effective tools ─────────────────────────────────
        self._plan_state = reduce_plan_state(self.session_manager.get_branch())
        self._question_behavior = config.question_behavior
        self._question_future: asyncio.Future[str] | None = None
        self._question_signal_task: asyncio.Task | None = None
        self._plan_abort_requested = False
        self._control_tool = RequestUserInputTool(
            self._request_plan_question,
            deferred=config.question_behavior == "deferred",
        )
        if self._plan_state.phase == "idle" and config.collaboration_mode == "plan":
            plan_id = new_plan_id()
            self.session_manager.append_collaboration_mode_change("plan", plan_id=plan_id)
            self._plan_state = PlanState(
                mode="plan", phase="drafting", active_plan_id=plan_id,
            )
        self._tools = self._effective_tools()

        # ── System prompt ────────────────────────────────────────────────
        # Always go through build_system_prompt so context files, skills, and
        # append text are applied in both the custom-prompt and default branches
        #. config.system_prompt becomes custom_prompt.
        shell_kind = getattr(self._bash_tool, "shell_kind", "bash") if self._bash_tool else "bash"
        self._system_prompt = self._build_effective_system_prompt(shell_kind)

        # ── Agent ─────────────────────────────────────────────────────────
        # Wire convert_to_llm so custom message roles (bashExecution,
        # compactionSummary, branchSummary, custom) reach the model instead of
        # being dropped by the Agent's default role filter.
        from coding_agent.core.messages import convert_to_llm as _convert_to_llm
        self._agent = Agent(
            model=config.model,
            system_prompt=self._system_prompt,
            tools=self._tools,
            stream_fn=self._create_stream_fn(),
            get_api_key=config.get_api_key,
            reasoning=config.reasoning,
            session_manager=self.session_manager,
            convert_to_llm=_convert_to_llm,
            before_tool_call=self._before_tool_call,
            after_tool_call=config.after_tool_call,
        )
        self._restore_persisted_context()
        self._agent.subscribe(self._on_agent_event)

        # ── Compaction ────────────────────────────────────────────────────
        self._compaction_orchestrator = CompactionOrchestrator(
            agent=self._agent, session_manager=self.session_manager,
        )
        # Bridge orchestrator lifecycle events (compaction_start/end) to our
        # listeners, so callers don't double-emit.
        self._compaction_orchestrator.on_event = self._emit_event

    # ── Public properties ─────────────────────────────────────────────────

    @property
    def model(self) -> Model:
        return self._model

    @property
    def tools(self) -> list[AgentTool]:
        return list(self._tools)

    @property
    def agent(self) -> Agent:
        return self._agent

    @property
    def state(self):
        return self._agent.state

    @property
    def is_processing(self) -> bool:
        return self._is_processing

    @property
    def turn_index(self) -> int:
        return self._turn_index

    @property
    def collaboration_mode(self) -> CollaborationMode:
        return self._plan_state.mode

    @property
    def plan_state(self) -> PlanState:
        return self._plan_state

    # ── Event subscription ────────────────────────────────────────────────

    def on_event(
        self,
        listener: Callable[[Any], Any],
    ) -> Callable[[], None]:
        """Subscribe to session events. Returns an unsubscribe function."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    # ── Collaboration mode ───────────────────────────────────────────────

    def _effective_tools(self) -> list[AgentTool]:
        tools = list(self._development_tools)
        if self._plan_state.mode == "plan":
            tools.append(cast(AgentTool, self._control_tool))
        return tools

    def _build_effective_system_prompt(self, shell_kind: str | None = None) -> str:
        resolved_shell_kind = shell_kind or (
            getattr(self._bash_tool, "shell_kind", "bash") if self._bash_tool else "bash"
        )
        prompt = build_system_prompt(
            cwd=self._config.cwd,
            tools=self._tools,
            shell_kind=str(resolved_shell_kind),
            platform=sys.platform,
            custom_prompt=self._config.system_prompt,
            context_files=self._config.context_files,
            append_system_prompt=self._config.append_system_prompt,
            skills=self._config.skills,
        )
        if self._plan_state.mode == "plan":
            prompt = f"{prompt.rstrip()}\n\n{PLAN_MODE_OVERLAY}\n"
        return prompt

    def _refresh_collaboration_runtime(self) -> None:
        self._tools = self._effective_tools()
        self._system_prompt = self._build_effective_system_prompt()
        if hasattr(self, "_agent"):
            self._agent.state.tools = list(self._tools)
            self._agent.state.system_prompt = self._system_prompt

    async def _before_tool_call(
        self, context: BeforeToolCallContext, signal: asyncio.Event,
    ) -> BeforeToolCallResult | None:
        if self._plan_state.mode == "plan":
            policy_result = await enforce_plan_tool_policy(context, self.cwd)
            if policy_result is not None and policy_result.block:
                self._emit_event({
                    "type": "plan_policy_blocked",
                    "code": "PLAN_POLICY_BLOCKED",
                    "tool_name": context.tool_call.name,
                    "reason": policy_result.reason,
                })
                return policy_result
        return await call_hook(self._config.before_tool_call, context, signal)

    def enter_plan_mode(self) -> PlanState:
        if self._is_processing:
            raise PlanModeError("RUN_IN_PROGRESS", "任务运行中，不能切换协作模式")
        if self._plan_state.mode == "plan":
            return self._plan_state
        plan_id = new_plan_id()
        self.session_manager.append_collaboration_mode_change("plan", plan_id=plan_id)
        self._plan_state = PlanState(
            mode="plan", phase="drafting", active_plan_id=plan_id,
        )
        self._refresh_collaboration_runtime()
        self._emit_event({
            "type": "collaboration_mode_changed", "mode": "plan",
            "phase": "drafting", "plan_id": plan_id,
        })
        return self._plan_state

    def cancel_plan_mode(self, plan_id: str | None = None) -> PlanState:
        if self._is_processing and self._question_future is None:
            raise PlanModeError("RUN_IN_PROGRESS", "任务运行中，不能取消规划")
        if self._plan_state.mode != "plan" or not self._plan_state.active_plan_id:
            raise PlanModeError("INVALID_MODE_TRANSITION", "当前不在 Plan Mode")
        if plan_id is not None and plan_id != self._plan_state.active_plan_id:
            raise PlanModeError("INVALID_MODE_TRANSITION", "planId 与当前 Plan Episode 不匹配")
        active_id = self._plan_state.active_plan_id
        if self._question_future is not None and not self._question_future.done():
            self._question_future.cancel()
        self.session_manager.append_collaboration_mode_change("default", plan_id=active_id)
        self._plan_state.mode = "default"
        self._plan_state.phase = "cancelled"
        self._plan_state.pending_question = None
        self._refresh_collaboration_runtime()
        self._emit_event({
            "type": "collaboration_mode_changed", "mode": "default",
            "phase": "cancelled", "plan_id": active_id,
        })
        return self._plan_state

    async def _request_plan_question(
        self, question: PlanQuestion, signal: asyncio.Event | None,
    ) -> str | None:
        if self._plan_state.mode != "plan" or not self._plan_state.active_plan_id:
            raise PlanModeError("INVALID_MODE_TRANSITION", "结构化问题只能在 Plan Mode 使用")
        if self._plan_state.pending_question is not None:
            raise PlanModeError("QUESTION_ALREADY_PENDING", "已有一个问题等待回答")
        plan_id = self._plan_state.active_plan_id
        self.session_manager.append_plan_question(
            plan_id=plan_id, question_id=question.question_id,
            header=question.header, question=question.question,
            options=[asdict(option) for option in question.options],
            allow_custom=question.allow_custom,
        )
        self._plan_state.phase = "awaiting_answer"
        self._plan_state.pending_question = question
        self._emit_event({
            "type": "plan_question_requested", "plan_id": plan_id,
            "question": question.to_payload(),
        })
        if self._question_behavior == "deferred":
            return None

        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._question_future = future
        waitables: set[asyncio.Future | asyncio.Task] = {future}
        signal_task: asyncio.Task | None = None
        if signal is not None:
            signal_task = asyncio.create_task(signal.wait())
            self._question_signal_task = signal_task
            waitables.add(signal_task)
        try:
            done, _ = await asyncio.wait(waitables, return_when=asyncio.FIRST_COMPLETED)
            if future in done and not future.cancelled():
                return future.result()
            return None
        finally:
            if signal_task is not None:
                signal_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await signal_task
            self._question_signal_task = None
            self._question_future = None

    async def answer_plan_question(self, question_id: str, answer: str) -> PlanState:
        answer = answer.strip()
        pending = self._plan_state.pending_question
        if self._plan_state.mode != "plan" or pending is None:
            raise PlanModeError("QUESTION_NOT_PENDING", "当前没有等待回答的 Plan 问题")
        if pending.question_id != question_id:
            raise PlanModeError("QUESTION_NOT_PENDING", "questionId 与当前问题不匹配")
        if not answer:
            raise PlanModeError("INVALID_PARAMS", "回答不能为空")
        plan_id = self._plan_state.active_plan_id or ""
        self.session_manager.append_plan_question_answer(
            plan_id=plan_id, question_id=question_id, answer=answer,
        )
        self._plan_state.pending_question = None
        self._plan_state.phase = "drafting"
        self._emit_event({
            "type": "plan_question_answered", "plan_id": plan_id,
            "question_id": question_id, "answer": answer,
        })
        if self._question_future is not None and not self._question_future.done():
            self._question_future.set_result(answer)
            return self._plan_state
        await self.prompt(
            f"<plan_question_answer question_id=\"{question_id}\">\n{answer}\n"
            "</plan_question_answer>"
        )
        return self._plan_state

    async def execute_plan(
        self, plan_id: str, revision: int, digest: str, run_id: str | None = None,
    ) -> PlanRevision:
        if self._is_processing:
            raise PlanModeError("RUN_IN_PROGRESS", "任务运行中，不能执行计划")
        latest = self._plan_state.latest_revision
        if self._plan_state.mode != "plan" or self._plan_state.phase != "ready" or latest is None:
            raise PlanModeError("PLAN_NOT_READY", "当前没有可执行的计划")
        if plan_id != latest.plan_id or revision != latest.revision or digest != latest.digest:
            self._emit_event({
                "type": "plan_validation_failed", "code": "STALE_PLAN_REVISION",
                "plan_id": plan_id, "revision": revision,
            })
            raise PlanModeError("STALE_PLAN_REVISION", "只能执行最新的 Plan revision")

        self.session_manager.append_plan_run(
            plan_id=plan_id, revision=revision, digest=digest,
            status="started", run_id=run_id,
        )
        self._plan_state.mode = "default"
        self._plan_state.phase = "executing"
        self._plan_abort_requested = False
        self._refresh_collaboration_runtime()
        self._emit_event({
            "type": "plan_execution_started", "plan": latest.to_payload(),
            "run_id": run_id,
        })
        execution_prompt = (
            "<confirmed_plan_execution>\n"
            f"plan_id: {plan_id}\nrevision: {revision}\ndigest: {digest}\n\n"
            f"{latest.markdown}\n"
            "</confirmed_plan_execution>\n"
            "Execute this exact confirmed plan now."
        )
        try:
            await self.prompt(execution_prompt)
        except asyncio.CancelledError:
            self._finish_plan_run(latest, "aborted", run_id=run_id)
            raise
        except Exception as exc:
            self._finish_plan_run(latest, "failed", run_id=run_id, error=str(exc))
            raise
        stop_reason = getattr(self._last_assistant_message, "stop_reason", None)
        if self._plan_abort_requested:
            self._finish_plan_run(latest, "aborted", run_id=run_id)
        elif stop_reason == "error":
            self._finish_plan_run(
                latest, "failed", run_id=run_id,
                error=getattr(self._last_assistant_message, "error_message", None),
            )
        else:
            self._finish_plan_run(latest, "completed", run_id=run_id)
        return latest

    def _finish_plan_run(
        self, plan: PlanRevision, status: Literal["completed", "failed", "aborted"],
        *, run_id: str | None, error: str | None = None,
    ) -> None:
        self.session_manager.append_plan_run(
            plan_id=plan.plan_id, revision=plan.revision, digest=plan.digest,
            status=status, run_id=run_id, error=error,
        )
        self._plan_state.mode = "default"
        self._plan_state.phase = status
        event_suffix = {"completed": "completed", "failed": "failed", "aborted": "aborted"}[status]
        self._emit_event({
            "type": f"plan_execution_{event_suffix}", "plan": plan.to_payload(),
            "run_id": run_id, "error": error,
        })

    def refresh_plan_state_from_branch(self) -> PlanState:
        self._plan_state = reduce_plan_state(self.session_manager.get_branch())
        self._refresh_collaboration_runtime()
        return self._plan_state

    # ── Prompt ────────────────────────────────────────────────────────────

    async def prompt(self, message: Any) -> None:
        """Send a user message and run the agent loop.

        This path has no extension interception, queueing, or skill expansion. Compaction
        lifecycle events (start/end) are emitted by the orchestrator via the
        bridged callback; on overflow recovery (``need_retry``) the original
        message is re-prompted once after compaction.
        """
        if self._is_processing:
            raise RuntimeError("Agent is already processing a prompt.")
        if (
            self._plan_state.mode == "plan"
            and self._plan_state.pending_question is not None
            and self._question_future is None
        ):
            raise PlanModeError(
                "QUESTION_NOT_PENDING",
                "请先通过结构化问题控件回答当前 Plan 问题",
            )
        if self._plan_state.mode == "plan" and self._plan_state.phase == "ready":
            # Ordinary user text is feedback, never execution authorization.
            # The persisted user message reproduces this transition on resume.
            self._plan_state.phase = "drafting"
            self._emit_event({
                "type": "collaboration_mode_changed",
                "mode": "plan",
                "phase": "drafting",
                "plan_id": self._plan_state.active_plan_id,
            })

        self._is_processing = True
        self._retry_abort_event.clear()
        self._compaction_orchestrator.reset_overflow_guard()
        self._turn_index += 1

        try:
            await self._agent.prompt(message)
        finally:
            self._is_processing = False

        # Auto-compaction check after each turn.
        # The orchestrator emits compaction_start/end via on_event.
        outcome = await self._compaction_orchestrator.check_compaction()

        # Overflow recovery: the orchestrator stripped the errored assistant
        # message and compacted; re-prompt the original message once so the
        # user's request is retried against the compacted context.
        if outcome.need_retry:
            self._is_processing = True
            try:
                await self._agent.prompt(message)
            finally:
                self._is_processing = False
            # A second compaction pass after the retry is intentionally skipped:
            # Limit overflow recovery to one attempt per turn.

    def new_session(self) -> SessionManager:
        """Finish the current session and attach a brand-new one.

        Storage options are inherited from the current manager so ``/new``
        respects ``--session-dir`` and ``--no-session``.  The old transcript is
        flushed before it is detached; subsequent messages can therefore never
        leak into the old JSONL file.
        """
        previous = self.session_manager
        if not previous.in_memory and previous.has_meaningful_activity():
            previous.flush()

        new_manager = SessionManager.create(
            cwd=self.cwd,
            agent_dir=previous.agent_dir,
            sessions_dir=previous.sessions_dir,
            in_memory=previous.in_memory,
        )
        self._agent.reset()
        self._agent.attach_session(new_manager)
        self.session_manager = new_manager
        self._last_assistant_message = None
        self._turn_index = 0
        self._plan_state = PlanState()
        self._question_future = None
        self._refresh_collaboration_runtime()
        return new_manager

    async def abort(self) -> None:
        """Abort the current agent run."""
        if self._plan_state.phase == "executing":
            self._plan_abort_requested = True
        self._retry_abort_event.set()
        await self._agent.abort()

    def abort_compaction(self) -> None:
        """Request abort of the in-flight compaction (best-effort).

        Called by the UI (Esc during compaction). The orchestrator checks the
        abort signal and reports the compaction as aborted via its callback.
        """
        self._compaction_orchestrator.abort()

    async def wait_for_idle(self) -> None:
        """Resolve when the current run finishes."""
        await self._agent.wait_for_idle()

    # ── Model management ──────────────────────────────────────────────────

    def set_model(self, model: Model, thinking_level: ThinkingLevel | None = None) -> None:
        """Switch the active model.

        Updates both the Agent's internal model and our cached reference.
        Does NOT rebuild tools — callers should call _refresh_tools() if
        the model change requires different tool schemas.
        """
        self._model = model
        self._agent.state.model = model
        # Persist model change to session.
        self.session_manager.append_model_change(
            provider=model.provider, model_id=model.id,
        )
        # Route thinking-level changes through set_thinking_level so they persist
        # and emit the event (consistent with the standalone /thinking command).
        if thinking_level is not None:
            self.set_thinking_level(thinking_level)

    # ── Thinking level ────────────────────────────────────────────────────

    @property
    def thinking_level(self) -> ThinkingLevel | None:
        """The current reasoning/thinking level (read from the agent)."""
        return getattr(self._agent, "reasoning", None)

    def set_thinking_level(self, level: ThinkingLevel | None) -> None:
        """Switch the reasoning/thinking level at runtime.

        Updates the agent's reasoning attribute, persists the change to the
        session, and emits a ``thinking_level_changed`` event.
        """
        self._agent.reasoning = level
        # Persist to session (append_thinking_level_change exists on SessionManager).
        try:
            self.session_manager.append_thinking_level_change(level or "off")
        except Exception:
            pass
        self._emit_event({"type": "thinking_level_changed", "level": level})

    # ── Bash passthrough (the ! command) ──────────────────────────────────

    async def run_bash(self, command: str, *, exclude_from_context: bool = False) -> dict:
        """Run a shell command directly, bypassing the LLM (the ``!`` passthrough).

        Executes ``command`` via the session's BashTool, records the result as a
        :class:`BashExecutionMessage` in both the agent state and the session
        (so it appears in future LLM context unless ``exclude_from_context``),
        and emits a ``bash_execution`` event for the UI.

        Returns a dict with ``output``, ``exit_code``, ``truncated``,
        ``timed_out``, ``exclude_from_context``, and ``error`` (on failure).
        """
        if self._bash_tool is None:
            return {"error": "No bash tool available"}
        if self._plan_state.mode == "plan" and not is_plan_safe_shell_command(command, self.cwd):
            self._emit_event({
                "type": "plan_policy_blocked", "code": "PLAN_POLICY_BLOCKED",
                "tool_name": "bash", "reason": "PLAN_POLICY_BLOCKED: Plan Mode 禁止该命令",
            })
            return {"error": "PLAN_POLICY_BLOCKED", "code": "PLAN_POLICY_BLOCKED"}
        try:
            raw = await self._bash_tool.run_raw(command, timeout=60)
        except Exception as e:
            return {"error": f"Failed to run command: {e}"}

        from coding_agent.core.messages import BashExecutionMessage
        # Unix timestamp in milliseconds.
        ts = time.time() * 1000.0

        msg = BashExecutionMessage(
            command=command,
            output=raw.output,
            exit_code=raw.exit_code,
            cancelled=raw.timed_out,
            truncated=raw.truncated,
            timestamp=ts,
            exclude_from_context=exclude_from_context,
        )
        # Add to agent state + persist to session.
        self._agent.state.messages.append(msg)
        try:
            self.session_manager.append_message(cast(Any, msg))
        except Exception:
            # append_message may type-check the role; fall back to a no-op.
            pass

        self._emit_event({
            "type": "bash_execution",
            "command": command,
            "output": raw.output,
            "exit_code": raw.exit_code,
            "truncated": raw.truncated,
            "timed_out": raw.timed_out,
            "exclude_from_context": exclude_from_context,
        })
        return {
            "output": raw.output,
            "exit_code": raw.exit_code,
            "truncated": raw.truncated,
            "timed_out": raw.timed_out,
            "exclude_from_context": exclude_from_context,
        }

    # ── Compaction ────────────────────────────────────────────────────────

    async def compact(self, reason: CompactionReason = "manual") -> dict:
        """Run a manual compaction (e.g. from /compact).

        Returns a dict with ``performed``, ``summary_preview``, ``error``.
        The orchestrator emits ``compaction_start`` / ``compaction_end`` events
        via the bridged callback, so no explicit emit is needed here.
        """
        outcome = await self._compaction_orchestrator.manual_compact()
        return {
            "performed": outcome.performed,
            "reason": outcome.reason,
            "summary_preview": outcome.summary_preview,
            "error": outcome.error,
        }

    # ── Statistics ────────────────────────────────────────────────────────

    def get_stats(self) -> SessionStats:
        """Compute session statistics from session entries.

        Iterates all entries to count messages, tokens, and cost.
        """
        stats = SessionStats(
            session_id=self.session_manager.header.id,
            session_file=str(self.session_manager.path) if self.session_manager.path else None,
        )

        entries = self.session_manager.get_branch()
        for entry in entries:
            from agent_core.session.types import SessionMessageEntry
            if not isinstance(entry, SessionMessageEntry) or entry.message is None:
                continue
            msg = entry.message
            role = getattr(msg, "role", None)
            if role == "user":
                stats.user_messages += 1
            elif role == "assistant":
                stats.assistant_messages += 1
                # Count tool calls within assistant messages.
                content = getattr(msg, "content", [])
                for block in (content if isinstance(content, list) else []):
                    if getattr(block, "type", None) == "toolCall":
                        stats.tool_calls += 1
                # Accumulate token usage.
                usage = getattr(msg, "usage", None)
                if usage is not None:
                    stats.tokens.input += int(getattr(usage, "input", 0) or 0)
                    stats.tokens.output += int(getattr(usage, "output", 0) or 0)
                    stats.tokens.cache_read += int(getattr(usage, "cache_read", 0) or 0)
                    stats.tokens.cache_write += int(getattr(usage, "cache_write", 0) or 0)
                    stats.tokens.total += int(getattr(usage, "total_tokens", 0) or 0)
                    cost = getattr(usage, "cost", None)
                    if cost is not None:
                        stats.cost += float(getattr(cost, "total", 0) or 0)
            elif role == "toolResult":
                stats.tool_results += 1

        stats.total_messages = stats.user_messages + stats.assistant_messages
        return stats

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def dispose(self) -> None:
        """Clean up resources, persisting only sessions that contain entries."""
        if (
            self.session_manager is not None
            and not self.session_manager.in_memory
            and self.session_manager.has_meaningful_activity()
        ):
            self.session_manager.flush()

    def _restore_persisted_context(self) -> None:
        """Load an opened session into the live Agent without re-persisting it."""
        context = self.session_manager.build_session_context()
        if context.messages:
            self._agent.load_messages(context.messages)
        if context.thinking_level is not None:
            self._agent.reasoning = (
                None if context.thinking_level == "off" else context.thinking_level
            )
        self.refresh_plan_state_from_branch()
        self._recover_latest_complete_plan()

    def _recover_latest_complete_plan(self) -> None:
        """Upgrade the latest bare plan in a resumed Plan episode to ready.

        Older clients persisted the assistant Markdown but silently skipped a
        revision when the model omitted the control envelope. Only the newest
        assistant response in the active episode is considered, so later user
        feedback cannot accidentally revive an obsolete plan.
        """
        if (
            self._plan_state.mode != "plan"
            or self._plan_state.latest_revision is not None
            or not self._plan_state.active_plan_id
        ):
            return

        from agent_core.session.types import (
            CollaborationModeChangeEntry,
            SessionMessageEntry,
        )

        active_plan_id = self._plan_state.active_plan_id
        for entry in reversed(self.session_manager.get_branch()):
            if (
                isinstance(entry, CollaborationModeChangeEntry)
                and entry.mode == "plan"
                and entry.plan_id == active_plan_id
            ):
                break
            if isinstance(entry, SessionMessageEntry) and entry.message is not None:
                role = getattr(entry.message, "role", None)
                if role == "user":
                    break
                if role == "assistant":
                    self._capture_plan_revision(entry.message)
                    break

    # ── Internal: event forwarding ────────────────────────────────────────

    async def _on_agent_event(self, event: AgentEvent, signal: asyncio.Event) -> None:
        """Forward agent events to session listeners, plus internal bookkeeping.

        Updates session state before notifying registered listeners.
        """
        etype = event.get("type")

        # Track last assistant message for auto-compaction.
        if etype == "message_end":
            msg = event.get("message")
            if msg is not None and getattr(msg, "role", None) == "assistant":
                self._last_assistant_message = msg
                self._capture_plan_revision(msg)

        # Emit compaction events for the UI.
        if etype == "message_end":
            msg = event.get("message")
            if msg is not None and getattr(msg, "role", None) == "assistant":
                stop = getattr(msg, "stop_reason", "stop")
                if stop == "error":
                    err_msg = getattr(msg, "error_message", "") or ""
                    if any(k in err_msg.lower() for k in ("context", "overflow", "too long", "too many tokens")):
                        self._emit_event({
                            "type": "compaction_needed",
                            "reason": "overflow",
                        })

        # Forward to external listeners.
        self._emit_event(event)

    def _capture_plan_revision(self, message: Any) -> None:
        """Validate a completed assistant plan before publishing ``plan_ready``."""
        if self._plan_state.mode != "plan" or not self._plan_state.active_plan_id:
            return
        if getattr(message, "stop_reason", None) == "error":
            return
        text = _assistant_text(message)
        prepared_reply = prepare_plan_reply(text)
        if prepared_reply is None:
            return
        revision_number = (
            self._plan_state.latest_revision.revision + 1
            if self._plan_state.latest_revision is not None else 1
        )
        try:
            revision = validate_proposed_plan(
                prepared_reply, plan_id=self._plan_state.active_plan_id,
                revision=revision_number,
                source_message_id=str(getattr(message, "id", "")),
            )
        except PlanModeError as exc:
            self._emit_event({
                "type": "plan_validation_failed", "code": exc.code,
                "message": str(exc), "plan_id": self._plan_state.active_plan_id,
            })
            return
        self.session_manager.append_plan_revision(
            plan_id=revision.plan_id, revision=revision.revision,
            title=revision.title, markdown=revision.markdown,
            digest=revision.digest, source_message_id=revision.source_message_id,
        )
        self._plan_state.phase = "ready"
        self._plan_state.pending_question = None
        self._plan_state.latest_revision = revision
        self._emit_event({"type": "plan_ready", "plan": revision.to_payload()})

    def _emit_event(self, event: Any) -> None:
        """Emit an event to all registered listeners.

        Sync listeners run inline. Async listeners (coroutines) are scheduled on
        the running loop with a strong reference held until completion (the loop
        only keeps a weak ref, so an unreferenced task can be GC'd mid-flight).
        If no loop is running (e.g. called from a sync setter), async listeners
        are skipped — callers driving events outside a loop should run their own.
        """
        event_type = str(event.get("type", "")) if isinstance(event, dict) else ""
        if (
            isinstance(event, dict)
            and (event_type.startswith("plan_") or event_type == "collaboration_mode_changed")
        ):
            event.setdefault("session_id", self.session_manager.header.id)
        # ── DIAGNOSTIC ─────────────────────────────────────────────────
        import os as _os
        import time as _t
        _log = _os.environ.get("CODING_AGENT_STREAM_DEBUG")
        if _log:
            with open(_log, "a", encoding="utf-8") as f:
                f.write(f"{_t.perf_counter():.6f} SESSION→UI t={event.get('type')}\n")
        # ────────────────────────────────────────────────────────────────
        for listener in list(self._listeners):
            try:
                result = listener(event)
            except Exception:
                continue
            if hasattr(result, "__await__"):
                self._schedule_background(result)

    def _schedule_background(self, coro: Any) -> None:
        """Schedule a fire-and-forget coroutine, keeping a strong reference."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — close the coroutine to avoid 'never awaited'.
            coro.close()
            return
        task = loop.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ── Internal: stream_fn factory ───────────────────────────────────────

    def _create_stream_fn(self):
        """Create the stream function that the Agent will use.

        Uses ``agent_llm.compat.stream_simple`` with the global
        Models instance.
        """
        from agent_llm.compat import stream_simple

        def _stream(model: Model, context, options=None):
            return retrying_stream(
                lambda: stream_simple(model, context, options),
                self.retry_policy,
                on_retry=lambda attempt, delay, error: self._emit_event({
                    "type": "retry",
                    "attempt": attempt,
                    "max_retries": self.retry_policy.max_retries,
                    "delay": delay,
                    "error": error,
                }),
                abort_event=self._retry_abort_event,
            )

        return _stream

    # ── Internal: API key resolution ──────────────────────────────────────

    def _resolve_api_key(self) -> str | None:
        """Resolve the API key for the current model.

        Delegates to the ``get_api_key`` callback from config.
        Model lookup is delegated to the configured provider.
        """
        if self._config.get_api_key is not None:
            return self._config.get_api_key(self._model.provider)
        return None


def _assistant_text(message: Any) -> str:
    """Join text blocks from an assistant message without including thinking."""
    content = getattr(message, "content", [])
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content if isinstance(content, list) else []:
        if getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "")))
    return "".join(parts)
