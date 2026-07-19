"""Tests for the autocomplete provider."""
from __future__ import annotations

from agent_tui.autocomplete import (
    AutocompleteItem,
    CombinedAutocompleteProvider,
)
from agent_tui.components.select_list import SelectItem  # noqa: F401  (ensure exports ok)


class _FakeCmd:
    def __init__(self, name, description="", getter=None):
        self.name = name
        self.description = description
        self.get_argument_completions = getter


def _provider(*cmds):
    return CombinedAutocompleteProvider(list(cmds))


# ── get_suggestions: command-name branch ──────────────────────────────


def test_no_suggestion_when_not_slash():
    p = _provider(_FakeCmd("model"))
    assert p.get_suggestions(["hello"], 0, 5) is None


def test_no_suggestion_on_non_first_line():
    p = _provider(_FakeCmd("model"))
    assert p.get_suggestions(["x", "/mo"], 1, 3) is None


def test_slash_alone_lists_all_commands():
    p = _provider(_FakeCmd("model"), _FakeCmd("thinking"), _FakeCmd("help"))
    s = p.get_suggestions(["/"], 0, 1)
    assert s is not None
    assert {i.value for i in s.items} == {"/model", "/thinking", "/help"}


def test_prefix_filter_case_insensitive():
    p = _provider(_FakeCmd("model"), _FakeCmd("thinking"), _FakeCmd("help"))
    s = p.get_suggestions(["/TH"], 0, 3)
    assert s is not None
    assert [i.value for i in s.items] == ["/thinking"]


def test_no_match_returns_none():
    p = _provider(_FakeCmd("model"))
    assert p.get_suggestions(["/zzz"], 0, 4) is None


def test_prefix_includes_leading_whitespace():
    p = _provider(_FakeCmd("model"))
    s = p.get_suggestions(["   /m"], 0, 5)
    assert s is not None
    assert [i.value for i in s.items] == ["/model"]


# ── get_suggestions: argument branch ──────────────────────────────────


def test_argument_suggestion_calls_command_getter():
    def getter(prefix):
        return [AutocompleteItem(value="claude-3", label="claude-3-opus", description="anthropic")]

    p = _provider(_FakeCmd("model", getter=getter))
    s = p.get_suggestions(["/model "], 0, 7)
    assert s is not None
    assert [i.value for i in s.items] == ["claude-3"]
    assert s.prefix == ""


def test_argument_suggestion_filters_via_getter_prefix():
    def getter(prefix):
        all_models = ["claude-3", "gpt-4", "claude-sonnet"]
        return [AutocompleteItem(value=m, label=m) for m in all_models if m.startswith(prefix)]

    p = _provider(_FakeCmd("model", getter=getter))
    s = p.get_suggestions(["/model claud"], 0, 13)
    assert s is not None
    assert {i.value for i in s.items} == {"claude-3", "claude-sonnet"}


def test_argument_no_getter_returns_none():
    p = _provider(_FakeCmd("help"))
    assert p.get_suggestions(["/help "], 0, 6) is None


def test_argument_getter_returns_none_propagates():
    def getter(prefix):
        return None

    p = _provider(_FakeCmd("model", getter=getter))
    assert p.get_suggestions(["/model zzz"], 0, 10) is None


def test_argument_unknown_command_returns_none():
    p = _provider(_FakeCmd("model"))
    assert p.get_suggestions(["/nope x"], 0, 7) is None


# ── apply_completion ──────────────────────────────────────────────────


def test_apply_command_name_replaces_with_trailing_space():
    p = _provider(_FakeCmd("model"))
    item = AutocompleteItem(value="/model", label="model")
    lines, li, col = p.apply_completion(["/mo"], 0, 3, item)
    assert lines == ["/model "]
    assert col == len("/model ")
    assert li == 0


def test_apply_command_name_preserves_trailing_text():
    p = _provider(_FakeCmd("model"))
    item = AutocompleteItem(value="/model", label="model")
    lines, li, col = p.apply_completion(["/mo tail"], 0, 3, item)
    assert lines == ["/model  tail"]
    assert col == len("/model ")


def test_apply_argument_replaces_arg_prefix():
    p = _provider(_FakeCmd("model"))
    item = AutocompleteItem(value="claude-3-opus", label="claude-3-opus")
    lines, li, col = p.apply_completion(["/model claud"], 0, 13, item)
    assert lines == ["/model claude-3-opus"]
    assert col == len("/model claude-3-opus")


def test_apply_argument_with_empty_prefix():
    p = _provider(_FakeCmd("model"))
    item = AutocompleteItem(value="gpt-4", label="gpt-4")
    lines, li, col = p.apply_completion(["/model "], 0, 7, item)
    assert lines == ["/model gpt-4"]
    assert col == len("/model gpt-4")


def test_apply_argument_preserves_trailing_text():
    p = _provider(_FakeCmd("model"))
    item = AutocompleteItem(value="gpt-4", label="gpt-4")
    # Cursor right after "claud" (col 13 lands on the space before "rest",
    # which is part of the replaced arg-prefix range). Place cursor at 12 so
    # the trailing " rest" is preserved past the replacement range.
    lines, li, col = p.apply_completion(["/model claud rest"], 0, 12, item)
    assert lines == ["/model gpt-4 rest"]
    assert col == len("/model gpt-4")


def test_apply_with_leading_whitespace():
    p = _provider(_FakeCmd("model"))
    item = AutocompleteItem(value="/model", label="model")
    lines, li, col = p.apply_completion(["  /mo"], 0, 5, item)
    assert lines == ["  /model "]
    assert col == len("  /model ")
