"""Semantic consolidation between extracted statements and durable records."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from agent_llm import Context, UserMessage

from coding_agent.memory.extractor import _parse_object, _text_from_message
from coding_agent.memory.prompts import CONSOLIDATION_SYSTEM_PROMPT
from coding_agent.memory.types import (
    CompletedTask,
    ConsolidationDecision,
    ExtractedMemory,
    MemoryRecord,
    MemoryScope,
)


class MemoryConsolidationError(RuntimeError):
    pass


def memory_fingerprint(memory: ExtractedMemory) -> str:
    payload = {
        "kind": memory.kind,
        "scope": memory.scope,
        "content": " ".join(memory.content.casefold().split()),
        "value": memory.value,
        "relation_key": memory.relation_key,
    }
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_consolidation_payload(
    payload: dict[str, Any],
    extracted: list[ExtractedMemory],
    existing: list[tuple[MemoryScope, MemoryRecord]],
) -> list[ConsolidationDecision]:
    if set(payload) != {"decisions"}:
        raise MemoryConsolidationError("consolidator root must contain only decisions")
    raw_decisions = payload.get("decisions")
    if not isinstance(raw_decisions, list):
        raise MemoryConsolidationError("decisions must be an array")
    if len(raw_decisions) != len(extracted):
        raise MemoryConsolidationError("consolidator must decide every candidate exactly once")
    target_by_id = {record.id: (scope, record) for scope, record in existing}
    result: list[ConsolidationDecision] = []
    seen: set[int] = set()
    required = {"candidateIndex", "action", "targetRecordId", "reason"}
    targeted_actions = {"reinforce", "supersede", "conflict"}
    for index, raw in enumerate(raw_decisions):
        if not isinstance(raw, dict) or set(raw) != required:
            raise MemoryConsolidationError(f"decision {index} has invalid fields")
        candidate_index = raw["candidateIndex"]
        action = raw["action"]
        target_id = raw["targetRecordId"]
        reason = raw["reason"]
        if (
            isinstance(candidate_index, bool)
            or not isinstance(candidate_index, int)
            or not 0 <= candidate_index < len(extracted)
            or candidate_index in seen
        ):
            raise MemoryConsolidationError(f"decision {index} has invalid candidate index")
        if action not in {
            "add", "reinforce", "supersede", "conflict", "ignore",
        }:
            raise MemoryConsolidationError(f"decision {index} has invalid action")
        if not isinstance(reason, str) or len(reason) > 300:
            raise MemoryConsolidationError(f"decision {index} has invalid reason")
        if action in targeted_actions:
            if not isinstance(target_id, str) or target_id not in target_by_id:
                raise MemoryConsolidationError(f"decision {index} has invalid target")
            target_scope, target = target_by_id[target_id]
            candidate = extracted[candidate_index]
            if target_scope != candidate.scope:
                raise MemoryConsolidationError(f"decision {index} targets another scope")
            target_relation = target.relation_key or target.key
            if target.kind != candidate.kind:
                raise MemoryConsolidationError(
                    f"decision {index} cannot target an unrelated record"
                )
            if action == "reinforce":
                same_relation = (
                    candidate.relation_key is not None
                    and candidate.relation_key == target_relation
                    and candidate.value == target.value
                )
                same_atomic_memory = (
                    candidate.relation_key is None
                    and (
                        target.fingerprint == memory_fingerprint(candidate)
                        or (
                            target.value == candidate.value
                            and target.summary.casefold() == candidate.content.casefold()
                        )
                    )
                )
                if not same_relation and not same_atomic_memory:
                    raise MemoryConsolidationError(
                        f"decision {index} cannot reinforce an unrelated record"
                    )
            if action in {"supersede", "conflict"} and (
                candidate.relation_key is None or candidate.relation_key != target_relation
            ):
                raise MemoryConsolidationError(
                    f"decision {index} cannot replace an unrelated record"
                )
        elif target_id is not None:
            raise MemoryConsolidationError(f"decision {index} must not include a target")
        seen.add(candidate_index)
        result.append(ConsolidationDecision(
            candidate_index=candidate_index,
            action=action,
            target_record_id=target_id,
            reason=reason,
        ))
    result.sort(key=lambda item: item.candidate_index)
    return result


class DeterministicMemoryConsolidator:
    """Conservative fallback useful for offline hosts and deterministic tests."""

    async def consolidate(
        self,
        task: CompletedTask,
        extracted: list[ExtractedMemory],
        existing: list[tuple[MemoryScope, MemoryRecord]],
    ) -> list[ConsolidationDecision]:
        del task
        decisions: list[ConsolidationDecision] = []
        for index, memory in enumerate(extracted):
            scoped = [record for scope, record in existing if scope == memory.scope]
            fingerprint = memory_fingerprint(memory)
            exact = next((item for item in scoped if item.fingerprint == fingerprint), None)
            if exact is None:
                exact = next((
                    item for item in scoped
                    if item.kind == memory.kind
                    and item.summary.casefold() == memory.content.casefold()
                    and item.value == memory.value
                ), None)
            if exact is not None:
                decisions.append(ConsolidationDecision(index, "reinforce", exact.id, "exact"))
                continue
            relation_target = None
            if memory.relation_key is not None:
                relation_target = next((
                    item for item in scoped
                    if (item.relation_key or item.key) == memory.relation_key
                ), None)
            if relation_target is None:
                decisions.append(ConsolidationDecision(index, "add", reason="new"))
            elif memory.source_kind == "explicit_correction":
                decisions.append(ConsolidationDecision(
                    index, "supersede", relation_target.id, "explicit_correction",
                ))
            else:
                decisions.append(ConsolidationDecision(
                    index, "conflict", relation_target.id, "different_value",
                ))
        return decisions


class LLMMemoryConsolidator:
    """Run a second isolated model pass over candidates and existing records."""

    def __init__(
        self,
        *,
        get_model: Callable[[], Any],
        get_stream_fn: Callable[[], Any],
        get_api_key: Callable[[str], Any] | None = None,
        get_reasoning: Callable[[], Any] | None = None,
    ) -> None:
        self._get_model = get_model
        self._get_stream_fn = get_stream_fn
        self._get_api_key = get_api_key
        self._get_reasoning = get_reasoning

    async def consolidate(
        self,
        task: CompletedTask,
        extracted: list[ExtractedMemory],
        existing: list[tuple[MemoryScope, MemoryRecord]],
    ) -> list[ConsolidationDecision]:
        request = {
            "identity": task.identity.to_dict(),
            "candidates": [
                {"candidateIndex": index, **memory.to_dict()}
                for index, memory in enumerate(extracted)
            ],
            "existing": [
                {
                    "scope": scope,
                    "recordId": record.id,
                    "kind": record.kind,
                    "storageKey": record.key,
                    "relationKey": record.relation_key or record.key,
                    "content": record.summary,
                    "value": record.value,
                    "authority": record.authority,
                    "confidence": record.confidence,
                }
                for scope, record in existing
            ],
        }
        context = Context(
            system_prompt=CONSOLIDATION_SYSTEM_PROMPT,
            messages=[UserMessage(content=json.dumps(request, ensure_ascii=False))],
            tools=None,
        )
        model = self._get_model()
        options: dict[str, Any] = {"max_tokens": 1200}
        if self._get_api_key is not None:
            key = self._get_api_key(getattr(model, "provider", ""))
            if hasattr(key, "__await__"):
                key = await key
            if key:
                options["api_key"] = key
        if self._get_reasoning is not None:
            reasoning = self._get_reasoning()
            if reasoning:
                options["reasoning"] = reasoning
        stream = self._get_stream_fn()(model, context, options)
        async for _ in stream:
            pass
        final = await stream.result()
        if getattr(final, "stop_reason", "stop") in {"error", "aborted"}:
            raise MemoryConsolidationError(
                getattr(final, "error_message", None) or "memory consolidator failed"
            )
        try:
            payload = _parse_object(_text_from_message(final), label="consolidator")
        except Exception as exc:
            raise MemoryConsolidationError(str(exc)) from exc
        return validate_consolidation_payload(payload, extracted, existing)


__all__ = [
    "DeterministicMemoryConsolidator",
    "LLMMemoryConsolidator",
    "MemoryConsolidationError",
    "memory_fingerprint",
    "validate_consolidation_payload",
]
