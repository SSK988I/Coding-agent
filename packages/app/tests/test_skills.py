"""Tests for core/skills.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coding_agent.core.skills import (
    Skill,
    escape_xml,
    format_skills_for_prompt,
    load_skill_from_file,
    load_skills,
    load_skills_from_dir,
    parse_frontmatter,
    validate_skill_description,
    validate_skill_name,
)


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


# ─── frontmatter parsing ───────────────────────────────────────────────


def test_parse_frontmatter_extracts_key_value_pairs():
    fm, body = parse_frontmatter("---\nname: my-skill\ndescription: Does things\n---\nbody text")
    assert fm["name"] == "my-skill"
    assert fm["description"] == "Does things"
    assert body == "body text"


def test_parse_frontmatter_handles_quoted_values():
    fm, _ = parse_frontmatter('---\ndescription: "has: colon"\n---\n')
    assert fm["description"] == "has: colon"


def test_parse_frontmatter_parses_boolean():
    fm, _ = parse_frontmatter("---\ndisable-model-invocation: true\n---\n")
    assert fm["disable-model-invocation"] is True


def test_parse_frontmatter_no_fence_returns_empty():
    fm, body = parse_frontmatter("no frontmatter here")
    assert fm == {}
    assert body == "no frontmatter here"


def test_parse_frontmatter_unclosed_fence_returns_empty():
    fm, body = parse_frontmatter("---\nname: x\n")
    assert fm == {}
    assert "name: x" in body


def test_parse_frontmatter_preserves_colon_in_url():
    fm, _ = parse_frontmatter("---\ndescription: http://example.com/a\n---\n")
    assert fm["description"] == "http://example.com/a"


# ─── validation ────────────────────────────────────────────────────────


def test_validate_name_accepts_valid():
    assert validate_skill_name("pdf-gen") == []
    assert validate_skill_name("a") == []


def test_validate_name_rejects_uppercase_and_underscores():
    # The [a-z0-9-]+ regex catches all invalid chars (uppercase, underscore)
    # in a single message.
    errs = validate_skill_name("Bad_Name")
    assert len(errs) == 1
    assert "invalid characters" in errs[0]


def test_validate_name_rejects_leading_trailing_double_hyphen():
    assert validate_skill_name("-bad")
    assert validate_skill_name("bad-")
    assert validate_skill_name("a--b")


def test_validate_description_requires_nonempty():
    assert validate_skill_description(None)
    assert validate_skill_description("   ")
    assert validate_skill_description("ok") == []


# ─── single-file loader ────────────────────────────────────────────────


def test_load_skill_from_file_full_frontmatter(tmp_path: Path):
    f = tmp_path / "SKILL.md"
    f.write_text("---\nname: pdf\ndescription: Make PDFs\n---\nbody", encoding="utf-8")
    skill, diags = load_skill_from_file(f, "user")
    assert skill is not None
    assert skill.name == "pdf"
    assert skill.description == "Make PDFs"
    assert skill.source == "user"
    assert diags == []


def test_load_skill_name_defaults_to_parent_dir(tmp_path: Path):
    skill_dir = tmp_path / "git-workflow"
    skill_dir.mkdir()
    f = skill_dir / "SKILL.md"
    f.write_text("---\ndescription: Git helpers\n---\n", encoding="utf-8")
    skill, _ = load_skill_from_file(f)
    assert skill is not None
    assert skill.name == "git-workflow"


def test_load_skill_missing_description_returns_none(tmp_path: Path):
    f = tmp_path / "SKILL.md"
    f.write_text("---\nname: no-desc\n---\nbody", encoding="utf-8")
    skill, diags = load_skill_from_file(f)
    assert skill is None
    assert any("description is required" in d.message for d in diags)


def test_load_skill_disable_model_invocation(tmp_path: Path):
    f = tmp_path / "SKILL.md"
    f.write_text(
        "---\nname: secret\ndescription: hidden\n---\n",
        encoding="utf-8",
    )
    skill, _ = load_skill_from_file(f)
    assert skill is not None
    assert skill.disable_model_invocation is False


def test_load_skill_unreadable_returns_diagnostic(tmp_path: Path):
    f = tmp_path / "SKILL.md"
    f.write_bytes(b"\xff\xfe\x00")  # invalid utf-8
    skill, diags = load_skill_from_file(f)
    assert skill is None
    assert diags and diags[0].type == "warning"


# ─── directory loader ──────────────────────────────────────────────────


def test_load_skills_from_dir_skill_md_short_circuits(tmp_path: Path):
    """A SKILL.md at the dir root means the dir is a single skill."""
    (tmp_path / "SKILL.md").write_text("---\ndescription: root skill\n---\n", encoding="utf-8")
    # This subdir should NOT be loaded (no recursion past a skill root).
    sub = tmp_path / "nested"
    sub.mkdir()
    (sub / "SKILL.md").write_text("---\ndescription: nested\n---\n", encoding="utf-8")
    result = load_skills_from_dir(tmp_path)
    assert len(result.skills) == 1
    assert result.skills[0].description == "root skill"


def test_load_skills_from_dir_recurses_into_subdirs(tmp_path: Path):
    """No root SKILL.md → load .md children and recurse."""
    (tmp_path / "loose.md").write_text("---\ndescription: loose\n---\n", encoding="utf-8")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "SKILL.md").write_text("---\ndescription: pkg skill\n---\n", encoding="utf-8")
    result = load_skills_from_dir(tmp_path)
    names = {s.description for s in result.skills}
    assert names == {"loose", "pkg skill"}


def test_load_skills_from_dir_skips_dotfiles_and_build_dirs(tmp_path: Path):
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "SKILL.md").write_text("---\ndescription: hidden\n---\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "SKILL.md").write_text("---\ndescription: nm\n---\n", encoding="utf-8")
    result = load_skills_from_dir(tmp_path)
    assert result.skills == []


def test_load_skills_from_dir_missing_returns_empty(tmp_path: Path):
    assert load_skills_from_dir(tmp_path / "nope").skills == []


# ─── multi-source aggregator ───────────────────────────────────────────


def test_load_skills_user_and_project_sources(tmp_path: Path):
    agent_dir = tmp_path / "agent"
    (agent_dir / "skills" / "global").mkdir(parents=True)
    (agent_dir / "skills" / "global" / "SKILL.md").write_text(
        "---\ndescription: global skill\n---\n", encoding="utf-8",
    )
    cwd = tmp_path / "proj"
    (cwd / ".coding-agent" / "skills" / "local").mkdir(parents=True)
    (cwd / ".coding-agent" / "skills" / "local" / "SKILL.md").write_text(
        "---\ndescription: local skill\n---\n", encoding="utf-8",
    )
    result = load_skills(cwd, agent_dir)
    descs = {s.description for s in result.skills}
    assert descs == {"global skill", "local skill"}
    assert {s.source for s in result.skills} == {"user", "project"}


def test_load_skills_explicit_path(tmp_path: Path):
    f = tmp_path / "x.md"
    f.write_text("---\ndescription: explicit\n---\n", encoding="utf-8")
    result = load_skills(tmp_path, tmp_path, skill_paths=[str(f)])
    assert any(s.description == "explicit" for s in result.skills)


def test_load_skills_dedupes_by_name_first_wins(tmp_path: Path):
    agent_dir = tmp_path / "agent"
    (agent_dir / "skills" / "dup").mkdir(parents=True)
    (agent_dir / "skills" / "dup" / "SKILL.md").write_text(
        "---\nname: dup\ndescription: from user\n---\n", encoding="utf-8",
    )
    cwd = tmp_path / "proj"
    (cwd / ".coding-agent" / "skills" / "dup").mkdir(parents=True)
    (cwd / ".coding-agent" / "skills" / "dup" / "SKILL.md").write_text(
        "---\nname: dup\ndescription: from project\n---\n", encoding="utf-8",
    )
    result = load_skills(cwd, agent_dir)
    dup_skills = [s for s in result.skills if s.name == "dup"]
    assert len(dup_skills) == 1
    assert dup_skills[0].description == "from user"  # user scope wins
    assert any(d.type == "collision" for d in result.diagnostics)


def test_load_skills_no_skills_flag_disables_defaults(tmp_path: Path):
    agent_dir = tmp_path / "agent"
    (agent_dir / "skills" / "g").mkdir(parents=True)
    (agent_dir / "skills" / "g" / "SKILL.md").write_text(
        "---\ndescription: g\n---\n", encoding="utf-8",
    )
    result = load_skills(tmp_path, agent_dir, include_defaults=False)
    assert result.skills == []


def test_load_skills_missing_explicit_path_warns(tmp_path: Path):
    result = load_skills(tmp_path, tmp_path, skill_paths=[str(tmp_path / "nope.md")])
    assert any(d.type == "warning" for d in result.diagnostics)
