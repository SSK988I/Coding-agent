"""State projection tests for Plan controls in the terminal UI."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from coding_agent.modes.interactive.interactive_mode import InteractiveMode


@pytest.mark.parametrize(
    ("phase", "field", "expected"),
    [
        ("awaiting_answer", "pending_question", "question"),
        ("ready", "latest_revision", "ready"),
        ("drafting", None, "drafting"),
        ("executing", None, "executing"),
        ("uncertain", None, "uncertain"),
        ("recovery_error", None, "recovery_error"),
        ("settled", None, "closed"),
    ],
)
def test_rehydrate_routes_authoritative_plan_phase(
    phase: str, field: str | None, expected: str,
) -> None:
    marker = object()
    state = SimpleNamespace(
        phase=phase,
        pending_question=marker if field == "pending_question" else None,
        latest_revision=marker if field == "latest_revision" else None,
    )
    calls: list[tuple[str, object | None]] = []
    mode = SimpleNamespace(
        _session=SimpleNamespace(plan_state=state),
        _mount_plan_question=lambda value: calls.append(("question", value)),
        _mount_plan_ready=lambda value: calls.append(("ready", value)),
        _mount_plan_phase_menu=lambda value: calls.append((value, None)),
        _close_plan_controls=lambda: calls.append(("closed", None)),
    )

    InteractiveMode._render_plan_state_controls(mode)  # type: ignore[arg-type]

    assert calls == [(expected, marker if field else None)]


def test_bare_plan_in_drafting_reopens_actions_without_new_episode() -> None:
    calls: list[str] = []
    session = SimpleNamespace(
        plan_state=SimpleNamespace(phase="drafting"),
        enter_plan_mode=lambda: calls.append("enter"),
    )
    mode = SimpleNamespace(
        _session=session,
        _render_plan_state_controls=lambda: calls.append("render"),
    )

    InteractiveMode._cmd_plan(mode)  # type: ignore[arg-type]

    assert calls == ["render"]


def test_bare_plan_with_pending_question_offers_answer_or_cancel_menu() -> None:
    calls: list[str] = []
    session = SimpleNamespace(
        plan_state=SimpleNamespace(phase="awaiting_answer"),
        enter_plan_mode=lambda: calls.append("enter"),
    )
    mode = SimpleNamespace(
        _session=session,
        _mount_plan_phase_menu=lambda phase: calls.append(phase),
        _render_plan_state_controls=lambda: calls.append("render"),
    )

    InteractiveMode._cmd_plan(mode)  # type: ignore[arg-type]

    assert calls == ["awaiting_answer"]


def test_handoff_switches_to_clean_session_and_rehydrates_ready_controls() -> None:
    calls: list[object] = []
    latest = SimpleNamespace(plan_id="plan-1", revision=2, digest="abc")
    target = SimpleNamespace(header=SimpleNamespace(id="new-session"))
    session = SimpleNamespace(
        plan_state=SimpleNamespace(latest_revision=latest),
        handoff_plan_to_new_session=lambda *args: calls.append(args) or target,
    )
    mode = SimpleNamespace(
        _session=session,
        chat_container=SimpleNamespace(clear=lambda: calls.append("clear")),
        _tool_cards={},
        _print_welcome=lambda: calls.append("welcome"),
        _refresh_footer=lambda: calls.append("footer"),
        _update_editor_border_color=lambda: calls.append("border"),
        _add_system_message=lambda value: calls.append(value),
        _render_plan_state_controls=lambda: calls.append("rehydrate"),
    )

    InteractiveMode._handoff_latest_plan(mode)  # type: ignore[arg-type]

    assert ("plan-1", 2, "abc") in calls
    assert "clear" in calls
    assert "rehydrate" in calls
    assert any("new-session" in value for value in calls if isinstance(value, str))


@pytest.mark.parametrize(
    ("event_type", "session_field"),
    [
        ("plan.stateChanged", "sessionId"),
        ("plan_state_changed", "session_id"),
    ],
)
def test_full_plan_state_event_refreshes_footer_border_and_controls(
    event_type: str, session_field: str,
) -> None:
    calls: list[str] = []
    mode = SimpleNamespace(
        _refresh_footer=lambda: calls.append("footer"),
        _update_editor_border_color=lambda: calls.append("border"),
        _render_plan_state_controls=lambda: calls.append("controls"),
    )

    InteractiveMode._on_agent_event(mode, {  # type: ignore[arg-type]
        "type": event_type,
        session_field: "session-1",
        "state": {"phase": "ready"},
    })

    assert calls == ["footer", "border", "controls"]


def test_ready_body_is_visible_once_and_reappears_after_chat_rebuild() -> None:
    from coding_agent.core.plan_mode import create_plan_revision
    from agent_tui import load_theme
    plan = create_plan_revision(plan_id="p", revision=1, title="Review title", markdown="exact body",
                                source_message_id="m", submitted_by_tool_call_id="t")
    texts: list[str] = []
    mode = SimpleNamespace(
        _session=SimpleNamespace(session_manager=SimpleNamespace(header=SimpleNamespace(id="child"))),
        theme=load_theme("dark"),
        _add_assistant_text=texts.append,
        _swap_editor_for=lambda _component: None,
        _restore_editor=lambda: None,
    )
    InteractiveMode._mount_plan_ready(mode, plan)  # type: ignore[arg-type]
    InteractiveMode._mount_plan_ready(mode, plan)  # type: ignore[arg-type]
    assert len(texts) == 1
    assert "Review title" in texts[0] and "exact body" in texts[0]
    mode._displayed_plan_key = None
    InteractiveMode._mount_plan_ready(mode, plan)  # type: ignore[arg-type]
    assert len(texts) == 2


def test_legacy_candidate_has_visible_resubmission_requirement() -> None:
    texts: list[str] = []
    mode = SimpleNamespace(
        _session=SimpleNamespace(
            session_manager=SimpleNamespace(header=SimpleNamespace(id="old")),
            plan_state=SimpleNamespace(phase="drafting", legacy_candidate=True, active_plan_id="p"),
        ),
        _add_system_message=texts.append,
        _mount_plan_phase_menu=lambda _phase: None,
    )
    InteractiveMode._render_plan_state_controls(mode)  # type: ignore[arg-type]
    InteractiveMode._render_plan_state_controls(mode)  # type: ignore[arg-type]
    assert len(texts) == 1 and "submit_plan" in texts[0]
