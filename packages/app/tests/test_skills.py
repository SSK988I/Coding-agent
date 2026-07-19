"""Tests for core/skills.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coding_agent.core.skills import Skill, escape_xml, format_skills_for_prompt


def test_escape_xml_replaces_ampersand_first():
    assert escape_xml("a & b") == "a &amp; b"
    assert escape_xml("<tag>") == "&lt;tag&gt;"
    assert escape_xml('"q"') == "&quot;q&quot;"
    assert escape_xml("it's") == "it&apos;s"


def test_escape_xml_does_not_double_escape_ampersand_in_entity():
    # & must be first so pre-existing entities survive intact.
    assert escape_xml("&lt;") == "&amp;lt;"


def test_format_empty_returns_empty_string():
    assert format_skills_for_prompt([]) == ""


def test_format_skill_disabled_model_invocation_returns_empty():
    s = Skill(name="x", description="d", file_path="/p", disable_model_invocation=True)
    assert format_skills_for_prompt([s]) == ""


def test_format_skill_emits_available_skills_block():
    s = Skill(name="pdf", description="Make PDFs", file_path="/skills/pdf/SKILL.md")
    out = format_skills_for_prompt([s])
    assert out.startswith("\n\nThe following skills")
    assert "<available_skills>" in out
    assert "  <skill>" in out
    assert "    <name>pdf</name>" in out
    assert "    <description>Make PDFs</description>" in out
    assert "    <location>/skills/pdf/SKILL.md</location>" in out
    assert out.endswith("</available_skills>")


def test_format_escapes_special_chars_in_skill_fields():
    s = Skill(name="a<b>", description='d "e"', file_path="<p>")
    out = format_skills_for_prompt([s])
    assert "    <name>a&lt;b&gt;</name>" in out  # name escaped
    assert "&quot;e&quot;" in out  # description escaped
    assert "&lt;p&gt;" in out  # location escaped
