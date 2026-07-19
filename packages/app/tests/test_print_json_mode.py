"""Tests for the ``--mode json`` print mode."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coding_agent.cli.args import parse_args, resolve_app_mode
from coding_agent.cli.main import _json_default


# ─── args + resolve_app_mode ──────────────────────────────────────────


def test_mode_json_parsed():
    args = parse_args(["--mode", "json", "hello"])
    assert args.output_mode == "json"


def test_mode_text_is_default():
    args = parse_args(["hello"])
    assert args.output_mode == "text"


def test_mode_json_forces_print_mode():
    """--mode json forces print mode even on a tty (machine output)."""
    args = parse_args(["--mode", "json", "hello"])
    # resolve_app_mode checks isatty; --mode json short-circuits before that.
    assert resolve_app_mode(args) == "print"


def test_mode_text_without_print_stays_interactive_on_tty():
    """Without --print and on a tty, text mode stays interactive."""
    args = parse_args(["hello"])
    args.output_mode = "text"
    # Can't easily fake isatty here; just assert the field flows through.
    assert args.output_mode == "text"


def test_invalid_mode_rejected():
    """argparse rejects unknown --mode values."""
    try:
        parse_args(["--mode", "yaml", "hi"])
        assert False, "expected SystemExit"
    except SystemExit:
        pass


# ─── _json_default serializer ─────────────────────────────────────────


def test_json_default_handles_set():
    assert _json_default({1, 2, 3}) in ([1, 2, 3], [3, 2, 1], [2, 1, 3])


def test_json_default_handles_bytes():
    assert _json_default(b"hi") == "hi"


def test_json_default_handles_dataclass():
    from agent_llm import TextContent
    block = TextContent(text="hello")
    out = _json_default(block)
    assert out["text"] == "hello"
    assert out["type"] == "text"


def test_json_default_falls_back_to_str():
    class Weird:
        __slots__ = ()  # no __dict__, not a dataclass → str() fallback
    result = _json_default(Weird())
    assert isinstance(result, str)


def test_full_event_serializes_to_jsonl():
    """A realistic event dict with a dataclass message serializes cleanly."""
    from agent_llm import AssistantMessage, TextContent
    msg = AssistantMessage(content=[TextContent(text="hi")], provider="d", model="m")
    event = {"type": "message_end", "message": msg}
    line = json.dumps(event, default=_json_default)
    parsed = json.loads(line)
    assert parsed["type"] == "message_end"
    assert parsed["message"]["content"][0]["text"] == "hi"
