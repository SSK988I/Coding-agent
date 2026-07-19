"""Tests for the ls and edit tools (no external binaries required).

EditTool tests cover the core algorithm: single/multi edit, uniqueness, fuzzy
matching, BOM/CRLF preservation, and the flat-argument shim.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_core.tools.edit import EditTool
from agent_core.tools.bash import BashTool
from agent_core.tools.ls import LsTool


def _run(coro):
    return asyncio.run(coro)


# ─── LsTool ────────────────────────────────────────────────────────────

def test_ls_lists_directory_with_slash_for_dirs(tmp_path: Path):
    (tmp_path / "subdir").mkdir()
    (tmp_path / "file.py").write_text("x", encoding="utf-8")
    (tmp_path / "README.md").write_text("y", encoding="utf-8")

    tool = LsTool(cwd=str(tmp_path))
    result = _run(tool.execute("id", {"path": "."}))
    text = result.content[0].text
    lines = text.splitlines()
    assert "subdir/" in lines
    assert "file.py" in lines
    assert "README.md" in lines
    # Directories get a slash, files don't.
    assert "subdir/" in lines
    assert all(not line.endswith("/") for line in lines if line != "subdir/")


def test_ls_includes_dotfiles(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("x", encoding="utf-8")
    (tmp_path / ".hidden").write_text("y", encoding="utf-8")
    tool = LsTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"path": "."})).content[0].text
    assert ".gitignore" in text.splitlines()
    assert ".hidden" in text.splitlines()


def test_ls_empty_directory(tmp_path: Path):
    tool = LsTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"path": "."})).content[0].text
    assert text == "(empty directory)"


def test_ls_case_insensitive_sort(tmp_path: Path):
    for name in ["Banana", "apple", "Cherry"]:
        (tmp_path / name).write_text("x", encoding="utf-8")
    tool = LsTool(cwd=str(tmp_path))
    lines = _run(tool.execute("id", {"path": "."})).content[0].text.splitlines()
    assert lines == ["apple", "Banana", "Cherry"]


def test_ls_missing_path_raises(tmp_path: Path):
    tool = LsTool(cwd=str(tmp_path))
    with pytest.raises(FileNotFoundError):
        _run(tool.execute("id", {"path": "no_such_dir"}))


def test_ls_not_a_directory_raises(tmp_path: Path):
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    tool = LsTool(cwd=str(tmp_path))
    with pytest.raises(NotADirectoryError):
        _run(tool.execute("id", {"path": "file.txt"}))


def test_ls_limit_truncates(tmp_path: Path):
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    tool = LsTool(cwd=str(tmp_path))
    text = _run(tool.execute("id", {"path": ".", "limit": 3})).content[0].text
    assert "3 entries limit reached" in text
    assert "Use limit=6" in text


# ─── EditTool: basic replacement ──────────────────────────────────────

def test_edit_single_replacement(tmp_path: Path):
    f = tmp_path / "a.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    tool = EditTool(cwd=str(tmp_path))
    result = _run(tool.execute("id", {
        "path": "a.py",
        "edits": [{"oldText": "return 1", "newText": "return 2"}],
    }))
    assert "Successfully replaced 1 block(s)" in result.content[0].text
    assert f.read_text(encoding="utf-8") == "def foo():\n    return 2\n"


def test_edit_multiple_disjoint_edits(tmp_path: Path):
    f = tmp_path / "a.py"
    f.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    tool = EditTool(cwd=str(tmp_path))
    _run(tool.execute("id", {
        "path": "a.py",
        "edits": [
            {"oldText": "a = 1", "newText": "a = 10"},
            {"oldText": "c = 3", "newText": "c = 30"},
        ],
    }))
    assert f.read_text(encoding="utf-8") == "a = 10\nb = 2\nc = 30\n"


# ─── EditTool: error cases ────────────────────────────────────────────

def test_edit_oldtext_not_found_raises(tmp_path: Path):
    f = tmp_path / "a.py"
    f.write_text("hello\n", encoding="utf-8")
    tool = EditTool(cwd=str(tmp_path))
    with pytest.raises(ValueError, match="Could not find the exact text"):
        _run(tool.execute("id", {
            "path": "a.py",
            "edits": [{"oldText": "nonexistent", "newText": "x"}],
        }))


def test_edit_oldtext_not_unique_raises(tmp_path: Path):
    f = tmp_path / "a.py"
    f.write_text("dup\ndup\ndup\n", encoding="utf-8")
    tool = EditTool(cwd=str(tmp_path))
    with pytest.raises(ValueError, match="3 occurrences"):
        _run(tool.execute("id", {
            "path": "a.py",
            "edits": [{"oldText": "dup", "newText": "x"}],
        }))


def test_edit_empty_oldtext_raises(tmp_path: Path):
    f = tmp_path / "a.py"
    f.write_text("content\n", encoding="utf-8")
    tool = EditTool(cwd=str(tmp_path))
    with pytest.raises(ValueError, match="oldText must not be empty"):
        _run(tool.execute("id", {
            "path": "a.py",
            "edits": [{"oldText": "", "newText": "x"}],
        }))


def test_edit_overlapping_edits_raises(tmp_path: Path):
    f = tmp_path / "a.py"
    f.write_text("abcdefg\n", encoding="utf-8")
    tool = EditTool(cwd=str(tmp_path))
    with pytest.raises(ValueError, match="overlap"):
        _run(tool.execute("id", {
            "path": "a.py",
            "edits": [
                {"oldText": "abcd", "newText": "X"},
                {"oldText": "cdef", "newText": "Y"},
            ],
        }))


def test_edit_missing_file_raises(tmp_path: Path):
    tool = EditTool(cwd=str(tmp_path))
    with pytest.raises(FileNotFoundError):
        _run(tool.execute("id", {
            "path": "nope.py",
            "edits": [{"oldText": "a", "newText": "b"}],
        }))


# ─── EditTool: robustness ─────────────────────────────────────────────

def test_edit_preserves_crlf_line_endings(tmp_path: Path):
    f = tmp_path / "a.py"
    f.write_bytes("def foo():\r\n    return 1\r\n".encode("utf-8"))
    tool = EditTool(cwd=str(tmp_path))
    _run(tool.execute("id", {
        "path": "a.py",
        "edits": [{"oldText": "return 1", "newText": "return 2"}],
    }))
    # CRLF preserved.
    assert f.read_bytes() == b"def foo():\r\n    return 2\r\n"


def test_edit_preserves_bom(tmp_path: Path):
    f = tmp_path / "a.py"
    f.write_bytes("\ufeffcontent here\n".encode("utf-8"))
    tool = EditTool(cwd=str(tmp_path))
    _run(tool.execute("id", {
        "path": "a.py",
        "edits": [{"oldText": "content here", "newText": "edited here"}],
    }))
    raw = f.read_bytes().decode("utf-8")
    assert raw.startswith("\ufeff")
    assert "edited here" in raw


def test_edit_flat_arguments_shim(tmp_path: Path):
    """Models sometimes send flat {path, oldText, newText} instead of edits[]."""
    f = tmp_path / "a.py"
    f.write_text("hello world\n", encoding="utf-8")
    tool = EditTool(cwd=str(tmp_path))
    _run(tool.execute("id", {
        "path": "a.py",
        "oldText": "hello world",
        "newText": "goodbye world",
    }))
    assert f.read_text(encoding="utf-8") == "goodbye world\n"


def test_edit_fuzzy_match_smart_quotes(tmp_path: Path):
    """Fuzzy matching should tolerate smart quotes / trailing whitespace."""
    f = tmp_path / "a.md"
    # File uses a smart apostrophe and trailing space.
    f.write_text("It\u2019s a test \n", encoding="utf-8")
    tool = EditTool(cwd=str(tmp_path))
    _run(tool.execute("id", {
        "path": "a.md",
        "edits": [{"oldText": "It's a test", "newText": "It was a test"}],
    }))
    # The edit should have applied (via fuzzy normalization).
    content = f.read_text(encoding="utf-8")
    assert "was a test" in content


def test_edit_no_change_raises(tmp_path: Path):
    f = tmp_path / "a.py"
    f.write_text("same\n", encoding="utf-8")
    tool = EditTool(cwd=str(tmp_path))
    with pytest.raises(ValueError, match="No changes"):
        _run(tool.execute("id", {
            "path": "a.py",
            "edits": [{"oldText": "same", "newText": "same"}],
        }))


def test_edit_includes_patch_in_details(tmp_path: Path):
    f = tmp_path / "a.py"
    f.write_text("old line\n", encoding="utf-8")
    tool = EditTool(cwd=str(tmp_path))
    result = _run(tool.execute("id", {
        "path": "a.py",
        "edits": [{"oldText": "old line", "newText": "new line"}],
    }))
    assert result.details is not None
    patch = result.details.get("patch", "")
    assert "---" in patch and "+++" in patch
    assert "-old line" in patch
    assert "+new line" in patch

def test_bash_raw_output_has_a_byte_cap():
    tool = BashTool(max_bytes=8)
    output, truncated = tool._tail_cap_bytes(b"abcdefghijkl")
    assert truncated is True
    assert output.endswith(b"efghijkl")
    assert b"truncated" in output
