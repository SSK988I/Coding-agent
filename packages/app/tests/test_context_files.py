"""Tests for core/context_files.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coding_agent.core.context_files import (
    load_context_file_from_dir,
    load_project_context_files,
)


# ─── load_context_file_from_dir ────────────────────────────────────────


def test_returns_none_when_no_candidate(tmp_path):
    assert load_context_file_from_dir(tmp_path) is None


def test_agents_md_takes_precedence_over_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("claude", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("agents", encoding="utf-8")
    ctx = load_context_file_from_dir(tmp_path)
    assert ctx is not None
    assert ctx.content == "agents"


def test_claude_md_used_when_no_agents_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("claude", encoding="utf-8")
    ctx = load_context_file_from_dir(tmp_path)
    assert ctx is not None
    assert ctx.content == "claude"


def test_case_variants_supported(tmp_path):
    (tmp_path / "AGENTS.MD").write_text("upper", encoding="utf-8")
    ctx = load_context_file_from_dir(tmp_path)
    assert ctx is not None
    assert ctx.content == "upper"


# ─── load_project_context_files ordering ───────────────────────────────


def test_no_context_files_flag_returns_empty(tmp_path):
    out = load_project_context_files(tmp_path, tmp_path, no_context_files=True)
    assert out == []


def test_global_context_comes_first(tmp_path):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "AGENTS.md").write_text("global", encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("project", encoding="utf-8")

    out = load_project_context_files(project, agent_dir)
    assert [c.content for c in out] == ["global", "project"]


def test_ancestor_order_is_outermost_first(tmp_path):
    # tmp_path / root / mid / leaf
    root = tmp_path / "root"
    mid = root / "mid"
    leaf = mid / "leaf"
    leaf.mkdir(parents=True)

    (root / "AGENTS.md").write_text("root", encoding="utf-8")
    (leaf / "AGENTS.md").write_text("leaf", encoding="utf-8")

    out = load_project_context_files(leaf, tmp_path / "nonexistent-agent")
    contents = [c.content for c in out]
    # Outermost ancestor first, cwd last.
    assert contents == ["root", "leaf"]


def test_dedup_by_absolute_path(tmp_path):
    # When cwd and agent_dir resolve to the same dir, only one entry.
    (tmp_path / "AGENTS.md").write_text("once", encoding="utf-8")
    out = load_project_context_files(tmp_path, tmp_path)
    assert len(out) == 1
    assert out[0].content == "once"
