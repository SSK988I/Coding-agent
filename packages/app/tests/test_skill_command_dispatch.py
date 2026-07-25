"""Tests for /skill:name dispatch and autocomplete wiring.

These verify the skill-invocation lookup and file-loading logic without
standing up the full InteractiveMode TUI. The InteractiveMode methods are
bound onto a lightweight stub that supplies only the attributes the skill
command path touches.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coding_agent.core.skills import Skill


def _run(coro):
    return asyncio.run(coro)


def _bind_skill_method():
    """Import _handle_skill_command as an unbound function for stub binding."""
    from coding_agent.modes.interactive.interactive_mode import InteractiveMode
    return InteractiveMode._handle_skill_command


def _make_stub(skills: list[Skill], respond_mock):
    """Build a stub exposing just what _handle_skill_command reads."""
    obj = SimpleNamespace()
    obj._session = SimpleNamespace(_config=SimpleNamespace(skills=skills))
    # _skills_for_command reads session._config.skills
    from coding_agent.modes.interactive.interactive_mode import InteractiveMode
    obj._skills_for_command = InteractiveMode._skills_for_command.__get__(obj)
    # UI helpers invoked by _handle_skill_command.
    obj._add_user_message = lambda text: setattr(obj, "_last_user_msg", text)
    obj._add_system_message = lambda text: setattr(obj, "_last_sys_msg", text)
    obj._is_responding = False
    obj.editor = SimpleNamespace(disable_submit=False)
    obj._respond = respond_mock
    obj._refresh_footer = lambda: None
    # theme.fg is used in the error path; provide a minimal stand-in.
    obj.theme = SimpleNamespace(fg=lambda *_a, **_kw: "err")
    return obj


def test_skill_command_loads_file_and_responds(tmp_path: Path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("---\nname: pdf\ndescription: Make PDFs\n---\nGenerate the PDF.", encoding="utf-8")
    skill = Skill(name="pdf", description="Make PDFs", file_path=str(skill_md))
    respond = AsyncMock()
    stub = _make_stub([skill], respond)
    method = _bind_skill_method()

    result = _run(method(stub, "skill:pdf", ""))
    assert result is True
    respond.assert_awaited_once()
    # The prompt passed to _respond is the skill body (frontmatter stripped).
    sent = respond.await_args.args[0]
    assert "Generate the PDF." in sent


def test_skill_command_appends_extra_args(tmp_path: Path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("---\nname: lint\ndescription: Lint\n---\nRun linters.", encoding="utf-8")
    skill = Skill(name="lint", description="Lint", file_path=str(skill_md))
    respond = AsyncMock()
    stub = _make_stub([skill], respond)
    method = _bind_skill_method()

    result = _run(method(stub, "skill:lint", "src/ tests/"))
    assert result is True
    sent = respond.await_args.args[0]
    assert "src/ tests/" in sent


def test_skill_command_unknown_name_returns_false(tmp_path: Path):
    respond = AsyncMock()
    stub = _make_stub([], respond)
    method = _bind_skill_method()

    result = _run(method(stub, "skill:nope", ""))
    assert result is False
    respond.assert_not_awaited()


def test_skill_command_empty_body_reports_and_does_not_call_model(tmp_path: Path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("---\nname: empty\ndescription: Empty\n---\n", encoding="utf-8")
    skill = Skill(name="empty", description="Empty", file_path=str(skill_md))
    respond = AsyncMock()
    stub = _make_stub([skill], respond)
    method = _bind_skill_method()

    result = _run(method(stub, "skill:empty", ""))
    assert result is True  # handled (reported empty), but no model call
    respond.assert_not_awaited()
    assert hasattr(stub, "_last_sys_msg")
    assert "为空" in stub._last_sys_msg


def test_skill_command_missing_file_reports_error(tmp_path: Path):
    skill = Skill(name="ghost", description="Ghost", file_path=str(tmp_path / "nope.md"))
    respond = AsyncMock()
    stub = _make_stub([skill], respond)
    method = _bind_skill_method()

    result = _run(method(stub, "skill:ghost", ""))
    assert result is True
    respond.assert_not_awaited()
    assert hasattr(stub, "_last_sys_msg")
