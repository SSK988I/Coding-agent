"""InteractiveMode — TUI chat interface.

Holds the TUI instance, manages the component tree, dispatches agent events
to UI updates, and handles slash commands.

Component tree:
    TUI
      ├─ chat_container      # message stream + tool cards
      ├─ status_container    # Loader spinner or empty spacer
      └─ editor              # input box (Editor component)

"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from agent_tui import (
    Component,
    Container,
    Editor,
    Markdown,
    ProcessTerminal,
    Spacer,
    TUI,
    Text,
    get_markdown_theme,
    load_theme,
    matches_key,
)
from agent_tui.autocomplete import CombinedAutocompleteProvider

from coding_agent.core.agent_session import AgentSession
from coding_agent.core.config import get_auth_path, VERSION
from coding_agent.core.credentials import CredentialStore
from coding_agent.core.retry import RetryPolicy
from coding_agent.core.slash_commands import (
    BUILTIN_SLASH_COMMANDS,
    BuiltinSlashCommand,
    get_active_commands,
)
from coding_agent.modes.interactive.components.assistant_message import (
    AssistantMessageComponent,
)
from coding_agent.modes.interactive.components.footer import FooterComponent
from coding_agent.modes.interactive.components.login_dialog import (
    LoginDialogComponent,
)
from coding_agent.modes.interactive.components.model_selector import (
    ModelSelectorComponent,
)
from coding_agent.modes.interactive.components.status_indicator import (
    StatusIndicator,
    WorkingStatusIndicator,
    CompactionStatusIndicator,
    RetryStatusIndicator,
)
from coding_agent.modes.interactive.components.tool_execution import ToolExecutionComponent
from coding_agent.modes.interactive.components.user_message import UserMessageComponent
from coding_agent.modes.interactive.components.welcome import WelcomeComponent

# ─── Authentication storage ───────────────────────────────────────────────

AUTH_FILE = get_auth_path()


def _credential_store_for(owner: Any) -> CredentialStore:
    """Resolve a store for full InteractiveMode and lightweight embedded instances."""
    store = getattr(owner, "_credential_store", None)
    if store is None or store.path != AUTH_FILE:
        store = CredentialStore(AUTH_FILE)
        owner._credential_store = store
    return store

class InteractiveMode:
    """Interactive TUI mode for the coding agent.

    Owns the TUI, component tree, and agent event -> UI mapping.
    """

    def __init__(self, session: AgentSession) -> None:
        self._session = session

        # ── Prompt templates (for /name args expansion) ─────────────────
        self._prompt_templates = getattr(session._config, "prompt_templates", None) or []

        # ── Theme ────────────────────────────────────────────────────────
        try:
            self.theme = load_theme(session.theme_name)
        except (FileNotFoundError, KeyError, ValueError):
            self.theme = load_theme("dark")
        self.md_theme = get_markdown_theme(self.theme)

        # ── Credentials ──────────────────────────────────────────────────
        self._credential_store = CredentialStore(AUTH_FILE)
        self._credentials = self._load_credentials()
        self._api_key: str | None = self._get_stored_key()

        # ── TUI + component tree ─────────────────────────────────────────
        self.terminal = ProcessTerminal()
        self.tui = TUI(self.terminal)
        self.chat_container = Container()
        self.status_container = Container()
        self.editor = Editor(self.tui, padding_x=1)
        self._footer = FooterComponent(session, self.theme)

        self.editor.on_submit = self._on_submit_callback
        self.tui.add_child(self.chat_container)
        self.tui.add_child(self.status_container)
        self.tui.add_child(self.editor)
        self.tui.add_child(self._footer)
        self.tui.set_focus(self.editor)

        # ── State ────────────────────────────────────────────────────────
        self._running = True
        self._is_responding = False

        # ── Streaming state ───────────────────────────────────────────────
        #: The streaming AssistantMessageComponent for the current turn, or None.
        self._streaming_component: AssistantMessageComponent | None = None
        #: Tool cards awaiting results, keyed by tool_call_id.
        self._pending_tools: dict[str, ToolExecutionComponent] = {}
        #: Alias kept for legacy code paths / clarity.
        self._tool_cards = self._pending_tools

        # ── Status indicator (working / compaction) ──────────────────────
        self._active_status_indicator: StatusIndicator | None = None

        # ── Interrupt state ───────────────────────────────────────────────
        #: Timestamp of the last Ctrl+C press; a second press within 500 ms quits.
        self._last_sigint_time: float = 0.0
        #: Strong refs for fire-and-forget background tasks (prevents GC).
        self._background_tasks: set = set()

        # ── Selector swap state ──────────────────────────────────────────
        #: The currently-mounted selector/dialog replacing the editor, or None.
        #: Either a ModelSelectorComponent or a LoginDialogComponent (duck-typed
        #: via handle_input + render + focused).
        self._current_selector: Any = None

    def _spawn(self, coro):
        """Schedule a fire-and-forget coroutine, keeping a strong reference.

        The asyncio loop only holds a weak reference to tasks, so an unreferenced
        task can be garbage-collected mid-flight. We track it until completion.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return None
        task = loop.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # ── Interrupt handling ─────────────────────────────────────────────────

    def _on_escape(self) -> bool | None:
        """Esc handler wired into the editor.

        Aborts streaming when responding; aborts an in-flight compaction when a
        compaction indicator is active. Returns True to consume the key.
        """
        if self._is_responding:
            self._spawn(self._session.abort())
            return True
        active = self._active_status_indicator
        if active is not None and getattr(active, "kind", None) == "compaction":
            self._session.abort_compaction()
            return True
        return None

    def _clear_editor(self) -> None:
        """Clear the editor text after a single Ctrl+C."""
        self.editor.set_text("")
        self.tui.request_render()

    # ── Autocomplete (slash command names) ──────────────────────────────

    def _setup_autocomplete(self) -> None:
        """Build the slash-command-name autocomplete provider and wire it.

        Command-name prefix matching only (``/`` → list commands, ``/th`` →
        filter). Per-command argument completion is NOT wired here:
        ``/model`` uses an inline selector (see :meth:`_open_model_selector`)
        and ``/thinking`` was removed in favor of the Shift+Tab hotkey.

        Prompt templates (``/tplname args``) and skill invocations
        (``/skill:name``) are appended so they appear in completion and are
        dispatched by :meth:`_on_submit` / :meth:`_handle_skill_command`.
        """

        from coding_agent.modes.interactive.autocomplete_manager import AutocompleteManager

        # Clone so we don't mutate the shared BUILTIN_SLASH_COMMANDS list.
        commands = [
            BuiltinSlashCommand(name=cmd.name, description=cmd.description, active=cmd.active)
            for cmd in get_active_commands()
        ]
        # Prompt templates: surfaced as /name for completion + expansion.
        for tpl in self._prompt_templates:
            # Avoid clashing with a real builtin command of the same name.
            if not any(c.name == tpl.name for c in commands):
                commands.append(BuiltinSlashCommand(
                    name=tpl.name,
                    description=tpl.description or "提示词模板",
                ))
        # Skills: surfaced as /skill:<name> for explicit invocation.
        for skill in self._skills_for_command():
            cmd_name = f"skill:{skill.name}"
            if not any(c.name == cmd_name for c in commands):
                commands.append(BuiltinSlashCommand(
                    name=cmd_name,
                    description=skill.description or "技能",
                ))
        provider = CombinedAutocompleteProvider(commands)
        self._autocomplete = AutocompleteManager(self.tui, self.editor, provider)

    def _skills_for_command(self) -> list:
        """Return the skills available for ``/skill:name`` invocation."""
        return getattr(self._session._config, "skills", None) or []

    # ── Bash passthrough (the ! command) ──────────────────────────────────

    async def _handle_bash_command(self, command: str, excluded: bool) -> None:
        """Run a shell command directly via session.run_bash and render the result.

        Shows the command, runs it, renders the output as a code block, and
        records the result in the session. No streaming output or border color.
        """
        prefix = "!!" if excluded else "!"
        self._add_assistant_text(f"`{prefix} {command}`")

        self._show_status_indicator(WorkingStatusIndicator(self.tui, "running...", self.theme))
        try:
            result = await self._session.run_bash(command, exclude_from_context=excluded)
        finally:
            self._clear_status_indicator()
            self.tui.request_render()

        if "error" in result:
            self._add_system_message(self.theme.fg("error", f"错误：{result['error']}"))
            return

        output = result.get("output", "") or "(no output)"
        exit_code = result.get("exit_code", 0)
        truncated = result.get("truncated", False)
        timed_out = result.get("timed_out", False)

        # Render as a fenced code block + status footer.
        block = f"```\n{output.rstrip()}\n```"
        footer_parts: list[str] = []
        if exit_code != 0:
            footer_parts.append(self.theme.fg("error", f"exit {exit_code}"))
        if timed_out:
            footer_parts.append(self.theme.fg("warning", "timed out"))
        if truncated:
            footer_parts.append(self.theme.fg("dim", "truncated"))
        if excluded:
            footer_parts.append(self.theme.fg("dim", "excluded from context"))
        footer = " · ".join(footer_parts)
        if footer:
            block += f"\n\n{footer}"
        self._add_assistant_text(block)

    # ── Submit (stdin thread -> main loop) ────────────────────────────────

    def _on_submit_callback(self, text: str) -> None:
        """Called from stdin reader thread. Hop to event loop."""
        loop = self.tui._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._schedule_submit, text)

    def _schedule_submit(self, text: str) -> None:
        self._spawn(self._on_submit(text))

    async def _on_submit(self, text: str) -> None:
        """Handle user input submission."""
        if not text.strip() or self._is_responding:
            return

        # Check for slash commands.
        if text.startswith("/"):
            if await self._handle_command(text):
                return
            # Prompt template expansion: /name args → template content.
            if self._prompt_templates:
                from coding_agent.core.prompt_templates import expand_prompt_template
                expanded = expand_prompt_template(text, self._prompt_templates)
                if expanded != text:
                    # Show the expanded prompt as the user message, then run it.
                    self._add_user_message(expanded)
                    self._is_responding = True
                    self.editor.disable_submit = True
                    try:
                        await self._respond(expanded)
                    finally:
                        self._is_responding = False
                        self.editor.disable_submit = False
                        self._refresh_footer()
                    return
            self._add_user_message(text)
            err = f"未知命令：`{text}`\n\n输入 `/help` 查看可用命令。"
            self._add_assistant_text(err)
            return

        # Bash passthrough: "!cmd" runs a shell command directly (no LLM).
        # "!!cmd" excludes the result from LLM context (display-only).
        stripped = text.lstrip()
        if stripped.startswith("!"):
            is_excluded = stripped.startswith("!!")
            command = stripped[2:].strip() if is_excluded else stripped[1:].strip()
            if command:
                if self._is_responding:
                    self._add_system_message("命令执行中，请先按 Esc 中断当前响应。")
                    return
                await self._handle_bash_command(command, is_excluded)
                return
            # Bare "!" with no command → fall through to normal message.

        # Normal message.
        self._add_user_message(text)
        self._is_responding = True
        self.editor.disable_submit = True
        try:
            await self._respond(text)
        finally:
            self._is_responding = False
            self.editor.disable_submit = False
            self._refresh_footer()

    # ── Agent response ────────────────────────────────────────────────────

    async def _respond(self, prompt: Any) -> None:
        """Drive one agent prompt cycle with streaming UI updates.

        Uses the single-component model: one ``AssistantMessageComponent``
        created on ``message_start`` and rebuilt on every ``message_update``.
        No incremental delta accumulation — the full partial message drives
        each re-render, so the layout stays stable.
        """
        key = self._effective_api_key()
        if not key and not self._session._config.get_api_key:
            env_var = self._provider_env_var_for_current_session() or "(provider env var)"
            self._add_system_message(
                f"未配置 API key。用 /login 登录或设置 {env_var} 环境变量。")
            return

        # Streaming state is created lazily by _handle_message_start; reset it.
        self._streaming_component = None
        self._pending_tools.clear()

        # Subscribe to agent events for this turn.
        unsub = self._session.on_event(self._on_agent_event)

        try:
            await self._session.prompt(prompt)
        except Exception as e:
            self._add_system_message(self.theme.fg("error", f"错误：{e}"))
        finally:
            unsub()
            # If the run aborted before any message_start, drop any orphan.
            if self._streaming_component is not None:
                self.chat_container.remove_child(self._streaming_component)
                self._streaming_component = None
            self._pending_tools.clear()
            self._clear_status_indicator()
            self.tui.request_render()

    # ── Agent event -> UI mapping ─────────────────────────────────────────

    def _on_agent_event(self, event: dict) -> None:
        """Map agent events to TUI component updates (single dispatch)."""
        etype = event.get("type")

        if etype == "agent_start":
            self._pending_tools.clear()
            self._show_status_indicator(WorkingStatusIndicator(self.tui, "Working...", self.theme))
        elif etype == "message_start":
            self._clear_status_indicator("retry")
            self._handle_message_start(event)
        elif etype == "message_update":
            self._handle_message_update(event)
        elif etype == "message_end":
            self._handle_message_end(event)
        elif etype == "tool_execution_start":
            self._handle_tool_start(event)
        elif etype == "tool_execution_end":
            self._handle_tool_end(event)
        elif etype == "agent_end":
            self._clear_status_indicator("working")
        elif etype == "compaction_start":
            self._handle_compaction_start(event)
        elif etype == "compaction_end":
            self._handle_compaction_end(event)
        elif etype == "compaction_needed":
            # Soft hint from AgentSession that overflow occurred; the actual
            # compaction_start will follow if one runs.
            pass
        elif etype == "retry":
            self._show_status_indicator(RetryStatusIndicator(
                self.tui,
                int(event.get("attempt", 1)),
                int(event.get("max_retries", 1)),
                float(event.get("delay", 0)),
                self.theme,
            ))

    # 16 ms matches the TUI render interval. Duplicated as a
    # literal to avoid a cross-package import for one constant; the value is
    # stable and the throttle it gates is purely a streaming visual concern.
    _STREAM_RENDER_MIN_INTERVAL_S = 0.016

    def _render_streaming_delta(self) -> None:
        """Render a streaming delta NOW, unconditionally (diagnostic version).

        Replaces the throttle-aware version below. Hypothesis being tested: the
        throttle fallback (request_render under 16ms) gets starved by asyncio
        during dense SSE bursts, collapsing the whole burst into one final
        frame. Forcing render_now on every delta should yield visible per-batch
        streaming. If this fixes "一次性输出完", the throttle logic is the
        culprit and we'll design a better fix.

        Original throttle logic (preserved for restoration):
            if self.tui._last_render_at == 0.0:
                self.tui.render_now()
                return
            elapsed = time.monotonic() - self.tui._last_render_at
            if elapsed >= self._STREAM_RENDER_MIN_INTERVAL_S:
                self.tui.render_now()
            else:
                self.tui.request_render()
        """
        self.tui.render_now()

    def _handle_message_start(self, event: dict) -> None:
        """Create the streaming AssistantMessageComponent for an assistant turn."""
        msg = event.get("message")
        if msg is None:
            return
        role = getattr(msg, "role", None)
        if role == "user":
            # Echo of the user's own message — already shown by _add_user_message.
            return
        if role != "assistant":
            return
        # Create the single streaming component.
        self._streaming_component = AssistantMessageComponent(
            markdown_theme=self.md_theme,
            theme=self.theme,
            output_pad=1,
        )
        self._add_to_chat(self._streaming_component)
        self._streaming_component.update_content(msg)
        self._render_streaming_delta()

    def _handle_message_update(self, event: dict) -> None:
        """Rebuild the streaming component from the full partial message.

        Also surface new tool-call blocks as ``ToolExecutionComponent`` instances.
        """
        msg = event.get("message")
        if msg is None or getattr(msg, "role", None) != "assistant":
            return
        if self._streaming_component is None:
            # Late update without a start — create on demand.
            self._handle_message_start(event)
            return
        # ── DIAGNOSTIC: log text length on UI arrival ───────────────────
        import os as _os
        import time as _t
        _log = _os.environ.get("CODING_AGENT_STREAM_DEBUG")
        if _log:
            _txt_len = 0
            for _b in getattr(msg, "content", []) or []:
                if getattr(_b, "type", None) == "text":
                    _txt_len += len(getattr(_b, "text", "") or "")
                elif getattr(_b, "type", None) == "thinking":
                    _txt_len += len(getattr(_b, "thinking", "") or "")
            with open(_log, "a", encoding="utf-8") as f:
                f.write(f"{_t.perf_counter():.6f} UI_RECV textlen={_txt_len}\n")
        # ────────────────────────────────────────────────────────────────
        self._streaming_component.update_content(msg)

        # Surface tool-call blocks that are not tracked yet.
        for block in getattr(msg, "content", []) or []:
            if getattr(block, "type", None) == "toolCall":
                tc_id = getattr(block, "id", "") or ""
                if tc_id and tc_id not in self._pending_tools:
                    card = ToolExecutionComponent(
                        tool_name=getattr(block, "name", ""),
                        tool_call_id=tc_id,
                        args=getattr(block, "arguments", {}) or {},
                        theme=self.theme,
                    )
                    self._pending_tools[tc_id] = card
                    self._add_to_chat(card)
        self._render_streaming_delta()

    def _handle_message_end(self, event: dict) -> None:
        """Finalize the streaming component and flush pending tool errors."""
        msg = event.get("message")
        if msg is None or getattr(msg, "role", None) != "assistant":
            return
        if self._streaming_component is not None:
            self._streaming_component.update_content(msg)

        stop_reason = getattr(msg, "stop_reason", None)
        if stop_reason in ("aborted", "error") and self._pending_tools:
            # Push an error result onto every still-pending tool card.
            err_text = getattr(msg, "error_message", "") or (
                "Aborted after retries" if stop_reason == "aborted" else "Request failed"
            )
            for card in self._pending_tools.values():
                card.set_result(err_text, is_error=True)
            self._pending_tools.clear()
            self.tui.request_render()

        # The streaming component becomes part of chat history as-is.
        self._streaming_component = None

    def _handle_tool_start(self, event: dict) -> None:
        """Create (or reuse) a tool card when tool execution begins."""
        tc_id = event.get("tool_call_id", "")
        card = self._pending_tools.get(tc_id)
        if card is None:
            card = ToolExecutionComponent(
                tool_name=event.get("tool_name", ""),
                tool_call_id=tc_id,
                args=event.get("args", {}) or {},
                theme=self.theme,
            )
            self._pending_tools[tc_id] = card
            self._add_to_chat(card)
        self.tui.request_render()

    def _handle_tool_end(self, event: dict) -> None:
        """Update the tool card with the execution result."""
        tc_id = event.get("tool_call_id", "")
        card = self._pending_tools.get(tc_id)
        if card is not None:
            card.set_result(event.get("result"), event.get("is_error", False))
            self._pending_tools.pop(tc_id, None)
            self.tui.request_render()

    def _handle_compaction_start(self, event: dict) -> None:
        """Show the compaction spinner."""
        reason = event.get("reason", "manual")
        self._show_status_indicator(CompactionStatusIndicator(self.tui, reason, self.theme))

    def _handle_compaction_end(self, event: dict) -> None:
        """Clear the compaction spinner and rebuild chat from the session."""
        self._clear_status_indicator("compaction")
        preview = event.get("summary_preview", "")
        reason = event.get("reason", "unknown")
        # Rebuild the chat from the compacted session history.
        self._rebuild_chat_from_messages()
        if preview:
            self._add_system_message(f"Context compacted ({reason}): {preview}")
        else:
            self._add_system_message(f"Context compacted ({reason}).")

    # ── Status indicator management ───────────────────────────────────────

    def _show_status_indicator(self, indicator: StatusIndicator) -> None:
        """Replace any active status indicator with ``indicator`` and start it."""
        if self._active_status_indicator is not None:
            try:
                self._active_status_indicator.dispose()
            except Exception:
                pass
        self._active_status_indicator = indicator
        self.status_container.clear()
        self.status_container.add_child(indicator)
        indicator.start()
        self.tui.request_render()

    def _clear_status_indicator(self, kind: str | None = None) -> None:
        """Clear the active indicator, optionally only if its kind matches."""
        active = self._active_status_indicator
        if active is None:
            return
        if kind is not None and getattr(active, "kind", None) != kind:
            return
        try:
            active.dispose()
        except Exception:
            pass
        self._active_status_indicator = None
        self.status_container.clear()
        self.tui.request_render()

    # ── Chat rebuild (after compaction) ──────────────────────────────────

    def _rebuild_chat_from_messages(self) -> None:
        """Clear the chat and re-render finalized messages from the session.

        Used after compaction rewrites the session history.
        """
        self.chat_container.clear()
        self._pending_tools.clear()
        self._streaming_component = None
        try:
            entries = self._session.session_manager.get_branch()
        except Exception:
            entries = []
        from agent_core.session.types import SessionMessageEntry
        for entry in entries:
            if not isinstance(entry, SessionMessageEntry) or entry.message is None:
                continue
            msg = entry.message
            role = getattr(msg, "role", None)
            if role == "user":
                content = getattr(msg, "content", "")
                if isinstance(content, list):
                    text = " ".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")
                else:
                    text = str(content or "")
                if text.strip():
                    self._add_user_message(text)
            elif role == "assistant":
                comp = AssistantMessageComponent(
                    message=msg, markdown_theme=self.md_theme, theme=self.theme, output_pad=1,
                )
                self._add_to_chat(comp)
        self.tui.request_render()

    # ── Slash commands ────────────────────────────────────────────────────

    async def _handle_command(self, text: str) -> bool:
        """Dispatch a built-in slash command. Returns True if handled."""
        trimmed = text.strip()
        parts = trimmed.split(maxsplit=1)
        cmd_name = parts[0][1:]  # strip leading /

        # /skill:name — explicit skill invocation. Reads the SKILL.md content
        # and sends it as a user message so the model applies it this turn.
        if cmd_name.startswith("skill:"):
            handled = await self._handle_skill_command(cmd_name, " ".join(parts[1:]) if len(parts) > 1 else "")
            if handled:
                return True

        cmd = next((c for c in BUILTIN_SLASH_COMMANDS if c.name == cmd_name), None)
        if cmd is None or not cmd.active:
            # /thinking was removed in favor of the Shift+Tab hotkey — point
            # users at the new path instead of reporting "unknown command".
            if cmd_name == "thinking":
                self._add_system_message(
                    "`/thinking` 已改为快捷键：按 **Shift+Tab** 循环切换思考级别。"
                )
                return True
            return False

        if cmd_name == "help":
            self._cmd_help()
        elif cmd_name == "clear":
            self._cmd_clear()
        elif cmd_name == "model":
            self._open_model_selector(" ".join(parts[1:]) if len(parts) > 1 else "")
        elif cmd_name == "login":
            self._open_login_dialog()
        elif cmd_name == "logout":
            self._cmd_logout()
        elif cmd_name == "compact":
            await self._cmd_compact()
        elif cmd_name == "quit":
            self._cmd_quit()
        elif cmd_name == "session":
            self._cmd_session()
        elif cmd_name == "name":
            self._cmd_name(" ".join(parts[1:]) if len(parts) > 1 else "")
        elif cmd_name == "new":
            self._cmd_new()
        elif cmd_name == "tree":
            self._cmd_tree()
        elif cmd_name == "export":
            self._cmd_export(" ".join(parts[1:]) if len(parts) > 1 else "")
        elif cmd_name == "copy":
            self._cmd_copy()
        elif cmd_name == "hotkeys":
            self._cmd_hotkeys()
        elif cmd_name == "settings":
            self._cmd_settings(" ".join(parts[1:]) if len(parts) > 1 else "")
        else:
            return False
        return True

    async def _handle_skill_command(self, cmd_name: str, extra: str) -> bool:
        """Handle ``/skill:name [args]`` by loading and sending the skill body.

        Reads the SKILL.md content and sends it as a user turn so the model
        applies the skill instructions for this request. Returns False (so the
        caller falls through to "unknown command") when the name matches no
        loaded skill.
        """
        name = cmd_name[len("skill:"):]
        skill = next((s for s in self._skills_for_command() if s.name == name), None)
        if skill is None:
            return False
        try:
            from pathlib import Path
            body = Path(skill.file_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            self._add_system_message(self.theme.fg("error", f"无法读取技能文件：{e}"))
            return True
        # Strip frontmatter so only the instructional body is sent.
        from coding_agent.core.skills import parse_frontmatter
        _fm, body_content = parse_frontmatter(body)
        prompt = body_content.strip()
        if extra:
            prompt = f"{prompt}\n\n{extra}"
        if not prompt:
            self._add_system_message(f"技能 `{name}` 的内容为空。")
            return True
        self._add_user_message(f"应用技能 `{name}`：\n\n{prompt}")
        self._is_responding = True
        self.editor.disable_submit = True
        try:
            await self._respond(prompt)
        finally:
            self._is_responding = False
            self.editor.disable_submit = False
            self._refresh_footer()
        return True

    def _cmd_help(self) -> None:
        active = get_active_commands()
        lines = ["**可用命令：**\n"]
        for cmd in active:
            lines.append(f"- `/{cmd.name}` — {cmd.description}")
        # Prompt templates (user/project-discovered).
        if self._prompt_templates:
            lines.append("\n**提示词模板：**\n")
            for tpl in self._prompt_templates:
                desc = tpl.description or "提示词模板"
                hint = f" {tpl.argument_hint}" if tpl.argument_hint else ""
                lines.append(f"- `/{tpl.name}{hint}` — {desc}")
        # Skills available for explicit /skill:name invocation.
        skills = self._skills_for_command()
        if skills:
            lines.append("\n**技能：**\n")
            for skill in skills:
                lines.append(f"- `/skill:{skill.name}` — {skill.description or '技能'}")
        self._add_assistant_text("\n".join(lines))

    def _cmd_clear(self) -> None:
        self._session.agent.reset()
        self.chat_container.clear()
        self._tool_cards.clear()
        self._print_welcome()
        self._add_system_message("对话已清空。")

    async def _cmd_compact(self) -> None:
        self._add_system_message("正在压缩上下文…")
        self.tui.request_render()
        result = await self._session.compact("manual")
        if result["performed"]:
            preview = result.get("summary_preview", "")
            self._add_system_message(f"上下文已压缩。{preview}")
        else:
            self._add_system_message(result.get("error", "无需压缩。"))

    def _cmd_quit(self) -> None:
        self._add_system_message("再见！")
        self._running = False

    def _cmd_session(self) -> None:
        # 用 Text 组件 + ANSI 直接拼（不走 Markdown），避免 Markdown list 项
        # 对超长 inline code 不做 wrap、且 list 后段紧贴下一段的问题。
        # - Name 仅在已设置时显示（来自最新 session_info entry）
        # - Cache Read/Write 仅 >0 时显示
        # - Cost 段仅 cost>0 时显示
        # - 数字千分位
        bold = self.theme.bold

        def dim(text: str) -> str:
            return self.theme.fg("dim", text)

        stats = self._session.get_stats()
        name = self._session.session_manager.get_name()

        lines: list[str] = [bold("Session Info"), ""]
        if name:
            lines.append(f"{dim('Name:')} {name}")
        lines.append(f"{dim('File:')} {stats.session_file or 'In-memory'}")
        lines.append(f"{dim('ID:')} {stats.session_id}")
        lines += ["", bold("Messages")]
        lines.append(f"{dim('User:')} {stats.user_messages}")
        lines.append(f"{dim('Assistant:')} {stats.assistant_messages}")
        lines.append(f"{dim('Tool Calls:')} {stats.tool_calls}")
        lines.append(f"{dim('Tool Results:')} {stats.tool_results}")
        lines.append(f"{dim('Total:')} {stats.total_messages}")
        lines += ["", bold("Tokens")]
        lines.append(f"{dim('Input:')} {stats.tokens.input:,}")
        lines.append(f"{dim('Output:')} {stats.tokens.output:,}")
        if stats.tokens.cache_read > 0:
            lines.append(f"{dim('Cache Read:')} {stats.tokens.cache_read:,}")
        if stats.tokens.cache_write > 0:
            lines.append(f"{dim('Cache Write:')} {stats.tokens.cache_write:,}")
        lines.append(f"{dim('Total:')} {stats.tokens.total:,}")
        if stats.cost > 0:
            lines += ["", bold("Cost")]
            lines.append(f"{dim('Total:')} {stats.cost:.4f}")

        self._add_to_chat(Text("\n".join(lines), padding_x=1, padding_y=0))
        self.tui.request_render()

    # ── Model selector ──

    def _provider_for_current_session(self):
        """Return the provider factory matching the current session's model.

        Falls back to DeepSeek when the session has no model yet (shouldn't
        happen post-startup, but keeps the selector usable in early states).
        """
        from agent_llm import deepseek_provider, zhipu_provider
        pname = self._session.model.provider if self._session.model else "deepseek"
        if pname == "zai-coding-cn":
            return zhipu_provider
        return deepseek_provider

    def _provider_env_var_for_current_session(self) -> str | None:
        """First env var the current provider reads its API key from."""
        from coding_agent.cli.main import _provider_env_var
        return _provider_env_var(self._provider_for_current_session()())

    def _available_models(self) -> list:
        """Return models the user can run with the current credentials.

        Filters the global catalog down to models whose provider has auth
        configured (stored key OR env var), so /model only shows providers
        the user has authenticated against, not every built-in provider.
        """
        from coding_agent.core.providers import get_configured_models
        return get_configured_models(
            stored_keys=self._credentials,
            env=dict(os.environ),
        )

    def _open_model_selector(self, search: str) -> None:
        """Open the inline model selector, replacing the editor in the tree.

        Lists every model from every **configured** provider — providers the user has either
        stored a key for via /login OR has an env var set. Unconfigured
        providers are hidden so the user can't accidentally switch to a
        model that will fail with "no API key".

        If ``search`` exactly matches a model id, switch directly without
        opening the selector. Otherwise
        mount the :class:`ModelSelectorComponent` pre-seeded with the search
        text as the filter.
        """
        try:
            models = self._available_models()
        except Exception:
            self._add_system_message("无法获取模型列表。")
            return
        if not models:
            self._add_system_message(
                "没有可用模型。用 /login 配置一个 provider 的 API key。"
            )
            return

        current_id = self._session.model.id if self._session.model else None

        # Exact-match shortcut: don't open the selector at all.
        term = search.strip()
        if term:
            exact = next((m for m in models if m.id == term), None)
            if exact is not None:
                self._session.set_model(exact)
                self._refresh_footer()
                self._update_editor_border_color()
                return

        selector = ModelSelectorComponent(
            self.theme,
            models,
            current_id,
            on_select=self._on_model_selected,
            on_cancel=self._on_selector_cancelled,
            initial_filter=term,
            # The selector uses the static border color and does NOT
            # recolor with thinking level (only the editor does).
        )
        self._swap_editor_for(selector)

    def _on_model_selected(self, model_id: str) -> None:
        """Selector callback: apply the picked model and restore the editor.

        Re-resolves the model from the configured catalog so picking a
        model from a different configured provider works.

        ``_restore_editor`` MUST run even if model application throws —
        otherwise the selector stays mounted and the editor never comes
        back (symptom: selector "关不掉", list keeps growing as user
        presses arrow keys on the orphaned component).
        """
        try:
            try:
                models = self._available_models()
            except Exception:
                models = []
            chosen = next((m for m in models if m.id == model_id), None)
            if chosen is not None:
                self._session.set_model(chosen)
                self._refresh_footer()
                self._update_editor_border_color()
        finally:
            self._restore_editor()

    def _on_selector_cancelled(self) -> None:
        """Selector callback (Esc/Ctrl+C): just restore the editor."""
        self._restore_editor()

    def _cmd_tree(self) -> None:
        """打开会话树选择器（``/tree``）。"""
        from coding_agent.modes.interactive.components.tree_selector import (
            TreeSelectorComponent,
        )
        selector = TreeSelectorComponent(
            theme=self.theme,
            session_manager=self._session.session_manager,
            on_select=self._on_tree_selected,
            on_cancel=self._on_selector_cancelled,
        )
        self._swap_editor_for(selector)

    def _on_tree_selected(self, entry_id: str) -> None:
        """切换到指定分支：持久化叶指针 + 重载 agent 上下文 + 重建聊天 UI。

        与 ``/compact`` 完成后的重建走同一条路径（``load_messages`` +
        ``_rebuild_chat_from_messages``），只是触发源不同。``set_leaf_id``
        会把切换持久化到 JSONL，下次重启会话能恢复到这个分支。
        """
        try:
            sm = self._session.session_manager
            sm.set_leaf_id(entry_id)
            ctx = sm.build_session_context()
            self._session.agent.load_messages(ctx.messages)
            self._rebuild_chat_from_messages()
            self._add_system_message("已切换到所选分支。")
        finally:
            self._restore_editor()

    def _swap_editor_for(self, selector: Any) -> None:
        """Replace the editor with ``selector`` in the TUI tree.

        Works for any component with ``handle_input`` + ``render`` + ``focused``
        (ModelSelectorComponent, LoginDialogComponent, ProviderSelectorComponent).

        Mounts the new component in the editor slot (just above the footer).
        The footer is a stable anchor at the bottom of the tree, so we always
        ``insert_before(footer, ...)`` — this works whether the editor itself
        is currently mounted (first swap) or a previous selector/dialog is
        (subsequent swaps from Esc-back navigation). The previously-mounted
        component is removed first; without that, the old dialogs would
        stack up in the tree (because ``insert_after`` falls back to append
        when the editor isn't found, leaving stale children behind).
        """
        # Hide any autocomplete popup first — it's keyed to the editor.
        ac = getattr(self, "_autocomplete", None)
        if ac is not None:
            ac._close_popup()  # type: ignore[attr-defined]
        # Remove whatever's currently at the editor slot: the editor on first
        # entry, or a previously-swapped selector/dialog on subsequent swaps.
        prev = self._current_selector or self.editor
        self.tui.remove_child(prev)
        self._current_selector = selector
        self.tui.insert_before(self._footer, selector)
        self.tui.set_focus(selector)
        self.tui.request_render()

    def _restore_editor(self) -> None:
        """Put the editor back after a selector closes."""
        selector = getattr(self, "_current_selector", None)
        if selector is not None:
            self.tui.remove_child(selector)
            self._current_selector = None
        if self.editor not in self.tui.children:
            # Re-insert the editor above the footer (which sits at the bottom).
            self.tui.insert_before(self._footer, self.editor)
        self.tui.set_focus(self.editor)
        self.tui.request_render()

    # ── Thinking-level cycle (Shift+Tab) ───────────────────────────────────

    def _cycle_thinking(self) -> None:
        """Advance to the next thinking level the current model supports."""
        from coding_agent.core.defaults import VALID_THINKING_LEVELS

        model = self._session.model
        if model is None or not getattr(model, "reasoning", False):
            self._add_system_message("当前模型不支持思考级别。")
            return

        # Restrict to levels the model actually maps (thinking_level_map),
        # but always include "off" as the cycle's starting point so the user
        # can return to no-reasoning. off is not in the map (it means "unset").
        level_map = getattr(model, "thinking_level_map", None) or {}
        supported = ["off"] + [
            lvl for lvl in VALID_THINKING_LEVELS
            if lvl != "off" and level_map.get(lvl) is not None
        ]
        if len(supported) <= 1:
            self._add_system_message("当前模型只有一个可用思考级别。")
            return

        current = self._session.thinking_level or "off"
        idx = supported.index(current) if current in supported else 0
        nxt = supported[(idx + 1) % len(supported)]
        # "off" is represented at the session level as None (no reasoning).
        self._session.set_thinking_level(None if nxt == "off" else nxt)  # type: ignore[arg-type]
        self._refresh_footer()
        self._update_editor_border_color()


    def _login_provider_options(self) -> list:
        """Build ProviderOption rows for the /login selector.

        Lists every registered provider with its current auth state so the
        user can see at a glance which providers are ready to use. Only API-key
        providers are included.
        """
        from coding_agent.cli.main import _resolve_env_api_key
        from coding_agent.core.providers import ALL_PROVIDER_FACTORIES
        from coding_agent.modes.interactive.components.provider_selector import ProviderOption

        options: list = []
        for factory in ALL_PROVIDER_FACTORIES:
            try:
                p = factory()
            except Exception:
                continue
            if self._get_stored_key_for(p.id):
                state = "configured"
            elif _resolve_env_api_key(p):
                # Any advertised env var set → "env" (walks all aliases, not
                # just the canonical first one).
                state = "env"
            else:
                state = ""
            options.append(ProviderOption(id=p.id, name=p.name, auth_state=state))
        return options

    def _open_login_dialog(self) -> None:
        """Open the login flow: provider selector → API-key dialog.

        Two steps:
          1. Pick which provider to authenticate against.
          2. Type the API key.

        The provider selector is what makes /login work for providers that
        aren't the current session's provider — e.g. logging in to GLM
        while still in a DeepSeek session.

        Esc behavior: pressing Esc in the key dialog returns to the
        provider selector (not the main editor) so the user can pick a
        different provider without restarting /login. Esc in the selector
        itself exits the flow.
        """
        from coding_agent.modes.interactive.components.provider_selector import (
            ProviderSelectorComponent,
        )

        options = self._login_provider_options()
        if not options:
            self._add_system_message("没有可配置的 provider。")
            return

        # Single-provider shortcut: skip the selector, go straight to the key
        # dialog (preserves the old UX when only one provider exists).
        if len(options) == 1:
            self._open_key_dialog_for(options[0].id, options[0].name)
            return

        current_id = self._session.model.provider if self._session.model else None

        def mount_selector() -> None:
            """(Re)mount the provider selector. Called on entry and on Esc-back."""

            def on_select(provider_id: str) -> None:
                name = next((o.name for o in options if o.id == provider_id), provider_id)
                # Pass mount_selector as the Esc-back target so the key dialog
                # returns here instead of the main editor.
                self._open_key_dialog_for(provider_id, name, on_cancel=mount_selector)

            def on_cancel() -> None:
                self._restore_editor()

            selector = ProviderSelectorComponent(
                self.theme,
                options,
                current_id,
                on_select,
                on_cancel,
                title="登录 provider",
            )
            self._swap_editor_for(selector)

        mount_selector()

    def _open_key_dialog_for(
        self,
        provider_id: str,
        provider_name: str,
        *,
        on_cancel: "Callable[[], None] | None" = None,
    ) -> None:
        """Step 2 of /login: the actual API-key input dialog for one provider.

        Replaces whatever is mounted (the selector or the editor) with the
        key dialog. On submit, persists via :meth:`_save_key_for` so the
        saved credential is keyed to ``provider_id`` — not the current
        session's provider (which may differ if the user is logging in to
        a different provider than the session is running).

        Args:
            on_cancel: optional Esc handler. When the dialog was reached
                via the provider selector, this remounts the selector so
                Esc navigates back up the flow instead of exiting to the
                main editor. When None (single-provider shortcut), Esc
                restores the main editor.
        """
        cancel_handler = on_cancel or self._restore_editor

        def on_submit(key: str) -> None:
            key = (key or "").strip()
            if key:
                self._save_key_for(provider_id, key)
                self._complete_provider_authentication(provider_id, provider_name)
            self._restore_editor()

        dialog = LoginDialogComponent(
            self.theme,
            provider_name,
            on_submit,
            cancel_handler,
        )
        self._swap_editor_for(dialog)

    def _complete_provider_authentication(self, provider_id: str, provider_name: str) -> None:
        """Switch to the just-authenticated provider's default model.

        The selected provider becomes active immediately after authentication. Because
        the user explicitly picked it in the /login selector, switching providers is
        intentional. If it already matches the current provider, the model is unchanged.

        On failure (no models, no default configured), surface a message
        telling the user to use /model.
        """
        from coding_agent.core.providers import get_default_model_for_provider

        # Same provider as current session → keep current model, just confirm.
        if self._session.model and self._session.model.provider == provider_id:
            self._add_system_message(
                f"已保存 {provider_name} 的 API key（凭据保存在 {AUTH_FILE}）。"
            )
            return

        default = get_default_model_for_provider(provider_id)
        if default is None:
            self._add_system_message(
                f"已保存 {provider_name} 的 API key，但该 provider 没有可用模型。"
                f"用 /model 手动选择。"
            )
            return
        try:
            self._session.set_model(default)
            self._refresh_footer()
            self._update_editor_border_color()
            self._add_system_message(
                f"已保存 {provider_name} 的 API key，已切换到 {default.id}。"
            )
        except Exception as e:
            self._add_system_message(
                f"已保存 {provider_name} 的 API key，但切换默认模型失败：{e}。用 /model 手动选择。"
            )

    def _cmd_logout(self) -> None:
        """List stored credentials and let the user pick one to remove.

        Only providers with a stored credential are listed (env-var-
        only providers can't be "logged out" — the env var isn't ours to
        delete). If only one credential is stored, remove it directly.
        """
        if not self._credentials:
            self._add_system_message(
                "没有已存凭据。/logout 只会移除 /login 保存的凭据；"
                "环境变量和 models.json 配置不受影响。"
            )
            return

        from coding_agent.core.providers import ALL_PROVIDER_FACTORIES
        from coding_agent.modes.interactive.components.provider_selector import (
            ProviderOption,
            ProviderSelectorComponent,
        )

        # Build display names for any stored provider id.
        name_by_id: dict[str, str] = {}
        for factory in ALL_PROVIDER_FACTORIES:
            try:
                p = factory()
                name_by_id[p.id] = p.name
            except Exception:
                continue

        options = [
            ProviderOption(id=pid, name=name_by_id.get(pid, pid), auth_state="configured")
            for pid in self._credentials
            if isinstance(pid, str)
        ]

        # Single-credential shortcut: no selector.
        if len(options) == 1:
            target = options[0]
            if self._remove_key_for(target.id):
                self._add_system_message(f"已移除 {target.name} 的凭据。")
            else:
                self._add_system_message("没有已存凭据")
            return

        current_id = self._session.model.provider if self._session.model else None

        def on_select(provider_id: str) -> None:
            name = name_by_id.get(provider_id, provider_id)
            if self._remove_key_for(provider_id):
                self._add_system_message(f"已移除 {name} 的凭据。")
            else:
                self._add_system_message("没有已存凭据")
            self._restore_editor()

        def on_cancel() -> None:
            self._restore_editor()

        selector = ProviderSelectorComponent(
            self.theme,
            options,
            current_id,
            on_select,
            on_cancel,
            title="登出 provider",
        )
        self._swap_editor_for(selector)

    def _cmd_name(self, name: str) -> None:
        if name.strip():
            self._session.session_manager.set_name(name.strip())
            self._add_system_message(f"会话名称已设为：{name.strip()}")
        else:
            self._add_system_message("用法：/name <名称>")

    def _cmd_new(self) -> None:
        manager = self._session.new_session()
        self.chat_container.clear()
        self._tool_cards.clear()
        self._print_welcome()
        self._refresh_footer()
        self._add_system_message(f"已创建新会话：{manager.header.id}")

    def _cmd_settings(self, arg: str) -> None:
        """Show settings or persist ``/settings <key> <value>``."""
        manager = self._session.settings_manager
        if manager is None:
            self._add_system_message("当前运行未配置持久化设置管理器。")
            return
        value = arg.strip()
        if not value:
            from dataclasses import asdict
            lines = [f"设置文件：`{manager.path}`"]
            lines.extend(f"- `{key}`: `{item}`" for key, item in asdict(manager.settings).items())
            lines.append("\n修改：`/settings <key> <value>`（大多数设置下次启动生效）")
            self._add_assistant_text("\n".join(lines))
            return
        pieces = value.split(maxsplit=1)
        if len(pieces) != 2:
            self._add_system_message("用法：/settings <key> <value>")
            return
        try:
            settings = manager.set_value(pieces[0], pieces[1])
            self._session.retry_policy = RetryPolicy(
                enabled=settings.auto_retry,
                max_retries=settings.max_retries,
                initial_delay=settings.retry_initial_delay,
                max_delay=settings.retry_max_delay,
            )
        except (OSError, TypeError, ValueError) as exc:
            self._add_system_message(f"设置更新失败：{exc}")
            return
        self._add_system_message(f"已保存 `{pieces[0]}`；必要时重启后生效。")

    def _cmd_export(self, arg: str) -> None:
        """Export the current session to HTML (default) or JSONL."""
        sm = self._session.session_manager
        session_path = getattr(sm, "path", None)
        if session_path is None or not Path(str(session_path)).exists():
            self._add_system_message("当前会话未持久化（in-memory），无法导出。")
            return

        # Output path from the arg, or alongside the session (.html).
        if arg.strip():
            output_path = Path(arg.strip())
        else:
            output_path = Path(str(session_path)).with_suffix(".html")

        if output_path.suffix.lower() == ".jsonl":
            import shutil
            shutil.copyfile(str(session_path), output_path)
            self._add_system_message(f"已导出 JSONL：`{output_path}`")
            return

        # HTML export.
        from coding_agent.cli.main import _render_session_html
        try:
            html = _render_session_html(sm)
            output_path.write_text(html, encoding="utf-8")
            self._add_system_message(f"已导出 HTML：`{output_path}`")
        except Exception as e:
            self._add_system_message(self.theme.fg("error", f"导出失败：{e}"))

    def _cmd_copy(self) -> None:
        """Copy the last assistant message text to the clipboard."""
        last_text = self._last_assistant_text()
        if not last_text:
            self._add_system_message("没有可复制的助手消息。")
            return
        try:
            import subprocess
            # Try clipboard-commands across platforms (clip on Windows,
            # pbcopy on macOS, xclip/wl-copy elsewhere).
            if sys.platform == "win32":
                subprocess.run(["clip"], input=last_text.encode("utf-8"), check=False)
            elif sys.platform == "darwin":
                subprocess.run(["pbcopy"], input=last_text.encode("utf-8"), check=False)
            else:
                for cmd in (["xclip", "-selection", "clipboard"], ["wl-copy"]):
                    try:
                        subprocess.run(cmd, input=last_text.encode("utf-8"), check=True)
                        break
                    except (FileNotFoundError, subprocess.CalledProcessError):
                        continue
                else:
                    raise RuntimeError("no clipboard utility found")
            self._add_system_message(f"已复制 {len(last_text)} 字符到剪贴板。")
        except Exception as e:
            self._add_system_message(self.theme.fg("error", f"复制失败：{e}"))

    def _cmd_hotkeys(self) -> None:
        """Show keyboard shortcuts (generated from the keybinding table)."""
        from coding_agent.core.keybindings import all_hotkeys
        lines = ["**Keyboard shortcuts**\n"]
        last_category = None
        for category, keys, desc in all_hotkeys():
            if category != last_category:
                lines.append(f"\n**{category}**\n")
                last_category = category
            lines.append(f"- `{keys}` — {desc}")
        self._add_assistant_text("\n".join(lines))

    def _last_assistant_text(self) -> str:
        """Return the concatenated text of the last assistant message."""
        try:
            entries = self._session.session_manager.get_branch()
        except Exception:
            return ""
        from agent_core.session.types import SessionMessageEntry
        for entry in reversed(entries):
            if not isinstance(entry, SessionMessageEntry) or entry.message is None:
                continue
            msg = entry.message
            if getattr(msg, "role", None) != "assistant":
                continue
            chunks = []
            for block in getattr(msg, "content", []) or []:
                if getattr(block, "type", None) == "text":
                    chunks.append(getattr(block, "text", ""))
            text = "\n".join(chunks).strip()
            if text:
                return text
        return ""

    # ── Credential persistence ────────────────────────────────────────────

    def _load_credentials(self) -> dict:
        return _credential_store_for(self).load()

    def _get_stored_key(self) -> str | None:
        provider_id = self._session.model.provider
        return self._get_stored_key_for(provider_id)

    def _get_stored_key_for(self, provider_id: str) -> str | None:
        """Read a stored API key for an arbitrary provider (not just current)."""
        cred = self._credentials.get(provider_id)
        if isinstance(cred, dict) and cred.get("type") == "api_key":
            key = cred.get("key")
            if isinstance(key, str) and key:
                return key
        return None

    def _save_key(self, key: str) -> None:
        provider_id = self._session.model.provider
        self._save_key_for(provider_id, key)

    def _save_key_for(self, provider_id: str, key: str) -> None:
        """Persist an API key for an arbitrary provider (multi-provider /login).

        If the saved provider is the current session's provider, also update
        the in-memory ``self._api_key`` cache so the next request picks it up
        without a restart.
        """
        self._credentials[provider_id] = {"type": "api_key", "key": key}
        _credential_store_for(self).save(self._credentials)
        if self._session.model and self._session.model.provider == provider_id:
            self._api_key = key

    def _remove_key(self) -> bool:
        provider_id = self._session.model.provider
        return self._remove_key_for(provider_id)

    def _remove_key_for(self, provider_id: str) -> bool:
        """Remove a stored credential for an arbitrary provider (multi-provider /logout)."""
        if provider_id in self._credentials:
            del self._credentials[provider_id]
            try:
                _credential_store_for(self).save(self._credentials)
            except OSError:
                return False
            if self._session.model and self._session.model.provider == provider_id:
                self._api_key = None
            return True
        if self._session.model and self._session.model.provider == provider_id:
            self._api_key = None
        return False

    def _effective_api_key(self) -> str | None:
        if self._api_key:
            return self._api_key
        # Walk every env var the current provider advertises; this matches
        # the CLI's _resolve_env_api_key so interactive and print modes agree.
        from coding_agent.cli.main import _resolve_env_api_key
        try:
            return _resolve_env_api_key(self._provider_for_current_session()())
        except Exception:
            return None

    # ── Chat helpers ─────────────────────────────────────────────────────

    def _add_user_message(self, text: str) -> None:
        box = UserMessageComponent(
            text,
            markdown_theme=self.md_theme,
            bg_fn=lambda t: self.theme.bg("userMessageBg", t),
        )
        self._add_to_chat(box)

    def _add_assistant_text(self, text: str) -> None:
        container = Container()
        container.add_child(Spacer(1))
        container.add_child(Markdown(text, padding_x=1, padding_y=0, theme=self.md_theme))
        self._add_to_chat(container)

    def _add_system_message(self, text: str) -> None:
        styled = self.theme.italic(self.theme.fg("dim", text))
        self._add_to_chat(Text(styled, padding_x=1, padding_y=0))

    def _add_to_chat(self, component: Component) -> None:
        self.chat_container.add_child(component)
        self.tui.request_render()

    def _refresh_footer(self) -> None:
        """Called after model/thinking/turn changes to keep footer current."""
        self._footer.refresh_git_branch()
        self.tui.request_render()

    # ── Editor border color ─────────────────────────────────────────────────

    #: Thinking level → theme color token.
    _THINKING_COLOR_TOKEN = {
        "off": "thinkingOff",
        "minimal": "thinkingMinimal",
        "low": "thinkingLow",
        "medium": "thinkingMedium",
        "high": "thinkingHigh",
        "xhigh": "thinkingXhigh",
    }

    def _border_color_fn(self) -> "Callable[[str], str]":
        """The border-color closure for the current thinking level."""
        level = self._session.thinking_level or "off"
        token = self._THINKING_COLOR_TOKEN.get(level, "thinkingOff")
        return lambda s: self.theme.fg(token, s)

    def _update_editor_border_color(self) -> None:
        """Recolor the editor's frame to match the current thinking level.

        off→dark gray, high→purple (#b294bb), xhigh→bright purple (#d183e8),
        etc. The editor renders its border by passing ``─`` glyphs through
        ``_border_color_fn``, so swapping that function recolors the frame.
        """
        self.editor._border_color_fn = self._border_color_fn()  # type: ignore[attr-defined]
        self.tui.request_render()

    # ── Welcome ──────────────────────────────────────────────────────────

    def _print_welcome(self) -> None:
        self._add_to_chat(WelcomeComponent(self._session, self.theme, VERSION))

    # ── Main loop ────────────────────────────────────────────────────────

    async def run(self, initial_prompt: Any = None, initial_display_text: str = "") -> None:
        """Start the interactive TUI and run the event loop.

        Interrupt model:
          - Esc is the real interrupt key: aborts streaming, or aborts an
            in-flight compaction when a compaction indicator is active.
          - Ctrl+C clears the editor text; a second Ctrl+C within 500ms quits.
          - Ctrl+D exits when the editor is empty; otherwise
            forward-deletes one char.
        """
        self._print_welcome()
        self.tui.start()

        # Color the editor's frame to reflect the initial thinking level.
        self._update_editor_border_color()

        # Esc interrupt (wired into the focused editor).
        self.editor.on_escape = self._on_escape

        self._register_input_listeners()

        # Autocomplete (slash command names).
        self._setup_autocomplete()

        # Send initial message if provided.
        if initial_prompt is not None:
            self._is_responding = True
            self.editor.disable_submit = True
            self._add_user_message(initial_display_text or "[image attachment]")
            try:
                await self._respond(initial_prompt)
            finally:
                self._is_responding = False
                self.editor.disable_submit = False

        # Event loop.
        while self._running:
            await asyncio.sleep(0.05)

        self.tui.stop()

    def _register_input_listeners(self) -> None:
        """Register the TUI-level input listeners (extracted from run() for
        testability).

        Listeners (run in registration order before the focused component):
          1. Ctrl+C (app.clear): single tap clears the editor, double-tap
             within 500ms quits.
          2. Ctrl+D (app.exit): on an empty editor, exits immediately; on a
             non-empty editor, falls through so the editor forward-deletes.
          3. Shift+Tab (app.thinking.cycle): cycles thinking level when no
             selector is open.
        """
        from coding_agent.core.keybindings import get_keybinding

        # ── 1. Ctrl+C: clear / double-tap-to-quit ───────────────────────
        clear_keys = get_keybinding("app.clear")

        def on_input(data: str) -> dict | None:
            if matches_key(data, clear_keys):  # type: ignore[arg-type]
                now = time.monotonic()
                if now - self._last_sigint_time < 0.5:
                    # Double-tap within 500ms → quit.
                    self._running = False
                else:
                    # Single tap → clear the editor.
                    self._last_sigint_time = now
                    self._clear_editor()
                return {"consume": True}
            return None

        self.tui.add_input_listener(on_input)

        # ── 2. Ctrl+D: exit when empty, else fall through to editor ─────
        #. When the editor has text,
        # Ctrl+D falls through to the editor which forward-deletes.
        exit_keys = get_keybinding("app.exit")

        def on_exit(data: str) -> dict | None:
            if exit_keys and matches_key(data, exit_keys):  # type: ignore[arg-type]
                if not self.editor.get_text():
                    # Empty editor → exit immediately (no double-tap needed,
                    # unlike Ctrl+C).
                    self._add_system_message("再见！")
                    self._running = False
                    return {"consume": True}
                # Non-empty → let the editor forward-delete (return None so
                # the editor's handle_input sees ctrl+d).
            return None

        self.tui.add_input_listener(on_exit)

        # ── 3. Shift+Tab: cycle thinking level ──────────────────────────
        # Only active when no selector is open (otherwise the selector owns
        # nav keys).
        cycle_keys = get_keybinding("app.thinking.cycle")

        def on_thinking_cycle(data: str) -> dict | None:
            if getattr(self, "_current_selector", None) is not None:
                return None
            if cycle_keys and matches_key(data, cycle_keys):  # type: ignore[arg-type]
                self._cycle_thinking()
                return {"consume": True}
            return None

        self.tui.add_input_listener(on_thinking_cycle)

    def stop(self) -> None:
        self._running = False
