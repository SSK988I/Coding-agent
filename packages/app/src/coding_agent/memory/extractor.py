"""Isolated LLM pass that extracts evidence-backed atomic memories."""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from agent_llm import Context, UserMessage

from coding_agent.memory.prompts import EXTRACTION_SYSTEM_PROMPT
from coding_agent.memory.types import (
    CompletedTask,
    ExtractedMemory,
    MemoryEvidence,
    contains_sensitive_data,
    contains_sensitive_key,
    validate_extracted_memory,
)

_KINDS = {"preference", "decision", "constraint", "convention", "fact", "lesson"}
_SCOPES = {"global", "project"}
_SOURCE_KINDS = {
    "explicit_correction", "explicit_user", "accepted_plan", "inferred_user",
}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class MemoryExtractionError(RuntimeError):
    pass


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"non-finite number: {constant}")


def _text_from_message(message: Any) -> str:
    chunks: list[str] = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            chunks.append(str(getattr(block, "text", "") or ""))
    return "".join(chunks)


def _parse_object(text: str, *, label: str = "extractor") -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        payload = json.loads(value, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise MemoryExtractionError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MemoryExtractionError(f"{label} JSON root must be an object")
    return payload


def _latest_timestamp(evidence: list[MemoryEvidence]) -> str:
    return max((item.timestamp for item in evidence), default="")


def validate_extraction_payload(
    payload: dict[str, Any],
    task: CompletedTask,
    *,
    max_candidates: int = 8,
) -> list[ExtractedMemory]:
    if set(payload) != {"memories"}:
        raise MemoryExtractionError("extractor root must contain only memories")
    memories = payload.get("memories")
    if not isinstance(memories, list):
        raise MemoryExtractionError("memories must be an array")
    if len(memories) > max_candidates:
        raise MemoryExtractionError(f"too many extracted memories: {len(memories)}")

    evidence_by_id = {item.id: item for item in task.evidence}
    required = {
        "kind", "scope", "content", "value", "relationKey", "confidence",
        "sourceKind", "evidenceEntryIds",
    }
    result: list[ExtractedMemory] = []
    for index, raw in enumerate(memories):
        if not isinstance(raw, dict) or set(raw) != required:
            raise MemoryExtractionError(f"memory {index} has invalid fields")
        kind = raw["kind"]
        scope = raw["scope"]
        source_kind = raw["sourceKind"]
        if kind not in _KINDS or scope not in _SCOPES or source_kind not in _SOURCE_KINDS:
            raise MemoryExtractionError(f"memory {index} has invalid enum value")
        if scope == "project" and not task.identity.project_id:
            raise MemoryExtractionError(f"memory {index} uses project scope without project_id")
        content = raw["content"]
        if not isinstance(content, str) or not content.strip() or len(content) > 300:
            raise MemoryExtractionError(f"memory {index} has invalid content")
        relation_key = raw["relationKey"]
        if relation_key is not None and (
            not isinstance(relation_key, str)
            or len(relation_key) > 120
            or not _KEY_RE.fullmatch(relation_key)
        ):
            raise MemoryExtractionError(f"memory {index} has invalid relation key")
        if relation_key is not None and contains_sensitive_key(relation_key):
            continue
        confidence = raw["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise MemoryExtractionError(f"memory {index} has invalid confidence")
        confidence = float(confidence)
        if not 0 <= confidence <= 1:
            raise MemoryExtractionError(f"memory {index} confidence is out of range")
        if source_kind == "inferred_user" and confidence < 0.85:
            continue
        evidence_ids = raw["evidenceEntryIds"]
        if (
            not isinstance(evidence_ids, list)
            or not 1 <= len(evidence_ids) <= 12
            or any(not isinstance(item, str) or item not in evidence_by_id for item in evidence_ids)
        ):
            raise MemoryExtractionError(f"memory {index} has invalid evidence IDs")
        selected = [evidence_by_id[item] for item in evidence_ids]
        if source_kind == "accepted_plan":
            if not any(item.source_kind in {"accepted_plan", "plan_completed"} for item in selected):
                raise MemoryExtractionError(f"memory {index} claims unaccepted Plan evidence")
        elif any(item.source_kind != "user" for item in selected):
            raise MemoryExtractionError(f"memory {index} must use user-authored evidence")
        value = raw["value"]
        try:
            serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise MemoryExtractionError(f"memory {index} value is not JSON serializable") from exc
        if len(serialized) > 2000:
            raise MemoryExtractionError(f"memory {index} value is too large")
        if contains_sensitive_data({"content": content, "value": value}):
            continue
        memory = ExtractedMemory(
            kind=kind,
            scope=scope,
            content=content.strip(),
            value=value,
            relation_key=relation_key,
            confidence=confidence,
            source_kind=source_kind,
            evidence_entry_ids=list(dict.fromkeys(evidence_ids)),
            source_timestamp=_latest_timestamp(selected) or task.ended_at,
        )
        try:
            validate_extracted_memory(memory)
        except ValueError as exc:
            raise MemoryExtractionError(f"memory {index} is invalid: {exc}") from exc
        result.append(memory)
    return result


class LLMMemoryExtractor:
    """Run memory extraction with an isolated context and configurable model."""

    def __init__(
        self,
        *,
        get_model: Callable[[], Any],
        get_stream_fn: Callable[[], Any],
        get_api_key: Callable[[str], Any] | None = None,
        get_reasoning: Callable[[], Any] | None = None,
        max_candidates: int = 8,
    ) -> None:
        self._get_model = get_model
        self._get_stream_fn = get_stream_fn
        self._get_api_key = get_api_key
        self._get_reasoning = get_reasoning
        self.max_candidates = max_candidates

    async def extract(self, task: CompletedTask) -> list[ExtractedMemory]:
        selected_evidence = (
            task.evidence if len(task.evidence) <= 12 else [task.evidence[0], *task.evidence[-11:]]
        )
        evidence: list[dict[str, Any]] = []
        remaining_chars = 24_000
        for item in selected_evidence:
            if remaining_chars <= 0:
                break
            text = item.text[:min(8_000, remaining_chars)]
            remaining_chars -= len(text)
            evidence.append({
                "id": item.id,
                "sourceKind": item.source_kind,
                "timestamp": item.timestamp,
                "text": text,
            })
        request = {
            "identity": {
                "userId": task.identity.user_id,
                "projectId": task.identity.project_id,
            },
            "mode": task.mode,
            "evidence": evidence,
            "finalAssistantResponseForUnderstandingOnly": task.final_response[:6000],
        }
        context = Context(
            system_prompt=EXTRACTION_SYSTEM_PROMPT,
            messages=[UserMessage(content=json.dumps(request, ensure_ascii=False))],
            tools=None,
        )
        model = self._get_model()
        options: dict[str, Any] = {"max_tokens": 1600}
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
            raise MemoryExtractionError(
                getattr(final, "error_message", None) or "memory extractor failed"
            )
        payload = _parse_object(_text_from_message(final))
        return validate_extraction_payload(payload, task, max_candidates=self.max_candidates)


__all__ = [
    "LLMMemoryExtractor", "MemoryExtractionError", "validate_extraction_payload",
]
