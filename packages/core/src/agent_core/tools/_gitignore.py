"""Pure-Python .gitignore support for grep/find fallback paths.

Used when ripgrep/fd are NOT available. When rg/fd are used, they handle
.gitignore natively (and correctly), so this module is only the fallback.

Uses the ``pathspec`` library (the de-facto standard for gitignore matching).
Loads .gitignore files from the search root and all parent directories up to
the filesystem root (mirroring git's behavior for nested repos).

This module keeps fallback searches consistent with normal repository ignore rules.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import pathspec
    _HAS_PATHSPEC = True
except ImportError:
    _HAS_PATHSPEC = False

__all__ = ["is_ignored", "GitignoreMatcher", "has_pathspec"]


def has_pathspec() -> bool:
    """Whether the pathspec library is available (for nicer error messages)."""
    return _HAS_PATHSPEC


class GitignoreMatcher:
    """Accumulate .gitignore patterns from a directory tree and match paths.

    Construct per search root, then call :meth:`is_ignored` for each candidate.
    Patterns are loaded from the root's .gitignore; nested .gitignore files
    deeper in the tree are handled at match time (simplified: only the root
    .gitignore plus any .gitignore in the path's ancestor chain within the root).

    For the grep/fallback use case, this is a pragmatic approximation — rg/fd
    remain the source of truth when available.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._specs: list[tuple[Path, "pathspec.PathSpec"]] = []
        if _HAS_PATHSPEC:
            self._load_chain()

    def _load_chain(self) -> None:
        """Load .gitignore from the root and all ancestor dirs up to FS root."""
        # Walk from root upward.
        current = self.root
        chain: list[Path] = []
        while True:
            gi = current / ".gitignore"
            if gi.is_file():
                chain.append(gi)
            if current.parent == current:
                break
            current = current.parent
        # Also scan for nested .gitignore under root (depth-limited).
        for gi in self._find_nested_gitignores(self.root, max_depth=8):
            if gi not in chain:
                chain.append(gi)
        for gi in chain:
            try:
                lines = gi.read_text(encoding="utf-8", errors="replace").splitlines()
                spec = pathspec.PathSpec.from_lines("gitwildmatch", lines)
                self._specs.append((gi.parent, spec))
            except Exception:
                continue

    @staticmethod
    def _find_nested_gitignores(root: Path, max_depth: int) -> list[Path]:
        """Find .gitignore files under root, up to max_depth deep."""
        results: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            rel = os.path.relpath(dirpath, root)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth > max_depth:
                dirnames[:] = []
                continue
            # Skip .git dir entirely.
            dirnames[:] = [d for d in dirnames if d != ".git"]
            if ".gitignore" in filenames:
                results.append(Path(dirpath) / ".gitignore")
        return results

    def is_ignored(self, abs_path: str | Path, is_dir: bool = False) -> bool:
        """True if abs_path matches any loaded .gitignore rule."""
        if not self._specs:
            return False
        p = Path(abs_path).resolve()
        for rule_root, spec in self._specs:
            try:
                rel = p.relative_to(rule_root)
            except ValueError:
                continue
            rel_str = rel.as_posix()
            if spec.match_file(rel_str):
                return True
            # gitwildmatch also tests with trailing slash for dirs.
            if is_dir and spec.match_file(rel_str + "/"):
                return True
        return False


# Module-level cache so repeated grep/find calls in the same root reuse specs.
_matcher_cache: dict[str, GitignoreMatcher] = {}


def is_ignored(abs_path: str | Path, root: str | Path, is_dir: bool = False) -> bool:
    """Convenience: check abs_path against the .gitignore rules under root."""
    if not _HAS_PATHSPEC:
        return False
    root_resolved = str(Path(root).resolve())
    matcher = _matcher_cache.get(root_resolved)
    if matcher is None:
        matcher = GitignoreMatcher(root_resolved)
        _matcher_cache[root_resolved] = matcher
    return matcher.is_ignored(abs_path, is_dir=is_dir)
