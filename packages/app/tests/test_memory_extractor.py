from __future__ import annotations

import asyncio
import json

import pytest
from agent_llm import AssistantMessage, Model, ModelCost, TextContent

from coding_agent.memory.extractor import (
    LLMMemoryExtractor,
    MemoryExtractionError,
    validate_extraction_payload,
)
from coding_agent.memory.types import CompletedTask, MemoryEvidence, MemoryIdentity


def _task(source_kind: str = "user") -> CompletedTask:
    return CompletedTask(
        identity=MemoryIdentity("local-user", "project-a"),
        session_id="session-1",
        evidence=[MemoryEvidence(
            id="entry-1",
            text="以后回答使用中文",
            source_kind=source_kind,  # type: ignore[arg-type]
            timestamp="2026-08-24T10:00:00Z",
        )],
    )


def _payload(**overrides):
    operation = {
        "operation": "upsert",
        "kind": "preference",
        "scope": "global",
        "key": "response.language",
        "value": "zh-CN",
        "summary": "回答使用中文",
        "confidence": 1.0,
        "sourceKind": "explicit_user",
        "evidenceEntryIds": ["entry-1"],
    }
    operation.update(overrides)
    return {"operations": [operation]}


def test_validates_explicit_user_memory() -> None:
    candidates = validate_extraction_payload(_payload(), _task())
    assert len(candidates) == 1
    assert candidates[0].key == "response.language"
    assert candidates[0].source_timestamp == "2026-08-24T10:00:00Z"


def test_rejects_unaccepted_plan_authority() -> None:
    with pytest.raises(MemoryExtractionError, match="unaccepted Plan"):
        validate_extraction_payload(_payload(sourceKind="accepted_plan"), _task())


def test_accepts_exact_accepted_plan_evidence() -> None:
    candidates = validate_extraction_payload(
        _payload(
            kind="decision",
            scope="project",
            key="architecture.memory_storage",
            value="files",
            summary="长期记忆使用文件存储",
            sourceKind="accepted_plan",
        ),
        _task("accepted_plan"),
    )
    assert candidates[0].source_kind == "accepted_plan"


def test_filters_secret_like_values_and_weak_inference() -> None:
    secret = validate_extraction_payload(
        _payload(key="credentials.api_key", value="sk-abcdefghijklmnop"),
        _task(),
    )
    weak = validate_extraction_payload(
        _payload(sourceKind="inferred_user", confidence=0.5),
        _task(),
    )
    auth_header = validate_extraction_payload(
        _payload(key="credentials.authorization", value="Authorization: Bearer abcdefghijklmnop"),
        _task(),
    )
    sensitive_key = validate_extraction_payload(
        _payload(key="personal.health", value="not provided"),
        _task(),
    )
    assert secret == []
    assert weak == []
    assert auth_header == []
    assert sensitive_key == []


def test_rejects_retraction_without_direct_user_authority() -> None:
    with pytest.raises(MemoryExtractionError, match="direct user authority"):
        validate_extraction_payload(
            _payload(operation="retract", sourceKind="inferred_user", confidence=0.95),
            _task(),
        )


def test_llm_extractor_rejects_text_wrapped_around_json() -> None:
    from coding_agent.memory.extractor import _parse_object

    with pytest.raises(MemoryExtractionError, match="invalid extractor JSON"):
        _parse_object('Here is the result: {"operations": []}')


def test_llm_extractor_uses_isolated_context_and_validates_final_json() -> None:
    calls: list[tuple[Model, object, dict]] = []

    class FakeStream:
        def __aiter__(self):
            async def events():
                if False:
                    yield None
            return events()

        async def result(self) -> AssistantMessage:
            return AssistantMessage(
                content=[TextContent(text=json.dumps(_payload(), ensure_ascii=False))],
                stop_reason="stop",
            )

    def stream_fn(model: Model, context: object, options: dict) -> FakeStream:
        calls.append((model, context, options))
        return FakeStream()

    model = Model(
        id="memory-extractor-test",
        provider="test",
        context_window=64_000,
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
    )
    extractor = LLMMemoryExtractor(
        get_model=lambda: model,
        get_stream_fn=lambda: stream_fn,
        get_api_key=lambda _provider: "test-key",
    )

    candidates = asyncio.run(extractor.extract(_task()))

    assert candidates[0].key == "response.language"
    assert calls[0][0] is model
    assert calls[0][2] == {"max_tokens": 1600, "api_key": "test-key"}
    context = calls[0][1]
    assert len(context.messages) == 1  # type: ignore[attr-defined]
    assert "以后回答使用中文" in context.messages[0].content  # type: ignore[attr-defined]
