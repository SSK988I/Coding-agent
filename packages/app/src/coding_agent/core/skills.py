"""Skill discovery, loading, and prompt formatting.

A *skill* is a ``SKILL.md`` file with YAML frontmatter declaring at least a
``description``. Skills are discovered from multiple locations and injected
into the system prompt's ``<available_skills>`` block (rendered by
``agent_core.prompts.format_skills_for_prompt``) so the model knows it can
``read`` a skill file for specialized instructions.

Discovery mirrors pi's ``skills.ts``:
  - user skills: ``<agent_dir>/skills/``
  - project skills: ``<cwd>/.coding-agent/skills/``
  - explicit paths (files or directories) passed via ``--skill``

A directory containing ``SKILL.md`` is treated as a skill root and is not
descended into further; otherwise direct ``.md`` children are loaded and
subdirectories are recursed. The skill ``name`` defaults to the parent
directory name when the frontmatter omits it.

Only a minimal frontmatter parser is used here (no PyYAML dependency): the
skill/prompt fields are simple ``key: value`` scalars, so a hand-rolled
parser covering quoted/unquoted strings and booleans is sufficient and keeps
the dependency footprint unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Re-exported from agent_core to avoid duplicating the prompt-rendering logic.
# ``build_system_prompt`` in agent_core reads skill entries duck-typed on
# name/description/file_path/disable_model_invocation, so a coding-agent Skill
# works directly without conversion.
from agent_core.prompts import escape_xml, format_skills_for_prompt  # noqa: F401

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024

#: Directories skipped during recursive skill discovery.
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}


@dataclass
class Skill:
    """A discoverable skill (SKILL.md).

    Only ``name``, ``description`` and ``file_path`` are needed for prompt
    formatting. ``disable_model_invocation`` hides it from the prompt.
    """

    name: str
    description: str
    file_path: str
    base_dir: str = ""
    source: str = "path"
    source_info: object | None = None
    disable_model_invocation: bool = False


@dataclass
class SkillDiagnostic:
    """A warning/collision emitted during skill discovery."""

    type: str  # "warning" | "collision"
    message: str
    path: str


@dataclass
class LoadSkillsResult:
    """Outcome of loading skills from one or more locations."""

    skills: list[Skill] = field(default_factory=list)
    diagnostics: list[SkillDiagnostic] = field(default_factory=list)


# ─── Minimal frontmatter parser ─────────────────────────────────────────


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse a leading ``---\\n...\\n---`` YAML frontmatter block.

    Returns ``(frontmatter, body)``. Only the scalar shapes skills/prompts need
    are supported: unquoted strings, single/double-quoted strings, and the
    booleans ``true``/``false``. Nested structures are not parsed (and not
    needed here). If no frontmatter is present, returns ``({}, content)``.

    ``description:`` values are kept verbatim (no quote stripping) when they
    contain a colon, so values like ``description: http://example.com`` survive.
    """
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---"):
        return {}, normalized

    # The closing fence is a line that is exactly "---" (possibly with
    # trailing whitespace) after the opening one.
    lines = normalized.split("\n")
    # lines[0] == "---"
    close_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break
    if close_idx == -1:
        return {}, normalized

    yaml_lines = lines[1:close_idx]
    body = "\n".join(lines[close_idx + 1:]).strip()

    frontmatter: dict[str, Any] = {}
    for line in yaml_lines:
        # Skip blank lines and comments.
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        idx = line.find(":")
        if idx == -1:
            continue
        key = line[:idx].strip()
        value = line[idx + 1:].strip()
        if not value:
            continue
        frontmatter[key] = _parse_scalar(value)
    return frontmatter, body


def _parse_scalar(raw: str) -> Any:
    """Parse a YAML-ish scalar value.

    Handles ``true``/``false`` booleans and strips surrounding matching quotes.
    Everything else is returned as-is (a string), preserving internal colons.
    """
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    return raw


# ─── Validation ─────────────────────────────────────────────────────────


def validate_skill_name(name: str) -> list[str]:
    """Validate a skill name per the Agent Skills spec.

    Returns a list of error messages (empty if valid).
    """
    errors: list[str] = []
    if len(name) > MAX_NAME_LENGTH:
        errors.append(f"name exceeds {MAX_NAME_LENGTH} characters ({len(name)})")
    import re
    if not re.fullmatch(r"[a-z0-9-]+", name):
        errors.append("name contains invalid characters (must be lowercase a-z, 0-9, hyphens only)")
    if name.startswith("-") or name.endswith("-"):
        errors.append("name must not start or end with a hyphen")
    if "--" in name:
        errors.append("name must not contain consecutive hyphens")
    return errors


def validate_skill_description(description: str | None) -> list[str]:
    """Validate a skill description (required, length-bounded)."""
    errors: list[str] = []
    if not description or not description.strip():
        errors.append("description is required")
    elif len(description) > MAX_DESCRIPTION_LENGTH:
        errors.append(f"description exceeds {MAX_DESCRIPTION_LENGTH} characters ({len(description)})")
    return errors


# ─── Single-file loader ─────────────────────────────────────────────────


def load_skill_from_file(file_path: str | Path, source: str = "path") -> tuple[Skill | None, list[SkillDiagnostic]]:
    """Load one skill from a ``SKILL.md`` file.

    Returns ``(skill_or_none, diagnostics)``. A missing/empty description
    suppresses the skill but still returns validation diagnostics.
    """
    diagnostics: list[SkillDiagnostic] = []
    path = Path(file_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        diagnostics.append(SkillDiagnostic("warning", f"failed to read skill: {e}", str(path)))
        return None, diagnostics

    frontmatter, _body = parse_frontmatter(raw)
    skill_dir = path.parent
    parent_dir_name = skill_dir.name

    description = frontmatter.get("description")
    desc_errors = validate_skill_description(description)
    for err in desc_errors:
        diagnostics.append(SkillDiagnostic("warning", err, str(path)))

    name = frontmatter.get("name") or parent_dir_name
    name_errors = validate_skill_name(name)
    for err in name_errors:
        diagnostics.append(SkillDiagnostic("warning", err, str(path)))

    # Suppress the skill entirely when the description is missing/empty.
    if not description or not str(description).strip():
        return None, diagnostics

    skill = Skill(
        name=str(name),
        description=str(description),
        file_path=str(path.resolve()),
        base_dir=str(skill_dir.resolve()),
        source=source,
        disable_model_invocation=bool(frontmatter.get("disable-model-invocation", False)),
    )
    return skill, diagnostics


# ─── Directory loader ───────────────────────────────────────────────────


def load_skills_from_dir(directory: str | Path, source: str = "path") -> LoadSkillsResult:
    """Load skills from a directory tree.

    Discovery rules (mirrors pi):
      - If the directory contains ``SKILL.md``, treat it as a skill root and do
        not recurse further (load just that file).
      - Otherwise load direct ``.md`` children, then recurse into subdirs.
      - Dot-dirs and known build/vcs dirs are skipped.
    """
    result = LoadSkillsResult()
    root = Path(directory)
    if not root.is_dir():
        return result

    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return result

    # Skill-root short-circuit.
    skill_md = root / "SKILL.md"
    if skill_md.is_file():
        skill, diags = load_skill_from_file(skill_md, source)
        if skill:
            result.skills.append(skill)
        result.diagnostics.extend(diags)
        return result

    for entry in entries:
        name = entry.name
        if name.startswith(".") or name in _SKIP_DIRS:
            continue
        try:
            is_dir = entry.is_dir()
            is_file = entry.is_file()
        except OSError:
            continue
        if is_dir:
            sub = load_skills_from_dir(entry, source)
            result.skills.extend(sub.skills)
            result.diagnostics.extend(sub.diagnostics)
        elif is_file and name.endswith(".md"):
            skill, diags = load_skill_from_file(entry, source)
            if skill:
                result.skills.append(skill)
            result.diagnostics.extend(diags)
    return result


# ─── Multi-source aggregator ────────────────────────────────────────────


def load_skills(
    cwd: str | Path,
    agent_dir: str | Path,
    *,
    skill_paths: Iterable[str] | None = None,
    include_defaults: bool = True,
) -> LoadSkillsResult:
    """Load skills from user, project, and explicit-path sources.

    Sources (first wins per name):
      1. ``<agent_dir>/skills/``  (user scope)
      2. ``<cwd>/.coding-agent/skills/``  (project scope)
      3. Each explicit path in ``skill_paths`` (file or directory)

    Deduplicates by resolved file path (handles symlinks) and by skill name
    (name collisions produce a collision diagnostic, later loaders lose).
    """
    from coding_agent.core.config import CONFIG_DIR_NAME

    resolved_cwd = Path(cwd).resolve()
    resolved_agent_dir = Path(agent_dir).resolve()

    skill_map: dict[str, Skill] = {}
    seen_paths: set[str] = set()
    all_diagnostics: list[SkillDiagnostic] = []
    collisions: list[SkillDiagnostic] = []

    def add(result: LoadSkillsResult) -> None:
        all_diagnostics.extend(result.diagnostics)
        for skill in result.skills:
            try:
                real = str(Path(skill.file_path).resolve())
            except OSError:
                real = skill.file_path
            if real in seen_paths:
                continue
            if skill.name in skill_map:
                collisions.append(SkillDiagnostic(
                    "collision",
                    f'skill name "{skill.name}" collision',
                    skill.file_path,
                ))
                continue
            skill_map[skill.name] = skill
            seen_paths.add(real)

    if include_defaults:
        add(load_skills_from_dir(resolved_agent_dir / "skills", "user"))
        add(load_skills_from_dir(resolved_cwd / CONFIG_DIR_NAME / "skills", "project"))

    user_skills_dir = (resolved_agent_dir / "skills").resolve()
    project_skills_dir = (resolved_cwd / CONFIG_DIR_NAME / "skills").resolve()

    def source_of(resolved_path: Path) -> str:
        try:
            if resolved_path == user_skills_dir or user_skills_dir in resolved_path.parents:
                return "user"
            if resolved_path == project_skills_dir or project_skills_dir in resolved_path.parents:
                return "project"
        except OSError:
            pass
        return "path"

    for raw_path in (skill_paths or []):
        p = Path(raw_path)
        if not p.is_absolute():
            p = resolved_cwd / p
        p = p.resolve()
        if not p.exists():
            all_diagnostics.append(SkillDiagnostic("warning", "skill path does not exist", str(p)))
            continue
        source = source_of(p)
        if p.is_dir():
            add(load_skills_from_dir(p, source))
        elif p.is_file() and p.suffix == ".md":
            skill, diags = load_skill_from_file(p, source)
            all_diagnostics.extend(diags)
            if skill:
                add(LoadSkillsResult(skills=[skill]))
        else:
            all_diagnostics.append(SkillDiagnostic("warning", "skill path is not a markdown file", str(p)))

    return LoadSkillsResult(
        skills=list(skill_map.values()),
        diagnostics=all_diagnostics + collisions,
    )


__all__ = [
    "Skill",
    "SkillDiagnostic",
    "LoadSkillsResult",
    "MAX_NAME_LENGTH",
    "MAX_DESCRIPTION_LENGTH",
    "parse_frontmatter",
    "validate_skill_name",
    "validate_skill_description",
    "load_skill_from_file",
    "load_skills_from_dir",
    "load_skills",
    "escape_xml",
    "format_skills_for_prompt",
]
