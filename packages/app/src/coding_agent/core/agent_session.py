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
import sys
import time
from dataclasses import dataclass, field
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
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    settings_manager: Any = None
    theme_name: str = "dark"


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
        self._tools = _filter_tools(
            raw_tools,
            config.allowed_tool_names,
            config.excluded_tool_names,
            config.no_tools,
        )
        self._bash_tool = cast(
            BashTool | None,
            next((tool for tool in self._tools if tool.name == "bash"), None),
        )

        # ── Session persistence ──────────────────────────────────────────
        if config.session_manager is not None:
            self.session_manager = config.session_manager
        else:
            self.session_manager = SessionManager.create(
                cwd=config.cwd, in_memory=True,
            )

        # ── System prompt ────────────────────────────────────────────────
        # Always go through build_system_prompt so context files, skills, and
        # append text are applied in both the custom-prompt and default branches
        #. config.system_prompt becomes custom_prompt.
        shell_kind = getattr(self._bash_tool, "shell_kind", "bash") if self._bash_tool else "bash"
        self._system_prompt = build_system_prompt(
            cwd=config.cwd,
            tools=self._tools,
            shell_kind=shell_kind,
            platform=sys.platform,
            custom_prompt=config.system_prompt,
            context_files=config.context_files,
            append_system_prompt=config.append_system_prompt,
            skills=config.skills,
        )

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

        # ── State ─────────────────────────────────────────────────────────
        self._listeners: list[Callable[[AgentEvent], Any]] = []
        self._is_processing = False
        self._turn_index = 0
        self._last_assistant_message: AssistantMessage | None = None
        #: Strong refs for fire-and-forget async listener tasks (prevents GC).
        self._background_tasks: set = set()

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

    # ── Event subscription ────────────────────────────────────────────────

    def on_event(
        self,
        listener: Callable[[Any], Any],
    ) -> Callable[[], None]:
        """Subscribe to session events. Returns an unsubscribe function."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

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
        if not previous.in_memory:
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
        return new_manager

    async def abort(self) -> None:
        """Abort the current agent run."""
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
        """Clean up resources. Flushes the session to disk if needed."""
        if self.session_manager is not None and not self.session_manager.in_memory:
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

    def _emit_event(self, event: Any) -> None:
        """Emit an event to all registered listeners.

        Sync listeners run inline. Async listeners (coroutines) are scheduled on
        the running loop with a strong reference held until completion (the loop
        only keeps a weak ref, so an unreferenced task can be GC'd mid-flight).
        If no loop is running (e.g. called from a sync setter), async listeners
        are skipped — callers driving events outside a loop should run their own.
        """
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
