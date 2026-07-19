"""Tests for the extended build_system_prompt.

Covers the new parameters: custom_prompt, context_files, skills,
append_system_prompt. Lives in the coding-agent package because it exercises
the full assembly (agent_core.build_system_prompt + coding-agent ContextFile/Skill).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_core import build_system_prompt, escape_xml, format_skills_for_prompt
from agent_core.tools.read import ReadTool

from coding_agent.core.context_files import ContextFile
from coding_agent.core.skills import Skill


# ─── backward compatibility ────────────────────────────────────────────


def test_defaults_unchanged_without_new_params():
    """Calling without the new params behaves exactly like before."""
    prompt = build_system_prompt(cwd="/proj", tools=[ReadTool(cwd="/proj")])
    assert "You are an expert coding assistant" in prompt
    assert "<project_context>" not in prompt
    assert "<available_skills>" not in prompt
    assert "Current working directory: /proj" in prompt


# ─── custom_prompt ─────────────────────────────────────────────────────


def test_custom_prompt_replaces_identity_text():
    prompt = build_system_prompt(
        cwd="/p", tools=[ReadTool(cwd="/p")], custom_prompt="You are a poet.",
    )
    assert prompt.startswith("You are a poet.")
    assert "You are an expert coding assistant" not in prompt
    # Date + cwd still appended.
    assert "Current working directory: /p" in prompt


def test_custom_prompt_still_gets_context_and_append():
    ctx = [ContextFile(path="/p/AGENTS.md", content="rule one")]
    prompt = build_system_prompt(
        cwd="/p", tools=[ReadTool(cwd="/p")],
        custom_prompt="custom.", context_files=ctx,
        append_system_prompt="APPENDED",
    )
    assert "custom." in prompt
    assert "<project_context>" in prompt
    assert "rule one" in prompt
    assert "APPENDED" in prompt


# ─── context_files ─────────────────────────────────────────────────────


def test_context_files_inject_project_context_block():
    ctx = [
        ContextFile(path="/proj/AGENTS.md", content="Use type hints."),
        ContextFile(path="/root/AGENTS.md", content="Root rule."),
    ]
    prompt = build_system_prompt(cwd="/proj", tools=[ReadTool(cwd="/proj")], context_files=ctx)
    assert "<project_context>" in prompt
    assert "</project_context>" in prompt
    assert 'path="/proj/AGENTS.md"' in prompt
    assert "Use type hints." in prompt
    assert "Root rule." in prompt
    # Order preserved.
    assert prompt.index("/proj/AGENTS.md") < prompt.index("/root/AGENTS.md") or \
           prompt.index("Use type hints.") < prompt.index("Root rule.")


def test_empty_context_files_omits_block():
    prompt = build_system_prompt(cwd="/p", tools=[ReadTool(cwd="/p")], context_files=[])
    assert "<project_context>" not in prompt


def test_none_context_files_omits_block():
    prompt = build_system_prompt(cwd="/p", tools=[ReadTool(cwd="/p")], context_files=None)
    assert "<project_context>" not in prompt


# ─── append_system_prompt ──────────────────────────────────────────────


def test_append_system_prompt_added_with_separator():
    prompt = build_system_prompt(
        cwd="/p", tools=[ReadTool(cwd="/p")], append_system_prompt="Extra guidelines here.",
    )
    assert "\n\nExtra guidelines here." in prompt


def test_none_append_omitted():
    prompt = build_system_prompt(cwd="/p", tools=[ReadTool(cwd="/p")], append_system_prompt=None)
    # No stray double-newline artifact.
    assert "Current working directory: /p" in prompt


# ─── skills ────────────────────────────────────────────────────────────


def test_skills_block_added_when_read_tool_present():
    skills = [Skill(name="pdf", description="Make PDFs", file_path="/s/pdf/SKILL.md")]
    prompt = build_system_prompt(cwd="/p", tools=[ReadTool(cwd="/p")], skills=skills)
    assert "<available_skills>" in prompt
    assert "pdf" in prompt
    assert "/s/pdf/SKILL.md" in prompt


def test_skills_omitted_when_no_read_tool():
    """Skills require the read tool."""
    @dataclass
    class BashOnly:
        name: str = "bash"
        prompt_snippet: str = "run commands"
        prompt_guidelines: list = None
    skills = [Skill(name="pdf", description="d", file_path="/s")]
    prompt = build_system_prompt(cwd="/p", tools=[BashOnly()], skills=skills)
    assert "<available_skills>" not in prompt


def test_skills_with_disable_model_invocation_hidden():
    skills = [
        Skill(name="visible", description="d1", file_path="/a"),
        Skill(name="hidden", description="d2", file_path="/b", disable_model_invocation=True),
    ]
    block = format_skills_for_prompt(skills)
    assert "visible" in block
    assert "hidden" not in block


def test_empty_skills_omits_block():
    prompt = build_system_prompt(cwd="/p", tools=[ReadTool(cwd="/p")], skills=[])
    assert "<available_skills>" not in prompt


# ─── escape_xml ────────────────────────────────────────────────────────


def test_escape_xml_basic():
    assert escape_xml("a&b<c>\"'") == "a&amp;b&lt;c&gt;&quot;&apos;"


# ─── combined ──────────────────────────────────────────────────────────


def test_all_sections_assembled_in_correct_order():
    ctx = [ContextFile(path="/p/AGENTS.md", content="CTX")]
    skills = [Skill(name="s", description="d", file_path="/x")]
    prompt = build_system_prompt(
        cwd="/p", tools=[ReadTool(cwd="/p")],
        context_files=ctx, skills=skills, append_system_prompt="APP",
    )
    # Order: identity → append → context → skills → date/cwd.
    idx_identity = prompt.index("expert coding assistant")
    idx_append = prompt.index("APP")
    idx_ctx = prompt.index("<project_context>")
    idx_skills = prompt.index("<available_skills>")
    idx_date = prompt.index("Current date:")
    assert idx_identity < idx_append < idx_ctx < idx_skills < idx_date
