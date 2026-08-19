"""Prompt template discovery, argument substitution, and ``/name`` expansion.

A *prompt template* is a ``.md`` file whose body becomes a reusable prompt
invoked as ``/name args`` in the interactive editor. The filename (minus
``.md``) is the template name; the body supports bash-style argument
placeholders (``$1``, ``$@``, ``$ARGUMENTS``, ``${N:-default}``, ``${@:N:L}``).

Templates are discovered from:
  - ``<agent_dir>/prompts/``      (user scope)
  - ``<cwd>/.coding-agent/prompts/``  (project scope)
  - explicit paths via ``--prompt-template``

Mirrors pi's ``prompt-templates.ts``. Uses the minimal frontmatter parser
shared with skills (no PyYAML dependency).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from coding_agent.core.skills import parse_frontmatter

#: Max length of an auto-derived description (from the body's first line).
_MAX_AUTO_DESC = 60


@dataclass
class PromptTemplate:
    """A reusable prompt loaded from a markdown file."""

    name: str
    description: str
    content: str
    file_path: str
    argument_hint: str = ""
    source: str = "path"


# ─── Argument parsing (bash-style, respects quotes) ────────────────────


def parse_command_args(args_string: str) -> list[str]:
    """Split an argument string respecting single/double quotes.

    Mirrors bash word-splitting: whitespace separates args outside quotes,
    matching quote chars toggle quote state. Quote chars are stripped.
    """
    args: list[str] = []
    current = ""
    in_quote: str | None = None
    for char in args_string:
        if in_quote:
            if char == in_quote:
                in_quote = None
            else:
                current += char
        elif char in ('"', "'"):
            in_quote = char
        elif char.isspace():
            if current:
                args.append(current)
                current = ""
        else:
            current += char
    if current:
        args.append(current)
    return args


# ─── Argument substitution ─────────────────────────────────────────────

#: Matches the placeholder forms supported by substitute_args, in priority:
#:   ${N:-default}   ${@:N:L}   ${@:N}   $ARGUMENTS  $@  $N
_PLACEHOLDER_RE = re.compile(
    r"\$\{(\d+):-([^}]*)\}"      # ${1:-default}
    r"|\$\{@:(\d+)(?::(\d+))?\}"  # ${@:N} or ${@:N:L}
    r"|\$(ARGUMENTS|@|\d+)"       # $ARGUMENTS | $@ | $1
)


def substitute_args(content: str, args: list[str]) -> str:
    """Substitute argument placeholders in template content.

    Supports:
      - ``$1``, ``$2``, … — positional args (1-indexed)
      - ``$@`` / ``$ARGUMENTS`` — all args joined by spaces
      - ``${N:-default}`` — positional N, or ``default`` when missing/empty
      - ``${@:N}`` — args from Nth onwards
      - ``${@:N:L}`` — L args starting from Nth

    Replacement values are NOT recursively substituted (a literal ``$1`` in an
    argument is preserved).
    """
    all_args = " ".join(args)

    def _replace(match: re.Match[str]) -> str:
        default_num, default_val, slice_start, slice_len, simple = match.groups()
        if default_num is not None:
            idx = int(default_num) - 1
            val = args[idx] if 0 <= idx < len(args) else ""
            return val if val else (default_val or "")
        if slice_start is not None:
            start = int(slice_start) - 1
            if start < 0:
                start = 0
            if slice_len is not None:
                length = int(slice_len)
                return " ".join(args[start:start + length])
            return " ".join(args[start:])
        if simple in ("ARGUMENTS", "@"):
            return all_args
        idx = int(simple) - 1
        return args[idx] if 0 <= idx < len(args) else ""

    return _PLACEHOLDER_RE.sub(_replace, content)


# ─── Single-file loader ─────────────────────────────────────────────────


def load_template_from_file(file_path: str | Path, source: str = "path") -> PromptTemplate | None:
    """Load one prompt template from a markdown file.

    Returns ``None`` on read/parse failure. The description comes from
    frontmatter, or falls back to the first non-empty body line (truncated).
    """
    path = Path(file_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    frontmatter, body = parse_frontmatter(raw)
    name = path.stem

    description = str(frontmatter.get("description") or "")
    if not description:
        for line in body.split("\n"):
            if line.strip():
                description = line.strip()[:_MAX_AUTO_DESC]
                if len(line.strip()) > _MAX_AUTO_DESC:
                    description += "..."
                break

    return PromptTemplate(
        name=name,
        description=description,
        content=body,
        file_path=str(path.resolve()),
        argument_hint=str(frontmatter.get("argument-hint") or ""),
        source=source,
    )


def _load_templates_from_dir(directory: Path, source: str) -> list[PromptTemplate]:
    """Load direct ``.md`` children of a directory (non-recursive)."""
    templates: list[PromptTemplate] = []
    if not directory.is_dir():
        return templates
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError:
        return templates
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            is_file = entry.is_file()
        except OSError:
            continue
        if is_file and entry.suffix == ".md":
            tpl = load_template_from_file(entry, source)
            if tpl is not None:
                templates.append(tpl)
    return templates


# ─── Multi-source aggregator ────────────────────────────────────────────


def load_prompt_templates(
    cwd: str | Path,
    agent_dir: str | Path,
    *,
    prompt_paths: Iterable[str] | None = None,
    include_defaults: bool = True,
) -> list[PromptTemplate]:
    """Load templates from user, project, and explicit-path sources.

    First definition wins per name (user scope precedes project precedes
    explicit paths). Deduplicated by template name.
    """
    from coding_agent.core.config import CONFIG_DIR_NAME

    resolved_cwd = Path(cwd).resolve()
    resolved_agent_dir = Path(agent_dir).resolve()

    templates: list[PromptTemplate] = []
    seen: set[str] = set()

    def add(tpls: list[PromptTemplate]) -> None:
        for tpl in tpls:
            if tpl.name not in seen:
                seen.add(tpl.name)
                templates.append(tpl)

    if include_defaults:
        add(_load_templates_from_dir(resolved_agent_dir / "prompts", "user"))
        add(_load_templates_from_dir(resolved_cwd / CONFIG_DIR_NAME / "prompts", "project"))

    user_prompts_dir = (resolved_agent_dir / "prompts").resolve()
    project_prompts_dir = (resolved_cwd / CONFIG_DIR_NAME / "prompts").resolve()

    def source_of(p: Path) -> str:
        try:
            if p == user_prompts_dir or user_prompts_dir in p.parents:
                return "user"
            if p == project_prompts_dir or project_prompts_dir in p.parents:
                return "project"
        except OSError:
            pass
        return "path"

    for raw_path in (prompt_paths or []):
        p = Path(raw_path)
        if not p.is_absolute():
            p = resolved_cwd / p
        try:
            p = p.resolve()
        except OSError:
            continue
        if not p.exists():
            continue
        source = source_of(p)
        if p.is_dir():
            add(_load_templates_from_dir(p, source))
        elif p.is_file() and p.suffix == ".md":
            tpl = load_template_from_file(p, source)
            if tpl is not None:
                add([tpl])
    return templates


# ─── /name expansion ────────────────────────────────────────────────────

_NAME_RE = re.compile(r"^/(?P<name>\S+)(?:\s+(?P<args>[\s\S]*))?$")


def expand_prompt_template(text: str, templates: list[PromptTemplate]) -> str:
    """Expand a ``/name args`` line into template content.

    Returns the original ``text`` unchanged when it is not a ``/``-command or
    does not match any template name.
    """
    if not text.startswith("/"):
        return text
    match = _NAME_RE.match(text)
    if match is None:
        return text
    name = match.group("name")
    args_string = match.group("args") or ""
    for tpl in templates:
        if tpl.name == name:
            return substitute_args(tpl.content, parse_command_args(args_string))
    return text


__all__ = [
    "PromptTemplate",
    "parse_command_args",
    "substitute_args",
    "load_template_from_file",
    "load_prompt_templates",
    "expand_prompt_template",
]
