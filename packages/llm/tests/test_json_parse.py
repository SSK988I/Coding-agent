from __future__ import annotations

import pytest

from agent_llm.utils.json_parse import _close_trailing, parse_streaming_json


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, {}),
        ("", {}),
        (" \t\n", {}),
        ('{"count": 2}', {"count": 2}),
        (' {"count": 2}\n', {"count": 2}),
        ("[1, 2]", {"value": [1, 2]}),
        ('"text"', {"value": "text"}),
        ("12", {"value": 12}),
        ("true", {"value": True}),
        ("null", {"value": None}),
        ("{", {}),
        ('{"items": [1, {"nested": true', {"items": [1, {"nested": True}]}),
        ('{"count": 2,', {"count": 2}),
        ('{"items": [1, 2, ', {"items": [1, 2]}),
        ('{"count":', {}),
        ('{"count": 2, "next":', {}),
        ('{"text": "braces } [ are literal", "count": 2',
         {"text": "braces } [ are literal", "count": 2}),
        (r'{"text": "quote \" and slash \\ and brace {", "count": 2',
         {"text": 'quote " and slash \\ and brace {', "count": 2}),
        ('{"text": "unfinished', {}),
        ('{"text": "unfinished' + "\\", {}),
        ("[1, 2", {}),
        ('{"items": [1, 2}', {}),
        ("not JSON", {}),
        ('{"count": nope}', {}),
    ],
)
def test_parse_streaming_json_preserves_complete_and_partial_results(text, expected) -> None:
    assert parse_streaming_json(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"items": [1, {"nested": true', '{"items": [1, {"nested": true}]}'),
        ('{"count": 2, \t', '{"count": 2}'),
        ('{"count":', '{"count"}'),
        ('{"text": "braces } [ are literal"', '{"text": "braces } [ are literal"}'),
        (r'{"text": "quote \" and slash \\ and brace {"',
         r'{"text": "quote \" and slash \\ and brace {"}'),
        ('{"text": "unfinished', '{"text": "unfinished}'),
    ],
)
def test_close_trailing_preserves_original_characters(text: str, expected: str) -> None:
    assert _close_trailing(text) == expected


@pytest.mark.parametrize(
    ("source", "expected_prefixes"),
    [
        ('{"a":[1,2]}', [
            {}, {}, {}, {}, {}, {}, {"a": []}, {"a": [1]}, {"a": [1]},
            {"a": [1, 2]}, {"a": [1, 2]}, {"a": [1, 2]},
        ]),
        (r'{"s":"x\"y"}', [
            {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {"s": 'x"y'}, {"s": 'x"y'},
        ]),
    ],
)
def test_every_streaming_prefix_preserves_existing_repair_semantics(source: str, expected_prefixes: list[dict]) -> None:
    assert len(expected_prefixes) == len(source) + 1
    assert [parse_streaming_json(source[:end]) for end in range(len(source) + 1)] == expected_prefixes
