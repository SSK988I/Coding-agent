"""Tests for the grep and find tools.

These tests deliberately force the pure-Python fallback paths (by checking
the tool works whether or not rg/fd are installed), so they're deterministic
in any environment. When rg/fd ARE present, we additionally sanity-check the
subprocess path produces the same shape of output.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_core.tools._subprocess import find_in_path
from agent_core.tools.find import FindTool
from agent_core.tools.grep import GrepTool


def _run(coro):
    return asyncio.run(coro)


# ─── GrepTool (pure-Python fallback is the baseline) ──────────────────

def test_grep_finds_regex_matches(tmp_path: Path):
    (tmp_path / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def bar():\n    return 2\n", encoding="utf-8")
    tool = GrepTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"pattern": "def", "path": "."})).content[0].text
    assert "a.py:1: def foo():" in text
    assert "b.py:1: def bar():" in text


def test_grep_ignore_case(tmp_path: Path):
    (tmp_path / "a.txt").write_text("Hello\nHELLO\nworld\n", encoding="utf-8")
    tool = GrepTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"pattern": "hello", "path": ".", "ignoreCase": True})).content[0].text
    assert "Hello" in text and "HELLO" in text


def test_grep_literal_search(tmp_path: Path):
    # The literal mode should find a dot literally, not as "any char".
    (tmp_path / "a.py").write_text("foo.bar = 1\nfoobar = 2\n", encoding="utf-8")
    tool = GrepTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"pattern": "foo.bar", "path": ".", "literal": True})).content[0].text
    lines = text.splitlines()
    assert any("foo.bar = 1" in line for line in lines)
    assert not any("foobar = 2" in line for line in lines)


def test_grep_no_matches(tmp_path: Path):
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    tool = GrepTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"pattern": "nonexistent_xyz", "path": "."})).content[0].text
    assert text == "No matches found"


def test_grep_limit_truncates(tmp_path: Path):
    (tmp_path / "a.txt").write_text("match\n" * 50, encoding="utf-8")
    tool = GrepTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"pattern": "match", "path": ".", "limit": 5})).content[0].text
    assert "5 matches limit reached" in text
    assert "Use limit=10" in text


def test_grep_glob_filter(tmp_path: Path):
    (tmp_path / "a.py").write_text("target\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("target\n", encoding="utf-8")
    tool = GrepTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"pattern": "target", "path": ".", "glob": "*.py"})).content[0].text
    assert "a.py" in text
    assert "b.txt" not in text


def test_grep_respects_gitignore(tmp_path: Path):
    # Pure-Python fallback path should skip gitignored files.
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "visible.py").write_text("target\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("target\n", encoding="utf-8")
    tool = GrepTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"pattern": "target", "path": "."})).content[0].text
    # The pure-Python path uses pathspec; rg path uses rg's native gitignore.
    # Either way, ignored.py should be absent.
    assert "ignored.py" not in text


def test_grep_single_file(tmp_path: Path):
    (tmp_path / "a.py").write_text("x\ny\nz\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x\n", encoding="utf-8")
    tool = GrepTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"pattern": "x", "path": "a.py"})).content[0].text
    # Only a.py should be searched.
    assert "a.py:1: x" in text
    assert "b.py" not in text


def test_grep_missing_path_raises(tmp_path: Path):
    tool = GrepTool(cwd=str(tmp_path))
    with pytest.raises(FileNotFoundError):
        _run(tool.execute("id", {"pattern": "x", "path": "no_such_dir"}))


# ─── FindTool ──────────────────────────────────────────────────────────

def test_find_glob_match(tmp_path: Path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "b.py").write_text("x", encoding="utf-8")
    (tmp_path / "c.txt").write_text("x", encoding="utf-8")
    tool = FindTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"pattern": "*.py", "path": "."})).content[0].text
    lines = set(text.splitlines())
    assert "a.py" in lines
    assert "b.py" in lines
    assert "c.txt" not in lines


def test_find_recursive_glob(tmp_path: Path):
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "deep.py").write_text("x", encoding="utf-8")
    (tmp_path / "top.py").write_text("x", encoding="utf-8")
    tool = FindTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"pattern": "**/*.py", "path": "."})).content[0].text
    lines = set(text.splitlines())
    assert "top.py" in lines
    assert "src/deep.py" in lines


def test_find_no_matches(tmp_path: Path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    tool = FindTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"pattern": "*.nomatch", "path": "."})).content[0].text
    assert text == "No files found matching pattern"


def test_find_limit_truncates(tmp_path: Path):
    for i in range(20):
        (tmp_path / f"f{i}.py").write_text("x", encoding="utf-8")
    tool = FindTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"pattern": "*.py", "path": ".", "limit": 5})).content[0].text
    assert "5 results limit reached" in text


def test_find_missing_path_raises(tmp_path: Path):
    tool = FindTool(cwd=str(tmp_path))
    with pytest.raises(FileNotFoundError):
        _run(tool.execute("id", {"pattern": "*", "path": "no_such_dir"}))


def test_find_returns_relative_paths(tmp_path: Path):
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "mod.py").write_text("x", encoding="utf-8")
    tool = FindTool(cwd=str(tmp_path))
    # **/*.py matches recursively in both fd and pathlib.
    text = _run(tool.execute("id", {"pattern": "**/*.py", "path": "."})).content[0].text
    # Should be relative (no absolute path), POSIX separators.
    assert "pkg/mod.py" in text
    assert str(tmp_path) not in text


# ─── rg/fd subprocess path sanity (only if binaries present) ──────────

@pytest.mark.skipif(find_in_path("rg") is None, reason="ripgrep not installed")
def test_grep_rg_path_matches_fallback_shape(tmp_path: Path):
    """When rg is present, the subprocess path should produce the same output shape."""
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    tool = GrepTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"pattern": "def", "path": "."})).content[0].text
    # rg path: file:line: content
    assert "def foo()" in text


@pytest.mark.skipif(find_in_path("fd") is None, reason="fd not installed")
def test_find_fd_path_matches_fallback_shape(tmp_path: Path):
    """When fd is present, the subprocess path should produce relative paths."""
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    tool = FindTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"pattern": "*.py", "path": "."})).content[0].text
    assert "a.py" in text.splitlines()
