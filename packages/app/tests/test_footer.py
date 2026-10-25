"""Tests for FooterComponent."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from agent_tui.utils import visible_width

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
class _Metrics:
    cache_hit_rate: float | None = None
    input_tokens: int = 0
    context_tokens: int | None = None
    context_window: int | None = 1_000_000
    context_percent: float | None = None
    context_estimated: bool = False


@dataclass
class _FakeModel:
    id: str = "deepseek-v4-flash"
    reasoning: bool = True
    context_window: int = 1_000_000


@dataclass
class _Stats:
    tokens: _Usage = field(default_factory=_Usage)
    metrics: _Metrics = field(default_factory=_Metrics)
    cost: float = 0.0


class FakeSession:
    """Minimal session stub for footer render tests."""

    def __init__(self, *, model=None, thinking_level="high", metrics=None,
                 tokens_in=0, tokens_out=0, cost=0.0, cwd=".",
                 collaboration_mode="default", plan_phase="idle", permission_mode=None):
        self.model = model or _FakeModel()
        self.thinking_level = thinking_level
        self.cwd = cwd
        self._metrics = metrics or _Metrics(input_tokens=tokens_in)
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        self._cost = cost
        self.collaboration_mode = collaboration_mode
        self.plan_state = SimpleNamespace(phase=plan_phase)
        self.permission_mode = permission_mode
        self.stats_calls = 0

    def get_stats(self):
        self.stats_calls += 1
        u = _Usage(input=self._tokens_in, output=self._tokens_out,
                   total=self._tokens_in + self._tokens_out)
        return _Stats(tokens=u, metrics=self._metrics, cost=self._cost)


def _make(**kw) -> "tuple[FooterComponent, FakeSession]":
    sess = FakeSession(**kw)
    # Construct without __init__, so refresh_git_branch never spawns Git.
    footer = FooterComponent.__new__(FooterComponent)
    footer._session = sess
    footer._theme = _theme()
    footer._git_branch = None
    footer.invalidate()
    return footer, sess


def test_stats_snapshot_starts_empty_and_is_loaded_lazily(monkeypatch):
    session = FakeSession(cwd=".")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="main\n"))
    footer = FooterComponent(session, _theme())

    assert footer._stats_snapshot is None
    assert session.stats_calls == 0
    footer.render(180)
    assert session.stats_calls == 1


def test_stats_snapshot_is_reused_across_renders_and_width_changes(monkeypatch):
    footer, session = _make(metrics=_Metrics(cache_hit_rate=20), tokens_out=10)

    def unexpected_git(*args, **kwargs):
        pytest.fail("Rendering or invalidating the footer must not start Git")

    monkeypatch.setattr(subprocess, "run", unexpected_git)
    for width in (180, 24, 80, 180):
        assert "cache 20.0%" in _strip("\n".join(footer.render(width)))
    assert session.stats_calls == 1

    session._metrics = _Metrics(cache_hit_rate=95, input_tokens=120_000)
    session._tokens_out = 2000
    assert "cache 20.0%" in _strip("\n".join(footer.render(180)))
    assert session.stats_calls == 1
    footer.invalidate()
    assert session.stats_calls == 1
    text = _strip("\n".join(footer.render(180)))
    assert "cache 95.0%" in text and "in=120k out=2k" in text
    assert session.stats_calls == 2


def test_nonpositive_width_does_not_request_stats():
    footer, session = _make()
    assert footer.render(0) == [""]
    assert footer.render(-1) == [""]
    assert session.stats_calls == 0
    footer.render(80)
    assert session.stats_calls == 1


@pytest.mark.parametrize("phase,label", [("ready", "plan ready"), ("uncertain", "plan uncertain"), ("recovery_error", "plan recovery")])
@pytest.mark.parametrize("width", [24, 180])
def test_plan_and_permission_labels_remain_live_with_stats_snapshot(phase, label, width):
    footer, session = _make()
    footer.render(width)
    session.plan_state = SimpleNamespace(phase=phase)
    session.permission_mode = "full_access"

    text = _strip("\n".join(footer.render(width)))

    assert label in text
    assert "bypass on" in text or "bypass permissions on" in text
    assert session.stats_calls == 1


def test_model_thinking_and_cwd_remain_live_with_stats_snapshot():
    footer, session = _make()
    footer.render(180)
    session.model = _FakeModel(id="next-model")
    session.thinking_level = "low"
    session.cwd = "G:/next-workspace"

    text = _strip("\n".join(footer.render(180)))

    assert "next-model" in text and "thinking low" in text and "G:/next-workspace" in text
    assert session.stats_calls == 1


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


# ── Plan state ─────────────────────────────────────────────────────────


def test_plan_footer_labels_are_state_aware():
    cases = {
        "drafting": "plan",
        "awaiting_answer": "plan",
        "ready": "plan ready",
        "executing": "plan",
        "uncertain": "plan uncertain",
        "recovery_error": "plan recovery",
    }
    for phase, expected in cases.items():
        mode = "plan" if phase in {"drafting", "awaiting_answer", "ready"} else "default"
        footer, _ = _make(collaboration_mode=mode, plan_phase=phase)
        line = _strip(footer.render(120)[0])
        assert expected in line
        assert "mode plan" not in line


def test_plan_recovery_label_survives_narrow_footer():
    footer, _ = _make(
        collaboration_mode="default",
        plan_phase="recovery_error",
        model=_FakeModel(id="very-long-model-name"),
        cwd="C:/a/very/long/project/path/that/would/hide/the/state",
    )
    line = _strip(footer.render(24)[0])
    assert "plan recovery" in line


# ── tokens + context ───────────────────────────────────────────────────


def test_renders_token_counts():
    footer, _ = _make(tokens_in=5000, tokens_out=1200)
    line = _strip(footer.render(120)[0])
    assert "in=5k" in line
    assert "out=1k" in line


def test_zero_tokens_do_not_fabricate_cache_hit_or_context():
    footer, _ = _make(tokens_in=0, tokens_out=0)
    line = _strip("\n".join(footer.render(120)))
    assert "in=0 out=0" in line
    assert "cache —" in line
    assert "ctx —/1.0m" in line
    assert "0.0%" not in line


def test_context_and_input_use_central_metrics_not_uncached_input():
    footer, _ = _make(
        model=_FakeModel(context_window=1_000_000),
        metrics=_Metrics(input_tokens=120_000, context_tokens=100_000,
                         context_percent=10, cache_hit_rate=95),
        tokens_in=5_000,
    )
    line = _strip("\n".join(footer.render(160)))
    assert "ctx 100k/1.0m (10.0%)" in line
    assert "cache 95.0%" in line
    assert "in=120k" in line
    assert "in=5k" not in line


def test_context_unknown_remains_visible():
    footer, _ = _make()
    line = _strip(footer.render(120)[0])
    assert "ctx —/1.0m" in line


def test_context_percent_none_when_no_context_window():
    footer, _ = _make(
        model=_FakeModel(context_window=0),
        metrics=_Metrics(context_tokens=50_000, context_window=None),
    )
    line = _strip(footer.render(120)[0])
    assert "ctx 50k/—" in line


@pytest.mark.parametrize("rate", [0.0, 100.0])
def test_cache_extremes_are_real_percentages(rate):
    footer, _ = _make(metrics=_Metrics(cache_hit_rate=rate))
    text = _strip("\n".join(footer.render(120)))
    assert f"cache {rate:.1f}%" in text


def test_context_estimate_and_over_capacity_are_not_hidden():
    footer, _ = _make(metrics=_Metrics(
        context_tokens=120_000, context_window=100_000,
        context_percent=120.0, context_estimated=True,
    ))
    text = _strip("\n".join(footer.render(160)))
    assert "ctx ~120k/100k (120.0%)" in text


@pytest.mark.parametrize("percent,style", [(69, None), (70, "warning"), (90, "error"), (120, "error")])
def test_context_threshold_color_is_applied_after_layout(percent, style):
    metrics = _Metrics(context_tokens=percent * 1000, context_window=100_000, context_percent=percent)
    footer, _ = _make(metrics=metrics)
    rows = footer.render(160)
    raw = f"ctx {percent}k/100k ({percent:.1f}%)"
    text = "\n".join(rows)
    assert raw in _strip(text)
    if style:
        assert footer._theme.fg(style, raw) in text


@pytest.mark.parametrize("width", [24, 40, 80, 180])
def test_cjk_width_keeps_metrics_and_plan_permission_indicators(width):
    footer, _ = _make(
        cwd="G:/项目/非常长的中文工作目录/另一个目录",
        plan_phase="recovery_error", permission_mode="full_access",
        metrics=_Metrics(input_tokens=2_000_000, cache_hit_rate=80,
                         context_tokens=120_000, context_window=100_000, context_percent=120),
        tokens_out=20_000, cost=1.2,
    )
    rows = footer.render(width)
    text = _strip("\n".join(rows))
    assert "plan recovery" in text
    assert "bypass on" in text or "bypass permissions on" in text
    assert "cache 80.0%" in text
    assert "ctx 120k/100k (120.0%)" in text
    assert "in=2.0m out=20k" in text
    assert "$1.2000" in text
    assert all(visible_width(row) == width for row in rows)
    assert "\x1b" not in _strip(text)


def test_wide_footer_retains_single_inline_row():
    footer, _ = _make(metrics=_Metrics(cache_hit_rate=20), plan_phase="ready", permission_mode="ask")
    rows = footer.render(180)
    assert len(rows) == 1
    assert visible_width(rows[0]) == 180


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
