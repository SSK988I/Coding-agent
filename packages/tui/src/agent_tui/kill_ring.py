"""Emacs-style kill/yank ring.

Tracks killed (deleted) text entries. Consecutive kills can accumulate into a
single entry. Supports yank (paste most recent) and yank-pop (cycle older).
"""
from __future__ import annotations


class KillRing:
    """Ring buffer for Emacs kill/yank operations.

    ``push`` with ``accumulate=True`` merges with the most recent entry
    (prepend for backward deletion, append for forward). ``rotate`` moves the
    last entry to front for yank-pop cycling.
    """

    def __init__(self) -> None:
        self._ring: list[str] = []

    def push(self, text: str, *, prepend: bool, accumulate: bool = False) -> None:
        if not text:
            return
        if accumulate and self._ring:
            last = self._ring.pop()
            self._ring.append(text + last if prepend else last + text)
        else:
            self._ring.append(text)

    def peek(self) -> "str | None":
        """Most recent entry, or None if empty."""
        return self._ring[-1] if self._ring else None

    def rotate(self) -> None:
        """Move last entry to front (for yank-pop cycling)."""
        if len(self._ring) > 1:
            last = self._ring.pop()
            self._ring.insert(0, last)

    @property
    def length(self) -> int:
        return len(self._ring)
