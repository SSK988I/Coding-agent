"""System prompt construction.

Assembles the coding-agent system prompt from: an identity sentence, the
available tools (one line each, from ``prompt_snippet``), guidelines (per-tool
``prompt_guidelines`` plus two always-on style rules), optional platform hints
(only when no bash was found), optional project context files
(``<project_context>`` block), optional skills (``<available_skills>`` block),
an optional appended section, and the current date + working directory.

"""
from __future__ import annotations

import datetime
from typing import Any, Iterable, Protocol


# ─── Context file + skill value types ──────────────────────────────────


class _HasPathContent(Protocol):
    """Shape of a context-file entry (``path`` + ``content``)."""

    path: str
    content: str


class _HasSkillFields(Protocol):
    """Shape of a skill entry needed for prompt formatting."""

    name: str
    description: str
    file_path: str
    disable_model_invocation: bool


# ─── XML escape + skill formatting ──


def escape_xml(text: str) -> str:
    """Escape XML special characters.

    ``&`` is replaced first to avoid double-escaping the other entities.
    """
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    text = text.replace("'", "&apos;")
    return text


def format_skills_for_prompt(skills: Iterable[Any]) -> str:
    """Render the ``<available_skills>`` block for the system prompt.

    Returns ``""`` when there are no visible skills. Output starts with two
    leading newlines for concatenation.
    """
    visible = [s for s in skills if not getattr(s, "disable_model_invocation", False)]
    if not visible:
        return ""

    lines = [
        "\n\nThe following skills provide specialized instructions for specific tasks.",
        "Use the read tool to load a skill's file when the task matches its description.",
        "When a skill file references a relative path, resolve it against the skill directory (parent of SKILL.md / dirname of the path) and use that absolute path in tool commands.",
        "",
        "<available_skills>",
    ]
    for skill in visible:
        lines.append("  <skill>")
        lines.append(f"    <name>{escape_xml(getattr(skill, 'name', ''))}</name>")
        lines.append(f"    <description>{escape_xml(getattr(skill, 'description', ''))}</description>")
        lines.append(f"    <location>{escape_xml(getattr(skill, 'file_path', ''))}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


# ─── Context-block builder ───────────


def _build_context_block(context_files: Iterable[Any]) -> str:
    """Build the ``<project_context>`` block from context-file entries."""
    files = list(context_files)
    if not files:
        return ""
    out = "\n\n<project_context>\n\n"
    out += "Project-specific instructions and guidelines:\n\n"
    for f in files:
        path = getattr(f, "path", "")
        content = getattr(f, "content", "")
        out += f'<project_instructions path="{path}">\n{content}\n</project_instructions>\n\n'
    out += "</project_context>\n"
    return out


# ─── Main builder ──────────────────────────────────────────────────────


def build_system_prompt(
    *,
    cwd: str,
    tools: Iterable[Any],
    shell_kind: str = "bash",
    platform: str = "",
    custom_prompt: str | None = None,
    context_files: Iterable[Any] | None = None,
    append_system_prompt: str | None = None,
    skills: Iterable[Any] | None = None,
) -> str:
    """Build the coding-agent system prompt.

    Args:
        cwd: Current working directory using forward slashes.
        tools: Tool objects with optional ``prompt_snippet`` / ``prompt_guidelines``
               attributes and a ``name`` attribute.
        shell_kind: ``"bash"`` if a real bash is in use; ``"system"`` if we
                    fell back to the platform shell. When ``"system"``, platform
                    hints are injected so the model picks the right commands.
        platform: ``sys.platform`` value, used only for the system-shell hint.
        custom_prompt: If set, replaces the default identity text while leaving
                       the rest of prompt assembly unchanged.
        context_files: Iterable of ``{path, content}`` entries (e.g.
                       :class:`ContextFile`). Injected as a ``<project_context>``
                       block.
        append_system_prompt: Text appended after the assembled prompt (the
                              ``appendSystemPrompt``).
        skills: Iterable of skill entries (``name/description/file_path/
                disable_model_invocation``). Injected as an
                ``<available_skills>`` block when the read tool is present.
    """
    tools_list = list(tools)
    prompt_cwd = cwd.replace("\\", "/")

    # Available tools.
    snippets = [
        f"- {t.name}: {t.prompt_snippet}"
        for t in tools_list
        if getattr(t, "prompt_snippet", None)
    ]
    tools_section = "\n".join(snippets) if snippets else "(none)"

    # Guidelines: per-tool + two always-on.
    guidelines: list[str] = []
    seen: set[str] = set()
    for t in tools_list:
        for g in getattr(t, "prompt_guidelines", None) or []:
            g = g.strip()
            if g and g not in seen:
                seen.add(g)
                guidelines.append(g)
    for g in ("Be concise in your responses", "Show file paths clearly when working with files"):
        if g not in seen:
            seen.add(g)
            guidelines.append(g)
    guidelines_section = "\n".join(f"- {g}" for g in guidelines)

    # ── Base prompt: custom override or default identity ──
    if custom_prompt:
        prompt = custom_prompt
    else:
        prompt = (
            "You are an expert coding assistant. You help users by reading files, "
            "executing commands, editing code, and writing new files.\n\n"
            "Available tools:\n"
            f"{tools_section}\n\n"
            "In addition to the tools above, you may have access to other tools depending on the project.\n\n"
            "Guidelines:\n"
            f"{guidelines_section}"
        )

        # Add platform hints only when bash is unavailable (normal sessions inject
        # none, because normal sessions always have bash). On the system-shell fallback, tell the
        # model which OS/shell it's on so it picks Windows commands (dir, type).
        if shell_kind == "system":
            shell_name = "cmd.exe" if platform == "win32" else "sh"
            hint = (
                f"\n\nPlatform: {platform}\n"
                f"Shell: {shell_name} (use this shell's commands; on Windows prefer "
                f"dir / type / findstr over ls / cat / grep)"
            )
            prompt += hint

    # ── Appended section ──
    if append_system_prompt:
        prompt += f"\n\n{append_system_prompt}"

    # ── Project context files ──
    if context_files:
        prompt += _build_context_block(context_files)

    # ── Skills ──
    # Skills instruct the model to "use the read tool to load a skill's file",
    # so they are only useful (and only emitted) when a read tool is present.
    if skills:
        tool_names = {getattr(t, "name", "") for t in tools_list}
        if "read" in tool_names:
            prompt += format_skills_for_prompt(skills)

    # ── Date + cwd last ──
    today = datetime.date.today().isoformat()
    prompt += f"\n\nCurrent date: {today}"
    prompt += f"\nCurrent working directory: {prompt_cwd}"
    return prompt
