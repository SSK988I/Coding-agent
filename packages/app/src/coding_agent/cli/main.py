"""CLI main entry point.

Orchestrates the full startup flow: parse args → configure paths → create
session → create AgentSession → dispatch to mode.

"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, cast

from agent_llm import ThinkingLevel, create_models, deepseek_provider, zhipu_provider

from coding_agent.cli.args import Args, parse_args, resolve_app_mode
from coding_agent.cli.file_processor import process_file_arguments
from coding_agent.cli.initial_message import build_initial_message
from coding_agent.core.context_files import load_project_context_files
from coding_agent.core.agent_session import (
    AgentSession,
    AgentSessionConfig,
)
from coding_agent.core.config import (
    get_agent_dir,
    get_auth_path,
    get_sessions_dir,
)
from coding_agent.core.credentials import CredentialStore
from coding_agent.core.retry import RetryPolicy
from coding_agent.core.settings import SettingsManager


# ─── Credential helpers ──────────────────────────────────────────────────


def _load_api_key(provider_id: str) -> str | None:
    """Read the stored API key for a provider from auth.json."""
    return CredentialStore(get_auth_path()).get_api_key(provider_id)


# ─── Provider selection (multi-provider dispatch) ────────────────────────


#: Aliases users may pass via ``--provider`` that resolve to Zhipu. Keep this
#: small and obvious; the canonical provider id is ``zai-coding-cn``, but
#: ``zhipu`` / ``glm`` / ``zai`` are far more discoverable.
_ZHIPU_ALIASES = {"zhipu", "glm", "zai", "zai-coding-cn"}
_DEEPSEEK_ALIASES = {"deepseek"}


def _select_provider(args: Args) -> Any:
    """Pick a Provider based on ``--provider``.

    Defaults to DeepSeek when the flag is absent, preserving pre-v1 behavior.
    A future ModelRegistry will replace this dispatch; for now it's the single
    point that decides which provider factory runs.
    """
    name = (args.provider or "deepseek").lower().strip()
    if name in _ZHIPU_ALIASES:
        return zhipu_provider()
    if name in _DEEPSEEK_ALIASES:
        return deepseek_provider()
    supported = "deepseek、zhipu（别名：glm、zai、zai-coding-cn）"
    print(f"错误：未知 Provider '{args.provider}'。支持：{supported}", file=sys.stderr)
    raise SystemExit(2)


def _normalize_model_options(args: Args) -> None:
    """Apply documented ``provider/model:thinking`` shorthand to ``args``."""
    value = (args.model or "").strip()
    if not value:
        return

    model_value, separator, suffix = value.rpartition(":")
    if separator and suffix in {"off", "minimal", "low", "medium", "high", "xhigh"}:
        args.model = model_value
        if args.thinking is None:
            args.thinking = suffix

    value = args.model or ""
    if "/" in value:
        provider_name, model_id = value.split("/", 1)
        if args.provider is None:
            args.provider = provider_name
        args.model = model_id


def _provider_env_var(provider: Any) -> str | None:
    """Return the canonical env var name a provider reads its API key from.

    Walks the provider's ``auth.api_key.env_vars`` list (most providers declare
    at least one). Returns the first so ``--list-models`` and auth-status
    messages show the same env var the auth resolver will actually consult.
    """
    try:
        env_vars = provider.auth.api_key.env_vars
        if env_vars:
            return env_vars[0]
    except AttributeError:
        pass
    return None


def _resolve_env_api_key(provider: Any) -> str | None:
    """Read the first non-empty env var the provider advertises."""
    try:
        for name in provider.auth.api_key.env_vars:
            val = os.environ.get(name)
            if val:
                return val
    except AttributeError:
        pass
    return None


def _resolve_api_key_for(provider_id: str, *, override: "str | None" = None) -> str | None:
    """Resolve the API key for an arbitrary provider id at call time.

    Used as the live key resolver that ``AgentSession.get_api_key`` delegates
    to. Reads ``auth.json`` and the advertised env vars **every call**, so
    model switches (which change ``provider_id``) get the right key without
    restarting. This is the fix for the "switch to GLM but the closure still
    handed the session the DeepSeek key" bug.

    Args:
        provider_id: e.g. ``"deepseek"`` or ``"zai-coding-cn"``.
        override: if set (e.g. ``--api-key`` flag), returned verbatim without
            touching auth.json or environment variables. Explicit keys take precedence.
    """
    if override:
        return override
    # 1. Stored credential in auth.json.
    stored = _load_api_key(provider_id)
    if stored:
        return stored
    # 2. Any env var this provider advertises. Look it up via the matching
    #    built-in provider factory so we know which env var names to check.
    from coding_agent.core.providers import _all_providers
    for p in _all_providers():
        if p.id == provider_id:
            return _resolve_env_api_key(p)
    return None


# ─── Session manager creation ─────────────────────────────────────────────


def _create_session_manager(args: Args, cwd: str) -> Any:
    """Create or open a SessionManager based on CLI args.

    Supports selecting, forking, naming, and redirecting session storage.
    """
    from agent_core.session import SessionManager

    sessions_dir = (
        Path(args.session_dir).expanduser().resolve()
        if args.session_dir
        else get_sessions_dir()
    )

    def find_session(reference: str) -> Any | None:
        path = Path(reference).expanduser()
        if not path.is_absolute():
            path = Path(cwd) / path
        if path.is_file():
            return SessionManager.open(path, sessions_dir=sessions_dir)
        available = SessionManager.list_sessions(
            cwd=cwd, sessions_dir=sessions_dir,
        )
        exact = next((info for info in available if info.id == reference), None)
        if exact is not None:
            return SessionManager.open(exact.path, sessions_dir=sessions_dir)
        partial = [info for info in available if info.id.startswith(reference)]
        if len(partial) == 1:
            return SessionManager.open(partial[0].path, sessions_dir=sessions_dir)
        if len(partial) > 1:
            print(f"错误：会话引用 '{reference}' 匹配到多个会话。", file=sys.stderr)
            sys.exit(1)
        return None

    if args.no_session:
        return SessionManager.create(cwd=cwd, in_memory=True)

    if args.session:
        selected = find_session(args.session)
        if selected is not None:
            return selected
        print(f"错误：找不到与 '{args.session}' 匹配的会话。", file=sys.stderr)
        sys.exit(1)

    if args.fork_session:
        source = find_session(args.fork_session)
        if source is None:
            print(
                f"错误：找不到与 '{args.fork_session}' 匹配的会话。",
                file=sys.stderr,
            )
            sys.exit(1)
        return SessionManager.fork(
            source, cwd=cwd, sessions_dir=sessions_dir,
        )

    if args.session_id:
        available = SessionManager.list_sessions(
            cwd=cwd, sessions_dir=sessions_dir,
        )
        exact = next((info for info in available if info.id == args.session_id), None)
        if exact is not None:
            return SessionManager.open(exact.path, sessions_dir=sessions_dir)
        try:
            return SessionManager.create(
                cwd=cwd,
                sessions_dir=sessions_dir,
                session_id=args.session_id,
            )
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            sys.exit(2)

    if args.continue_session:
        # Continue the most recent session.
        recent = SessionManager.continue_recent(
            cwd=cwd, sessions_dir=sessions_dir,
        )
        if recent is not None:
            return recent
        print("错误：找不到可继续的最近会话。", file=sys.stderr)
        sys.exit(1)

    # Default: create a new session.
    return SessionManager.create(cwd=cwd, sessions_dir=sessions_dir)


# ─── Model resolution ────────────────────────────────────────────────────


def _resolve_model(args: Args, provider: Any) -> Any:
    """Resolve the model from CLI args or default.

    Uses ``--model`` when provided, otherwise the provider's first model.
    """
    available = provider.get_models()
    if not available:
        print("错误：没有可用模型。", file=sys.stderr)
        sys.exit(1)

    if args.model:
        # Match by id (exact or prefix).
        for m in available:
            if m.id == args.model:
                return m
        for m in available:
            if m.id.startswith(args.model):
                return m
        # Also try provider/model format.
        if "/" in args.model:
            prov, mid = args.model.split("/", 1)
            if prov == provider.id:
                for m in available:
                    if m.id == mid or m.id.startswith(mid):
                        return m
        print(f"错误：找不到模型 '{args.model}'。可用模型：{[m.id for m in available]}", file=sys.stderr)
        sys.exit(1)

    return available[0]


def _load_context_for_run(args: Args, cwd: str, agent_dir: Path) -> list | None:
    """Load project instructions unless context discovery or trust is disabled."""
    if args.no_context_files or args.project_trust_override is False:
        return None
    return load_project_context_files(cwd, agent_dir) or None


# ─── Stdout takeover ─────────


def _configure_output_encoding(*streams: Any) -> None:
    """Use UTF-8 for CLI output even when Windows redirects to a legacy code page."""
    if not streams:
        streams = (sys.stdout, sys.stderr)
    for stream in streams:
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            # StringIO, detached streams, and embedded hosts may not support
            # reconfiguration. Keep their existing behavior in that case.
            pass


def _take_over_stdout() -> None:
    """Reconfigure stdout for unbuffered streaming in print mode.

    Reconfigure ``sys.stdout`` to ``write_through=True`` so streamed text-delta tokens
    reach the terminal/pipe immediately instead of waiting for a 4KB block buffer
    to fill. Also set ``line_buffering`` for interactive feel.

    On Python < 3.7 (no ``reconfigure``), fall back to reassigning stdout to a
    raw ``os.write(1, ...)`` wrapper.
    """
    # Ensure the underlying buffer writes through immediately.
    try:
        sys.stdout.reconfigure(line_buffering=True, write_through=True)  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        # reconfigure unavailable or already detached — best-effort fallback.
        try:
            sys.stdout = open(sys.stdout.fileno(), "w", buffering=1, encoding="utf-8")  # type: ignore[assignment]
        except Exception:
            pass


# ─── Main entry point ────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the coding agent CLI.

    Startup flow:
      parse args → resolve model → create session → create AgentSession →
      dispatch to mode.

    Supported modes are interactive (default) and print.
    """
    # argparse may print localized help before normal runtime setup. Windows
    # pipes can default to cp1252, which cannot encode the Chinese help text.
    _configure_output_encoding()
    args = parse_args(argv)

    # ── Metadata commands (no runtime needed) ────────────────────────────
    if args.list_models is not None:
        _cmd_list_models(args)
        return 0

    if args.export_path is not None:
        _cmd_export(args)
        return 0

    # ── Determine mode ───────────────────────────────────────────────────
    cwd = os.getcwd()
    agent_dir = get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    app_mode = resolve_app_mode(args)

    # ── Take over stdout for non-interactive streaming ──
    # In print mode, reconfigure stdout to unbuffered/write-through so streamed
    # tokens reach the terminal immediately. Without this, Python's default
    # block buffering (4KB) on non-tty/piped stdout holds tokens until a buffer
    # fills, making the output appear in chunks instead of streaming live.
    if app_mode == "print":
        _take_over_stdout()

    # ── Read piped stdin ─────────────────────────────────────────────────
    stdin_content: str | None = None
    if app_mode != "interactive" and not sys.stdin.isatty():
        stdin_content = sys.stdin.read().strip() or None

    # ── Session + persisted runtime options ──────────────────────────────
    _normalize_model_options(args)
    session_manager = _create_session_manager(args, cwd)
    persisted = session_manager.build_session_context()
    if persisted.model:
        if args.provider is None:
            args.provider = persisted.model.get("provider")
        if args.model is None:
            args.model = persisted.model.get("model_id")
    if args.thinking is None and persisted.thinking_level is not None:
        args.thinking = persisted.thinking_level

    settings_manager = SettingsManager()
    settings = settings_manager.load()
    if args.provider is None:
        args.provider = settings.default_provider
    if args.model is None:
        args.model = settings.default_model
    if args.thinking is None:
        args.thinking = settings.thinking_level
    _normalize_model_options(args)
    if args.name:
        session_manager.set_name(args.name)

    # ── Provider + model ─────────────────────────────────────────────────
    provider = _select_provider(args)
    models = create_models()
    models.set_provider(provider)
    model = _resolve_model(args, provider)

    # ── API key ──────────────────────────────────────────────────────────
    # The resolver delegates to _resolve_api_key_for(provider_id) every call,
    # so model switches at runtime (e.g. /model glm-5.2 from a DeepSeek
    # session) re-resolve to the destination provider's key instead of
    # returning the launch-time provider's key. The ``args.api_key`` override
    # (if set via --api-key) pins a single key for all providers for this run.
    override_key = args.api_key

    def _get_api_key(provider_id: str) -> str | None:
        return _resolve_api_key_for(provider_id, override=override_key)

    # ── Thinking level ───────────────────────────────────────────────────
    reasoning: ThinkingLevel | None = None
    if args.thinking and args.thinking != "off":
        reasoning = cast(ThinkingLevel, args.thinking)

    # ── Build initial message (stdin + @file text + first positional) ────
    # Combine stdin, referenced files, and the first positional message.
    file_text = ""
    file_images = None
    if args.file_args:
        processed = process_file_arguments(args.file_args, cwd)
        file_text = processed.text
        file_images = processed.images or None
    if file_images and "image" not in (model.input or []):
        print(
            f"错误：模型 '{model.provider}/{model.id}' 不支持图片输入。",
            file=sys.stderr,
        )
        return 2

    initial_result = build_initial_message(
        args, file_text=file_text or None, file_images=file_images,
        stdin_content=stdin_content,
    )
    initial_text = initial_result.initial_message or ""
    initial_prompt = initial_result.to_prompt()
    # Remaining positional messages become follow-up prompts in print mode.
    follow_up_messages = list(args.messages)

    # ── Create AgentSession ──────────────────────────────────────────────
    # Discover project context files (AGENTS.md/CLAUDE.md) unless disabled.
    context_files = _load_context_for_run(args, cwd, agent_dir)

    config = AgentSessionConfig(
        model=model,
        cwd=cwd,
        session_manager=session_manager,
        get_api_key=_get_api_key,
        reasoning=reasoning,
        system_prompt=args.system_prompt,
        append_system_prompt=(
            "\n\n".join(args.append_system_prompt) if args.append_system_prompt else None
        ),
        context_files=context_files,
        retry_policy=RetryPolicy(
            enabled=settings.auto_retry,
            max_retries=settings.max_retries,
            initial_delay=settings.retry_initial_delay,
            max_delay=settings.retry_max_delay,
        ),
        settings_manager=settings_manager,
        theme_name=settings.theme,
    )
    if args.no_tools:
        config.no_tools = True
    if args.no_builtin_tools:
        config.no_builtin_tools = True
    if args.tools:
        config.allowed_tool_names = args.tools
    if args.exclude_tools:
        config.excluded_tool_names = args.exclude_tools

    session = AgentSession(config)

    # ── Dispatch to mode ─────────────────────────────────────────────────
    if app_mode == "interactive":
        return _run_interactive(session, initial_prompt, initial_text)
    else:
        return _run_print(session, initial_prompt, follow_up_messages, args.output_mode)


# ─── Mode runners ────────────────────────────────────────────────────────


def _run_interactive(
    session: AgentSession,
    initial_prompt: Any,
    initial_display_text: str = "",
) -> int:
    """Run the interactive TUI mode.

    ``InteractiveMode`` is imported lazily to avoid circular dependencies.
    """
    from coding_agent.modes.interactive.interactive_mode import InteractiveMode

    mode = InteractiveMode(session)
    try:
        asyncio.run(mode.run(initial_prompt, initial_display_text))
    except KeyboardInterrupt:
        return 130
    finally:
        session.dispose()
    return 0


def _run_print(
    session: AgentSession,
    initial_prompt: Any,
    follow_ups: list[str] | None = None,
    mode: str = "text",
) -> int:
    """Run in print (non-interactive) mode.

    Sends the initial message, then any follow-up positional messages (the
    remainder after the first was consumed for the initial prompt).

    - ``mode="text"`` (default): stream the assistant's text to stdout, with
      tool calls and errors on stderr.
    - ``mode="json"``: write one JSON object per line per AgentSession event
      to stdout for machine-readable consumption.
    """
    follow_ups = follow_ups or []

    async def _print() -> int:
        if initial_prompt is None and not follow_ups:
            print("错误：非交互模式没有收到输入。", file=sys.stderr)
            return 2

        had_error = False

        def _is_terminal_error(event: dict) -> bool:
            if event.get("type") != "message_end":
                return False
            message = event.get("message")
            return getattr(message, "stop_reason", None) == "error"

        if mode == "json":
            # JSON mode: serialize every event as one JSONL line.
            def _on_event_json(event: dict) -> None:
                nonlocal had_error
                had_error = had_error or _is_terminal_error(event)
                sys.stdout.write(json.dumps(event, default=_json_default) + "\n")
                sys.stdout.flush()

            session.on_event(_on_event_json)
            if initial_prompt is not None:
                await session.prompt(initial_prompt)
            for msg in follow_ups:
                if had_error:
                    break
                await session.prompt(msg)
            return 1 if had_error else 0

        # Text mode (default).
        output_parts: list[str] = []

        def _on_event(event: dict) -> None:
            nonlocal had_error
            etype = event.get("type")
            if etype == "message_update":
                inner = event.get("event", {})
                if inner.get("type") == "text_delta":
                    delta = inner.get("delta", "")
                    output_parts.append(delta)
                    sys.stdout.write(delta)
                    sys.stdout.flush()
            elif etype == "tool_execution_start":
                print(f"\n[工具] {event.get('tool_name', '?')}({event.get('args', {})})", file=sys.stderr)
            elif etype == "tool_execution_end":
                status = "失败" if event.get("is_error") else "完成"
                print(f"[工具] {event.get('tool_name', '?')} -> {status}", file=sys.stderr)
            elif etype == "message_end":
                msg = event.get("message")
                if msg is not None and getattr(msg, "stop_reason", None) == "error":
                    had_error = True
                    err = getattr(msg, "error_message", "") or "未知错误"
                    print(f"\n错误：{err}", file=sys.stderr)

        session.on_event(_on_event)
        if initial_prompt is not None:
            await session.prompt(initial_prompt)
        for msg in follow_ups:
            if had_error:
                break
            sys.stdout.write("\n")
            sys.stdout.flush()
            output_parts.clear()
            await session.prompt(msg)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return 1 if had_error else 0

    try:
        return asyncio.run(_print())
    except KeyboardInterrupt:
        return 130
    finally:
        session.dispose()


def _json_default(obj: Any) -> Any:
    """JSON serializer for objects json.dumps can't handle by default.

    Handles dataclasses (agent_llm messages, usage, etc.), datetimes, sets, enums,
    Path, and objects with ``__dict__``. Falls back to ``str(obj)``.
    """
    import dataclasses
    import datetime
    import enum
    from pathlib import Path
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(cast(Any, obj))
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


# ─── Metadata commands ────────────────────────────────────────────────────


def _cmd_list_models(args: Args) -> None:
    """Handle --list-models."""
    provider = _select_provider(args)
    models = provider.get_models()
    search = args.list_models or ""

    for m in models:
        if search and search.lower() not in m.id.lower() and search.lower() not in m.name.lower():
            continue
        env_var = _provider_env_var(provider)
        has_env = bool(env_var and os.environ.get(env_var))
        auth = "已配置认证" if has_env or _load_api_key(provider.id) else "未配置认证"
        print(
            f"{m.id:30s} {m.name:30s} 上下文={m.context_window:>7d} "
            f"最大输出={m.max_tokens:>7d} [{auth}]"
        )


def _cmd_export(args: Args) -> None:
    """Handle --export.

    Exports a session file to HTML (default) or JSONL (when the output path
    ends in ``.jsonl``). The output path is taken from the first positional
    message argument; if omitted, HTML is written next to the session.
    """
    if args.export_path is None:
        raise ValueError("必须提供 export_path")
    session_path = Path(args.export_path)
    if not session_path.exists():
        print(f"错误：找不到会话文件：{session_path}", file=sys.stderr)
        sys.exit(1)

    # Output path: first positional arg, else alongside the session (.html).
    output_path_str = args.messages[0] if args.messages else None
    if output_path_str:
        output_path = Path(output_path_str)
    else:
        output_path = session_path.with_suffix(".html")

    from agent_core.session import SessionManager
    try:
        sm = SessionManager.open(str(session_path))
    except Exception as e:
        print(f"读取会话失败：{e}", file=sys.stderr)
        sys.exit(1)

    if output_path.suffix.lower() == ".jsonl":
        # JSONL export: copy the session file verbatim (it's already JSONL).
        import shutil
        shutil.copyfile(session_path, output_path)
        print(f"已导出到：{output_path}")
        return

    # HTML export: render messages into a self-contained HTML document.
    html = _render_session_html(sm)
    output_path.write_text(html, encoding="utf-8")
    print(f"已导出到：{output_path}")


def _render_session_html(sm: Any) -> str:
    """Render a session to a minimal self-contained HTML document.

    Produces a plain-text message dump with role headers in a self-contained page.
    """
    import html as html_lib

    ctx = sm.build_session_context()
    title = getattr(sm.header, "name", None) or sm.header.id
    cwd = getattr(sm.header, "cwd", "")

    body_parts: list[str] = [f"<h1>{html_lib.escape(title)}</h1>"]
    if cwd:
        body_parts.append(f"<p><em>cwd: {html_lib.escape(cwd)}</em></p>")
    body_parts.append("<div class=\"messages\">")

    for msg in ctx.messages:
        role = getattr(msg, "role", "?")
        content = getattr(msg, "content", "")
        if isinstance(content, list):
            chunks = []
            for block in content:
                btype = getattr(block, "type", None)
                if btype == "text":
                    chunks.append(getattr(block, "text", ""))
                elif btype == "thinking":
                    chunks.append(f"[thinking] {getattr(block, 'thinking', '')}")
                elif btype == "toolCall":
                    chunks.append(f"[tool call: {getattr(block, 'name', '')}]")
            text = "\n".join(chunks)
        else:
            text = str(content or "")
        cls = "user" if role == "user" else ("assistant" if role == "assistant" else "tool")
        body_parts.append(
            f"<div class=\"msg {cls}\"><div class=\"role\">{html_lib.escape(role)}</div>"
            f"<pre>{html_lib.escape(text)}</pre></div>"
        )
    body_parts.append("</div>")

    style = (
        "body{font-family:system-ui,sans-serif;max-width:900px;margin:2em auto;padding:0 1em}"
        ".msg{border-left:3px solid #ccc;margin:1em 0;padding-left:1em}"
        ".msg.user{border-color:#3b82f6}.msg.assistant{border-color:#10b981}"
        ".msg.tool{border-color:#f59e0b}.role{font-weight:bold;color:#555;font-size:.85em}"
        "pre{white-space:pre-wrap;word-wrap:break-word}"
    )
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<title>{html_lib.escape(title)}</title><style>{style}</style></head>"
        f"<body>{''.join(body_parts)}</body></html>"
    )


# ─── Direct execution ────────────────────────────────────────────────────

if __name__ == "__main__":
    raise SystemExit(main())
