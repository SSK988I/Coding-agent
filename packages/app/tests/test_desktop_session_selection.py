"""Session selection must not scan all history when only new/recent is needed."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from agent_core import SessionManager

from coding_agent.desktop.protocol import RpcError
from coding_agent.desktop.runtime import DesktopRuntime


@pytest.mark.parametrize("resume,has_recent", [(False, False), (True, False), (True, True)])
def test_new_and_recent_session_selection_do_not_list_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume: bool, has_recent: bool,
):
    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr("coding_agent.desktop.runtime.get_sessions_dir", lambda: sessions_dir)
    created = SessionManager.create(in_memory=True)
    recent = SessionManager.create(in_memory=True) if has_recent else None
    runtime = DesktopRuntime(lambda _event: None)
    with (
        patch.object(SessionManager, "list_sessions", side_effect=AssertionError("Unexpected full history scan")),
        patch.object(SessionManager, "continue_recent", return_value=recent) as continue_recent,
        patch.object(SessionManager, "create", return_value=created) as create,
    ):
        result = runtime._select_session_manager(tmp_path, session_id=None, resume=resume)

    assert result is (recent if has_recent else created)
    if resume:
        continue_recent.assert_called_once_with(cwd=str(tmp_path), sessions_dir=sessions_dir)
    else:
        continue_recent.assert_not_called()
    if has_recent:
        create.assert_not_called()
    else:
        create.assert_called_once_with(cwd=str(tmp_path), sessions_dir=sessions_dir)


@pytest.mark.parametrize("session_id", ["selected", "missing"])
def test_explicit_session_selection_still_validates_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_id: str,
):
    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr("coding_agent.desktop.runtime.get_sessions_dir", lambda: sessions_dir)
    selected = SessionManager.create(in_memory=True)
    session_path = sessions_dir / "selected.jsonl"
    infos = [SimpleNamespace(id="selected", path=str(session_path))]
    runtime = DesktopRuntime(lambda _event: None)
    with (
        patch.object(SessionManager, "list_sessions", return_value=infos) as list_sessions,
        patch.object(SessionManager, "open", return_value=selected) as open_session,
        patch.object(SessionManager, "continue_recent", side_effect=AssertionError("Must respect explicit id")),
        patch.object(SessionManager, "create", side_effect=AssertionError("Must respect explicit id")),
    ):
        if session_id == "selected":
            assert runtime._select_session_manager(tmp_path, session_id=session_id, resume=True) is selected
        else:
            with pytest.raises(RpcError) as error:
                runtime._select_session_manager(tmp_path, session_id=session_id, resume=True)
            assert error.value.code == "SESSION_NOT_FOUND"

    list_sessions.assert_called_once_with(cwd=str(tmp_path), sessions_dir=sessions_dir)
    if session_id == "selected":
        open_session.assert_called_once_with(str(session_path), sessions_dir=sessions_dir)
    else:
        open_session.assert_not_called()
