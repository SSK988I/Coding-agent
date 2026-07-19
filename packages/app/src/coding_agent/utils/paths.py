"""Path normalization helpers.

Supports tilde expansion, ``@`` prefix stripping, Unicode-space normalization,
and macOS screenshot filename fallbacks.

Path normalization also handles NFD normalization and curly-quote
variants used in localized screenshot filenames.

"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

#: Unicode space characters replaced with a regular space.
_UNICODE_SPACES = re.compile(r"[\u00a0\u2000-\u200a\u202f\u205f\u3000]")

#: Narrow no-break space, used in macOS screenshot AM/PM filenames.
_NARROW_NO_BREAK_SPACE = "\u202f"

#: Right single quote (curly apostrophe), used in French screenshot names.
_RIGHT_SINGLE_QUOTE = "\u2019"


def normalize_path(
    input_path: str,
    *,
    expand_tilde: bool = True,
    strip_at_prefix: bool = False,
    normalize_unicode_spaces: bool = False,
) -> str:
    """Normalize a path string."""
    s = input_path
    if normalize_unicode_spaces:
        s = _UNICODE_SPACES.sub(" ", s)
    if strip_at_prefix and s.startswith("@"):
        s = s[1:]
    if expand_tilde:
        home = str(Path.home())
        if s == "~":
            s = home
        elif s.startswith("~/") or s.startswith("~\\"):
            s = os.path.join(home, s[2:])
    if s.startswith("file://"):
        # Best-effort file:// decoding without urllib edge cases.
        s = s[len("file://"):]
        # Strip optional leading extra slashes after file:// for unix paths.
        if s.startswith("/") and not s.startswith("//") and len(s) > 2 and s[2] == ":":
            # /C:/... style (file:///C:/) — drop leading slash.
            s = s[1:]
    return s


def resolve_path(input_path: str, base_dir: str | os.PathLike[str] | None = None, **options) -> str:
    """Resolve ``input_path`` against ``base_dir``."""
    normalized = normalize_path(input_path, **options)
    base = normalize_path(str(base_dir or Path.cwd()))
    p = Path(normalized)
    if p.is_absolute():
        return str(Path(normalized).resolve())
    return str((Path(base) / normalized).resolve())


def resolve_to_cwd(file_path: str, cwd: str | os.PathLike[str]) -> str:
    """Resolve a path against cwd with @/unicode-space handling."""
    return resolve_path(
        file_path, cwd, strip_at_prefix=True, normalize_unicode_spaces=True,
    )


def resolve_read_path(file_path: str, cwd: str | os.PathLike[str]) -> str:
    """Resolve a path for the read tool, trying macOS screenshot variants.

    Order: direct → AM/PM narrow-space → NFD →
    curly-quote → NFD+curly. Returns the first that exists, else the direct
    attempt.
    """
    resolved = resolve_to_cwd(file_path, cwd)
    if Path(resolved).exists():
        return resolved

    # 2. AM/PM narrow no-break space variant.
    am_pm_variant = re.sub(r" (AM|PM)\.", lambda m: f"{_NARROW_NO_BREAK_SPACE}{m.group(1)}.", resolved, flags=re.IGNORECASE)
    if am_pm_variant != resolved and Path(am_pm_variant).exists():
        return am_pm_variant

    # 3. NFD normalization variant.
    nfd_variant = unicodedata.normalize("NFD", resolved)
    if nfd_variant != resolved and Path(nfd_variant).exists():
        return nfd_variant

    # 4. Curly-quote variant (French "Capture d'écran").
    curly_variant = resolved.replace("'", _RIGHT_SINGLE_QUOTE)
    if curly_variant != resolved and Path(curly_variant).exists():
        return curly_variant

    # 5. NFD + curly.
    nfd_curly_variant = nfd_variant.replace("'", _RIGHT_SINGLE_QUOTE)
    if nfd_curly_variant != resolved and Path(nfd_curly_variant).exists():
        return nfd_curly_variant

    return resolved
