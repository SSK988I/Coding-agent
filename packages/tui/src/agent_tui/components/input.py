"""Editor component: Multi-line text input with Emacs keybindings.

Uses prompt_toolkit for advanced input handling while maintaining
a Component-compatible interface for the TUI system.
"""

from typing import Optional, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style

from agent_tui.keys import create_key_bindings
from agent_tui.tui import Component


# Default prompt_toolkit style for the chat input
INPUT_STYLE = Style.from_dict(
    {
        "prompt": "bold green",
        "": "",  # Default
    }
)


class Editor(Component):
    """Multi-line text editor component.

    Provides a prompt_toolkit-based text input with Emacs keybindings,
    command history, and autocomplete support.

    Usage:
        editor = Editor(prompt="> ")
        editor.on_submit = lambda text: print(f"Submitted: {text}")

    Supports basic insertion, deletion, cursor movement, and submission.
    """

    def __init__(
        self,
        prompt: str = "> ",
        multiline: bool = False,
        history_size: int = 1000,
    ):
        self._prompt = prompt
        self._multiline = multiline
        self._cached_lines: list[str] = [prompt]

        # Callbacks
        self.on_submit: Optional[Callable[[str], None]] = None
        self.on_escape: Optional[Callable[[], None]] = None
        self.on_change: Optional[Callable[[str], None]] = None

        # State
        self.disable_submit: bool = False
        self.focused: bool = True

        # prompt_toolkit session
        kb = create_key_bindings()

        # Override Enter to call our submit handler
        @kb.add("enter")
        def handle_enter(event):
            if self.disable_submit:
                return
            text = event.current_buffer.text
            if self.on_submit:
                event.current_buffer.reset()
                # Schedule the callback after buffer reset
                event.app.exit(result=text)

        # Override Escape
        @kb.add("escape")
        def handle_escape(event):
            if event.current_buffer.text:
                event.current_buffer.reset()
            elif self.on_escape:
                event.app.exit(result="__escape__")

        # Override Ctrl+C for interrupt
        @kb.add("c-c")
        def handle_ctrl_c(event):
            event.app.exit(result="__interrupt__")

        self._key_bindings = kb
        self._session: Optional[PromptSession] = None
        self._history: list[str] = []

    def invalidate(self) -> None:
        """Clear cached rendering state."""
        self._cached_lines = [self._prompt]

    def handle_input(self, data: str) -> bool:
        """Handle raw keyboard input (unused - prompt_toolkit handles this)."""
        return False

    def render(self, width: int) -> list[str]:
        """Render the editor as a prompt line.

        The editor is rendered by prompt_toolkit's output system rather than as part of
        the component tree's render cycle. This method returns a
        minimal representation for layout purposes.
        """
        self._cached_lines = [self._prompt]
        return self._cached_lines

    async def get_input(self) -> Optional[str]:
        """Get a line of input using prompt_toolkit (async).

        Returns:
            The user's input text, or None if cancelled/EOF.
        """
        self._session = PromptSession(
            key_bindings=self._key_bindings,
            style=INPUT_STYLE,
            multiline=self._multiline,
            history=self._session.history if self._session else None,
        )
        try:
            result = await self._session.prompt_async(
                self._prompt,
            )
            if result == "__escape__":
                if self.on_escape:
                    self.on_escape()
                return None
            if result == "__interrupt__":
                return None
            if result and self.on_submit:
                self.on_submit(result)
            return result
        except (EOFError, KeyboardInterrupt):
            return None
