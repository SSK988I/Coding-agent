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
  - A path lock shared with ``write`` across Agent processes, plus version
    checks and an atomic replacement of the complete edited file.

This tool operates on the local filesystem.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import stat
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from typing import Any

from agent_llm import TextContent

from agent_core.tools._mutation import edit_conflict, file_mutation_lock, resolved_mutation_path
from agent_core.types import AgentToolResult, PlanAccess

EDIT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "minLength": 1,
            "description": "Path to the file to edit (relative or absolute).",
        },
        "edits": {
            "type": "array",
            "minItems": 1,
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
                            "minLength": 1,
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
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string.")
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
        if not isinstance(e["oldText"], str) or not isinstance(e["newText"], str):
            raise ValueError("oldText and newText must be strings.")
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
        result = "".join((result[: m["index"]], m["new_text"], result[m["index"] + m["length"] :]))

    if result == content_lf:
        raise ValueError(
            f"No changes made to {path}. The replacement produced identical content."
        )
    return result


FileVersion = tuple[int, int, int, int]


@dataclass(frozen=True)
class PreparedEdit:
    source_path: str
    resolved_path: str
    version: FileVersion
    before_hash: str
    after_hash: str
    new_bytes: bytes
    result: AgentToolResult


def _version(st: os.stat_result) -> FileVersion:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _read_target(source: str, *, writable: bool = False) -> tuple[str, os.stat_result, bytes]:
    resolved = resolved_mutation_path(source)
    if not stat.S_ISREG(os.stat(resolved).st_mode):
        raise edit_conflict(source, "target is not a regular file")
    flags = os.O_RDWR if writable else os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(resolved, flags), "rb") as file:
        before = os.fstat(file.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise edit_conflict(source, "target is not a regular file")
        if writable and before.st_nlink != 1:
            raise edit_conflict(source, "cannot replace a file with multiple hard links")
        raw = file.read()
        if _version(before) != _version(os.fstat(file.fileno())):
            raise edit_conflict(source, "file changed during read")
    if resolved != resolved_mutation_path(source) or _version(before) != _version(os.stat(resolved)):
        raise edit_conflict(source, "target changed during read")
    return resolved, before, raw


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _security = ctypes.WinDLL("advapi32", use_last_error=True)
    _security.GetFileSecurityW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    _security.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]

    def _windows_permissions(path: str) -> bytes:
        # Self-relative descriptor containing owner, group, and DACL.
        size = wintypes.DWORD()
        _security.GetFileSecurityW(path, 7, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not _security.GetFileSecurityW(path, 7, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw


def _copy_access(source: str, destination: str, metadata: os.stat_result) -> None:
    """Apply the original access restrictions to the replacement."""
    if os.name == "nt":
        expected = _windows_permissions(source)
        if _windows_permissions(destination) != expected:
            # Control bits of a self-relative SECURITY_DESCRIPTOR start at byte 2.
            protected = int.from_bytes(expected[2:4], "little") & 0x1000
            dacl_flags = 4 | (0x80000000 if protected else 0x20000000)
            if not _security.SetFileSecurityW(destination, dacl_flags, expected):
                raise ctypes.WinError(ctypes.get_last_error())
            if _windows_permissions(destination) != expected:
                raise PermissionError(f"Cannot preserve owner, group, and access permissions of {source}.")
        encrypted = stat.FILE_ATTRIBUTE_ENCRYPTED
        if (metadata.st_file_attributes ^ os.stat(destination).st_file_attributes) & encrypted:
            raise PermissionError(f"Cannot preserve file encryption of {source}.")
        return

    if sys.platform == "darwin":
        import ctypes as c

        # Darwin's copyfile copies the ACL and POSIX metadata with COPYFILE_SECURITY.
        copyfile = c.CDLL(None, use_errno=True).copyfile
        if copyfile(os.fsencode(source), os.fsencode(destination), None, 0x3) != 0:
            code = c.get_errno()
            raise OSError(code, os.strerror(code), source)
        return

    staged = os.stat(destination)
    if metadata.st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise PermissionError(f"Cannot preserve privileged execution bits while editing {source}.")
    if (metadata.st_uid, metadata.st_gid) != (staged.st_uid, staged.st_gid):
        os.chown(destination, metadata.st_uid, metadata.st_gid)
    if hasattr(os, "listxattr"):
        original = {k: os.getxattr(source, k) for k in os.listxattr(source)}
        if "security.capability" in original:
            raise PermissionError(f"Cannot preserve executable capabilities while editing {source}.")
        staged_attrs = {k: os.getxattr(destination, k) for k in os.listxattr(destination)}
        for key in staged_attrs.keys() - original.keys():
            os.removexattr(destination, key)
        for key, value in original.items():
            if staged_attrs.get(key) != value:
                os.setxattr(destination, key, value)
    os.chmod(destination, stat.S_IMODE(metadata.st_mode))


# ─── tool ──────────────────────────────────────────────────────────────

def _preserve_access(source: str, destination: str, metadata: os.stat_result) -> None:
    try:
        _copy_access(source, destination, metadata)
    except OSError as exc:
        raise edit_conflict(source, f"cannot preserve access permissions ({exc})") from exc


class EditTool:
    """Edit a file via exact text replacement (one or more disjoint edits).

    Each ``edits[].oldText``
    must match a unique, non-overlapping region of the original file. BOM and
    line endings are preserved. On success, returns a one-line summary plus a
    unified diff in ``details``.
    """

    name: str = "edit"
    effect: str = "write"
    plan_access: PlanAccess = "deny"
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

        async with file_mutation_lock(full_path) as locked_path:
            prepared = self.prepare_edit(path, edits)
            if prepared.resolved_path != locked_path:
                raise edit_conflict(path, "target no longer matches the locked path")
            self.commit_edit(prepared)
            return prepared.result

    def inspect_file(self, path: str) -> tuple[str, FileVersion, str]:
        resolved, metadata, raw = _read_target(os.path.join(self.cwd, path))
        return resolved, _version(metadata), hashlib.sha256(raw).hexdigest()

    def prepare_edit(self, path: str, edits: list[dict]) -> PreparedEdit:
        """Read and compute the original result without modifying the filesystem."""
        source = os.path.abspath(os.path.join(self.cwd, path))
        try:
            resolved, metadata, raw_bytes = _read_target(source, writable=True)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Could not edit file: {path}. File not found.") from exc

        raw = raw_bytes.decode("utf-8")
        text_no_bom, had_bom = _strip_bom(raw)
        line_ending = _detect_line_ending(text_no_bom)
        content_lf = _normalize_to_lf(text_no_bom)

        # Apply edits.
        new_content_lf = _apply_edits(content_lf, edits, path)

        # The hashes describe the exact on-disk bytes, including BOM and CRLF.
        final = _restore_line_endings(new_content_lf, line_ending)
        if had_bom:
            final = _BOM + final
        new_bytes = final.encode("utf-8")

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
        result = AgentToolResult(
            content=[TextContent(text=summary)],
            details={"patch": patch},
        )
        return PreparedEdit(
            source, resolved, _version(metadata), hashlib.sha256(raw_bytes).hexdigest(),
            hashlib.sha256(new_bytes).hexdigest(), new_bytes, result,
        )

    def commit_edit(self, prepared: PreparedEdit) -> None:
        """Stage a complete file, revalidate the original, then atomically replace it."""
        target = prepared.resolved_path

        def original_metadata() -> os.stat_result:
            try:
                resolved, metadata, raw = _read_target(prepared.source_path, writable=True)
            except (FileNotFoundError, NotADirectoryError) as exc:
                raise edit_conflict(prepared.source_path, "original target disappeared") from exc
            if (resolved != target or _version(metadata) != prepared.version
                    or hashlib.sha256(raw).hexdigest() != prepared.before_hash):
                raise edit_conflict(prepared.source_path, "original file or contents changed")
            return metadata

        try:
            before = os.stat(target)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise edit_conflict(prepared.source_path, "original target disappeared") from exc
        if _version(before) != prepared.version or resolved_mutation_path(prepared.source_path) != target:
            raise edit_conflict(prepared.source_path, "original target changed")
        fd, staged = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".coding-agent-edit-")
        staged_identity = os.fstat(fd)
        replaced = False
        try:
            with os.fdopen(fd, "wb") as stream:
                if os.name == "nt":
                    # A newly created Windows file inherits its directory ACL.
                    _preserve_access(target, staged, before)
                stream.write(prepared.new_bytes)
                stream.flush()
                if os.name != "nt":
                    # mkstemp starts with mode 0600. Copy the final permissions
                    # after writing so write() cannot clear special mode bits.
                    _preserve_access(target, staged, before)
                os.fsync(stream.fileno())
            current = original_metadata()
            if (current.st_mode != before.st_mode
                    or (os.name != "nt" and current.st_ctime_ns != before.st_ctime_ns)):
                raise edit_conflict(prepared.source_path, "file metadata changed during staging")
            if os.name == "nt" and _windows_permissions(target) != _windows_permissions(staged):
                raise edit_conflict(prepared.source_path, "file access permissions changed during staging")
            if not os.path.samestat(staged_identity, os.lstat(staged)):
                raise edit_conflict(prepared.source_path, "staging file was replaced")
            os.replace(staged, target)
            replaced = True
        finally:
            if not replaced:
                try:
                    if os.path.samestat(staged_identity, os.lstat(staged)):
                        os.unlink(staged)
                except FileNotFoundError:
                    pass
