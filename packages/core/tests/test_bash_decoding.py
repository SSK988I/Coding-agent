from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent_core.tools.bash as bash_module


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("text", ["", "plain ASCII", "中文输出 😀"])
def test_valid_utf8_never_queries_the_windows_code_page(platform: str, text: str, monkeypatch) -> None:
    monkeypatch.setattr(bash_module, "sys", SimpleNamespace(platform=platform))

    def unexpected_oem_lookup():
        raise AssertionError("Valid UTF-8 output does not need an OEM encoding lookup")

    monkeypatch.setattr(bash_module, "_windows_oem_encoding", unexpected_oem_lookup)
    assert bash_module._decode_output(text.encode("utf-8")) == text


def test_invalid_utf8_falls_back_to_windows_oem(monkeypatch) -> None:
    monkeypatch.setattr(bash_module, "sys", SimpleNamespace(platform="win32"))
    lookups = []

    def oem_encoding():
        lookups.append("cp936")
        return "cp936"

    monkeypatch.setattr(bash_module, "_windows_oem_encoding", oem_encoding)
    assert bash_module._decode_output("中文输出".encode("cp936")) == "中文输出"
    assert lookups == ["cp936"]


@pytest.mark.parametrize("encoding", ["cp936", "", "unknown-output-encoding"])
def test_failed_oem_decoding_keeps_lossy_utf8_fallback(encoding: str, monkeypatch) -> None:
    monkeypatch.setattr(bash_module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(bash_module, "_windows_oem_encoding", lambda: encoding)
    data = b"prefix\xff\xfe\x80suffix"
    assert bash_module._decode_output(data) == data.decode("utf-8", errors="replace")


def test_non_windows_invalid_utf8_does_not_query_an_oem_encoding(monkeypatch) -> None:
    monkeypatch.setattr(bash_module, "sys", SimpleNamespace(platform="linux"))

    def unexpected_oem_lookup():
        raise AssertionError("Non-Windows output does not use an OEM encoding")

    monkeypatch.setattr(bash_module, "_windows_oem_encoding", unexpected_oem_lookup)
    data = b"prefix\xffsuffix"
    assert bash_module._decode_output(data) == data.decode("utf-8", errors="replace")
