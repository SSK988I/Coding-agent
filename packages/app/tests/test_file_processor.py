"""Tests for cli/file_processor.py and cli/initial_message.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coding_agent.cli.args import Args
from coding_agent.cli.file_processor import process_file_arguments
from coding_agent.cli.initial_message import build_initial_message
from agent_llm import UserMessage


# ─── process_file_arguments ────────────────────────────────────────────


def test_text_file_is_wrapped_in_file_tag(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("hello world", encoding="utf-8")
    out = process_file_arguments([str(f)], str(tmp_path))
    assert out.images == []
    assert f'<file name="{f.resolve()}">\nhello world\n</file>\n' in out.text


def test_empty_file_is_skipped_silently(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("", encoding="utf-8")
    out = process_file_arguments([str(f)], str(tmp_path))
    assert out.text == ""
    assert out.images == []


def test_missing_file_exits(tmp_path, capsys):
    missing = tmp_path / "nope.txt"
    try:
        process_file_arguments([str(missing)], str(tmp_path))
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code == 1
    captured = capsys.readouterr()
    assert "找不到文件" in captured.err


def test_relative_path_resolved_against_cwd(tmp_path):
    f = tmp_path / "rel.txt"
    f.write_text("rel", encoding="utf-8")
    out = process_file_arguments(["rel.txt"], str(tmp_path))
    assert "rel" in out.text


def test_at_prefix_stripped(tmp_path):
    f = tmp_path / "at.txt"
    f.write_text("at", encoding="utf-8")
    out = process_file_arguments(["@at.txt"], str(tmp_path))
    assert "at" in out.text


def test_multiple_text_files_concatenated(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("AAA", encoding="utf-8")
    b = tmp_path / "b.txt"
    b.write_text("BBB", encoding="utf-8")
    out = process_file_arguments([str(a), str(b)], str(tmp_path))
    assert "AAA" in out.text and "BBB" in out.text


def test_image_file_produces_image_content_and_reference(tmp_path):
    # Minimal 1x1 PNG.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63000100000005000100" "0d0a2db40000000049454e44ae426082"
    )
    img = tmp_path / "pixel.png"
    img.write_bytes(png)
    out = process_file_arguments([str(img)], str(tmp_path))
    assert len(out.images) == 1
    assert out.images[0].mime_type == "image/png"
    assert out.images[0].data  # base64 non-empty
    assert f'<file name="{img.resolve()}"></file>' in out.text


# ─── build_initial_message ─────────────────────────────────────────────


def test_no_inputs_returns_none():
    parsed = Args()
    res = build_initial_message(parsed)
    assert res.initial_message is None
    assert res.initial_images is None


def test_stdin_only():
    parsed = Args()
    res = build_initial_message(parsed, stdin_content="piped")
    assert res.initial_message == "piped"


def test_file_text_only():
    parsed = Args()
    res = build_initial_message(parsed, file_text="<file>...</file>\n")
    assert res.initial_message == "<file>...</file>\n"


def test_first_message_consumed_and_joined():
    parsed = Args()
    parsed.messages = ["first", "second"]
    res = build_initial_message(parsed, file_text="FILE")
    # Joined with "" (no separator): FILE + first.
    assert res.initial_message == "FILEfirst"
    # The first message was consumed; remainder stays.
    assert parsed.messages == ["second"]


def test_order_stdin_file_then_message():
    parsed = Args()
    parsed.messages = ["M"]
    res = build_initial_message(
        parsed, file_text="F", stdin_content="S",
    )
    # stdin gets a trailing newline when more parts follow (S\n + F + M).
    assert res.initial_message == "S\nFM"


def test_images_passed_through():
    parsed = Args()
    res = build_initial_message(parsed, file_images=["img"])  # type: ignore[arg-type]
    assert res.initial_images == ["img"]


def test_empty_file_images_becomes_none():
    parsed = Args()
    res = build_initial_message(parsed, file_images=[])
    assert res.initial_images is None


def test_images_are_included_in_actual_user_prompt():
    from agent_llm import ImageContent

    parsed = Args(messages=["describe this"])
    image = ImageContent(data="base64", mime_type="image/png")
    result = build_initial_message(parsed, file_images=[image])
    prompt = result.to_prompt()

    assert isinstance(prompt, UserMessage)
    assert [block.type for block in prompt.content] == ["text", "image"]
    assert prompt.content[1] is image
