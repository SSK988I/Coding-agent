"""Tests for core/messages.py."""
from __future__ import annotations

import sys
from pathlib import Path

# Make src importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_llm import UserMessage

from coding_agent.core.messages import (
    BRANCH_SUMMARY_PREFIX,
    BRANCH_SUMMARY_SUFFIX,
    COMPACTION_SUMMARY_PREFIX,
    COMPACTION_SUMMARY_SUFFIX,
    BashExecutionMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
    CustomMessage,
    bash_execution_to_text,
    convert_to_llm,
)


# ─── constant asymmetry ─────────────────────────


def test_compaction_summary_prefix_has_trailing_newline_after_summary_tag():
    # COMPACTION prefix ends with "<summary>\n" (asymmetric with branch).
    assert COMPACTION_SUMMARY_PREFIX.endswith("<summary>\n")
    assert COMPACTION_SUMMARY_SUFFIX.startswith("\n</summary>")


def test_branch_summary_prefix_has_no_trailing_newline_after_summary_tag():
    # BRANCH prefix ends with "<summary>" (no trailing newline).
    assert BRANCH_SUMMARY_PREFIX.endswith("<summary>")
    assert not BRANCH_SUMMARY_PREFIX.endswith("<summary>\n")
    assert BRANCH_SUMMARY_SUFFIX == "</summary>"


# ─── bash_execution_to_text ─────────────────────


def test_bash_execution_with_output_wraps_in_code_fence():
    msg = BashExecutionMessage(command="ls", output="file.txt")
    text = bash_execution_to_text(msg)
    assert text.startswith("Ran `ls`\n")
    assert "```\nfile.txt\n```" in text


def test_bash_execution_no_output_shows_placeholder():
    msg = BashExecutionMessage(command="true", output="")
    text = bash_execution_to_text(msg)
    assert "Ran `true`\n" in text
    assert "(no output)" in text


def test_bash_execution_cancelled_appends_marker():
    msg = BashExecutionMessage(command="sleep 10", cancelled=True)
    text = bash_execution_to_text(msg)
    assert "(command cancelled)" in text


def test_bash_execution_nonzero_exit_appends_code():
    msg = BashExecutionMessage(command="false", exit_code=1)
    text = bash_execution_to_text(msg)
    assert "Command exited with code 1" in text


def test_bash_execution_zero_exit_does_not_append_code():
    msg = BashExecutionMessage(command="true", exit_code=0)
    text = bash_execution_to_text(msg)
    assert "Command exited with code" not in text


def test_bash_execution_truncated_appends_full_output_path():
    msg = BashExecutionMessage(command="ls", truncated=True, full_output_path="/tmp/full.log")
    text = bash_execution_to_text(msg)
    assert "[Output truncated. Full output: /tmp/full.log]" in text


# ─── convert_to_llm ────────────────────────────


def test_convert_to_llm_passes_through_standard_roles():
    user = UserMessage(content="hi")
    out = convert_to_llm([user])
    assert out == [user]


def test_convert_to_llm_drops_bash_execution_with_exclude_from_context():
    msg = BashExecutionMessage(command="secret", exclude_from_context=True)
    assert convert_to_llm([msg]) == []


def test_convert_to_llm_bash_execution_becomes_user_message():
    msg = BashExecutionMessage(command="ls", output="x", timestamp=123.0)
    out = convert_to_llm([msg])
    assert len(out) == 1
    assert out[0].role == "user"
    assert out[0].timestamp == 123.0
    assert isinstance(out[0].content, list)
    assert out[0].content[0].text.startswith("Ran `ls`")


def test_convert_to_llm_custom_string_becomes_text_content():
    msg = CustomMessage(custom_type="note", content="hello", timestamp=5.0)
    out = convert_to_llm([msg])
    assert out[0].role == "user"
    assert out[0].content[0].text == "hello"


def test_convert_to_llm_branch_summary_wraps_with_prefix_suffix():
    msg = BranchSummaryMessage(summary="did a thing", from_id="abc")
    out = convert_to_llm([msg])
    text = out[0].content[0].text
    assert text == BRANCH_SUMMARY_PREFIX + "did a thing" + BRANCH_SUMMARY_SUFFIX


def test_convert_to_llm_compaction_summary_wraps_with_prefix_suffix():
    msg = CompactionSummaryMessage(summary="prior convo", tokens_before=1000)
    out = convert_to_llm([msg])
    text = out[0].content[0].text
    assert text == COMPACTION_SUMMARY_PREFIX + "prior convo" + COMPACTION_SUMMARY_SUFFIX


def test_convert_to_llm_drops_unknown_roles():
    class Weird:
        role = "weird"

    assert convert_to_llm([Weird()]) == []  # type: ignore[list-item]
