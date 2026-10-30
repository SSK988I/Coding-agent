"""Side chat creation and labels must remain distinguishable and idempotent."""
from __future__ import annotations

import asyncio
import os

import pytest
from agent_core import SessionManager
from agent_llm import Model, ModelCost, TextContent, UserMessage

from coding_agent.desktop.side_conversations import SideConversations
from test_workbench import make_parent


def test_repeated_new_chat_reuses_an_unused_draft_without_exhausting_capacity(tmp_path):
    async def scenario():
        parent = make_parent(tmp_path)
        chats = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        try:
            first = chats.open()["conversationId"]
            assert [chats.open()["conversationId"] for _ in range(20)] == [first] * 20
            assert len(chats.records) == 1
            assert len(chats.list()) == 1
            assert not list(chats.storage.rglob("*.jsonl"))
        finally:
            await chats.close()
            await parent.aclose()
    asyncio.run(scenario())


def test_a_conversation_with_content_is_never_reused_as_a_new_draft(tmp_path):
    async def scenario():
        parent = make_parent(tmp_path)
        chats = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        try:
            first = chats.open()["conversationId"]
            live = chats.records[first]
            message = UserMessage(content="Keep this conversation")
            live.session.session_manager.append_message(message)
            live.session.agent.load_messages([message])
            second = chats.open()["conversationId"]
            assert first != second
            assert chats.open(first)["messages"][0]["content"] == "Keep this conversation"
            assert chats.open()["conversationId"] == second
        finally:
            await chats.close()
            await parent.aclose()
    asyncio.run(scenario())


def test_block_content_and_duplicate_titles_are_distinguishable_without_changing_messages(tmp_path):
    async def scenario():
        parent = make_parent(tmp_path)
        chats = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        try:
            identities = []
            original = [TextContent(text="  同一个\n话题  ")]
            for _ in range(2):
                identity = chats.open()["conversationId"]
                identities.append(identity)
                message = UserMessage(content=list(original))
                live = chats.records[identity]
                live.session.session_manager.append_message(message)
                live.session.agent.load_messages([message])
            listed = {item["conversationId"]: item["title"] for item in chats.list()}
            assert listed[identities[0]] != listed[identities[1]]
            for identity in identities:
                assert "同一个 话题" in listed[identity]
                assert "\n" not in listed[identity]
                assert chats.records[identity].session.state.messages[0].content == original
        finally:
            await chats.close()
            await parent.aclose()
    asyncio.run(scenario())


def test_saved_conversations_keep_newest_first_order_and_existing_history(tmp_path):
    async def scenario():
        parent = make_parent(tmp_path)
        chats = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        sessions = []
        try:
            for title, modified in (("Older chat", 100), ("Newer chat", 200)):
                manager = SessionManager.create(cwd=parent.cwd, sessions_dir=chats.storage,
                    parent_session=parent.session_manager.header.id)
                manager.append_message(UserMessage(content=title))
                manager.flush()
                assert manager.path is not None
                os.utime(manager.path, (modified, modified))
                sessions.append(manager)
            before = {manager.path: manager.path.read_bytes() for manager in sessions if manager.path is not None}
            assert [item["title"] for item in chats.list()] == ["Newer chat", "Older chat"]
            assert before == {path: path.read_bytes() for path in before}
        finally:
            await chats.close()
            await parent.aclose()
    asyncio.run(scenario())


def test_empty_legacy_conversations_have_distinct_fallback_labels_and_remain_available(tmp_path):
    async def scenario():
        parent = make_parent(tmp_path)
        chats = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        try:
            # Recreate old in-memory empty records through explicit persisted IDs.
            identities = []
            for _ in range(2):
                manager = SessionManager.create(cwd=parent.cwd, sessions_dir=chats.storage,
                    parent_session=parent.session_manager.header.id)
                manager.append_message(UserMessage(content=""))
                manager.flush()
                identities.append(manager.header.id)
            titles = {item["conversationId"]: item["title"] for item in chats.list()}
            assert set(titles) == set(identities)
            assert len(set(titles.values())) == 2
            assert all(title.startswith("新对话") for title in titles.values())
            assert chats.open(identities[0])["conversationId"] == identities[0]
        finally:
            await chats.close()
            await parent.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["failed", "cancelled", "uncertain", "running", "unknown"])
def test_non_idle_records_are_never_reused_even_without_messages(tmp_path, status):
    async def scenario():
        parent = make_parent(tmp_path)
        chats = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        try:
            identity = chats.open()["conversationId"]
            chats.records[identity].status = status
            assert chats.open()["conversationId"] != identity
            assert identity in chats.records and chats.records[identity].status == status
        finally:
            await chats.close()
            await parent.aclose()
    asyncio.run(scenario())


def test_idle_record_with_unflushed_entries_is_not_reused(tmp_path):
    async def scenario():
        parent = make_parent(tmp_path)
        chats = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        try:
            identity = chats.open()["conversationId"]
            manager = chats.records[identity].session.session_manager
            entry = manager.append_message(UserMessage(content="A pending history entry"))
            assert not chats.records[identity].session.state.messages
            assert chats.open()["conversationId"] != identity
            assert manager.entries == [entry]
        finally:
            await chats.close()
            await parent.aclose()
    asyncio.run(scenario())


def test_unused_draft_can_be_reopened_when_other_records_already_fill_capacity(tmp_path):
    async def scenario():
        parent = make_parent(tmp_path)
        chats = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        try:
            for _ in range(15):
                identity = chats.open()["conversationId"]
                chats.records[identity].status = "failed"
            draft = chats.open()["conversationId"]
            assert len(chats.records) == 16
            assert chats.open()["conversationId"] == draft
            assert len(chats.records) == 16
        finally:
            await chats.close()
            await parent.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("configuration", ["model", "reasoning", "mode"])
def test_new_chat_inherits_current_parent_configuration_instead_of_reusing_stale_draft(tmp_path, configuration):
    async def scenario():
        parent = make_parent(tmp_path)
        chats = SideConversations(parent, tmp_path / "cache", lambda *_: None, lambda: "ask")
        try:
            old_id = chats.open()["conversationId"]
            old = chats.records[old_id]
            old_model = old.session.model
            old_reasoning = old.session.agent.reasoning
            old_tools = {tool.name for tool in old.session.tools}
            if configuration == "model":
                parent.set_model(Model(id="next-model", provider="next-provider", cost=ModelCost(0, 0, 0, 0)))
            elif configuration == "reasoning":
                parent.set_thinking_level("high")
            else:
                parent.enter_plan_mode()
            fresh_id = chats.open()["conversationId"]
            assert fresh_id != old_id
            fresh = chats.records[fresh_id]
            assert fresh.session.model == parent.model
            assert fresh.session.agent.reasoning == parent.agent.reasoning
            assert {tool.name for tool in fresh.session.tools} == {
                tool.name for tool in parent.tools if tool.name in {
                    "read", "write", "edit", "bash", "grep", "find", "ls", "git_status", "git_log", "git_diff", "git_show", "web_search"
                }
            }
            assert chats.open()["conversationId"] == fresh_id
            assert old.session.model == old_model
            assert old.session.agent.reasoning == old_reasoning
            assert {tool.name for tool in old.session.tools} == old_tools
            assert old.session.session_manager.entries == []
            assert set(chats.records) == {old_id, fresh_id}
        finally:
            await chats.close()
            await parent.aclose()
    asyncio.run(scenario())
