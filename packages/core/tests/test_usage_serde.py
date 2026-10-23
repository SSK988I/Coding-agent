from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.session.serde import dict_to_usage, usage_to_dict
from agent_core.session.session_manager import SessionManager
from agent_llm.types import AssistantMessage, Usage


@pytest.mark.parametrize("reported", [None, False, True])
@pytest.mark.parametrize("cached", [0, 40])
def test_cache_reporting_metadata_survives_json_roundtrip(reported: bool | None, cached: int) -> None:
    original = Usage(input=100 - cached, output=20, cache_read=cached,
                     total_tokens=120, cache_read_reported=reported)

    restored = dict_to_usage(json.loads(json.dumps(usage_to_dict(original))))

    assert restored == original
    assert restored.cache_read_reported is reported


@pytest.mark.parametrize("raw", [None, {}, {"input": 100}, {"input": 60, "cache_read": 40}])
def test_legacy_usage_retains_unknown_cache_reporting(raw) -> None:
    assert dict_to_usage(raw).cache_read_reported is None


@pytest.mark.parametrize("invalid", ["false", "true", 0, 1, {}, []])
def test_invalid_cache_reporting_metadata_is_not_coerced_to_measured(invalid) -> None:
    assert dict_to_usage({"input": 100, "cache_read_reported": invalid}).cache_read_reported is None


@pytest.mark.parametrize("reported", [None, False, True])
def test_cache_reporting_metadata_survives_jsonl_session_resume(tmp_path: Path, reported: bool | None) -> None:
    manager = SessionManager.create(cwd="/test/proj", agent_dir=tmp_path)
    manager.append_message(AssistantMessage(
        usage=Usage(input=100, output=20, total_tokens=120, cache_read_reported=reported),
    ))
    manager.flush()
    assert manager.path is not None

    restored = SessionManager.open(manager.path).build_session_context().messages[0]

    assert isinstance(restored, AssistantMessage)
    assert restored.usage.cache_read_reported is reported
    assert restored.usage.total_tokens == 120
