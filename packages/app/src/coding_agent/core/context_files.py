"""Context-file discovery.

Discovers ``AGENTS.md`` / ``CLAUDE.md`` files from the user config dir and by
walking from the project cwd up to the filesystem root, returning them in
priority order for injection into the system prompt's ``<project_context>``
block.

"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Candidate filenames, checked in this order within each directory.
#: ``AGENTS.md`` wins over ``CLAUDE.md`` in the same directory.
_CONTEXT_CANDIDATES: tuple[str, ...] = ("AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD")


@dataclass
class ContextFile:
    """A discovered context file (path + content)."""

    path: str
    content: str


def load_context_file_from_dir(directory: str | Path) -> ContextFile | None:
    """Find the first matching context file in ``directory``.

    Returns ``None`` if none exists. Read errors are swallowed (the caller logs a
    yellow warning and continues); here we silently skip.
    """
    dir_path = Path(directory)
    for name in _CONTEXT_CANDIDATES:
        candidate = dir_path / name
        if candidate.is_file():
            try:
                content = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            return ContextFile(path=str(candidate.resolve()), content=content)
    return None


def load_project_context_files(
    cwd: str | Path,
    agent_dir: str | Path,
    *,
    no_context_files: bool = False,
) -> list[ContextFile]:
    """Load context files in priority order: global → ancestors (root→cwd).

    Load order:
      1. Global/user context file from ``agent_dir`` (first).
      2. Ancestor walk from ``cwd`` up to root; each hit is *unshifted* so the
         final list runs outermost-ancestor → cwd.

    Result: ``[global?, outermost-ancestor?, ..., cwd?]``.
    Deduplicated by resolved absolute path.
    """
    if no_context_files:
        return []

    seen: set[str] = set()
    result: list[ContextFile] = []

    # 1. Global / user context (agent_dir).
    global_ctx = load_context_file_from_dir(agent_dir)
    if global_ctx is not None:
        result.append(global_ctx)
        seen.add(Path(global_ctx.path).resolve().as_posix().lower())

    # 2. Ancestor walk from cwd up to the root, unshifting hits so the final
    #    order is root → cwd.
    ancestor_hits: list[ContextFile] = []
    current = Path(cwd).resolve()
    last = None
    while True:
        ctx = load_context_file_from_dir(current)
        if ctx is not None:
            key = Path(ctx.path).resolve().as_posix().lower()
            if key not in seen:
                seen.add(key)
                ancestor_hits.insert(0, ctx)  # unshift → outermost first
        # Stop at the filesystem root: when current equals its parent, or the
        # path stops changing (defensive against odd resolve edge cases).
        parent = current.parent
        if current == parent or str(current) == last:
            break
        last = str(current)
        current = parent

    result.extend(ancestor_hits)
    return result
