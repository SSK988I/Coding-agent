"""ANSI string utilities.

The backbone of the TUI's string processing: visible-width calculation,
truncation, wrapping, slicing by terminal column, and ANSI escape extraction.
All operate on strings that may contain ANSI/OSC/APC escape sequences,
treating them as zero-width.

The public helpers cover visible-width measurement, ANSI extraction, truncation,
wrapping, terminal-column slicing, output normalization, and background styling.

Grapheme segmentation uses the ``regex`` module's ``\\X`` (grapheme cluster).
East-Asian width uses Unicode's ``East_Asian_Width`` property via ``unicodedata``
lookup tables.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List

import regex

# ─── grapheme segmentation ─────────────────────────────────────────────

#: Shared grapheme cluster segmenter. ``\X`` matches a single grapheme cluster,
#: so combined Unicode characters are measured as one visual unit.
_GRAPHEME_RE = regex.compile(r"\X")


def segment_graphemes(text: str) -> List[str]:
    """Split ``text`` into grapheme clusters."""
    if not text:
        return []
    return _GRAPHEME_RE.findall(text)


#: Shared Unicode-aware word segmenter used by word navigation.
_WORD_BOUNDARY_RE = regex.compile(r"(?:\w+|\W)")


def segment_words(text: str) -> List[str]:
    """Split ``text`` into word segments."""
    if not text:
        return []
    return _WORD_BOUNDARY_RE.findall(text)


# ─── character classification ─────────────────────────

#: Zero-width characters: default-ignorable, control, marks, surrogates.
#: Uses regex property escapes (Python ``regex`` supports ``\p{...}``).
_ZERO_WIDTH_RE = regex.compile(
    r"^(?:\p{Default_Ignorable_Code_Point}|\p{Control}|\p{Mark}|\p{Surrogate})+$"
)
_LEADING_NON_PRINTING_RE = regex.compile(
    r"^[\p{Default_Ignorable_Code_Point}\p{Control}\p{Format}\p{Mark}\p{Surrogate}]+"
)

#: CJK scripts for break-opportunity detection.
_CJK_RE = regex.compile(
    r"[\p{Script_Extensions=Han}\p{Script_Extensions=Hiragana}"
    r"\p{Script_Extensions=Katakana}\p{Script_Extensions=Hangul}"
    r"\p{Script_Extensions=Bopomofo}]"
)


def _could_be_emoji(segment: str) -> bool:
    """Fast heuristic: could this grapheme be an RGI emoji?."""
    cp = ord(segment[0]) if segment else 0
    return (
        (0x1F000 <= cp <= 0x1FBFF)
        or (0x2300 <= cp <= 0x23FF)
        or (0x2600 <= cp <= 0x27BF)
        or (0x2B50 <= cp <= 0x2B55)
        or ("\uFE0F" in segment)
        or (len(segment) > 2)  # multi-codepoint sequences (ZWJ, skin tones)
    )


# RGI Emoji check via regex property. ``regex`` supports \p{RGI_Emoji}
# in recent versions; fall back to couldBeEmoji heuristic if unavailable.
try:
    _RGI_EMOJI_RE = regex.compile(r"^\p{RGI_Emoji}$")
    _HAS_RGI_EMOJI = True
except (regex.error, TypeError):
    _RGI_EMOJI_RE = None
    _HAS_RGI_EMOJI = False


def _east_asian_width(cp: int) -> int:
    """East Asian width for a codepoint: 0, 1, or 2.

    the ``get-east-asian-width`` npm package logic: returns 2 for
    fullwidth/wide, 1 for narrow/ambiguous/neutral, 0 for zero-width-control.
    """
    # Fast path for ASCII.
    if cp < 0x80:
        # Control chars are zero-width (handled by caller); printable ASCII = 1.
        return 1 if cp >= 0x20 else 0
    try:
        ea = unicodedata.east_asian_width(chr(cp))
    except ValueError:
        return 1
    if ea in ("F", "W"):
        return 2
    return 1


def _grapheme_width(segment: str) -> int:
    """Width of a single grapheme cluster."""
    if segment == "\t":
        return 3
    if _ZERO_WIDTH_RE.match(segment):
        return 0
    # Emoji check with pre-filter.
    if _could_be_emoji(segment):
        if _HAS_RGI_EMOJI and _RGI_EMOJI_RE.match(segment):
            return 2
        if not _HAS_RGI_EMOJI:
            # Fall back to the heuristic.
            return 2
    # Base visible codepoint (strip leading non-printing).
    base = _LEADING_NON_PRINTING_RE.sub("", segment)
    if not base:
        return 0
    cp = ord(base[0])
    # Regional indicator symbols → conservative width 2.
    if 0x1F1E6 <= cp <= 0x1F1FF:
        return 2
    width = _east_asian_width(cp)
    # Trailing halfwidth/fullwidth forms that segment with a base.
    if len(segment) > 1:
        for ch in segment[1:]:
            c = ord(ch)
            if 0xFF00 <= c <= 0xFFEF:
                width += _east_asian_width(c)
            elif c in (0x0E33, 0x0EB3):  # Thai/Lao AM vowels
                width += 1
    return width


# ─── ANSI escape extraction ─────────────────────────

_ANSI_CSI_FINAL_RE = re.compile(r"[mGKHJ]")
_OSC_FINAL_BYTES = ("\x07", "\x1b\\")


def extract_ansi_code(s: str, pos: int) -> "tuple[str, int] | None":
    """Extract an ANSI/OSC/APC escape sequence starting at ``pos``.

    Returns ``(code, length)`` or None if ``s[pos]`` is not an escape.
    extractAnsiCode. Handles:
      - CSI: ESC [ ... <final byte in [mGKHJ]>
      - OSC: ESC ] ... BEL or ESC ] ... ST (ESC \\)
      - APC: ESC _ ... BEL or ESC _ ... ST (ESC \\)
    """
    if pos >= len(s) or s[pos] != "\x1b":
        return None
    nxt = s[pos + 1] if pos + 1 < len(s) else ""

    # CSI sequence.
    if nxt == "[":
        j = pos + 2
        while j < len(s) and not _ANSI_CSI_FINAL_RE.match(s[j]):
            j += 1
        if j < len(s):
            return s[pos : j + 1], j + 1 - pos
        return None

    # OSC sequence: ESC ] ... BEL | ST.
    if nxt == "]":
        j = pos + 2
        while j < len(s):
            if s[j] == "\x07":
                return s[pos : j + 1], j + 1 - pos
            if s[j] == "\x1b" and j + 1 < len(s) and s[j + 1] == "\\":
                return s[pos : j + 2], j + 2 - pos
            j += 1
        return None

    # APC sequence: ESC _ ... BEL | ST.
    if nxt == "_":
        j = pos + 2
        while j < len(s):
            if s[j] == "\x07":
                return s[pos : j + 1], j + 1 - pos
            if s[j] == "\x1b" and j + 1 < len(s) and s[j + 1] == "\\":
                return s[pos : j + 2], j + 2 - pos
            j += 1
        return None

    return None


def _strip_ansi(s: str) -> str:
    """Strip all ANSI/OSC/APC sequences from ``s``."""
    if "\x1b" not in s:
        return s
    out: list[str] = []
    i = 0
    while i < len(s):
        ansi = extract_ansi_code(s, i)
        if ansi:
            i += ansi[1]
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


# ─── visible_width ──────────────────────────────────

_WIDTH_CACHE: dict[str, int] = {}
_WIDTH_CACHE_SIZE = 512


def _is_printable_ascii(s: str) -> bool:
    """All chars in [0x20, 0x7e]?."""
    for ch in s:
        c = ord(ch)
        if c < 0x20 or c > 0x7E:
            return False
    return True


def visible_width(s: str) -> int:
    """Calculate the visible terminal width of ``s`` in columns.

    Strips ANSI/OSC/APC sequences (zero-width), converts tabs to 3 spaces,
    and sums grapheme-cluster widths (handling CJK wide chars and emoji).
    """
    if not s:
        return 0
    # Fast path: pure ASCII printable.
    if _is_printable_ascii(s):
        return len(s)
    # Cache.
    cached = _WIDTH_CACHE.get(s)
    if cached is not None:
        return cached
    # Normalize: tabs → 3 spaces, strip ANSI.
    clean = s.replace("\t", "   ") if "\t" in s else s
    if "\x1b" in clean:
        clean = _strip_ansi(clean)
    # Sum grapheme widths.
    width = 0
    for segment in segment_graphemes(clean):
        width += _grapheme_width(segment)
    # Cache (LRU-ish eviction).
    if len(_WIDTH_CACHE) >= _WIDTH_CACHE_SIZE:
        _WIDTH_CACHE.pop(next(iter(_WIDTH_CACHE)))
    _WIDTH_CACHE[s] = width
    return width


# ─── normalize_terminal_output ──────────────────────

_THAI_LAO_RE = re.compile(r"[\u0e33\u0eb3]")


def normalize_terminal_output(s: str) -> str:
    """Normalize Thai/Lao AM vowels to avoid stale-cell artifacts.

    Precomposed Thai SARA AM (U+0E33) / Lao AM (U+0EB3) decompose to the same
    cell width but render more consistently during differential repaint.
    """
    if not _THAI_LAO_RE.search(s):
        return s
    return s.replace("\u0e33", "\u0e4d\u0e32").replace("\u0eb3", "\u0ecd\u0eb2")


# ─── truncate_to_width ─────────────────────────────────

def truncate_to_width(text: str, max_width: int, *, ellipsis: str = "", pad: bool = False) -> str:
    """Truncate ``text`` to ``max_width`` visible columns.

    If ``ellipsis`` is given, it's appended (and counted against max_width).
    If ``pad``, the result is padded with spaces to exactly ``max_width``.
    ANSI state is preserved on the truncated prefix; a reset is appended.
    """
    if max_width <= 0:
        return ""
    vw = visible_width(text)
    if vw <= max_width:
        if pad:
            return text + " " * (max_width - vw)
        return text

    ellipsis_w = visible_width(ellipsis)
    target = max_width - ellipsis_w

    # Walk graphemes, accumulating until we hit target width.
    result = ""
    width = 0
    pending_ansi = ""
    i = 0
    while i < len(text):
        ansi = extract_ansi_code(text, i)
        if ansi:
            pending_ansi += ansi[0]
            i += ansi[1]
            continue
        if text[i] == "\t":
            if width + 3 > target:
                break
            if pending_ansi:
                result += pending_ansi
                pending_ansi = ""
            result += "\t"
            width += 3
            i += 1
            continue
        # Find the next ANSI/tab boundary to segment cleanly.
        end = i
        while end < len(text) and text[end] != "\t":
            a = extract_ansi_code(text, end)
            if a:
                break
            end += 1
        for segment in segment_graphemes(text[i:end]):
            w = _grapheme_width(segment)
            if width + w > target:
                # Finalize with ellipsis + reset + pad.
                return _finalize_truncated(result, ellipsis, max_width, pad)
            if pending_ansi:
                result += pending_ansi
                pending_ansi = ""
            result += segment
            width += w
        i = end

    return _finalize_truncated(result, ellipsis, max_width, pad)


def _finalize_truncated(prefix: str, ellipsis: str, max_width: int, pad: bool) -> str:
    """Append ellipsis + reset + optional padding."""
    reset = "\x1b[0m"
    vw = visible_width(prefix) + visible_width(ellipsis)
    if ellipsis:
        result = f"{prefix}{reset}{ellipsis}{reset}"
    else:
        result = f"{prefix}{reset}"
    if pad:
        result += " " * max(0, max_width - vw)
    return result


# ─── slice_by_column ───────────────────────────────────

def slice_by_column(line: str, start_col: int, length: int, *, strict: bool = False) -> str:
    """Slice ``line`` by visible column range [start_col, start_col+length).

    sliceByColumn. Returns the substring covering that
    column range, preserving any active ANSI styling. ``strict`` excludes wide
    chars that would straddle the boundary.
    """
    if length <= 0:
        return ""
    result = ""
    col = 0
    i = 0
    pending_ansi = ""
    while i < len(line):
        ansi = extract_ansi_code(line, i)
        if ansi:
            pending_ansi += ansi[0]
            i += ansi[1]
            continue
        for segment in segment_graphemes(line[i : i + 1]) or [line[i]]:
            w = _grapheme_width(segment)
            if col >= start_col + length:
                return result
            if col + w > start_col + length and strict:
                # Would straddle the end boundary.
                return result
            if col >= start_col or (col + w > start_col and not strict):
                if pending_ansi:
                    result += pending_ansi
                    pending_ansi = ""
                if col < start_col:
                    # Partial overlap at start: skip leading wide char portion.
                    pass
                else:
                    result += segment
            col += w
            i += len(segment)
            break
        else:
            i += 1
    return result


# ─── wrap_text_with_ansi ────────────────────────────

def wrap_text_with_ansi(text: str, width: int) -> List[str]:
    """Word-wrap ``text`` (which may contain ANSI codes) to ``width`` columns.

    Handles literal newlines (style carries across), breaks long tokens
    character-by-character, and trims trailing whitespace per wrapped line.
    """
    if not text:
        return [""]
    if width <= 0:
        return [text]

    input_lines = text.split("\n")
    result: list[str] = []
    # Cross-line ANSI state is intentionally not tracked; styles within each
    # individual line are preserved.
    for input_line in input_lines:
        wrapped = _wrap_single_line(input_line, width)
        result.extend(wrapped)
    return result if result else [""]


def _wrap_single_line(line: str, width: int) -> List[str]:
    """Wrap a single line (no newlines) to ``width`` columns."""
    if not line:
        return [""]
    vw = visible_width(line)
    if vw <= width:
        return [line]

    tokens = _split_into_tokens_with_ansi(line)
    wrapped: list[str] = []
    current = ""
    current_w = 0

    for token in tokens:
        tok_w = visible_width(token)
        is_ws = token.strip() == ""

        # Token itself exceeds width and isn't whitespace: break char by char.
        if tok_w > width and not is_ws:
            if current:
                wrapped.append(current.rstrip())
                current = ""
                current_w = 0
            broken = _break_long_word(token, width)
            for i in range(len(broken) - 1):
                wrapped.append(broken[i])
            current = broken[-1]
            current_w = visible_width(current)
            continue

        total = current_w + tok_w
        if total > width and current_w > 0:
            wrapped.append(current.rstrip())
            current = token if not is_ws else ""
            current_w = tok_w if not is_ws else 0
        else:
            current += token
            current_w += tok_w

    if current:
        wrapped.append(current.rstrip())
    return wrapped if wrapped else [""]


def _split_into_tokens_with_ansi(line: str) -> List[str]:
    """Split a line into whitespace/non-whitespace tokens, keeping ANSI codes
    attached to the following text."""
    tokens: list[str] = []
    i = 0
    while i < len(line):
        # Capture any leading ANSI codes.
        prefix = ""
        ansi = extract_ansi_code(line, i)
        while ansi:
            prefix += ansi[0]
            i += ansi[1]
            ansi = extract_ansi_code(line, i)
        if i >= len(line):
            if prefix:
                tokens.append(prefix)
            break
        # Consume a run of whitespace or non-whitespace.
        start = i
        is_ws = line[i].isspace()
        while i < len(line) and not extract_ansi_code(line, i) and line[i].isspace() == is_ws:
            i += 1
        tokens.append(prefix + line[start:i])
    return tokens


def _break_long_word(token: str, width: int) -> List[str]:
    """Break an over-long token into width-sized chunks.

    Preserves ANSI styling; appends a reset at each break point.
    """
    result: list[str] = []
    current = ""
    current_w = 0
    for segment in segment_graphemes(token):
        w = _grapheme_width(segment)
        if current_w + w > width and current:
            result.append(current + "\x1b[0m")
            current = ""
            current_w = 0
        current += segment
        current_w += w
    if current:
        result.append(current)
    return result


# ─── apply_background_to_line ───────────────────────────

def apply_background_to_line(line: str, width: int, bg_fn) -> str:
    """Apply a background color function to ``line``, padding to ``width``.

    applyBackgroundToLine. The bg_fn receives the
    stripped text and returns an ANSI-styled string. We split the line into
    visible segments and style each.
    """
    # Strip ANSI, apply the background to the whole line, and pad to width.
    # Per-segment style tracking is unnecessary for the
    # common case (solid background behind text).
    vw = visible_width(line)
    padding = max(0, width - vw)
    if padding > 0:
        line = line + " " * padding
    return bg_fn(line)
