"""Plan Episode policy, immutable revisions, controls, and recovery reducer.

This module is the single application-layer seam for Plan Mode. Frontends
render PlanState; they never infer lifecycle transitions.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Literal, cast

from agent_core import AgentToolResult, BeforeToolCallContext, BeforeToolCallResult, PlanAccess
from agent_core.session.types import (
    CollaborationModeChangeEntry,
    PlanQuestionAnswerEntry,
    PlanQuestionEntry,
    PlanRevisionEntry,
    PlanRunEntry,
    SessionEntry,
    SessionMessageEntry,
)
from agent_llm import TextContent

CollaborationMode = Literal["default", "plan"]
PlanPhase = Literal[
    "idle",
    "drafting",
    "awaiting_answer",
    "ready",
    "executing",
    "settled",
    "failed",
    "aborted",
    "cancelled",
    "uncertain",
    "recovery_error",
]
QuestionBehavior = Literal["interactive", "deferred"]

PLAN_REVISION_SCHEMA_VERSION = 1
MAX_PLAN_TITLE_CHARS = 200
MAX_PLAN_MARKDOWN_BYTES = 64 * 1024


class PlanModeError(RuntimeError):
    """Stable error-code exception shared by CLI and desktop adapters."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PlanQuestionOption:
    label: str
    description: str


@dataclass(frozen=True)
class PlanQuestion:
    question_id: str
    header: str
    question: str
    options: tuple[PlanQuestionOption, ...]
    allow_custom: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "questionId": self.question_id,
            "header": self.header,
            "question": self.question,
            "options": [asdict(option) for option in self.options],
            "allowCustom": self.allow_custom,
        }


@dataclass(frozen=True)
class PlanRevision:
    plan_id: str
    revision: int
    title: str
    markdown: str
    digest: str
    source_message_id: str
    schema_version: int = PLAN_REVISION_SCHEMA_VERSION
    submitted_by_tool_call_id: str = ""
    origin_session_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "planId": self.plan_id,
            "revision": self.revision,
            "title": self.title,
            "markdown": self.markdown,
            "digest": self.digest,
            "sourceMessageId": self.source_message_id,
            "schemaVersion": self.schema_version,
            "submittedByToolCallId": self.submitted_by_tool_call_id,
            "originSessionId": self.origin_session_id,
        }


@dataclass(frozen=True)
class PlanRun:
    plan_id: str
    revision: int
    digest: str
    status: Literal["started", "completed", "failed", "aborted"]
    run_id: str | None
    error: str | None
    assistant_message_id: str | None
    stop_reason: str | None
    entry_id: str
    timestamp: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "planId": self.plan_id,
            "revision": self.revision,
            "digest": self.digest,
            "status": self.status,
            "runId": self.run_id,
            "error": self.error,
            "assistantMessageId": self.assistant_message_id,
            "stopReason": self.stop_reason,
            "entryId": self.entry_id,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class PlanRecoveryError:
    code: str
    message: str
    entry_id: str

    def to_payload(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "entryId": self.entry_id,
        }


@dataclass
class PlanState:
    mode: CollaborationMode = "default"
    phase: PlanPhase = "idle"
    active_plan_id: str | None = None
    latest_revision: PlanRevision | None = None
    pending_question: PlanQuestion | None = None
    latest_run: PlanRun | None = None
    recovery_error: PlanRecoveryError | None = None
    handoff_target_session_id: str | None = None
    legacy_candidate: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "phase": self.phase,
            "activePlanId": self.active_plan_id,
            "latestRevision": (
                self.latest_revision.to_payload() if self.latest_revision else None
            ),
            "pendingQuestion": (
                self.pending_question.to_payload() if self.pending_question else None
            ),
            "latestRun": self.latest_run.to_payload() if self.latest_run else None,
            "recoveryError": (
                self.recovery_error.to_payload() if self.recovery_error else None
            ),
            "handoffTargetSessionId": self.handoff_target_session_id,
            "legacyCandidate": self.legacy_candidate,
        }


PLAN_MODE_OVERLAY = """
<collaboration_mode_policy mode="plan">
You are in Plan Mode. This policy has higher priority than custom prompts,
project context, and skill instructions.

- Inspect the repository before asking questions. Never ask for facts that can
  be discovered safely with the available observation tools.
- Only tools explicitly classified as Plan observation or control tools are
  available. Shell, write, edit, tests, builds, package managers, and unknown
  tools are forbidden until the user confirms execution.
- Ask at most one structured question at a time with request_user_input. Give
  2-3 mutually exclusive options, put the recommended option first, and explain
  each option's impact. The UI automatically offers a custom answer.
- Do not submit a plan until interfaces, transitions, failures, compatibility,
  and tests are decision complete.
- Submit the final plan with submit_plan(title, markdown). submit_plan must be
  the only tool call in that assistant message. Do not wrap the Markdown in
  <proposed_plan> tags; the host stores the exact submitted Markdown.
- A request to implement or natural-language approval does not change modes.
  Only the host may execute or cancel the exact latest Plan Revision.
</collaboration_mode_policy>
""".strip()


def normalize_plan_markdown(markdown: str) -> str:
    """Normalize line endings only; never rewrite plan prose."""
    return markdown.replace("\r\n", "\n")


def compute_plan_digest(
    *, plan_id: str, revision: int, title: str, markdown: str,
    schema_version: int = PLAN_REVISION_SCHEMA_VERSION,
) -> str:
    """Return the versioned authorization digest for a Plan Revision."""
    if schema_version == 0:
        return hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    if schema_version != PLAN_REVISION_SCHEMA_VERSION:
        raise PlanModeError(
            "UNSUPPORTED_PLAN_SCHEMA",
            f"不支持 Plan revision schema v{schema_version}",
        )
    encoded = json.dumps(
        {
            "schemaVersion": schema_version,
            "planId": plan_id,
            "revision": revision,
            "title": title,
            "markdown": markdown,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_plan_revision(
    *, plan_id: str, revision: int, title: str, markdown: str,
    source_message_id: str, submitted_by_tool_call_id: str,
    origin_session_id: str | None = None,
) -> PlanRevision:
    """Validate exact submit_plan arguments and construct schema-v1 revision."""
    if not plan_id or revision < 1:
        raise PlanModeError("INVALID_PLAN_SPEC", "planId 和 revision 无效")
    if not title.strip() or "\n" in title or "\r" in title:
        raise PlanModeError("INVALID_PLAN_SPEC", "title 必须是非空单行文本")
    if len(title) > MAX_PLAN_TITLE_CHARS:
        raise PlanModeError("INVALID_PLAN_SPEC", "title 不能超过 200 个字符")
    if any(ord(char) < 32 for char in title):
        raise PlanModeError("INVALID_PLAN_SPEC", "title 包含不允许的控制字符")
    normalized = normalize_plan_markdown(markdown)
    if not normalized.strip():
        raise PlanModeError("INVALID_PLAN_SPEC", "markdown 不能为空")
    if len(normalized.encode("utf-8")) > MAX_PLAN_MARKDOWN_BYTES:
        raise PlanModeError("INVALID_PLAN_SPEC", "markdown 不能超过 64 KiB")
    if any(ord(char) < 32 and char not in {"\n", "\r", "\t"} for char in normalized):
        raise PlanModeError("INVALID_PLAN_SPEC", "markdown 包含不允许的控制字符")
    digest = compute_plan_digest(
        plan_id=plan_id,
        revision=revision,
        title=title,
        markdown=normalized,
    )
    return PlanRevision(
        plan_id=plan_id,
        revision=revision,
        title=title,
        markdown=normalized,
        digest=digest,
        source_message_id=source_message_id,
        submitted_by_tool_call_id=submitted_by_tool_call_id,
        origin_session_id=origin_session_id,
    )


_PLAN_BLOCK_RE = re.compile(r"\A<proposed_plan>\s*\n([\s\S]*?)\n</proposed_plan>\s*\Z")


def validate_proposed_plan(
    text: str, *, plan_id: str, revision: int, source_message_id: str,
) -> PlanRevision:
    """Compatibility validator for callers holding a legacy plan envelope.

    Live Plan Mode never calls this function; only submit_plan can persist a
    new revision.
    """
    normalized = normalize_plan_markdown(text)
    match = _PLAN_BLOCK_RE.fullmatch(normalized)
    if match is None:
        raise PlanModeError(
            "INVALID_PLAN_SPEC", "回复必须只包含一个 <proposed_plan> 块",
        )
    markdown = match.group(1)
    h1 = re.findall(r"(?m)^#\s+(.+?)\s*$", markdown)
    if len(h1) != 1:
        raise PlanModeError("INVALID_PLAN_SPEC", "legacy 计划必须包含一个一级标题")
    return create_plan_revision(
        plan_id=plan_id,
        revision=revision,
        title=h1[0].strip(),
        markdown=markdown,
        source_message_id=source_message_id,
        submitted_by_tool_call_id="legacy-validator",
    )


def prepare_plan_reply(text: str) -> str | None:
    """Detect only an existing legacy envelope; never synthesize one."""
    normalized = normalize_plan_markdown(text)
    return normalized if _PLAN_BLOCK_RE.fullmatch(normalized) else None


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return "\n".join(
        str(getattr(block, "text", ""))
        for block in content or []
        if getattr(block, "type", None) == "text"
    )


def _recovery(
    state: PlanState, entry: SessionEntry, code: str, message: str,
) -> None:
    state.mode = "plan"
    state.phase = "recovery_error"
    state.pending_question = None
    state.recovery_error = PlanRecoveryError(code, message, entry.id)


def _revision_from_entry(entry: PlanRevisionEntry) -> PlanRevision:
    return PlanRevision(
        plan_id=entry.plan_id,
        revision=entry.revision,
        title=entry.title,
        markdown=entry.markdown,
        digest=entry.digest,
        source_message_id=entry.source_message_id,
        schema_version=entry.schema_version,
        submitted_by_tool_call_id=entry.submitted_by_tool_call_id,
        origin_session_id=entry.origin_session_id,
    )


def _run_from_entry(entry: PlanRunEntry) -> PlanRun:
    return PlanRun(
        plan_id=entry.plan_id,
        revision=entry.revision,
        digest=entry.digest,
        status=entry.status,
        run_id=entry.run_id,
        error=entry.error,
        assistant_message_id=entry.assistant_message_id,
        stop_reason=entry.stop_reason,
        entry_id=entry.id,
        timestamp=entry.timestamp,
    )


@dataclass(frozen=True)
class _ReplayIssue:
    line: int
    code: str
    message: str


def _replay_items(
    entries: list[SessionEntry],
    load_issues: list[dict[str, Any]] | None,
) -> list[SessionEntry | _ReplayIssue]:
    """Merge storage diagnostics back into their original JSONL order."""
    issues = sorted(
        (
            _ReplayIssue(
                line=max(1, int(raw.get("line", 1))),
                code=str(raw.get("code") or "CORRUPT_SESSION_ENTRY"),
                message=str(raw.get("message") or "Session JSONL entry is corrupt"),
            )
            for raw in (load_issues or [])
            if isinstance(raw, dict)
        ),
        key=lambda issue: issue.line,
    )
    merged: list[SessionEntry | _ReplayIssue] = []
    issue_index = 0
    for entry in entries:
        source_line = getattr(entry, "_source_line", None)
        if source_line is None:
            merged.extend(issues[issue_index:])
            issue_index = len(issues)
        else:
            while issue_index < len(issues) and issues[issue_index].line <= source_line:
                merged.append(issues[issue_index])
                issue_index += 1
        merged.append(entry)
    merged.extend(issues[issue_index:])
    return merged


def _valid_replay_token(value: Any, *, max_chars: int = 200) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= max_chars
        and not any(ord(char) < 32 for char in value)
    )


def reduce_plan_state(
    entries: list[SessionEntry], *, live_run_id: str | None = None,
    parent_session_id: str | None = None,
    load_issues: list[dict[str, Any]] | None = None,
) -> PlanState:
    """Validate and fold Plan state from one active JSONL branch.

    A persisted started entry is executing only while this Runtime owns its
    run id. On resume it is uncertain and never retried automatically.
    """
    state = PlanState()
    entry_ids = [entry.id for entry in entries]
    if len(entry_ids) != len(set(entry_ids)):
        state.mode = "plan"
        state.phase = "recovery_error"
        state.recovery_error = PlanRecoveryError(
            "DUPLICATE_ENTRY_ID",
            "Session JSONL 包含重复 entry ID",
            "session-log",
        )
        return state
    messages_by_id: dict[str, SessionMessageEntry] = {}
    submitted_tool_call_ids: set[str] = set()
    seen_plan_ids: set[str] = set()
    seen_run_ids: set[str] = set()
    seen_question_ids: set[tuple[str, str]] = set()
    handoff_origin_session_id: str | None = None
    previous_by_id: dict[str, SessionEntry | None] = {
        entry.id: entries[index - 1] if index else None
        for index, entry in enumerate(entries)
    }
    for replay_item in _replay_items(entries, load_issues):
        if isinstance(replay_item, _ReplayIssue):
            state.mode = "plan"
            state.phase = "recovery_error"
            state.pending_question = None
            state.recovery_error = PlanRecoveryError(
                replay_item.code,
                f"Session JSONL 第 {replay_item.line} 行损坏：{replay_item.message}",
                f"line:{replay_item.line}",
            )
            continue
        entry = replay_item
        if state.recovery_error is not None:
            if (
                isinstance(entry, CollaborationModeChangeEntry)
                and entry.mode == "default"
                and entry.reason in {None, "user"}
                and (
                    (state.active_plan_id is None and entry.plan_id is None)
                    or (
                        state.active_plan_id is not None
                        and entry.plan_id == state.active_plan_id
                    )
                )
            ):
                state.mode = "default"
                state.phase = "cancelled" if state.active_plan_id else "idle"
                state.pending_question = None
                state.recovery_error = None
            continue

        if isinstance(entry, SessionMessageEntry):
            messages_by_id[entry.id] = entry
            role = getattr(entry.message, "role", None)
            if state.mode == "plan" and state.phase == "ready" and role == "user":
                state.phase = "drafting"
                state.legacy_candidate = False
            elif state.mode == "plan" and state.phase == "drafting":
                if role == "user":
                    state.legacy_candidate = False
                elif role == "assistant":
                    text = _message_text(entry.message)
                    state.legacy_candidate = (
                        text.count("<proposed_plan>") == 1
                        and text.count("</proposed_plan>") == 1
                    )
            continue

        if isinstance(entry, CollaborationModeChangeEntry):
            if entry.reason not in {None, "user", "handoff"}:
                _recovery(
                    state, entry, "INVALID_MODE_TRANSITION",
                    "Collaboration mode change reason 无效",
                )
                continue
            if entry.mode == "plan":
                if not _valid_replay_token(entry.plan_id):
                    _recovery(
                        state, entry, "INVALID_PLAN_ID",
                        "Plan entry 缺少 planId",
                    )
                    continue
                if entry.plan_id in seen_plan_ids:
                    _recovery(
                        state, entry, "DUPLICATE_PLAN_ID",
                        "同一 session 不能复用 planId",
                    )
                    continue
                if state.phase == "executing":
                    _recovery(
                        state, entry, "INVALID_MODE_TRANSITION",
                        "Plan run 执行中不能开始新 Episode",
                    )
                    continue
                if state.mode == "plan":
                    _recovery(
                        state, entry, "INVALID_MODE_TRANSITION",
                        "活动 Plan Episode 中不能直接开始另一个 Plan",
                    )
                    continue
                if entry.reason == "handoff":
                    if (
                        not _valid_replay_token(entry.related_session_id)
                        or entry.related_session_id != parent_session_id
                    ):
                        _recovery(
                            state, entry, "INVALID_HANDOFF",
                            "Plan handoff 来源与 parentSession 不匹配",
                        )
                        continue
                    handoff_origin_session_id = entry.related_session_id
                else:
                    if entry.related_session_id is not None:
                        _recovery(
                            state, entry, "INVALID_HANDOFF",
                            "非 handoff Plan entry 包含来源 session ID",
                        )
                        continue
                    handoff_origin_session_id = None
                seen_plan_ids.add(entry.plan_id)
                state = PlanState(
                    mode="plan",
                    phase="drafting",
                    active_plan_id=entry.plan_id,
                )
            else:
                valid_exit_state = (
                    state.mode == "plan"
                    and state.phase in {"drafting", "awaiting_answer", "ready"}
                ) or state.phase == "uncertain"
                if not valid_exit_state:
                    _recovery(
                        state, entry, "INVALID_MODE_TRANSITION",
                        "当前状态不接受退出 Plan entry",
                    )
                    continue
                if entry.plan_id != state.active_plan_id:
                    _recovery(
                        state, entry, "INVALID_MODE_TRANSITION",
                        "退出 Plan 的 planId 与活动 Episode 不匹配",
                    )
                    continue
                if entry.reason == "handoff":
                    if (
                        state.phase != "ready"
                        or not _valid_replay_token(entry.related_session_id)
                    ):
                        _recovery(
                            state, entry, "INVALID_HANDOFF",
                            "只有 ready Plan 可以完成 handoff",
                        )
                        continue
                elif entry.related_session_id is not None:
                    _recovery(
                        state, entry, "INVALID_HANDOFF",
                        "非 handoff 退出 entry 包含目标 session ID",
                    )
                    continue
                state.mode = "default"
                state.phase = "cancelled" if state.active_plan_id else "idle"
                state.pending_question = None
                state.handoff_target_session_id = (
                    entry.related_session_id if entry.reason == "handoff" else None
                )
            continue

        if isinstance(entry, PlanQuestionEntry):
            if (
                state.mode != "plan"
                or state.phase != "drafting"
                or entry.plan_id != state.active_plan_id
                or state.pending_question is not None
                or (entry.plan_id, entry.question_id) in seen_question_ids
            ):
                _recovery(
                    state, entry, "INVALID_PLAN_QUESTION",
                    "Plan Question 状态顺序无效",
                )
                continue
            if (
                not entry.question_id
                or not entry.question
                or not entry.header.strip()
                or len(entry.header) > 12
                or not 2 <= len(entry.options) <= 3
                or any(
                    not isinstance(option, dict)
                    or not str(option.get("label", "")).strip()
                    or not str(option.get("description", "")).strip()
                    for option in entry.options
                )
            ):
                _recovery(
                    state, entry, "INVALID_PLAN_QUESTION",
                    "Plan Question 内容无效",
                )
                continue
            state.phase = "awaiting_answer"
            seen_question_ids.add((entry.plan_id, entry.question_id))
            state.pending_question = PlanQuestion(
                question_id=entry.question_id,
                header=entry.header,
                question=entry.question,
                options=tuple(
                    PlanQuestionOption(
                        label=str(option.get("label", "")),
                        description=str(option.get("description", "")),
                    )
                    for option in entry.options
                ),
                allow_custom=entry.allow_custom,
            )
            continue

        if isinstance(entry, PlanQuestionAnswerEntry):
            if (
                state.mode != "plan"
                or state.phase != "awaiting_answer"
                or state.pending_question is None
                or entry.plan_id != state.active_plan_id
                or entry.question_id != state.pending_question.question_id
                or not entry.answer.strip()
            ):
                _recovery(
                    state, entry, "INVALID_PLAN_ANSWER",
                    "Plan Question Answer 状态顺序无效",
                )
                continue
            state.pending_question = None
            state.phase = "drafting"
            continue

        if isinstance(entry, PlanRevisionEntry):
            if (
                state.mode != "plan"
                or state.phase != "drafting"
                or entry.plan_id != state.active_plan_id
            ):
                _recovery(
                    state, entry, "INVALID_PLAN_REVISION",
                    "Plan Revision 状态顺序无效",
                )
                continue
            expected_revision = (
                state.latest_revision.revision + 1 if state.latest_revision else 1
            )
            is_handoff_copy = (
                state.latest_revision is None
                and bool(entry.origin_session_id)
                and entry.origin_session_id == handoff_origin_session_id
            )
            if (
                entry.revision < 1
                or (entry.revision != expected_revision and not is_handoff_copy)
            ):
                _recovery(
                    state, entry, "INVALID_PLAN_REVISION",
                    "Plan revision 必须单调递增",
                )
                continue
            if entry.schema_version == PLAN_REVISION_SCHEMA_VERSION:
                if not _valid_replay_token(entry.submitted_by_tool_call_id):
                    _recovery(
                        state, entry, "INVALID_PLAN_REVISION_SOURCE",
                        "v1 Plan Revision 缺少 submit_plan tool call ID",
                    )
                    continue
                if entry.submitted_by_tool_call_id in submitted_tool_call_ids:
                    _recovery(
                        state, entry, "INVALID_PLAN_REVISION_SOURCE",
                        "submit_plan tool call 不能重复生成 revision",
                    )
                    continue
                if entry.origin_session_id is not None:
                    if not is_handoff_copy:
                        _recovery(
                            state, entry, "INVALID_HANDOFF",
                            "handoff revision 来源与 Plan entry 不匹配",
                        )
                        continue
                else:
                    source = messages_by_id.get(entry.source_message_id)
                    previous = previous_by_id.get(entry.id)
                    tool_calls = [
                        block
                        for block in getattr(source.message, "content", [])
                        if getattr(block, "type", None) == "toolCall"
                    ] if source is not None else []
                    call = tool_calls[0] if len(tool_calls) == 1 else None
                    arguments = getattr(call, "arguments", None)
                    if (
                        source is None
                        or previous is not source
                        or getattr(source.message, "role", None) != "assistant"
                        or getattr(source.message, "stop_reason", None) in {"aborted", "error", "length"}
                        or call is None
                        or getattr(call, "name", None) != "submit_plan"
                        or getattr(call, "id", None)
                        != entry.submitted_by_tool_call_id
                        or not isinstance(arguments, dict)
                        or arguments.get("title") != entry.title
                        or not isinstance(arguments.get("markdown"), str)
                        or normalize_plan_markdown(arguments["markdown"])
                        != entry.markdown
                    ):
                        _recovery(
                            state, entry, "INVALID_PLAN_REVISION_SOURCE",
                            "v1 Plan Revision 不对应独占 submit_plan 调用",
                        )
                        continue
            try:
                if entry.schema_version == PLAN_REVISION_SCHEMA_VERSION:
                    validated = create_plan_revision(
                        plan_id=entry.plan_id,
                        revision=entry.revision,
                        title=entry.title,
                        markdown=entry.markdown,
                        source_message_id=entry.source_message_id,
                        submitted_by_tool_call_id=entry.submitted_by_tool_call_id,
                        origin_session_id=entry.origin_session_id,
                    )
                    if validated.markdown != entry.markdown:
                        raise PlanModeError(
                            "INVALID_PLAN_SPEC",
                            "v1 Plan markdown 未按 CRLF 到 LF 规范化",
                        )
                    expected_digest = validated.digest
                else:
                    expected_digest = compute_plan_digest(
                        plan_id=entry.plan_id,
                        revision=entry.revision,
                        title=entry.title,
                        markdown=entry.markdown,
                        schema_version=entry.schema_version,
                    )
            except PlanModeError as exc:
                _recovery(state, entry, exc.code, str(exc))
                continue
            if entry.digest != expected_digest:
                _recovery(
                    state, entry, "PLAN_DIGEST_MISMATCH",
                    "Plan Revision digest 校验失败",
                )
                continue
            state.latest_revision = _revision_from_entry(entry)
            if (
                entry.schema_version == PLAN_REVISION_SCHEMA_VERSION
                and entry.origin_session_id is None
            ):
                submitted_tool_call_ids.add(entry.submitted_by_tool_call_id)
            state.pending_question = None
            state.phase = "ready"
            state.legacy_candidate = False
            continue

        if isinstance(entry, PlanRunEntry):
            if entry.status not in {"started", "completed", "failed", "aborted"}:
                _recovery(state, entry, "INVALID_PLAN_RUN", "Plan run status 无效")
                continue
            run = _run_from_entry(entry)
            if entry.status == "started":
                latest = state.latest_revision
                if (
                    state.mode != "plan"
                    or state.phase != "ready"
                    or latest is None
                    or (entry.plan_id, entry.revision, entry.digest)
                    != (latest.plan_id, latest.revision, latest.digest)
                    or (
                        latest.schema_version >= PLAN_REVISION_SCHEMA_VERSION
                        and not _valid_replay_token(entry.run_id)
                    )
                    or (entry.run_id is not None and entry.run_id in seen_run_ids)
                ):
                    _recovery(
                        state, entry, "INVALID_PLAN_RUN",
                        "Plan Run 未确认最新 revision",
                    )
                    continue
                if entry.run_id is not None:
                    seen_run_ids.add(entry.run_id)
                state.mode = "default"
                state.pending_question = None
                state.latest_run = run
                state.phase = (
                    "executing"
                    if live_run_id is not None and entry.run_id == live_run_id
                    else "uncertain"
                )
                continue

            previous = state.latest_run
            if (
                state.phase not in {"executing", "uncertain"}
                or state.latest_revision is None
                or (
                    state.latest_revision.schema_version
                    >= PLAN_REVISION_SCHEMA_VERSION
                    and not _valid_replay_token(entry.run_id)
                )
                or previous is None
                or previous.status != "started"
                or (entry.plan_id, entry.revision, entry.digest, entry.run_id)
                != (
                    previous.plan_id,
                    previous.revision,
                    previous.digest,
                    previous.run_id,
                )
            ):
                _recovery(
                    state, entry, "INVALID_PLAN_RUN",
                    "Plan Run 终态缺少匹配的 started",
                )
                continue
            state.mode = "default"
            state.latest_run = run
            state.phase = cast(PlanPhase, {
                "completed": "settled",
                "failed": "failed",
                "aborted": "aborted",
            }[entry.status])

    return state


def new_plan_id() -> str:
    return uuid.uuid4().hex


def new_question_id() -> str:
    return uuid.uuid4().hex


class RequestUserInputTool:
    """Plan control tool for one structured product decision."""

    name = "request_user_input"
    label = "request user input"
    effect = "control"
    plan_access: PlanAccess = "control"
    execution_mode = "sequential"
    description = (
        "Ask exactly one structured question and wait or defer for its answer."
    )
    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "header": {"type": "string", "maxLength": 12},
                        "question": {"type": "string"},
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 3,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["label", "description"],
                            },
                        },
                    },
                    "required": ["header", "question", "options"],
                },
            },
        },
        "required": ["questions"],
    }

    def __init__(
        self,
        callback: Callable[[PlanQuestion, Any], Awaitable[str | None]],
        *,
        deferred: bool,
    ) -> None:
        self._callback = callback
        self._deferred = deferred

    async def execute(
        self,
        tool_call_id: str,
        params: dict,
        signal: Any = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        del tool_call_id, on_update
        questions = params.get("questions")
        if not isinstance(questions, list) or len(questions) != 1:
            raise PlanModeError(
                "INVALID_PLAN_QUESTION", "一次必须且只能提交一个问题",
            )
        raw = questions[0]
        if not isinstance(raw, dict):
            raise PlanModeError("INVALID_PLAN_QUESTION", "问题格式无效")
        header = str(raw.get("header", "")).strip()
        question = str(raw.get("question", "")).strip()
        options_raw = raw.get("options")
        if not header or len(header) > 12 or not question:
            raise PlanModeError(
                "INVALID_PLAN_QUESTION",
                "header 必须为 1-12 个字符且 question 不能为空",
            )
        if not isinstance(options_raw, list) or not 2 <= len(options_raw) <= 3:
            raise PlanModeError(
                "INVALID_PLAN_QUESTION", "问题必须包含 2-3 个互斥选项",
            )
        options: list[PlanQuestionOption] = []
        for option in options_raw:
            if not isinstance(option, dict):
                raise PlanModeError("INVALID_PLAN_QUESTION", "选项格式无效")
            label = str(option.get("label", "")).strip()
            description = str(option.get("description", "")).strip()
            if not label or not description:
                raise PlanModeError(
                    "INVALID_PLAN_QUESTION", "选项标签和影响说明不能为空",
                )
            options.append(
                PlanQuestionOption(label=label, description=description),
            )
        plan_question = PlanQuestion(
            question_id=new_question_id(),
            header=header,
            question=question,
            options=tuple(options),
            allow_custom=True,
        )
        answer = await self._callback(plan_question, signal)
        if answer is None:
            return AgentToolResult(
                content=[
                    TextContent(
                        text="Question saved. Resume the session to answer it.",
                    ),
                ],
                terminate=True,
            )
        return AgentToolResult(
            content=[TextContent(text=f"User answer: {answer}")],
        )


class SubmitPlanTool:
    """The sole live path that can persist a schema-v1 Plan Revision."""

    name = "submit_plan"
    label = "submit plan"
    effect = "control"
    plan_access: PlanAccess = "control"
    execution_mode = "sequential"
    description = (
        "Submit the decision-complete plan. This must be the only tool call "
        "in the assistant message and ends the planning turn."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_PLAN_TITLE_CHARS,
            },
            "markdown": {"type": "string", "minLength": 1},
        },
        "required": ["title", "markdown"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        callback: Callable[
            [str, str, str], Awaitable[PlanRevision] | PlanRevision
        ],
    ) -> None:
        self._callback = callback

    async def execute(
        self,
        tool_call_id: str,
        params: dict,
        signal: Any = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        del signal, on_update
        title = params.get("title")
        markdown = params.get("markdown")
        if not isinstance(title, str) or not isinstance(markdown, str):
            raise PlanModeError(
                "INVALID_PLAN_SPEC", "submit_plan 需要 title 和 markdown",
            )
        result = self._callback(tool_call_id, title, markdown)
        revision = await result if inspect.isawaitable(result) else result
        return AgentToolResult(
            content=[
                TextContent(
                    text=(
                        f"Plan revision {revision.revision} is ready for "
                        f"explicit user confirmation (digest {revision.digest})."
                    ),
                ),
            ],
            details={"plan": revision.to_payload()},
            terminate=True,
        )


def is_plan_safe_shell_command(command: str, cwd: str = "") -> bool:
    """Compatibility helper: generic shell is never authorized in Plan Mode."""
    del command, cwd
    return False


async def enforce_plan_tool_policy(
    context: BeforeToolCallContext,
    cwd: str = "",
    *,
    phase: PlanPhase = "drafting",
) -> BeforeToolCallResult | None:
    """Fail closed before any frontend approval adapter is called."""
    del cwd
    tool = next(
        (
            item
            for item in context.context.tools or []
            if getattr(item, "name", None) == context.tool_call.name
        ),
        None,
    )
    access: PlanAccess = cast(
        PlanAccess, getattr(tool, "plan_access", "deny"),
    )
    name = context.tool_call.name
    if phase != "drafting":
        return BeforeToolCallResult(
            block=True,
            reason=f"PLAN_PHASE_BLOCKED: {phase} 状态不接受模型工具调用",
            code="PLAN_PHASE_BLOCKED",
        )
    if access not in {"observe", "control"}:
        return BeforeToolCallResult(
            block=True,
            reason=(
                f"PLAN_POLICY_BLOCKED: Plan Mode 禁止执行 {name}；"
                "请使用 read/grep/find/ls 或结构化 git_* 工具"
            ),
            code="PLAN_POLICY_BLOCKED",
            alternatives=[{"tool": item} for item in (
                "read", "grep", "find", "ls", "git_status", "git_log", "git_diff", "git_show",
            )],
        )
    if name == "submit_plan":
        if context.assistant_message.stop_reason in {"aborted", "error", "length"}:
            return BeforeToolCallResult(
                block=True, code="PLAN_SUBMIT_INCOMPLETE",
                reason="PLAN_SUBMIT_INCOMPLETE: 中止或截断的回复不能提交计划",
            )
        tool_calls = [
            block
            for block in getattr(context.assistant_message, "content", [])
            if getattr(block, "type", None) == "toolCall"
        ]
        if (
            len(tool_calls) != 1
            or getattr(tool_calls[0], "name", None) != "submit_plan"
        ):
            return BeforeToolCallResult(
                block=True,
                reason=(
                    "PLAN_SUBMIT_NOT_EXCLUSIVE: submit_plan "
                    "必须是本条消息唯一的工具调用"
                ),
                code="PLAN_SUBMIT_NOT_EXCLUSIVE",
                alternatives=[{"tool": "submit_plan", "exclusive": True}],
            )
    return None


async def call_hook(hook: Any, context: Any, signal: Any) -> Any:
    """Invoke a sync/async external hook without duplicating adapter logic."""
    if hook is None:
        return None
    result = hook(context, signal)
    return await result if inspect.isawaitable(result) else result
