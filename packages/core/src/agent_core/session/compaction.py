"""Pure-function compaction algorithm.

No I/O, no LLM calls here (the LLM call lives in summarize.py). This module
implements:
  - token estimation (chars/4 heuristic, mixing real usage + estimate)
  - cut-point selection (the heart of "what to summarize vs keep")
  - compaction preparation (gathering messages, detecting split turns)
  - file-operation extraction

Optional custom instructions can be supplied by the caller.
"""
from __future__ import annotations

from typing import Any

from agent_core.session.prompts import ESTIMATED_IMAGE_CHARS
from agent_core.session.types import (
    CompactionPreparation,
    CompactionSettings,
    ContextUsageEstimate,
    CutPointResult,
    SessionEntry,
    SessionMessageEntry,
)
from agent_core.session.messages import CompactionSummaryMessage

__all__ = [
    "estimate_tokens",
    "estimate_context_tokens",
    "calculate_context_tokens_from_usage",
    "should_compact",
    "find_cut_point",
    "prepare_compaction",
    "FileOperations",
    "extract_file_operations",
]

#: Roles that are safe cut points.
_CUT_ROLES = {"user", "assistant", "compactionSummary"}


# ─── token estimation ──────────────────────────

def _content_chars(message: Any) -> int:
    """Conservative char count for a message (used by the chars/4 heuristic)."""
    role = getattr(message, "role", None)
    content = getattr(message, "content", None)

    if role == "user":
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            total = 0
            for b in content:
                btype = getattr(b, "type", None)
                if btype == "text":
                    total += len(getattr(b, "text", "") or "")
                elif btype == "image":
                    total += ESTIMATED_IMAGE_CHARS
            return total
        return 0

    if role == "assistant":
        total = 0
        for b in content if isinstance(content, list) else []:
            btype = getattr(b, "type", None)
            if btype == "text":
                total += len(getattr(b, "text", "") or "")
            elif btype == "thinking":
                total += len(getattr(b, "thinking", "") or "")
            elif btype == "toolCall":
                import json as _json
                total += len(getattr(b, "name", "") or "")
                total += len(_json.dumps(getattr(b, "arguments", {}) or {}, ensure_ascii=False))
        return total

    if role == "toolResult":
        total = 0
        for b in content if isinstance(content, list) else []:
            if getattr(b, "type", None) == "text":
                total += len(getattr(b, "text", "") or "")
        return total

    if role == "compactionSummary":
        return len(getattr(message, "summary", "") or "")

    return 0


def estimate_tokens(message: Any) -> int:
    """Estimate tokens for a single message: chars / 4."""
    chars = _content_chars(message)
    return max(1, chars // 4)


def calculate_context_tokens_from_usage(usage: Any) -> int:
    """Real context tokens from a usage object.

    Prefers total_tokens, falls back to input+output+cache sums.
    """
    if usage is None:
        return 0
    total = getattr(usage, "total_tokens", 0) or 0
    if total:
        return int(total)
    inp = int(getattr(usage, "input", 0) or 0)
    out = int(getattr(usage, "output", 0) or 0)
    cr = int(getattr(usage, "cache_read", 0) or 0)
    cw = int(getattr(usage, "cache_write", 0) or 0)
    return inp + out + cr + cw


def estimate_context_tokens(messages: list) -> ContextUsageEstimate:
    """Mix real usage with estimated trailing tokens.

    Finds the last assistant message with non-error, non-aborted, non-zero
    usage; uses its real total_tokens as the base, then estimates everything
    after it. If no usable real usage, everything is estimated.
    """
    last_usage_index: int | None = None
    usage_tokens = 0
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if getattr(m, "role", None) != "assistant":
            continue
        stop = getattr(m, "stop_reason", "stop")
        if stop in ("error", "aborted"):
            continue
        usage = getattr(m, "usage", None)
        real = calculate_context_tokens_from_usage(usage)
        if real > 0:
            last_usage_index = i
            usage_tokens = real
            break

    trailing = sum(estimate_tokens(messages[i]) for i in range((last_usage_index + 1) if last_usage_index is not None else 0, len(messages)))
    if last_usage_index is None:
        # No real usage: estimate the whole thing.
        total = sum(estimate_tokens(m) for m in messages)
        return ContextUsageEstimate(tokens=total, usage_tokens=0, trailing_tokens=total, last_usage_index=None)

    return ContextUsageEstimate(
        tokens=usage_tokens + trailing,
        usage_tokens=usage_tokens,
        trailing_tokens=trailing,
        last_usage_index=last_usage_index,
    )


def should_compact(context_tokens: int, context_window: int, settings: CompactionSettings) -> bool:
    """True when context has less than reserve_tokens headroom."""
    return settings.enabled and context_tokens > context_window - settings.reserve_tokens


# ─── cut-point selection ───────────────────────

def _is_valid_cut_entry(entry: SessionEntry) -> bool:
    """A message entry whose message role is a valid cut point."""
    if not isinstance(entry, SessionMessageEntry) or entry.message is None:
        return False
    role = getattr(entry.message, "role", None)
    return role in _CUT_ROLES


def find_turn_start_index(entries: list[SessionEntry], cut_index: int) -> int:
    """Walk back from cut_index to the nearest turn boundary.

    A turn boundary is a user message (or a compactionSummary). Returns the
    index of that boundary.
    """
    for i in range(cut_index, -1, -1):
        e = entries[i]
        if isinstance(e, SessionMessageEntry) and e.message is not None:
            role = getattr(e.message, "role", None)
            if role in ("user", "compactionSummary"):
                return i
    return 0


def find_cut_point(
    entries: list[SessionEntry],
    start_index: int,
    end_index: int,
    keep_recent_tokens: int,
) -> CutPointResult:
    """Find where to cut history for summarization.

    Walk newest->oldest accumulating estimated tokens; once we exceed
    keep_recent_tokens, snap to the nearest valid cut point at or after that
    position. Never cut on a toolResult (it must follow its tool_call).
    """
    # Collect valid cut indices in [start_index, end_index).
    valid_cuts = [i for i in range(start_index, end_index) if _is_valid_cut_entry(entries[i])]
    if not valid_cuts:
        # No valid cut: keep everything from start_index.
        return CutPointResult(first_kept_entry_index=start_index, turn_start_index=-1, is_split_turn=False)

    accumulated = 0
    cut_index = valid_cuts[-1]  # default: keep from the newest valid cut (i.e. summarize little)
    found = False
    for i in range(end_index - 1, start_index - 1, -1):
        e = entries[i]
        if isinstance(e, SessionMessageEntry) and e.message is not None:
            accumulated += estimate_tokens(e.message)
        if accumulated >= keep_recent_tokens:
            # Snap forward to the first valid cut at or after i.
            snap = next((v for v in valid_cuts if v >= i), None)
            if snap is not None:
                cut_index = snap
            found = True
            break

    if not found:
        # Everything fit in keep_recent_tokens: cut at the oldest valid point
        # so we summarize the least (but still have a valid boundary).
        cut_index = valid_cuts[0]

    # Determine split-turn: is the cut entry a user message?
    cut_entry = entries[cut_index]
    cut_msg = cut_entry.message if isinstance(cut_entry, SessionMessageEntry) else None
    is_user = cut_msg is not None and getattr(cut_msg, "role", None) == "user"
    if is_user:
        return CutPointResult(
            first_kept_entry_index=cut_index, turn_start_index=-1, is_split_turn=False,
        )
    # Split turn: the cut lands mid-turn on an assistant message. Find the
    # turn boundary to know where the summarized history ends.
    turn_start = find_turn_start_index(entries, cut_index)
    return CutPointResult(
        first_kept_entry_index=cut_index, turn_start_index=turn_start, is_split_turn=True,
    )


# ─── file-operation tracking ──────────────────────────

class FileOperations:
    """Read/written/edited file sets across the summarized history."""
    def __init__(self) -> None:
        self.read: set[str] = set()
        self.written: set[str] = set()
        self.edited: set[str] = set()

    def merge(self, other: "FileOperations") -> None:
        self.read |= other.read
        self.written |= other.written
        self.edited |= other.edited

    def to_details_dict(self) -> dict:
        return {
            "read_files": sorted(self.read),
            "modified_files": sorted(self.written | self.edited),
        }


def _extract_from_tool_call(name: str, args: dict, ops: FileOperations) -> None:
    """Heuristically extract file paths from a tool call (read/write/edit/bash)."""
    if name in ("read", "write", "edit"):
        path = args.get("path") or args.get("file_path")
        if isinstance(path, str):
            if name == "read":
                ops.read.add(path)
            elif name == "write":
                ops.written.add(path)
            elif name == "edit":
                ops.edited.add(path)
    elif name == "bash":
        # Best-effort: no reliable path extraction from arbitrary shell. Skip.
        pass


def extract_file_operations(messages: list) -> FileOperations:
    """Walk assistant tool_calls to record read/written/edited files."""
    ops = FileOperations()
    for m in messages:
        if getattr(m, "role", None) != "assistant":
            continue
        content = getattr(m, "content", [])
        for b in content if isinstance(content, list) else []:
            if getattr(b, "type", None) == "toolCall":
                _extract_from_tool_call(getattr(b, "name", ""), getattr(b, "arguments", {}) or {}, ops)
    return ops


# ─── compaction preparation ────────────────────

def _get_message_for_compaction(entry: SessionEntry) -> Any:
    """Return the message for summarization, or None for compaction entries.

    Prior compaction entries are skipped because their summary is supplied through
    ``previous_summary``.
    """
    if isinstance(entry, SessionMessageEntry):
        return entry.message
    return None


def prepare_compaction(
    branch_entries: list[SessionEntry],
    settings: CompactionSettings,
) -> CompactionPreparation | None:
    """Prepare a compaction: decide what to summarize vs keep.

    Returns None if there's nothing to compact.
    """
    if not branch_entries:
        return None

    # If the last entry is already a compaction, nothing new to compact.
    from agent_core.session.types import CompactionEntry
    if isinstance(branch_entries[-1], CompactionEntry):
        return None

    # Find the most recent prior compaction on this branch for iterative update.
    previous_summary: str | None = None
    boundary_start = 0
    for i in range(len(branch_entries) - 1, -1, -1):
        e = branch_entries[i]
        if isinstance(e, CompactionEntry):
            previous_summary = e.summary or None
            # Start summarizing just after this compaction's first_kept_entry_id.
            fk = e.first_kept_entry_id
            for j, ej in enumerate(branch_entries):
                if ej.id == fk:
                    boundary_start = j
                    break
            else:
                boundary_start = i + 1
            break

    boundary_end = len(branch_entries)

    # Tokens before: estimate the full active context, honoring the prior
    # compaction the same way build_session_context does.
    active_messages: list = []
    # Use a lightweight inline reconstruction to avoid needing a SessionManager.
    if previous_summary is not None:
        # find the prior compaction entry to get first_kept_entry_id
        for i in range(len(branch_entries) - 1, -1, -1):
            e = branch_entries[i]
            if isinstance(e, CompactionEntry):
                active_messages.append(CompactionSummaryMessage(summary=e.summary, tokens_before=e.tokens_before))
                compaction_idx = i
                fk = e.first_kept_entry_id
                found = not fk
                for ej in branch_entries[:compaction_idx]:
                    if not found and ej.id == fk:
                        found = True
                    if found and isinstance(ej, SessionMessageEntry) and ej.message is not None:
                        active_messages.append(ej.message)
                for ej in branch_entries[compaction_idx + 1:]:
                    if isinstance(ej, SessionMessageEntry) and ej.message is not None:
                        active_messages.append(ej.message)
                break
        tokens_before = estimate_context_tokens(active_messages).tokens
    else:
        msgs = [e.message for e in branch_entries if isinstance(e, SessionMessageEntry) and e.message is not None]
        tokens_before = estimate_context_tokens(msgs).tokens

    cut = find_cut_point(branch_entries, boundary_start, boundary_end, settings.keep_recent_tokens)
    first_kept_entry_id = branch_entries[cut.first_kept_entry_index].id

    # History to summarize: from boundary_start up to (excluding) the cut region.
    if cut.is_split_turn:
        history_end = cut.turn_start_index
    else:
        history_end = cut.first_kept_entry_index

    messages_to_summarize: list = []
    for i in range(boundary_start, history_end):
        msg = _get_message_for_compaction(branch_entries[i])
        if msg is not None:
            messages_to_summarize.append(msg)

    turn_prefix_messages: list = []
    if cut.is_split_turn:
        # Messages between turn_start (exclusive) and first_kept_entry (exclusive):
        # the part of the split turn that's neither fully summarized nor kept.
        for i in range(cut.turn_start_index, cut.first_kept_entry_index):
            msg = _get_message_for_compaction(branch_entries[i])
            if msg is not None:
                turn_prefix_messages.append(msg)

    if not messages_to_summarize and not turn_prefix_messages:
        return None

    file_ops = extract_file_operations(messages_to_summarize)
    if turn_prefix_messages:
        file_ops.merge(extract_file_operations(turn_prefix_messages))

    return CompactionPreparation(
        first_kept_entry_id=first_kept_entry_id,
        messages_to_summarize=messages_to_summarize,
        turn_prefix_messages=turn_prefix_messages,
        is_split_turn=cut.is_split_turn,
        tokens_before=tokens_before,
        previous_summary=previous_summary,
        file_ops=file_ops,
        settings=settings,
    )
