"""Shared Plan Mode state, validation, control tool, and shell policy.

This module deliberately lives in the application package: both the CLI/TUI
and desktop runtime depend on it, while ``agent_core`` remains UI agnostic.
"""
from __future__ import annotations

import hashlib
import inspect
import os
import re
import shlex
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, cast

from agent_llm import TextContent
from agent_core import AgentToolResult, BeforeToolCallContext, BeforeToolCallResult
from agent_core.session.types import (
    CollaborationModeChangeEntry,
    PlanQuestionAnswerEntry,
    PlanQuestionEntry,
    PlanRevisionEntry,
    PlanRunEntry,
    SessionEntry,
    SessionMessageEntry,
)

CollaborationMode = Literal["default", "plan"]
PlanPhase = Literal[
    "idle", "drafting", "awaiting_answer", "ready", "executing",
    "completed", "failed", "aborted", "cancelled",
]
QuestionBehavior = Literal["interactive", "deferred"]


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

    def to_payload(self) -> dict[str, Any]:
        return {
            "planId": self.plan_id,
            "revision": self.revision,
            "title": self.title,
            "markdown": self.markdown,
            "digest": self.digest,
            "sourceMessageId": self.source_message_id,
        }


@dataclass
class PlanState:
    mode: CollaborationMode = "default"
    phase: PlanPhase = "idle"
    active_plan_id: str | None = None
    latest_revision: PlanRevision | None = None
    pending_question: PlanQuestion | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "activePlanId": self.active_plan_id,
            "latestRevision": (
                self.latest_revision.to_payload() if self.latest_revision else None
            ),
            "pendingQuestion": (
                self.pending_question.to_payload() if self.pending_question else None
            ),
        }


PLAN_MODE_OVERLAY = """
<collaboration_mode_policy mode="plan">
You are in Plan Mode. This policy has higher priority than custom prompts,
project context, and skill instructions.

- Inspect the repository before asking questions. Never ask for facts that can
  be discovered safely from the workspace.
- You may explore, explain, run policy-approved read-only checks, and draft a
  decision-complete implementation specification. You must not edit files,
  implement changes, or use unknown/mutating tools or shell commands.
- Ask at most one structured question at a time with request_user_input. Give
  2-3 mutually exclusive options, put the recommended option first, and explain
  each option's impact. The UI automatically offers a custom answer.
- Do not present a final plan until interfaces, state transitions, failures,
  compatibility, and tests are resolved.
- A valid final response must consist solely of one <proposed_plan> block with
  no text outside it. Its Markdown must have exactly one H1 title and H2
  sections Summary/摘要, Implementation Changes/实现变更, Public
  Interfaces/公开接口, Test Plan/测试计划, and Assumptions/假设.
- A request to implement or natural-language approval does not change modes.
  Only the host application may confirm or cancel the exact latest revision.
</collaboration_mode_policy>
""".strip()


_PLAN_BLOCK_RE = re.compile(r"\A<proposed_plan>\s*\n([\s\S]*?)\n</proposed_plan>\s*\Z")
_REQUIRED_SECTIONS = (
    {"summary", "摘要"},
    {"implementation changes", "实现变更"},
    {"public interfaces", "公开接口"},
    {"test plan", "测试计划"},
    {"assumptions", "假设"},
)


def _section_heading_tokens(heading: str) -> set[str]:
    """Return aliases from an English, Chinese, or bilingual H2 heading."""
    normalized = heading.strip().casefold()
    return {
        token.strip()
        for token in re.split(r"[/／]", normalized)
        if token.strip()
    } | {normalized}


def _markdown_section_tokens(markdown: str) -> set[str]:
    tokens: set[str] = set()
    for heading in re.findall(r"(?m)^##\s+(.+?)\s*$", markdown):
        tokens.update(_section_heading_tokens(heading))
    return tokens


def prepare_plan_reply(text: str) -> str | None:
    """Normalize a recognizably complete bare Markdown plan for validation.

    Models occasionally omit the control envelope or use a sequence of
    ``## 变更 N`` headings instead of the implementation umbrella heading.
    The host accepts that narrow shape so a complete plan can reach ``ready``;
    ordinary Markdown discussion remains untouched and non-executable.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if "<proposed_plan>" in normalized or "</proposed_plan>" in normalized:
        return normalized

    h1_match = re.search(r"(?m)^#\s+.+?\s*$", normalized)
    if h1_match is None:
        return None
    markdown = normalized[h1_match.start():].strip()
    tokens = _markdown_section_tokens(markdown)
    non_implementation_sections = (
        _REQUIRED_SECTIONS[0],
        _REQUIRED_SECTIONS[2],
        _REQUIRED_SECTIONS[3],
        _REQUIRED_SECTIONS[4],
    )
    if any(tokens.isdisjoint(aliases) for aliases in non_implementation_sections):
        return None

    # Strip a trailing conversational execution question. It is not part of
    # the immutable specification and cannot authorize execution.
    separator = markdown.rfind("\n---\n")
    if separator >= 0:
        trailing = markdown[separator + 5:].strip()
        if (
            trailing.endswith(("?", "？"))
            and re.search(r"执行|开工|开始|implement|proceed", trailing, re.IGNORECASE)
        ):
            markdown = markdown[:separator].rstrip()

    tokens = _markdown_section_tokens(markdown)
    if tokens.isdisjoint(_REQUIRED_SECTIONS[1]):
        headings = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", markdown))
        summary_index = next(
            (index for index, match in enumerate(headings)
             if not _section_heading_tokens(match.group(1)).isdisjoint(_REQUIRED_SECTIONS[0])),
            None,
        )
        public_index = next(
            (index for index, match in enumerate(headings)
             if not _section_heading_tokens(match.group(1)).isdisjoint(_REQUIRED_SECTIONS[2])),
            None,
        )
        if (
            summary_index is not None
            and public_index is not None
            and public_index > summary_index + 1
        ):
            insert_at = headings[summary_index + 1].start()
            markdown = (
                markdown[:insert_at]
                + "## Implementation Changes / 实现变更\n\n"
                + markdown[insert_at:]
            )

    return f"<proposed_plan>\n{markdown}\n</proposed_plan>"


def normalize_plan_markdown(markdown: str) -> str:
    return markdown.replace("\r\n", "\n").replace("\r", "\n").strip()


def validate_proposed_plan(
    text: str, *, plan_id: str, revision: int, source_message_id: str,
) -> PlanRevision:
    """Validate the strict envelope and return an immutable revision."""
    normalized_reply = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized_reply.count("<proposed_plan>") != 1 or normalized_reply.count("</proposed_plan>") != 1:
        raise PlanModeError("INVALID_PLAN_SPEC", "回复必须只包含一个 <proposed_plan> 块")
    match = _PLAN_BLOCK_RE.fullmatch(normalized_reply)
    if match is None:
        raise PlanModeError("INVALID_PLAN_SPEC", "计划块外不能包含正文")
    markdown = normalize_plan_markdown(match.group(1))
    h1 = re.findall(r"(?m)^#\s+(.+?)\s*$", markdown)
    if len(h1) != 1:
        raise PlanModeError("INVALID_PLAN_SPEC", "计划必须包含且只包含一个一级标题")
    headings = _markdown_section_tokens(markdown)
    missing = ["/".join(sorted(aliases)) for aliases in _REQUIRED_SECTIONS if headings.isdisjoint(aliases)]
    if missing:
        raise PlanModeError("INVALID_PLAN_SPEC", f"计划缺少章节：{', '.join(missing)}")
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    return PlanRevision(
        plan_id=plan_id, revision=revision, title=h1[0].strip(),
        markdown=markdown, digest=digest, source_message_id=source_message_id,
    )


def reduce_plan_state(entries: list[SessionEntry]) -> PlanState:
    """Fold Plan state from the active JSONL branch."""
    state = PlanState()
    for entry in entries:
        if isinstance(entry, SessionMessageEntry):
            if (
                state.mode == "plan"
                and state.phase == "ready"
                and getattr(entry.message, "role", None) == "user"
            ):
                # Feedback after a ready revision starts another drafting pass.
                # Retain the immutable revision for history/revision numbering,
                # but it is not executable while the episode is drafting.
                state.phase = "drafting"
        elif isinstance(entry, CollaborationModeChangeEntry):
            state.mode = cast(CollaborationMode, entry.mode)
            if entry.mode == "plan":
                state.phase = "drafting"
                state.active_plan_id = entry.plan_id
                state.latest_revision = None
                state.pending_question = None
            else:
                state.phase = "cancelled" if state.active_plan_id else "idle"
                state.pending_question = None
        elif isinstance(entry, PlanQuestionEntry):
            state.mode = "plan"
            state.phase = "awaiting_answer"
            state.active_plan_id = entry.plan_id
            state.pending_question = PlanQuestion(
                question_id=entry.question_id, header=entry.header,
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
        elif isinstance(entry, PlanQuestionAnswerEntry):
            if state.pending_question and state.pending_question.question_id == entry.question_id:
                state.pending_question = None
            state.mode = "plan"
            state.phase = "drafting"
            state.active_plan_id = entry.plan_id
        elif isinstance(entry, PlanRevisionEntry):
            state.mode = "plan"
            state.phase = "ready"
            state.active_plan_id = entry.plan_id
            state.pending_question = None
            state.latest_revision = PlanRevision(
                plan_id=entry.plan_id, revision=entry.revision, title=entry.title,
                markdown=entry.markdown, digest=entry.digest,
                source_message_id=entry.source_message_id,
            )
        elif isinstance(entry, PlanRunEntry):
            state.mode = "default"
            state.active_plan_id = entry.plan_id
            state.pending_question = None
            state.phase = cast(PlanPhase, {
                "started": "executing", "completed": "completed",
                "failed": "failed", "aborted": "aborted",
            }[entry.status])
    return state


def new_plan_id() -> str:
    return uuid.uuid4().hex


def new_question_id() -> str:
    return uuid.uuid4().hex


class RequestUserInputTool:
    """Control tool used only in Plan Mode."""

    name = "request_user_input"
    label = "request user input"
    effect = "control"
    execution_mode = "sequential"
    description = "Ask exactly one structured question and wait or defer for its answer."
    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array", "minItems": 1, "maxItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "header": {"type": "string", "maxLength": 12},
                        "question": {"type": "string"},
                        "options": {
                            "type": "array", "minItems": 2, "maxItems": 3,
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
        self, callback: Callable[[PlanQuestion, Any], Awaitable[str | None]],
        *, deferred: bool,
    ) -> None:
        self._callback = callback
        self._deferred = deferred

    async def execute(
        self, tool_call_id: str, params: dict, signal: Any = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        del tool_call_id, on_update
        questions = params.get("questions")
        if not isinstance(questions, list) or len(questions) != 1:
            raise PlanModeError("INVALID_PLAN_QUESTION", "一次必须且只能提交一个问题")
        raw = questions[0]
        if not isinstance(raw, dict):
            raise PlanModeError("INVALID_PLAN_QUESTION", "问题格式无效")
        header = str(raw.get("header", "")).strip()
        question = str(raw.get("question", "")).strip()
        options_raw = raw.get("options")
        if not header or len(header) > 12 or not question:
            raise PlanModeError("INVALID_PLAN_QUESTION", "header 必须为 1-12 个字符且 question 不能为空")
        if not isinstance(options_raw, list) or not 2 <= len(options_raw) <= 3:
            raise PlanModeError("INVALID_PLAN_QUESTION", "问题必须包含 2-3 个互斥选项")
        options: list[PlanQuestionOption] = []
        for option in options_raw:
            if not isinstance(option, dict):
                raise PlanModeError("INVALID_PLAN_QUESTION", "选项格式无效")
            label = str(option.get("label", "")).strip()
            description = str(option.get("description", "")).strip()
            if not label or not description:
                raise PlanModeError("INVALID_PLAN_QUESTION", "选项标签和影响说明不能为空")
            options.append(PlanQuestionOption(label=label, description=description))
        plan_question = PlanQuestion(
            question_id=new_question_id(), header=header, question=question,
            options=tuple(options), allow_custom=True,
        )
        answer = await self._callback(plan_question, signal)
        if answer is None:
            return AgentToolResult(
                content=[TextContent(text="Question saved. Resume the session to answer it.")],
                terminate=self._deferred,
            )
        return AgentToolResult(content=[TextContent(text=f"User answer: {answer}")])


_CONTROL_TOKENS = (";", "||", "`", "$(", ">", "<", "\n", "\r")
_SHELL_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|\|)\s*")
_READ_COMMANDS = {
    "pwd", "ls", "dir", "cat", "head", "tail", "wc", "sort", "uniq",
    "cut", "grep", "rg", "fd", "find", "where", "which", "type",
    "get-content", "select-string", "get-childitem", "git",
}
_READ_ONLY_GIT = {
    "status", "diff", "show", "log", "branch", "rev-parse", "ls-files",
    "ls-tree", "cat-file", "grep", "blame", "remote", "tag", "describe",
}
_VALIDATION_EXECUTABLES = {"pytest", "ruff", "pyright", "mypy"}
_SAFE_PACKAGE_SCRIPTS = {"test", "typecheck", "lint", "check", "build"}
_DANGEROUS_FIND_FLAGS = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}


def is_plan_safe_shell_command(command: str, cwd: str) -> bool:
    """Conservatively allow reads and recognized validation/build commands."""
    command = command.strip()
    if not command or any(token in command for token in _CONTROL_TOKENS):
        return False
    if re.search(r"(?:^|\s)\.\.(?:[\\/]|(?:\s|$))", command):
        return False
    segments = _SHELL_SPLIT_RE.split(command)
    if not segments or any(not segment.strip() for segment in segments):
        return False
    for segment in segments:
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            return False
        if not tokens or not _is_safe_segment(tokens, cwd):
            return False
    return True


def _is_safe_segment(tokens: list[str], cwd: str) -> bool:
    executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    args = tokens[1:]
    if executable == "cd":
        return len(args) == 1 and _path_stays_in_workspace(args[0], cwd)
    if executable == "git":
        positional = [item for item in args if not item.startswith("-")]
        if not positional or positional[0].casefold() not in _READ_ONLY_GIT:
            return False
        if positional[0].casefold() == "branch" and len(positional) > 1:
            return False
        if any(
            item in {"-c", "-o", "--paginate"}
            or item.startswith(("--output", "--exec-path", "--open-files-in-pager"))
            for item in args
        ):
            return False
    elif executable in _READ_COMMANDS:
        if executable == "find" and any(item.casefold() in _DANGEROUS_FIND_FLAGS for item in args):
            return False
        if executable == "rg" and any(item == "--pre" or item.startswith("--pre=") for item in args):
            return False
        if executable == "sort" and any(item == "-o" or item.startswith("--output") for item in args):
            return False
        if executable == "uniq" and len([item for item in args if not item.startswith("-")]) > 1:
            return False
    elif executable in _VALIDATION_EXECUTABLES:
        pass
    elif executable in {"pnpm", "npm", "yarn"}:
        lowered = {item.casefold() for item in args if not item.startswith("-")}
        if lowered.isdisjoint(_SAFE_PACKAGE_SCRIPTS) or lowered & {"add", "install", "exec", "publish"}:
            return False
    elif executable == "uv":
        if not _is_safe_uv(args):
            return False
    else:
        return False
    return all(
        _path_stays_in_workspace(token, cwd)
        for token in args
        if _looks_like_path(token)
    )


def _is_safe_uv(args: list[str]) -> bool:
    lowered = [item.casefold() for item in args if not item.startswith("-")]
    if not lowered:
        return False
    if lowered[0] == "build":
        return True
    if lowered[0] != "run" or len(lowered) < 2:
        return False
    if lowered[1] in _VALIDATION_EXECUTABLES:
        return True
    return lowered[1:3] == ["python", "scripts/check_versions.py"]


def _looks_like_path(token: str) -> bool:
    if token.startswith("-"):
        return False
    return (
        "/" in token or "\\" in token or token in {".", ".."}
        or bool(re.match(r"^[A-Za-z]:", token))
    )


def _path_stays_in_workspace(token: str, cwd: str) -> bool:
    if any(char in token for char in "*?[]{}"):
        token = token.split("*", 1)[0].split("?", 1)[0] or "."
    try:
        root = Path(cwd).resolve()
        candidate = Path(token)
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        return os.path.commonpath((str(root), str(resolved))) == str(root)
    except (OSError, ValueError):
        return False


async def enforce_plan_tool_policy(
    context: BeforeToolCallContext, cwd: str,
) -> BeforeToolCallResult | None:
    """Return a block result before any frontend-specific approval hook runs."""
    tool = next(
        (item for item in context.context.tools or [] if getattr(item, "name", None) == context.tool_call.name),
        None,
    )
    effect = getattr(tool, "effect", "unknown")
    if effect in {"read", "control"}:
        return None
    if effect == "shell" and is_plan_safe_shell_command(str(context.args.get("command", "")), cwd):
        return None
    return BeforeToolCallResult(
        block=True,
        reason=f"PLAN_POLICY_BLOCKED: Plan Mode 禁止执行 {context.tool_call.name}（effect={effect}）",
    )


async def call_hook(hook: Any, context: Any, signal: Any) -> Any:
    """Invoke a sync/async external hook without duplicating adapter logic."""
    if hook is None:
        return None
    result = hook(context, signal)
    return await result if inspect.isawaitable(result) else result
