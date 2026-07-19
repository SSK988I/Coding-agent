"""Undo stack with clone-on-push semantics.

Stores deep copies of state snapshots. Popped snapshots are returned directly
(no re-copy) since they are already detached.
"""
from __future__ import annotations

import copy
from typing import Generic, List, TypeVar

S = TypeVar("S")


class UndoStack(Generic[S]):
    """Generic undo stack.

    ``push`` deep-copies the state so callers can keep mutating their copy.
    """

    def __init__(self) -> None:
        self._stack: List[S] = []

    def push(self, state: S) -> None:
        """Push a deep clone of ``state``."""
        self._stack.append(copy.deepcopy(state))

    def pop(self) -> "S | None":
        """Pop and return the most recent snapshot, or None if empty."""
        return self._stack.pop() if self._stack else None

    def clear(self) -> None:
        self._stack.clear()

    @property
    def length(self) -> int:
        return len(self._stack)
