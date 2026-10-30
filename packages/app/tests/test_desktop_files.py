"""General file attachments preserve actual bytes and restorable references."""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest
from agent_core import SessionManager
from agent_core.session.serde import dict_to_message, message_to_dict
from agent_llm import ImageContent, TextContent, UserMessage

from coding_agent.core.plan_mode import PlanQuestion
from coding_agent.desktop import attachments
from coding_agent.desktop.attachments import (
    FILE_CONTENT_MARKER, MAX_FILE_BYTES, MAX_FILE_EXCERPT_BYTES,
    FileAttachment, parse_file_attachments, store_file_attachments,
)
from coding_agent.desktop.protocol import RpcError
from test_desktop_images import make_runtime, png_attachment, successful_stream


def file_payload(name="notes.txt", data=b"Hello from a selected file", mime="text/plain"):
    return {"name": name, "mime_type": mime, "data": base64.b64encode(data).decode("ascii")}


@pytest.mark.parametrize("text", ["", "Summarize this file"])
def test_text_file_reaches_text_model_and_round_trips_without_binary_jsonl(tmp_path, text):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path, input_modalities=["text"])
        captured = []

        def stream(_model, context, _options):
            captured.append(context)
            return successful_stream()

        session.agent.stream_fn = stream
        raw = file_payload("笔记.txt", "你好，文件内容".encode())
        try:
            result = await runtime.dispatch("run.start", {"text": text, "files": [raw]})
            await asyncio.wait_for(runtime._run_task, 5)
            assert result["accepted"] and events[-1]["event"]["type"] == "run.completed"
            user = captured[0].messages[0]
            assert isinstance(user, UserMessage)
            assert len(user.attachments) == 1
            metadata = user.attachments[0]
            path = Path(metadata["path"])
            assert path.read_bytes() == "你好，文件内容".encode()
            assert not path.is_relative_to(tmp_path / "sessions")
            assert metadata == {"name": "笔记.txt", "mime_type": "text/plain", "size": len(path.read_bytes()), "path": str(path)}
            assert isinstance(user.content, list)
            file_block = user.content[-1]
            assert isinstance(file_block, TextContent) and file_block.text.startswith(FILE_CONTENT_MARKER)
            assert "你好，文件内容" in file_block.text
            assert json.loads(file_block.text.splitlines()[1]) == metadata
            if text:
                assert user.content[0] == TextContent(text=text)
            started = events[0]["event"]["payload"]
            assert started == {"text": text, "images": [], "files": user.attachments}
            snapshot = await runtime.dispatch("session.snapshot", {})
            assert snapshot["messages"][0]["attachments"] == user.attachments
            assert session.session_manager.path is not None
            saved = session.session_manager.path.read_text(encoding="utf-8")
            assert raw["data"] not in saved
            restored = SessionManager.open(session.session_manager.path).build_session_context().messages[0]
            assert restored == user
            assert Path(restored.attachments[0]["path"]).read_bytes() == path.read_bytes()
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_mixed_image_binary_and_text_keep_native_image_and_original_user_text(tmp_path):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path)
        session.agent.stream_fn = successful_stream
        raw = file_payload("document.pdf", b"%PDF-1.7\nnot extracted\x00\xff", "application/pdf")
        image = png_attachment()
        try:
            await runtime.dispatch("run.start", {"text": "Compare attachments", "images": [image], "files": [raw]})
            await asyncio.wait_for(runtime._run_task, 5)
            user = session.state.messages[0]
            assert user.content[0] == TextContent(text="Compare attachments")
            assert user.content[1] == ImageContent(**image)
            assert "No file contents were extracted" in user.content[2].text
            assert "not extracted" not in user.content[2].text
            assert Path(user.attachments[0]["path"]).read_bytes() == b"%PDF-1.7\nnot extracted\x00\xff"
            assert events[-1]["event"]["type"] == "run.completed"
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("raw", [
    None, "file.txt", {}, True, [None], [{"path": "C:/secret.txt"}],
    [{"name": "x", "mime_type": "text/plain", "data": "!!!!"}],
    [{"name": "x", "mime_type": "text/plain", "data": "AB=="}],
    [{"name": "x", "mime_type": "text/plain", "data": "data:text/plain;base64,YQ=="}],
    [{"name": "x", "mime_type": "text/plain", "data": 123}],
    [{"name": "x", "mime_type": "text/plain\nheader", "data": ""}],
    [file_payload()] * 9,
])
def test_file_parser_rejects_malformed_payloads(raw):
    with pytest.raises(RpcError) as error:
        parse_file_attachments(raw)
    assert error.value.code == "INVALID_FILES"


@pytest.mark.parametrize("name", ["", " ", ".", "..", "../x.txt", "folder/x", "folder\\x", "C:secret.txt", "x\n.txt", "x\x00.txt", "x\x7f.txt", "NUL.txt", "com1", "x.", "x ", "x" * 256, "\ud800.txt"])
def test_file_names_cannot_traverse_or_use_invalid_windows_names(name):
    with pytest.raises(RpcError) as error:
        parse_file_attachments([file_payload(name)])
    assert error.value.code == "INVALID_FILES"


def test_oversized_encoded_data_rejected_before_decode(monkeypatch):
    raw = file_payload()
    raw["data"] = "A" * ((((MAX_FILE_BYTES + 2) // 3) * 4) + 1)
    monkeypatch.setattr(attachments.base64, "b64decode", lambda *_args, **_kwargs: pytest.fail("Oversize must be rejected before allocation"))
    with pytest.raises(RpcError, match="10 MiB"):
        parse_file_attachments([raw])


def test_file_and_combined_byte_budgets(monkeypatch):
    monkeypatch.setattr(attachments, "MAX_FILE_BYTES", 6)
    monkeypatch.setattr(attachments, "MAX_TOTAL_FILE_BYTES", 10)
    with pytest.raises(RpcError, match="10 MiB"):
        parse_file_attachments([file_payload(data=b"1234567")])
    with pytest.raises(RpcError, match="20 MiB"):
        parse_file_attachments([file_payload(data=b"123456")] * 2)
    files = parse_file_attachments([file_payload(data=b"123456")])
    with pytest.raises(RpcError, match="20 MiB"):
        attachments.validate_attachment_budget(files, [ImageContent(data=base64.b64encode(b"12345").decode(), mime_type="image/png")])
    with pytest.raises(RpcError, match="8 个附件"):
        attachments.validate_attachment_budget([FileAttachment("x", "text/plain", b"")] * 8, [ImageContent()])


def test_storage_is_unique_bounded_and_retains_full_original_files(tmp_path):
    text = ("中文" * 20_000).encode()
    files = [FileAttachment("same.txt", "text/plain", text)] * 3
    stored = store_file_attachments(files, tmp_path / "owned", "session-1")
    assert stored is not None
    assert len({item["path"] for item in stored.metadata}) == 3
    assert all(Path(item["path"]).read_bytes() == text for item in stored.metadata)
    for block in stored.content[:2]:
        assert "Excerpt truncated" in block.text
        excerpt = block.text.split("UTF-8 text excerpt:\n", 1)[1].split("\n[Excerpt truncated", 1)[0]
        assert len(excerpt.encode()) <= MAX_FILE_EXCERPT_BYTES
    assert "No file contents were extracted" in stored.content[2].text
    stored.cleanup()
    assert not stored.directory.exists()


def test_empty_and_non_utf8_files_are_honest(tmp_path):
    stored = store_file_attachments([
        FileAttachment("empty.txt", "text/plain", b""),
        FileAttachment("legacy.txt", "text/plain", b"\xff\xfe\x00\x01"),
    ], tmp_path / "owned", "session")
    assert stored is not None
    assert "(empty file)" in stored.content[0].text
    assert "No file contents were extracted" in stored.content[1].text


def test_file_write_failure_rejects_before_run_and_cleans_partial_copy(tmp_path, monkeypatch):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path)
        real_fsync = attachments.os.fsync
        monkeypatch.setattr(attachments.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("disk full")))
        try:
            with pytest.raises(RpcError, match="disk full") as error:
                await runtime.dispatch("run.start", {"text": "read", "files": [file_payload()]})
            assert error.value.code == "ATTACHMENT_WRITE_FAILED"
            assert runtime._run_task is None and not events and not session.state.messages
            assert not list((tmp_path / "fake-agent-dir" / "attachments").rglob("*.txt"))
        finally:
            monkeypatch.setattr(attachments.os, "fsync", real_fsync)
            await runtime.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["uncertain", "recovery_error", "awaiting_answer"])
def test_file_messages_cannot_bypass_plan_gates_and_drop_unreferenced_copies(tmp_path, phase):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path)
        session._plan_state.mode = "plan"
        session._plan_state.phase = phase
        if phase == "awaiting_answer":
            session._plan_state.pending_question = PlanQuestion("question", "Header", "Choose", ())
        session.agent.stream_fn = lambda *_args: pytest.fail("No provider call")
        try:
            await runtime.dispatch("run.start", {"text": "read", "files": [file_payload()]})
            await asyncio.wait_for(runtime._run_task, 5)
            assert events[-1]["event"]["type"] == "run.failed"
            assert not session.state.messages
            started = events[0]["event"]["payload"]
            assert not Path(started["files"][0]["path"]).exists()
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_archived_session_rejects_files_before_storing(tmp_path):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path)
        session.session_manager.set_archived(True)
        try:
            with pytest.raises(RpcError) as error:
                await runtime.dispatch("run.start", {"text": "read", "files": [file_payload()]})
            assert error.value.code == "SESSION_ARCHIVED"
            assert not events and runtime._run_task is None
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_cancellation_before_run_coroutine_starts_cleans_staged_files(tmp_path):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path)
        session.agent.stream_fn = lambda *_args: pytest.fail("Cancelled before provider startup")
        try:
            await runtime.dispatch("run.start", {"text": "read", "files": [file_payload()]})
            root = tmp_path / "fake-agent-dir" / "attachments"
            assert list(root.rglob("*.txt"))
            task = runtime._run_task
            assert task is not None
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not list(root.rglob("*.txt"))
            assert not events and not session.state.messages
        finally:
            await runtime.dispose()
    asyncio.run(scenario())


def test_file_run_flush_failure_is_failed_and_keeps_recoverable_copies(tmp_path, monkeypatch):
    async def scenario():
        runtime, session, events = make_runtime(tmp_path)
        session.agent.stream_fn = successful_stream
        flush = session.session_manager.flush
        monkeypatch.setattr(session.session_manager, "flush", lambda: (_ for _ in ()).throw(OSError("cannot flush")))
        try:
            await runtime.dispatch("run.start", {"text": "read", "files": [file_payload()]})
            await asyncio.wait_for(runtime._run_task, 5)
            assert events[-1]["event"]["type"] == "run.failed"
            assert events[-1]["event"]["payload"]["code"] == "ATTACHMENT_PERSISTENCE_FAILED"
            assert Path(session.state.messages[0].attachments[0]["path"]).exists()
            monkeypatch.setattr(session.session_manager, "flush", flush)
            session.session_manager.flush()
            assert session.session_manager.path is not None
            restored = SessionManager.open(session.session_manager.path).build_session_context().messages[0]
            assert restored.attachments == session.state.messages[0].attachments
        finally:
            monkeypatch.setattr(session.session_manager, "flush", flush)
            await runtime.dispose()
    asyncio.run(scenario())


def test_lost_file_entry_is_detected_even_when_later_appends_and_flush_succeed(tmp_path, monkeypatch):
    import agent_core.session.session_manager as persistence

    async def scenario():
        runtime, session, events = make_runtime(tmp_path)
        session.agent.stream_fn = successful_stream
        previous = UserMessage(content="Already saved")
        session.state.messages.append(previous)
        session.session_manager.append_message(previous)
        session.session_manager.flush()
        append = persistence.append_entry_line

        def fail_file_entry(path, entry):
            if getattr(getattr(entry, "message", None), "attachments", None):
                raise OSError("cannot append file entry")
            return append(path, entry)

        monkeypatch.setattr(persistence, "append_entry_line", fail_file_entry)
        try:
            await runtime.dispatch("run.start", {"text": "read", "files": [file_payload()]})
            await asyncio.wait_for(runtime._run_task, 5)
            assert events[-1]["event"]["type"] == "run.failed"
            assert events[-1]["event"]["payload"]["code"] == "ATTACHMENT_PERSISTENCE_FAILED"
            assert not any(event["event"]["type"] == "run.completed" for event in events)
            assert Path(session.state.messages[1].attachments[0]["path"]).exists()
            session.session_manager.flush()
        finally:
            monkeypatch.setattr(persistence, "append_entry_line", append)
            await runtime.dispose()
    asyncio.run(scenario())


def test_display_metadata_dual_reads_legacy_and_omits_empty_or_binary_fields():
    old = dict_to_message({"role": "user", "content": "hi", "timestamp": 1})
    assert old.attachments == [] and "attachments" not in message_to_dict(old)
    metadata = {"name": "x.txt", "mime_type": "text/plain", "path": "C:/owned/file.txt", "size": 0}
    user = UserMessage(content="hi", attachments=[{**metadata, "data": "not-persisted"}])
    encoded = message_to_dict(user)
    assert encoded["attachments"] == [metadata]
    assert "not-persisted" not in json.dumps(encoded)
    assert dict_to_message(encoded).attachments == [metadata]
    assert dict_to_message({"role": "user", "content": "hi", "attachments": [None, {**metadata, "size": True}]}).attachments == []
