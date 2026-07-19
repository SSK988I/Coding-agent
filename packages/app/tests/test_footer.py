"""Tests for FooterComponent."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

from coding_agent.modes.interactive.components.footer import FooterComponent


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _theme():
    from agent_tui.theme import load_theme

    return load_theme("dark")


@dataclass
class _Usage:
    input: int = 0
    output: int = 0
    total: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: "float | None" = None


@dataclass
class _Msg:
    role: str
    usage: "_Usage | None" = None


@dataclass
class _Entry:
    message: "_Msg | None" = None


@dataclass
class _FakeModel:
    id: str = "deepseek-v4-flash"
    reasoning: bool = True
    context_window: int = 1_000_000


@dataclass
class _Stats:
    tokens: _Usage = field(default_factory=_Usage)
    cost: float = 0.0


class FakeSession:
    """Minimal session stub for footer render tests."""

    def __init__(self, *, model=None, thinking_level="high", entries=None,
                 tokens_in=0, tokens_out=0, cost=0.0, cwd="."):
        self.model = model or _FakeModel()
        self.thinking_level = thinking_level
        self.cwd = cwd
        self._entries = entries or []
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        self._cost = cost
        self.session_manager = type("SM", (), {"get_branch": self._branch})()

    def _branch(self):
        return self._entries

    def get_stats(self):
        u = _Usage(input=self._tokens_in, output=self._tokens_out,
                   total=self._tokens_in + self._tokens_out)
        return _Stats(tokens=u, cost=self._cost)


def _make(**kw) -> "tuple[FooterComponent, FakeSession]":
    sess = FakeSession(**kw)
    # Stub out git so refresh doesn't actually run git in tests.
    sess.cwd = "."
    footer = FooterComponent.__new__(FooterComponent)
    footer._session = sess
    footer._theme = _theme()
    footer._git_branch = None
    return footer, sess


# ── model + thinking ───────────────────────────────────────────────────


def test_renders_model_id():
    footer, _ = _make(model=_FakeModel(id="deepseek-v4-pro"))
    line = _strip(footer.render(120)[0])
    assert "deepseek-v4-pro" in line


def test_renders_thinking_level_when_reasoning():
    footer, _ = _make(model=_FakeModel(reasoning=True), thinking_level="high")
    line = _strip(footer.render(120)[0])
    assert "thinking high" in line


def test_no_thinking_when_model_not_reasoning():
    footer, _ = _make(model=_FakeModel(reasoning=False), thinking_level="high")
    line = _strip(footer.render(120)[0])
    assert "thinking" not in line


# ── tokens + context ───────────────────────────────────────────────────


def test_renders_token_counts():
    footer, _ = _make(tokens_in=5000, tokens_out=1200)
    line = _strip(footer.render(120)[0])
    assert "in=5k" in line
    assert "out=1k" in line


def test_no_token_part_when_zero():
    footer, _ = _make(tokens_in=0, tokens_out=0)
    line = _strip(footer.render(120)[0])
    assert "in=" not in line


def test_context_percent_from_last_assistant():
    entries = [_Entry(message=_Msg(role="assistant", usage=_Usage(input=50000)))]
    footer, _ = _make(
        model=_FakeModel(context_window=1_000_000),
        entries=entries,
        tokens_in=50000,
    )
    line = _strip(footer.render(120)[0])
    assert "ctx 5%" in line


def test_context_percent_none_when_no_entries():
    footer, _ = _make(model=_FakeModel(context_window=1_000_000), entries=[])
    line = _strip(footer.render(120)[0])
    assert "ctx" not in line


def test_context_percent_none_when_no_context_window():
    entries = [_Entry(message=_Msg(role="assistant", usage=_Usage(input=50000)))]
    footer, _ = _make(
        model=_FakeModel(context_window=0),
        entries=entries,
        tokens_in=50000,
    )
    line = _strip(footer.render(120)[0])
    assert "ctx" not in line


# ── git branch ─────────────────────────────────────────────────────────


def test_git_branch_displayed_when_set():
    footer, _ = _make()
    footer._git_branch = "main"
    footer._session.cwd = "/home/user/proj"
    line = _strip(footer.render(120)[0])
    assert "(main)" in line


def test_no_branch_parens_when_none():
    footer, _ = _make()
    footer._git_branch = None
    footer._session.cwd = "/home/user/proj"
    line = _strip(footer.render(120)[0])
    assert "()" not in line


# ── cost ───────────────────────────────────────────────────────────────


def test_cost_displayed_when_positive():
    footer, _ = _make(cost=0.0123)
    line = _strip(footer.render(120)[0])
    assert "$0.0123" in line


def test_no_cost_when_zero():
    footer, _ = _make(cost=0.0)
    line = _strip(footer.render(120)[0])
    assert "$" not in line


# ── refresh_git_branch ─────────────────────────────────────────────────


def test_refresh_git_branch_caches_result(monkeypatch):
    footer, _ = _make()

    class _Result:
        returncode = 0
        stdout = "feature-branch\n"

    called = []

    def fake_run(cmd, **kw):
        called.append(cmd)
        return _Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    footer._session.cwd = "/proj"
    footer.refresh_git_branch()
    assert footer._git_branch == "feature-branch"
    assert len(called) == 1


def test_refresh_git_branch_handles_not_a_repo(monkeypatch):
    footer, _ = _make()

    class _Result:
        returncode = 128  # not a git repo
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Result())
    footer._session.cwd = "/not/a/repo"
    footer.refresh_git_branch()
    assert footer._git_branch is None
