"""CLI/TUI presentation consumes the authoritative session metrics snapshot."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from agent_tui import load_theme

from coding_agent.cli.main import _print_usage_summary, _run_print
from coding_agent.modes.interactive.interactive_mode import InteractiveMode


def _stats(**overrides):
    values = {
        "cache_hit_rate": 75.0,
        "input_tokens": 80_000,
        "context_tokens": 60_000,
        "context_window": 100_000,
        "context_percent": 60.0,
        "context_estimated": False,
    }
    values.update(overrides)
    return SimpleNamespace(
        metrics=SimpleNamespace(**values),
        tokens=SimpleNamespace(input=20_000, output=1000, cache_read=60_000, cache_write=0, total=81_000),
        cost=0.0123, session_file=None, session_id="test-session", user_messages=1,
        assistant_messages=1, tool_calls=0, tool_results=0, total_messages=2,
    )


@pytest.mark.parametrize("rate,label", [(0.0, "0.0%"), (100.0, "100.0%"), (None, "—")])
def test_print_usage_summary_preserves_cache_zero_full_and_unknown(capsys, rate, label):
    session = SimpleNamespace(get_stats=lambda: _stats(cache_hit_rate=rate))
    _print_usage_summary(session)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"缓存命中率 {label}" in captured.err
    assert "上下文 60,000/100,000（60.0%）" in captured.err
    assert "累计输入（含缓存） 80,000 / 输出 1,000" in captured.err
    assert "费用 $0.0123" in captured.err


def test_print_usage_summary_unknown_context_does_not_fabricate_zero(capsys):
    session = SimpleNamespace(get_stats=lambda: _stats(
        context_tokens=None, context_window=None, context_percent=None, cache_hit_rate=None,
    ))
    _print_usage_summary(session)
    assert "上下文 —/—" in capsys.readouterr().err


def test_print_usage_summary_marks_estimated_context(capsys):
    session = SimpleNamespace(get_stats=lambda: _stats(context_estimated=True))
    _print_usage_summary(session)
    assert "上下文 约 60,000/100,000（60.0%）" in capsys.readouterr().err


class _PrintSession:
    def __init__(self):
        self.listeners = []
        self.disposed = False

    def on_event(self, listener):
        self.listeners.append(listener)

    async def prompt(self, _prompt):
        for listener in self.listeners:
            listener({"type": "message_update", "event": {"type": "text_delta", "delta": "answer"}})

    def get_stats(self):
        return _stats()

    def dispose(self):
        self.disposed = True


def test_text_print_mode_reports_metrics_only_on_stderr(capsys):
    session = _PrintSession()
    assert _run_print(session, "question") == 0  # type: ignore[arg-type]
    captured = capsys.readouterr()
    assert captured.out == "answer\n"
    assert "[用量]" in captured.err
    assert session.disposed


def test_json_print_mode_stays_machine_readable_without_human_summary(capsys):
    session = _PrintSession()
    assert _run_print(session, "question", mode="json") == 0  # type: ignore[arg-type]
    captured = capsys.readouterr()
    event = json.loads(captured.out)
    assert event["type"] == "message_update"
    assert captured.err == ""
    assert session.disposed


@pytest.mark.parametrize("estimated", [False, True])
def test_session_command_shows_plain_language_cumulative_and_context_metrics(estimated):
    stats = _stats(context_estimated=estimated)
    added = []
    mode = SimpleNamespace(
        theme=load_theme("dark"),
        _session=SimpleNamespace(get_stats=lambda: stats, session_manager=SimpleNamespace(get_name=lambda: None)),
        _add_to_chat=added.append,
        tui=SimpleNamespace(request_render=lambda: None),
    )
    InteractiveMode._cmd_session(mode)  # type: ignore[arg-type]
    rendered = "\n".join(added[0].render(140))
    assert "累计输入（含缓存）:" in rendered
    assert "80,000" in rendered
    assert "未命中缓存的输入:" in rendered
    assert "缓存命中率（当前分支累计）:" in rendered
    assert "75.0%" in rendered
    assert "上下文容量使用:" in rendered
    assert ("约 60,000" in rendered) is estimated
    assert "60.0%" in rendered
