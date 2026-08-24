"""LLM-backed extraction with strict application-side validation."""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from agent_llm import Context, UserMessage

from coding_agent.memory.prompts import EXTRACTION_SYSTEM_PROMPT
from coding_agent.memory.types import (
    CompletedTask,
    MemoryCandidate,
    MemoryEvidence,
    contains_sensitive_data,
    contains_sensitive_key,
)

_KINDS = {"preference", "decision", "constraint", "convention"}
_SCOPES = {"global", "project"}
_OPERATIONS = {"upsert", "retract", "noop"}
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


def _parse_object(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        payload = json.loads(
            value,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise MemoryExtractionError(f"invalid extractor JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MemoryExtractionError("extractor JSON root must be an object")
    return payload


def _latest_timestamp(evidence: list[MemoryEvidence]) -> str:
    return max((item.timestamp for item in evidence), default="")


def validate_extraction_payload(
    payload: dict[str, Any],
    task: CompletedTask,
    *,
    max_candidates: int = 8,
) -> list[MemoryCandidate]:
    if set(payload) != {"operations"}:
        raise MemoryExtractionError("extractor root must contain only operations")
    operations = payload.get("operations")
    if not isinstance(operations, list):
        raise MemoryExtractionError("operations must be an array")
    if len(operations) > max_candidates:
        raise MemoryExtractionError(f"too many memory candidates: {len(operations)}")

    evidence_by_id = {item.id: item for item in task.evidence}
    result: list[MemoryCandidate] = []
    required = {
        "operation", "kind", "scope", "key", "value", "summary",
        "confidence", "sourceKind", "evidenceEntryIds",
    }
    for index, raw in enumerate(operations):
        if not isinstance(raw, dict) or set(raw) != required:
            raise MemoryExtractionError(f"operation {index} has invalid fields")
        operation = raw["operation"]
        if operation not in _OPERATIONS:
            raise MemoryExtractionError(f"operation {index} has invalid operation")
        if operation == "noop":
            continue
        kind = raw["kind"]
        scope = raw["scope"]
        source_kind = raw["sourceKind"]
        if kind not in _KINDS or scope not in _SCOPES or source_kind not in _SOURCE_KINDS:
            raise MemoryExtractionError(f"operation {index} has invalid enum value")
        if operation == "retract" and source_kind not in {"explicit_correction", "explicit_user"}:
            raise MemoryExtractionError(
                f"operation {index} cannot retract memory without direct user authority"
            )
        if scope == "project" and not task.identity.project_id:
            raise MemoryExtractionError(f"operation {index} uses project scope without project_id")
        key = raw["key"]
        summary = raw["summary"]
        if not isinstance(key, str) or len(key) > 120 or not _KEY_RE.fullmatch(key):
            raise MemoryExtractionError(f"operation {index} has invalid canonical key")
        if contains_sensitive_key(key):
            continue
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 300:
            raise MemoryExtractionError(f"operation {index} has invalid summary")
        confidence = raw["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise MemoryExtractionError(f"operation {index} has invalid confidence")
        confidence = float(confidence)
        if not 0 <= confidence <= 1:
            raise MemoryExtractionError(f"operation {index} confidence is out of range")
        if source_kind == "inferred_user" and confidence < 0.85:
            continue
        evidence_ids = raw["evidenceEntryIds"]
        if (
            not isinstance(evidence_ids, list)
            or not 1 <= len(evidence_ids) <= 12
            or any(not isinstance(item, str) or item not in evidence_by_id for item in evidence_ids)
        ):
            raise MemoryExtractionError(f"operation {index} has invalid evidence IDs")
        selected = [evidence_by_id[item] for item in evidence_ids]
        if source_kind == "accepted_plan":
            if not any(item.source_kind in {"accepted_plan", "plan_completed"} for item in selected):
                raise MemoryExtractionError(f"operation {index} claims unaccepted Plan evidence")
        elif any(item.source_kind != "user" for item in selected):
            raise MemoryExtractionError(f"operation {index} must use user-authored evidence")
        value = raw["value"]
        try:
            serialized = json.dumps(
                value, ensure_ascii=False, sort_keys=True, allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise MemoryExtractionError(f"operation {index} value is not JSON serializable") from exc
        if len(serialized) > 2000:
            raise MemoryExtractionError(f"operation {index} value is too large")
        if contains_sensitive_data({"value": value, "summary": summary}):
            continue
        result.append(MemoryCandidate(
            operation=operation,
            kind=kind,
            scope=scope,
            key=key,
            value=value,
            summary=summary.strip(),
            confidence=confidence,
            source_kind=source_kind,
            evidence_entry_ids=list(dict.fromkeys(evidence_ids)),
            source_timestamp=_latest_timestamp(selected) or task.ended_at,
        ))
    return result


class LLMMemoryExtractor:
    """Use the session's current model and provider stream for extraction."""

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

    async def extract(self, task: CompletedTask) -> list[MemoryCandidate]:
        selected_evidence = (
            task.evidence
            if len(task.evidence) <= 12
            else [task.evidence[0], *task.evidence[-11:]]
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
    "LLMMemoryExtractor",
    "MemoryExtractionError",
    "validate_extraction_payload",
]
