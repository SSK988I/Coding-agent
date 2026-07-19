"""Fail a release when workspace package versions drift apart."""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FILES = [
    ROOT / "pyproject.toml",
    ROOT / "packages/llm/pyproject.toml",
    ROOT / "packages/core/pyproject.toml",
    ROOT / "packages/tui/pyproject.toml",
    ROOT / "packages/app/pyproject.toml",
]
SOURCE_FILES = [
    ROOT / "packages/app/src/coding_agent/__init__.py",
    ROOT / "packages/app/src/coding_agent/core/config.py",
    ROOT / "packages/core/src/agent_core/__init__.py",
]


def _version(path: Path) -> str:
    match = re.search(r'(?m)^(?:version\s*=\s*|__version__\s*=\s*|VERSION:\s*str\s*=\s*)["\']([^"\']+)', path.read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit(f"No version found in {path.relative_to(ROOT)}")
    return match.group(1)


def main() -> int:
    versions = {path.relative_to(ROOT).as_posix(): _version(path) for path in FILES + SOURCE_FILES}
    unique = set(versions.values())
    if len(unique) != 1:
        for path, version in versions.items():
            print(f"{path}: {version}")
        return 1
    print(f"Workspace version: {unique.pop()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
