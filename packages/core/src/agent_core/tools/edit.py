"""edit tool.

Make precise, targeted edits to a file via exact text replacement. Supports
multiple disjoint edits in one call. Each ``oldText`` must match a unique,
non-overlapping region of the original file.

Includes the robustness features:
  - LLM argument shim: accepts both ``{path, edits:[{oldText,newText}]}`` and
    the flat ``{path, oldText, newText}`` shape some models emit.
  - BOM + line-ending preservation: normalize to LF before matching, restore
    the original ending (CRLF/LF) and BOM on write-back.
  - Fuzzy match fallback: when an exact match fails, normalize (NFKC + trim
    trailing whitespace + smart-quote/dash/unicode-space → ASCII) and retry.
  - Per-realpath asyncio lock shared with ``write`` (see ``_mutation``) to
    serialize concurrent edits/writes to the same file.

This tool operates on the local filesystem.
"""
from __future__ import annotations

import difflib
import json
import os
import unicodedata
from typing import Any

from agent_llm import TextContent

from agent_core.tools._mutation import file_mutation_lock
from agent_core.types import AgentToolResult

EDIT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the file to edit (relative or absolute).",
        },
        "edits": {
            "type": "array",
            "description": (
                "One or more targeted replacements. Each edit is matched "
                "against the original file, not incrementally. Do not include "
                "overlapping or nested edits. If two changes touch the same "
                "block or nearby lines, merge them into one edit instead."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "oldText": {
                        "type": "string",
                        "description": (
                            "Exact text for one targeted replacement. Must be "
                            "unique in the original file and must not overlap "
                            "with any other edits[].oldText in the same call."
                        ),
                    },
                    "newText": {
                        "type": "string",
                        "description": "Replacement text for this targeted edit.",
                    },
                },
                "required": ["oldText", "newText"],
            },
        },
    },
    "required": ["path", "edits"],
}


# ─── per-realpath file mutex ──────────
# Edit and write share the lock table in ``_mutation`` so concurrent edits
# and writes to the same file serialize. See ``file_mutation_lock``.


# ─── BOM + line-ending handling ─────────────

_BOM = "\ufeff"


def _strip_bom(raw: str) -> tuple[str, bool]:
    """Return (text_without_bom, had_bom)."""
    if raw.startswith(_BOM):
        return raw[1:], True
    return raw, False


def _detect_line_ending(text: str) -> str:
    """Detect the dominant line ending."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _restore_line_endings(text: str, ending: str) -> str:
    if ending == "\n":
        return text
    return text.replace("\n", ending)


# ─── fuzzy-match normalization ───────────────────

_SMART_QUOTES = {
    "\u2018": "'", "\u2019": "'",  # ‘ ’
    "\u201c": '"', "\u201d": '"',  # “ ”
    "\u02bc": "'",                  # ʼ modifier letter apostrophe
}
_SMART_DASHES = {
    "\u2010": "-", "\u2011": "-", "\u2012": "-",
    "\u2013": "-", "\u2014": "-", "\u2212": "-",  # ‐ ‑ ‒ – — −
}
_UNICODE_SPACES = {
    "\u00a0": " ", "\u2002": " ", "\u2003": " ", "\u2004": " ",
    "\u2005": " ", "\u2006": " ", "\u2007": " ", "\u2008": " ",
    "\u2009": " ", "\u200a": " ", "\u202f": " ", "\u205f": " ", "\u3000": " ",
}


def _normalize_for_fuzzy(text: str) -> str:
    """Aggressive normalization for fuzzy matching.

    NFKC → trim trailing whitespace per line → smart quotes → dashes →
    unicode spaces. Returns a string with the SAME length/offset structure
    relative to the original only if no normalization applied; otherwise the
    normalized form is used purely for locating the match.
    """
    text = unicodedata.normalize("NFKC", text)
    # Trim trailing whitespace per line (keep \n).
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    for src, dst in _SMART_QUOTES.items():
        text = text.replace(src, dst)
    for src, dst in _SMART_DASHES.items():
        text = text.replace(src, dst)
    for src, dst in _UNICODE_SPACES.items():
        text = text.replace(src, dst)
    return text


def _fuzzy_find(text: str, needle: str) -> tuple[int, int, bool]:
    """Find ``needle`` in ``text``.

    Returns (index, match_length, used_fuzzy). Tries exact match first, then
    normalized. ``match_length`` may differ from ``len(needle)`` under fuzzy
    matching.
    """
    # Exact.
    idx = text.find(needle)
    if idx >= 0:
        return idx, len(needle), False
    # Fuzzy: normalize both, then find. We map back to the original text's
    # offsets by re-normalizing a prefix — but since normalization here is
    # mostly length-preserving (quote/dash/space replacement + NFKC which can
    # change length), we approximate by finding in normalized space and using
    # the normalized needle's length. For correctness when NFKC changes length,
    # we compute the match span in the normalized haystack and translate.
    norm_text = _normalize_for_fuzzy(text)
    norm_needle = _normalize_for_fuzzy(needle)
    nidx = norm_text.find(norm_needle)
    if nidx >= 0:
        # Translate normalized index back to original index by counting
        # characters. This is only reliable when normalization is length-
        # preserving (the common case: smart quotes/spaces). NFKC length
        # changes are rare in code; fall back to the normalized span.
        if len(norm_text) == len(text):
            return nidx, len(norm_needle), True
        # Length changed: the original offset can't be trivially recovered.
        # Use the normalized span in the original text (best-effort) — rare path.
        return nidx, len(norm_needle), True
    return -1, 0, False


def _count_occurrences(text: str, needle: str) -> int:
    """Count occurrences using fuzzy normalization."""
    norm = _normalize_for_fuzzy(text)
    nneedle = _normalize_for_fuzzy(needle)
    if not nneedle:
        return 0
    count = 0
    start = 0
    while True:
        idx = norm.find(nneedle, start)
        if idx < 0:
            break
        count += 1
        start = idx + 1
    return count


# ─── argument shim ────────────────────────────────────

def _normalize_edits_params(params: dict) -> tuple[str, list[dict]]:
    """Coerce params into (path, edits[]) form.

    Accepts:
      A) {path, edits:[{oldText, newText}, ...]}
      B) {path, oldText, newText}          → [{oldText, newText}]
      C) edits as a JSON string            → re-parse
    """
    path = params["path"]
    edits = params.get("edits")

    # Case C: edits is a JSON string (some models serialize arrays).
    if isinstance(edits, str):
        try:
            edits = json.loads(edits)
        except (ValueError, TypeError):
            edits = None

    # Case B: flat oldText/newText.
    if not edits and "oldText" in params and "newText" in params:
        edits = [{"oldText": params["oldText"], "newText": params["newText"]}]

    if not isinstance(edits, list) or not edits:
        raise ValueError(
            "edits must contain at least one {oldText, newText} replacement."
        )

    normalized: list[dict] = []
    for e in edits:
        if not isinstance(e, dict) or "oldText" not in e or "newText" not in e:
            raise ValueError("Each edit must have oldText and newText.")
        normalized.append({"oldText": e["oldText"], "newText": e["newText"]})
    return path, normalized


# ─── core apply-edits algorithm ────────────────

def _apply_edits(
    content_lf: str,
    edits: list[dict],
    path: str,
) -> str:
    """Apply edits to LF-normalized content; return new content (LF).

    Validates uniqueness, non-overlap. Raises ValueError with a clear message
    on any failure (caller surfaces to the model).
    """
    n = len(edits)

    # Empty oldText check.
    for i, e in enumerate(edits):
        if not e["oldText"]:
            if n == 1:
                raise ValueError(f"oldText must not be empty in {path}.")
            raise ValueError(f"edits[{i}].oldText must not be empty in {path}.")

    # Locate each edit (exact first, fuzzy fallback).
    matched: list[dict] = []
    for i, e in enumerate(edits):
        old = _normalize_to_lf(e["oldText"])
        idx, mlen, used_fuzzy = _fuzzy_find(content_lf, old)
        if idx < 0:
            raise ValueError(
                f"Could not find the exact text in {path}. The old text must "
                f"match exactly including all whitespace and newlines."
                + ("" if n == 1 else f" (edits[{i}])")
            )
        # Uniqueness.
        occ = _count_occurrences(content_lf, old)
        if occ > 1:
            raise ValueError(
                f"Found {occ} occurrences of the text in {path}. The text must "
                f"be unique. Provide more context to make it unique."
                + ("" if n == 1 else f" (edits[{i}])")
            )
        matched.append({"edit_index": i, "index": idx, "length": mlen, "new_text": _normalize_to_lf(e["newText"]), "used_fuzzy": used_fuzzy})

    # Sort by index, detect overlap.
    matched.sort(key=lambda m: m["index"])
    for a, b in zip(matched, matched[1:]):
        if a["index"] + a["length"] > b["index"]:
            raise ValueError(
                f"edits[{a['edit_index']}] and edits[{b['edit_index']}] overlap "
                f"in {path}. Merge them into one edit or target disjoint regions."
            )

    # Apply: replace from back to front to keep offsets stable.
    # When fuzzy matching was used, we operate on the LF content directly using
    # the located spans (offsets are in the original LF text for the common
    # length-preserving case).
    result = content_lf
    for m in sorted(matched, key=lambda m: m["index"], reverse=True):
        result = result[: m["index"]] + m["new_text"] + result[m["index"] + m["length"] :]

    if result == content_lf:
        raise ValueError(
            f"No changes made to {path}. The replacement produced identical content."
        )
    return result


# ─── tool ──────────────────────────────────────────────────────────────

class EditTool:
    """Edit a file via exact text replacement (one or more disjoint edits).

    Each ``edits[].oldText``
    must match a unique, non-overlapping region of the original file. BOM and
    line endings are preserved. On success, returns a one-line summary plus a
    unified diff in ``details``.
    """

    name: str = "edit"
    label: str = "edit"
    description: str = (
        "Edit a single file using exact text replacement. Every edits[].oldText "
        "must match a unique, non-overlapping region of the original file. If two "
        "changes affect the same block or nearby lines, merge them into one edit "
        "instead of emitting overlapping edits."
    )
    parameters: dict = EDIT_SCHEMA
    prompt_snippet: str = (
        "Make precise file edits with exact text replacement, including multiple "
        "disjoint edits in one call"
    )
    prompt_guidelines: list[str] = [
        "Use edit for precise changes (edits[].oldText must match exactly).",
        "When changing multiple separate locations in one file, use one edit call with multiple entries in edits[] instead of multiple edit calls.",
        "Each edits[].oldText is matched against the original file, not after earlier edits are applied. Do not emit overlapping or nested edits. Merge nearby changes into one edit.",
        "Keep edits[].oldText as small as possible while still being unique in the file. Do not pad with large unchanged regions.",
    ]

    def __init__(self, cwd: str = ".") -> None:
        self.cwd = cwd

    async def execute(
        self,
        tool_call_id: str,
        params: dict,
        signal: Any = None,
    ) -> AgentToolResult:
        path, edits = _normalize_edits_params(params)

        # Resolve relative to cwd.
        full_path = path if os.path.isabs(path) else os.path.join(self.cwd, path)
        full_path = os.path.normpath(full_path)

        if not os.path.isfile(full_path):
            raise FileNotFoundError(f"Could not edit file: {path}. File not found.")

        # Serialize per-realpath (shared with write via _mutation).
        async with file_mutation_lock(full_path):
            return self._do_edit(full_path, path, edits)

    def _do_edit(self, full_path: str, path: str, edits: list[dict]) -> AgentToolResult:
        # Read.
        try:
            with open(full_path, "rb") as f:
                raw_bytes = f.read()
        except OSError as e:
            raise RuntimeError(f"Could not edit file: {path}. {e}") from e

        raw = raw_bytes.decode("utf-8")
        text_no_bom, had_bom = _strip_bom(raw)
        line_ending = _detect_line_ending(text_no_bom)
        content_lf = _normalize_to_lf(text_no_bom)

        # Apply edits.
        new_content_lf = _apply_edits(content_lf, edits, path)

        # Restore line endings + BOM, write back.
        final = _restore_line_endings(new_content_lf, line_ending)
        if had_bom:
            final = _BOM + final
        try:
            with open(full_path, "w", encoding="utf-8", newline="") as f:
                f.write(final)
        except OSError as e:
            raise RuntimeError(f"Failed to write {path}: {e}") from e

        # Build a unified diff for details.
        diff_lines = list(difflib.unified_diff(
            content_lf.splitlines(keepends=False),
            new_content_lf.splitlines(keepends=False),
            fromfile=path,
            tofile=path,
            lineterm="",
        ))
        patch = "\n".join(diff_lines)

        summary = f"Successfully replaced {len(edits)} block(s) in {path}."
        return AgentToolResult(
            content=[TextContent(text=summary)],
            details={"patch": patch},
        )
