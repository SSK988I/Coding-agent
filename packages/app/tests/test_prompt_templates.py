"""Tests for prompt template loading, argument substitution, and expansion."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coding_agent.core.prompt_templates import (
    expand_prompt_template,
    load_prompt_templates,
    load_template_from_file,
    parse_command_args,
    substitute_args,
    PromptTemplate,
)


# ─── parse_command_args ────────────────────────────────────────────────


def test_parse_args_splits_on_whitespace():
    assert parse_command_args("a b c") == ["a", "b", "c"]


def test_parse_args_respects_double_quotes():
    assert parse_command_args('"hello world" bar') == ["hello world", "bar"]


def test_parse_args_respects_single_quotes():
    assert parse_command_args("'a b' c") == ["a b", "c"]


def test_parse_args_empty_string():
    assert parse_command_args("") == []


def test_parse_args_trailing_whitespace():
    assert parse_command_args("a b  ") == ["a", "b"]


# ─── substitute_args ───────────────────────────────────────────────────


def test_substitute_positional():
    assert substitute_args("hello $1", ["world"]) == "hello world"


def test_substitute_missing_positional_becomes_empty():
    assert substitute_args("$1 and $2", ["a"]) == "a and "


def test_substitute_all_args_dollar_at():
    assert substitute_args("args: $@", ["x", "y"]) == "args: x y"


def test_substitute_all_args_ARGUMENTS():
    assert substitute_args("args: $ARGUMENTS", ["x", "y"]) == "args: x y"


def test_substitute_default_when_missing():
    assert substitute_args("${1:-fallback}", []) == "fallback"


def test_substitute_default_ignored_when_present():
    assert substitute_args("${1:-fallback}", ["real"]) == "real"


def test_substitute_slice_from_n():
    assert substitute_args("${@:2}", ["a", "b", "c"]) == "b c"


def test_substitute_slice_with_length():
    assert substitute_args("${@:1:2}", ["a", "b", "c", "d"]) == "a b"


def test_substitute_no_recursion():
    # A literal $1 in an argument is preserved, not re-substituted.
    assert substitute_args("$1", ["$2"]) == "$2"


def test_substitute_mixed():
    tmpl = "Fix $1 in ${2:-main}, full: $@"
    assert substitute_args(tmpl, ["bug", "dev"]) == "Fix bug in dev, full: bug dev"


# ─── load_template_from_file ───────────────────────────────────────────


def test_load_template_full_frontmatter(tmp_path: Path):
    f = tmp_path / "review.md"
    f.write_text(
        "---\ndescription: Review a PR\nargument-hint: <pr-url>\n---\nReview $1 carefully.",
        encoding="utf-8",
    )
    tpl = load_template_from_file(f, "user")
    assert tpl is not None
    assert tpl.name == "review"
    assert tpl.description == "Review a PR"
    assert tpl.argument_hint == "<pr-url>"
    assert tpl.content == "Review $1 carefully."
    assert tpl.source == "user"


def test_load_template_description_from_first_body_line(tmp_path: Path):
    f = tmp_path / "notes.md"
    f.write_text("This is the first line.\nMore content.", encoding="utf-8")
    tpl = load_template_from_file(f)
    assert tpl is not None
    assert tpl.description == "This is the first line."


def test_load_template_unreadable_returns_none(tmp_path: Path):
    f = tmp_path / "bad.md"
    f.write_bytes(b"\xff\xfe\x00")
    assert load_template_from_file(f) is None


# ─── load_prompt_templates (multi-source) ──────────────────────────────


def test_load_templates_user_and_project(tmp_path: Path):
    agent_dir = tmp_path / "agent"
    (agent_dir / "prompts").mkdir(parents=True)
    (agent_dir / "prompts" / "global.md").write_text("global body", encoding="utf-8")
    cwd = tmp_path / "proj"
    (cwd / ".coding-agent" / "prompts").mkdir(parents=True)
    (cwd / ".coding-agent" / "prompts" / "local.md").write_text("local body", encoding="utf-8")
    templates = load_prompt_templates(cwd, agent_dir)
    names = {t.name for t in templates}
    assert names == {"global", "local"}


def test_load_templates_first_wins_on_name_collision(tmp_path: Path):
    agent_dir = tmp_path / "agent"
    (agent_dir / "prompts").mkdir(parents=True)
    (agent_dir / "prompts" / "dup.md").write_text("from user", encoding="utf-8")
    cwd = tmp_path / "proj"
    (cwd / ".coding-agent" / "prompts").mkdir(parents=True)
    (cwd / ".coding-agent" / "prompts" / "dup.md").write_text("from project", encoding="utf-8")
    templates = load_prompt_templates(cwd, agent_dir)
    dups = [t for t in templates if t.name == "dup"]
    assert len(dups) == 1
    assert dups[0].content == "from user"


def test_load_templates_explicit_path(tmp_path: Path):
    f = tmp_path / "extra.md"
    f.write_text("explicit body", encoding="utf-8")
    templates = load_prompt_templates(tmp_path, tmp_path, prompt_paths=[str(f)])
    assert any(t.content == "explicit body" for t in templates)


def test_load_templates_no_prompts_disables_defaults(tmp_path: Path):
    agent_dir = tmp_path / "agent"
    (agent_dir / "prompts").mkdir(parents=True)
    (agent_dir / "prompts" / "g.md").write_text("g", encoding="utf-8")
    templates = load_prompt_templates(tmp_path, agent_dir, include_defaults=False)
    assert templates == []


# ─── expand_prompt_template ────────────────────────────────────────────


def test_expand_non_command_returns_unchanged():
    tpls: list[PromptTemplate] = []
    assert expand_prompt_template("hello world", tpls) == "hello world"


def test_expand_no_match_returns_unchanged():
    tpls = [PromptTemplate(name="review", description="", content="body", file_path="/r.md")]
    assert expand_prompt_template("/unknown", tpls) == "/unknown"


def test_expand_matches_and_substitutes():
    tpls = [PromptTemplate(name="review", description="", content="Review $1", file_path="/r.md")]
    assert expand_prompt_template("/review PR-123", tpls) == "Review PR-123"


def test_expand_no_args():
    tpls = [PromptTemplate(name="clean", description="", content="clean up", file_path="/c.md")]
    assert expand_prompt_template("/clean", tpls) == "clean up"


def test_expand_preserves_internal_whitespace_in_args():
    tpls = [PromptTemplate(name="echo", description="", content="$@", file_path="/e.md")]
    assert expand_prompt_template('/echo "hello   world"', tpls) == "hello   world"
