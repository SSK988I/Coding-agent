"""State projection tests for Plan controls in the terminal UI."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from coding_agent.modes.interactive.interactive_mode import InteractiveMode


@pytest.mark.parametrize(
    ("phase", "field", "expected"),
    [
        ("awaiting_answer", "pending_question", "question"),
        ("ready", "latest_revision", "ready"),
        ("drafting", None, "closed"),
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
        mode="plan" if phase in {"drafting", "awaiting_answer", "ready"} else "default",
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
        plan_state=SimpleNamespace(phase="drafting", mode="plan"),
        enter_plan_mode=lambda: calls.append("enter"),
    )
    mode = SimpleNamespace(
        _session=session,
        _mount_plan_phase_menu=lambda phase: calls.append(phase),
        _render_plan_state_controls=lambda: calls.append("render"),
    )

    InteractiveMode._cmd_plan(mode)  # type: ignore[arg-type]

    assert calls == ["drafting"]


def test_bare_plan_in_default_enters_without_mounting_drafting_menu() -> None:
    calls: list[str] = []
    session = SimpleNamespace(
        plan_state=SimpleNamespace(phase="idle", mode="default"),
        enter_plan_mode=lambda: calls.append("enter"),
    )
    mode = SimpleNamespace(
        _session=session,
        _refresh_footer=lambda: calls.append("footer"),
        _update_editor_border_color=lambda: calls.append("border"),
        _restore_editor=lambda: calls.append("restore"),
        _add_system_message=lambda value: calls.append(value),
    )

    InteractiveMode._cmd_plan(mode)  # type: ignore[arg-type]

    assert calls == ["enter", "footer", "border", "已进入计划模式。", "restore"]


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
        _close_plan_controls=lambda: None,
    )
    InteractiveMode._render_plan_state_controls(mode)  # type: ignore[arg-type]
    InteractiveMode._render_plan_state_controls(mode)  # type: ignore[arg-type]
    assert len(texts) == 1 and "submit_plan" in texts[0]


def test_submission_shows_progress_before_first_event_and_clears_on_exit() -> None:
    from test_interactive_events import _make_mode

    mode = _make_mode()
    messages: list[str] = []
    mode._session.plan_state = SimpleNamespace(phase="drafting")
    mode.editor = SimpleNamespace(disable_submit=False)
    mode._add_user_message = lambda text: None
    mode._add_system_message = messages.append
    mode._refresh_footer = lambda: None
    mode._render_plan_state_controls = lambda: None

    async def respond(_prompt: str) -> None:
        # No agent_start or message_start has arrived yet: the click itself
        # must already have provided visible feedback.
        assert mode.editor.disable_submit
        assert mode._active_status_indicator is not None
        assert "正在整理并提交计划" in "".join(mode.status_container.render(100))
        mode._on_agent_event({"type": "agent_start"})
        assert "正在整理并提交计划" in "".join(mode.status_container.render(100))
        # Duplicate clicks must not start another request.
        await InteractiveMode._request_plan_submission(mode)

    mode._respond = respond
    asyncio.run(InteractiveMode._request_plan_submission(mode))

    assert not mode._is_responding
    assert not mode.editor.disable_submit
    assert mode._active_status_indicator is None
    assert any("尚未提交计划" in text for text in messages)


@pytest.mark.parametrize("phase", ["drafting", "ready", "awaiting_answer"])
def test_submission_cleanup_and_feedback_follow_core_phase(phase: str) -> None:
    from test_interactive_events import _make_mode

    mode = _make_mode()
    messages: list[str] = []
    mode._session.plan_state = SimpleNamespace(phase="drafting")
    mode.editor = SimpleNamespace(disable_submit=False)
    mode._add_user_message = lambda text: None
    mode._add_system_message = messages.append
    mode._refresh_footer = lambda: None
    rendered: list[str] = []
    mode._render_plan_state_controls = lambda: rendered.append(mode._session.plan_state.phase)

    async def respond(_prompt: str) -> None:
        mode._session.plan_state.phase = phase
        if phase == "drafting":
            raise asyncio.CancelledError

    mode._respond = respond
    if phase == "drafting":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(InteractiveMode._request_plan_submission(mode))
    else:
        asyncio.run(InteractiveMode._request_plan_submission(mode))

    assert rendered == [phase]
    assert not mode._is_responding
    assert not mode.editor.disable_submit
    assert mode._active_status_indicator is None
    assert any("尚未提交计划" in text for text in messages) == (phase == "drafting")
