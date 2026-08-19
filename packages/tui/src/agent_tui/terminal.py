"""Terminal abstraction.

Provides the ``Terminal`` interface and ``ProcessTerminal`` implementation
that the TUI's differential renderer depends on. Unlike the old no-op
``ProcessTerminal``, this enters real raw mode, reads stdin on a background
thread, enables bracketed paste, and queries terminal dimensions.

Cross-platform raw mode:
  - Unix: tty.setcbreak / termios.tcgetattr-tcsetattr
  - Windows: ctypes SetConsoleMode + ReadConsoleInputW for structured
    KEY_EVENT_RECORDs (translated back to VT sequences for keys.py). This
    path is what makes Windows IME preedit not leak letters into the editor.

The ``on_input`` callback receives decoded key sequences (possibly multi-byte
escapes); the TUI's keys.py decodes them via matches_key.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import signal
import sys
import threading
from typing import Callable, Protocol, runtime_checkable

# ANSI sequences
ESC = "\x1b"
CSI = "\x1b["
BRACKETED_PASTE_ENABLE = "\x1b[?2004h"
BRACKETED_PASTE_DISABLE = "\x1b[?2004l"

# Polling interval (seconds) for the Windows resize watcher. We poll the
# console OUTPUT screen-buffer info (GetConsoleScreenBufferInfo on CONOUT$),
# which is a different handle from stdin's input queue — so this never
# competes with the stdin reader (libuv uses the same decoupling on Windows,
# reading resize via EVENT_CONSOLE_LAYOUT + GetConsoleScreenBufferInfo on the
# output handle, not via the input queue).
_WIN_RESIZE_POLL_SEC = 0.1

# Windows output mode flags (wincon.h).  In particular,
# DISABLE_NEWLINE_AUTO_RETURN delays an automatic wrap at the final column
# until the next printable character is written.  The differential renderer
# relies on that behaviour when it writes a full-width line followed by CRLF.
_WIN_ENABLE_PROCESSED_OUTPUT = 0x0001
_WIN_ENABLE_WRAP_AT_EOL_OUTPUT = 0x0002
_WIN_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
_WIN_DISABLE_NEWLINE_AUTO_RETURN = 0x0008
_WIN_VT_OUTPUT_MODE = (
    _WIN_ENABLE_PROCESSED_OUTPUT
    | _WIN_ENABLE_WRAP_AT_EOL_OUTPUT
    | _WIN_ENABLE_VIRTUAL_TERMINAL_PROCESSING
)
_WIN_REQUIRED_OUTPUT_MODE = (
    _WIN_VT_OUTPUT_MODE
    | _WIN_DISABLE_NEWLINE_AUTO_RETURN
)


class _COORD(ctypes.Structure):
    """Windows ``COORD`` (windef.h)."""

    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _SMALL_RECT(ctypes.Structure):
    """Windows ``SMALL_RECT`` (windef.h)."""

    _fields_ = [
        ("Left", ctypes.c_short),
        ("Top", ctypes.c_short),
        ("Right", ctypes.c_short),
        ("Bottom", ctypes.c_short),
    ]


class _CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    """Windows ``CONSOLE_SCREEN_BUFFER_INFO`` (wincon.h), used to read the
    visible window size via GetConsoleScreenBufferInfo on the output handle.
    """

    _fields_ = [
        ("dwSize", _COORD),
        ("dwCursorPosition", _COORD),
        ("wAttributes", ctypes.c_ushort),
        ("srWindow", _SMALL_RECT),
        ("dwMaximumWindowSize", _COORD),
    ]


# ─── Windows console input event records (wincon.h) ─────────────────────
# Used by ReadConsoleInputW to decode stdin events into structured records.
# This is the path that lets us see IME-composed CJK characters without
# the pinyin letters leaking through during preedit (which is what os.read
# on a VT-input-enabled stdin does).

# INPUT_RECORD EventType values (wincon.h).
_KEY_EVENT_TYPE = 0x0001
_MOUSE_EVENT_TYPE = 0x0002
_WINDOW_BUFFER_SIZE_EVENT_TYPE = 0x0004
_MENU_EVENT_TYPE = 0x0008
_FOCUS_EVENT_TYPE = 0x0010

# ControlKeyState bit flags (wincon.h).
_ENHANCED_KEY = 0x0100
_SHIFT_PRESSED = 0x0010
_LEFT_CTRL_PRESSED = 0x0008
_RIGHT_CTRL_PRESSED = 0x0004
_LEFT_ALT_PRESSED = 0x0002
_RIGHT_ALT_PRESSED = 0x0001
_CTRL_PRESSED = _LEFT_CTRL_PRESSED | _RIGHT_CTRL_PRESSED
_ALT_PRESSED = _LEFT_ALT_PRESSED | _RIGHT_ALT_PRESSED
_NUMLOCK_ON = 0x0020
_SCROLLLOCK_ON = 0x0040
_CAPSLOCK_ON = 0x0080

# Virtual key codes that need explicit mapping (winuser.h).
_VK_BACK = 0x08
_VK_TAB = 0x09
_VK_RETURN = 0x0D
_VK_ESCAPE = 0x1B
_VK_SPACE = 0x20
_VK_PRIOR = 0x21   # PageUp
_VK_NEXT = 0x22    # PageDown
_VK_END = 0x23
_VK_HOME = 0x24
_VK_LEFT = 0x25
_VK_UP = 0x26
_VK_RIGHT = 0x27
_VK_DOWN = 0x28
_VK_INSERT = 0x2D
_VK_DELETE = 0x2E
_VK_F1 = 0x70
_VK_F12 = 0x7B

# VT sequences for F1-F12 (matches xterm convention used by keys.py).
# F1-F4 use SS3 (\x1bOP..) when from a normal key; F5+ use CSI ~.
_F_KEY_VT = [
    "\x1bOP",      # F1
    "\x1bOQ",      # F2
    "\x1bOR",      # F3
    "\x1bOS",      # F4
    "\x1b[15~",    # F5
    "\x1b[17~",    # F6
    "\x1b[18~",    # F7
    "\x1b[19~",    # F8
    "\x1b[20~",    # F9
    "\x1b[21~",    # F10
    "\x1b[23~",    # F11
    "\x1b[24~",    # F12
]


class _KEY_EVENT_RECORD(ctypes.Structure):
    """Windows ``KEY_EVENT_RECORD`` (wincon.h).

    ``uChar.UnicodeChar`` is filled by the console after IME composition
    completes — pinyin letters typed during preedit do NOT fire KEY_EVENTs
    with a UnicodeChar; only the final CJK character does. This is the
    property that lets us use ReadConsoleInputW for IME-safe input.
    """

    class _UCHAR(ctypes.Union):
        _fields_ = [
            ("UnicodeChar", ctypes.c_wchar),
            ("AsciiChar", ctypes.c_char),
            ("dwChar", ctypes.c_uint16),
        ]

    _fields_ = [
        ("bKeyDown", ctypes.c_int32),
        ("wRepeatCount", ctypes.c_uint16),
        ("wVirtualKeyCode", ctypes.c_uint16),
        ("wVirtualScanCode", ctypes.c_uint16),
        ("uChar", _UCHAR),
        ("dwControlKeyState", ctypes.c_uint32),
    ]


class _INPUT_RECORD(ctypes.Structure):
    """Windows ``INPUT_RECORD`` (wincon.h).

    Only the KeyEvent variant is decoded here; other event types are
    ignored (mouse) or handled elsewhere (WINDOW_BUFFER_SIZE → resize
    watcher thread).
    """

    class _EVENT(ctypes.Union):
        # Pad to the largest possible event record (MOUSE_EVENT_RECORD is
        # 16 bytes; KEY_EVENT_RECORD is also 16 bytes). The union ensures
        # the layout matches what ReadConsoleInputW writes.
        _fields_ = [
            ("KeyEvent", _KEY_EVENT_RECORD),
            ("_padding", ctypes.c_byte * 16),
        ]

    _fields_ = [
        ("EventType", ctypes.c_uint16),
        # 2 bytes of alignment padding between EventType and Event on Win64.
        ("_align", ctypes.c_uint16),
        ("Event", _EVENT),
    ]


@runtime_checkable
class Terminal(Protocol):
    """The terminal interface the TUI depends on."""

    def start(self, on_input: "Callable[[str], None]", on_resize: "Callable[[], None]") -> None: ...
    def stop(self) -> None: ...
    def write(self, data: str) -> None: ...
    @property
    def columns(self) -> int: ...
    @property
    def rows(self) -> int: ...
    @property
    def kitty_protocol_active(self) -> bool: ...
    def move_by(self, lines: int) -> None: ...
    def hide_cursor(self) -> None: ...
    def show_cursor(self) -> None: ...
    def clear_line(self) -> None: ...
    def clear_from_cursor(self) -> None: ...
    def clear_screen(self) -> None: ...
    def set_title(self, title: str) -> None: ...
    def set_progress(self, active: bool) -> None: ...


def _is_windows() -> bool:
    return os.name == "nt"


class ProcessTerminal:
    """Real terminal using sys.stdin/stdout in raw mode.

    On ``start()``: saves and enables raw mode, enables bracketed paste,
    starts a background thread reading stdin byte-by-byte (decoding to str
    and calling ``on_input``), and installs a SIGWINCH handler (Unix) for
    resize events. On ``stop()``: restores everything.
    """

    def __init__(self) -> None:
        self._on_input: "Callable[[str], None] | None" = None
        self._on_resize: "Callable[[], None] | None" = None
        self._reader_thread: threading.Thread | None = None
        self._resize_thread: threading.Thread | None = None
        self._running = False
        self._kitty_active = False
        # Unix raw-mode state.
        self._old_termios: list | None = None
        self._old_sigwinch: object | None = None
        # Windows console mode state.
        self._old_input_mode: int | None = None
        self._old_output_mode: int | None = None
        self._stdin_handle: int | None = None
        self._stdout_handle: int | None = None
        self._windows_delayed_wrap_enabled = False

    # ── dimensions ─────────────────────────────────────────────────────

    @property
    def columns(self) -> int:
        return shutil.get_terminal_size(fallback=(80, 24)).columns

    @property
    def rows(self) -> int:
        return shutil.get_terminal_size(fallback=(80, 24)).lines

    @property
    def kitty_protocol_active(self) -> bool:
        return self._kitty_active

    @property
    def delayed_wrap_supported(self) -> bool:
        """Whether writing the final column waits before wrapping.

        Real Unix terminals provide delayed-wrap semantics. On Windows this
        is only safe after ``DISABLE_NEWLINE_AUTO_RETURN`` was successfully
        enabled. This is deliberately an optional ``ProcessTerminal``
        capability rather than part of :class:`Terminal`, so existing fake
        terminals and third-party implementations remain compatible.
        """
        return not _is_windows() or self._windows_delayed_wrap_enabled

    # ── lifecycle ───────────────────────

    def start(
        self, on_input: "Callable[[str], None]", on_resize: "Callable[[], None]"
    ) -> None:
        self._on_input = on_input
        self._on_resize = on_resize
        self._running = True

        if _is_windows():
            self._enable_windows_vt()
        else:
            self._enable_unix_raw_mode()

        # Enable bracketed paste mode.
        self.write(BRACKETED_PASTE_ENABLE)

        # Start the stdin reader thread.
        self._reader_thread = threading.Thread(
            target=self._read_loop, name="agent_tui.stdin", daemon=True
        )
        self._reader_thread.start()

        # Windows: terminal resize events. We poll the OUTPUT handle's
        # screen-buffer info (GetConsoleScreenBufferInfo on CONOUT$), a
        # different handle from stdin's input queue, so it never competes
        # with the ReadConsoleInputW reader for events. This mirrors how
        # libuv gets resize on Windows.
        if _is_windows():
            self._resize_thread = threading.Thread(
                target=self._win_resize_loop, name="agent_tui.resize", daemon=True
            )
            self._resize_thread.start()

    def stop(self) -> None:
        self._running = False
        # Disable bracketed paste.
        self.write(BRACKETED_PASTE_DISABLE)
        self.flush()

        if _is_windows():
            self._disable_windows_vt()
        else:
            self._disable_unix_raw_mode()

    def drain_input(self, max_ms: int = 1000, idle_ms: int = 50) -> None:
        """Briefly drain pending stdin to avoid leaking keys to the parent shell.

        After stop(), consume buffered input for up to ``max_ms``, exiting
        early after ``idle_ms``
        of quiet. This prevents Kitty key-release events leaking over slow SSH.
        """
        import time
        deadline = time.monotonic() + max_ms / 1000
        import select
        while time.monotonic() < deadline:
            try:
                if not _is_windows() and select.select([sys.stdin], [], [], idle_ms / 1000)[0]:
                    sys.stdin.read(1)
                    continue
            except (OSError, ValueError):
                break
            break

    # ── stdin reader ───────────────────────────────────────────────────

    def _read_loop(self) -> None:
        """Background thread: read raw keystrokes from stdin, call on_input.

        On Unix: ``sys.stdin.read(1)`` in cbreak mode returns one byte at a time.
        On Windows: ``sys.stdin.read(1)`` is line-buffered by the C runtime,
        so we use ``msvcrt.getwch()`` which returns one key at a time. Special
        keys (arrows, function keys) come as a two-char sequence starting with
        ``\x00`` or ``\xe0``; we read the second byte to form the escape sequence.
        """
        try:
            if _is_windows():
                self._read_loop_windows()
            else:
                self._read_loop_unix()
        except (OSError, ValueError):
            pass

    def _read_loop_unix(self) -> None:
        """Unix: read one byte at a time from cbreak stdin."""
        stdin = sys.stdin
        while self._running:
            ch = stdin.read(1)
            if not ch:
                break
            if self._on_input:
                self._on_input(ch)

    def _read_loop_windows(self) -> None:
        """Windows: read structured events via ReadConsoleInputW.

        Why not os.read? With raw mode set on stdin (no line buffering,
        no echo), ``os.read(0)`` would still get raw key bytes. But the
        critical reason to prefer ReadConsoleInputW is **IME composition**:
        it returns ``INPUT_RECORD`` structs, and for ``KEY_EVENT_RECORD`` the
        ``uChar.UnicodeChar`` field is filled by the console AFTER IME
        composition completes. So pinyin letters typed during preedit do NOT
        generate KEY_EVENTs at all; only the final composed CJK character
        does. This is the property that prevents Chinese pinyin letters from
        leaking into the editor as raw ASCII during preedit. Symptom without
        this: typing "ni" to get "你" leaves stray "n" "i" at line end.

        We translate each event back into the VT escape sequence ``keys.py``
        expects (e.g. Shift+Tab → ``\\x1b[Z``, plain arrows → ``\\x1b[A``),
        so the rest of the TUI is unchanged.

        Note: VT input mode (``ENABLE_VIRTUAL_TERMINAL_INPUT``) is
        deliberately NOT set on stdin — see ``_enable_windows_vt``. With it
        on, the console pre-translates keys to VT byte streams and serves
        them via ReadConsoleInputW as per-byte KEY_EVENTs (ESC, '[', 'A'
        for one Up press). We'd forward those as 3 separate inputs and the
        ESC would get swallowed by listeners, leaving literal "[A" in the
        editor.
        """
        if self._stdin_handle is None:
            # Fallback: VT input was never enabled (no console, e.g. piped
            # stdin in a test). Fall back to byte-level reading.
            self._read_loop_windows_bytes_fallback()
            return

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.ReadConsoleInputW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_INPUT_RECORD),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.ReadConsoleInputW.restype = ctypes.c_int32

        BUF_SIZE = 16  # events per read; modest to keep latency low
        buf = (_INPUT_RECORD * BUF_SIZE)()
        read_count = ctypes.c_uint32(0)

        while self._running:
            try:
                ok = kernel32.ReadConsoleInputW(
                    self._stdin_handle, buf, BUF_SIZE, ctypes.byref(read_count)
                )
            except OSError:
                break
            if not ok:
                break
            for i in range(read_count.value):
                rec = buf[i]
                etype = rec.EventType
                if etype == _KEY_EVENT_TYPE:
                    ev = rec.Event.KeyEvent
                    if not ev.bKeyDown:
                        # Ignore key-up events; we only care about presses.
                        continue
                    s = self._encode_key_event(ev)
                    if s and self._on_input:
                        self._on_input(s)
                # WINDOW_BUFFER_SIZE_EVENT is handled by the resize watcher;
                # MOUSE / MENU / FOCUS events are ignored.

    def _read_loop_windows_bytes_fallback(self) -> None:
        """Legacy byte-level read loop, used when no console handle is available.

        Kept for non-tty stdin (tests, piped input). Uses os.read + the byte
        dispatcher, which is fine for those cases because there's no IME in
        play. NOT used for real terminal input — that goes through
        ReadConsoleInputW via ``_read_loop_windows``.
        """
        import os

        while self._running:
            try:
                chunk = os.read(0, 64)
            except OSError:
                break
            if not chunk:
                break
            self._dispatch_bytes(chunk)

    def _encode_key_event(self, ev: "_KEY_EVENT_RECORD") -> str:
        """Translate a Windows ``KEY_EVENT_RECORD`` to a VT sequence for keys.py.

        The output uses the same VT formats ``keys.py`` already matches
        (e.g. ``\\x1b[Z`` for Shift+Tab, ``\\x1b[A`` for Up), so the rest of
        the TUI doesn't need to know we're decoding Windows events.

        Order matters: virtual-key checks (arrows, F-keys, Tab, Return, …)
        run BEFORE the character check, because for those keys the VK code
        is meaningful even when ``uChar.UnicodeChar`` also carries the
        conventional char (e.g. Return has both VK_RETURN and uChar='\\r').

        For character input (printable ASCII, CJK from IME, etc.), we pass
        ``uChar.UnicodeChar`` straight through. IME-composed CJK characters
        arrive here with their full codepoint — preedit letters never do.
        """
        vk = ev.wVirtualKeyCode
        try:
            ucs = ev.uChar.UnicodeChar
        except (AttributeError, ValueError):
            ucs = "\x00"
        cks = ev.dwControlKeyState
        enhanced = bool(cks & _ENHANCED_KEY)
        shift = bool(cks & _SHIFT_PRESSED)
        ctrl = bool(cks & _CTRL_PRESSED)
        alt = bool(cks & _ALT_PRESSED)

        # ── 1. Virtual-key-only keys (no character semantics) ──────────
        if vk == _VK_LEFT:
            return self._encode_modified_arrow("D", shift, ctrl, alt)
        if vk == _VK_RIGHT:
            return self._encode_modified_arrow("C", shift, ctrl, alt)
        if vk == _VK_UP:
            return self._encode_modified_arrow("A", shift, ctrl, alt)
        if vk == _VK_DOWN:
            return self._encode_modified_arrow("B", shift, ctrl, alt)
        if vk == _VK_INSERT:
            return "\x1b[2~"
        if vk == _VK_DELETE:
            return "\x1b[3~"
        if vk == _VK_PRIOR:   # PageUp
            return "\x1b[5~"
        if vk == _VK_NEXT:    # PageDown
            return "\x1b[6~"
        if vk == _VK_HOME:
            return "\x1b[H" if enhanced else "\x1b[1~"
        if vk == _VK_END:
            return "\x1b[F" if enhanced else "\x1b[4~"
        if _VK_F1 <= vk <= _VK_F12:
            return _F_KEY_VT[vk - _VK_F1]
        if vk == _VK_TAB:
            # Shift+Tab = \x1b[Z (keys.py recognizes this); plain Tab = \t.
            # Ctrl+Tab / Alt+Tab are rare in terminals; encode as plain Tab.
            return "\x1b[Z" if shift else "\t"
        if vk == _VK_RETURN:
            # Alt+Enter = \x1b\r (matches keys.py's "alt+enter"); plain = \r.
            return "\x1b\r" if alt else "\r"
        if vk == _VK_BACK:
            # Ctrl+Backspace = \x08 (BS); plain = \x7f (DEL — modern convention).
            return "\x08" if ctrl else "\x7f"
        if vk == _VK_ESCAPE:
            return "\x1b"

        # ── 2. Character input (printable + IME-composed CJK) ──────────
        # ``ucs`` is the post-composition character. For Ctrl+letter the
        # console delivers ucs as the corresponding control character
        # (e.g. Ctrl+C → '\x03'), so we can pass it through directly.
        if ucs and ucs != "\x00":
            # Alt + printable → ESC + char (matches keys.py "alt+x").
            if alt and ucs.isprintable():
                return "\x1b" + ucs
            return ucs

        # Otherwise: a key event with no decodable character (e.g. raw Shift,
        # Ctrl, Alt modifier press). Drop it — the modifiers' effect is
        # captured via dwControlKeyState on the next real key event.
        return ""

    @staticmethod
    def _encode_modified_arrow(letter: str, shift: bool, ctrl: bool, alt: bool) -> str:
        """Encode an arrow key with optional modifiers.

        Plain: ``\\x1b[X``. With any modifier: xterm-style ``\\x1b[1;<mod>X``
        where <mod> is 2 (Shift), 5 (Ctrl), 3 (Alt), or a sum. ``keys.py``
        recognizes both plain and the legacy ``\\x1bO`` Ctrl-arrow forms;
        we emit the parameterized form so modifier combinations survive.
        """
        mod = 0
        if shift:
            mod += 2
        if alt:
            mod += 3
        if ctrl:
            mod += 5
        if mod == 0:
            return f"\x1b[{letter}"
        return f"\x1b[1;{mod}{letter}"

    # ── Windows resize watcher (libuv-style: poll the OUTPUT handle) ────

    def _win_resize_loop(self) -> None:
        """Poll the console OUTPUT screen-buffer size for resize events.

        This replicates libuv's decoupling on Windows: resize is detected via
        ``GetConsoleScreenBufferInfo`` on ``STD_OUTPUT_HANDLE`` (CONOUT$),
        an entirely separate handle from stdin's input queue, so this thread
        never touches the same console input buffer that the
        ReadConsoleInputW reader consumes from.

        The polling interval is ``_WIN_RESIZE_POLL_SEC`` (~100ms); on change
        of width or height, fires ``_on_resize`` (= ``TUI.request_render``,
        thread-safe via ``call_soon_threadsafe``). libuv uses a similar
        approach: an event hook wakes a worker which reads
        ``GetConsoleScreenBufferInfo`` after a debounce sleep.

        Lifecycle: daemon=True so process exit kills it. ``stop()`` flips
        ``_running``; the loop notices at the next poll tick.
        """
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        except AttributeError:
            return  # Not Windows; should never reach here.

        STD_OUTPUT_HANDLE = -11
        kernel32.GetConsoleScreenBufferInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_CONSOLE_SCREEN_BUFFER_INFO),
        ]
        kernel32.GetConsoleScreenBufferInfo.restype = ctypes.c_int
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        out_handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)

        info = _CONSOLE_SCREEN_BUFFER_INFO()
        last_w = -1
        last_h = -1
        import time

        while self._running:
            if not kernel32.GetConsoleScreenBufferInfo(out_handle, ctypes.byref(info)):
                # Output handle not available (e.g. redirected) — back off.
                time.sleep(_WIN_RESIZE_POLL_SEC)
                continue
            # srWindow is the visible viewport (rows/cols the user sees),
            # which is what changes when the window is resized. dwSize is the
            # buffer size (often wider than the window); srWindow tracks the
            # actual usable area.
            w = info.srWindow.Right - info.srWindow.Left + 1
            h = info.srWindow.Bottom - info.srWindow.Top + 1
            if (w, h) != (last_w, last_h):
                last_w, last_h = w, h
                if self._on_resize:
                    self._on_resize()
            time.sleep(_WIN_RESIZE_POLL_SEC)

    def _dispatch_bytes(self, chunk: bytes) -> None:
        """Dispatch raw input bytes, separating VT escapes from text input.

        - Bytes starting with 0x1b (ESC): an ASCII escape sequence — collect
          and dispatch as latin-1 (keys.py matches on ASCII).
        - Other bytes: text input — Windows console uses the system locale
          encoding (cp936/GBK on Chinese Windows), NOT UTF-8. We decode with
          the preferred encoding so multi-byte input (Chinese) is preserved.
        """
        if not self._on_input:
            return
        i = 0
        n = len(chunk)
        text_run = bytearray()

        def _windows_encoding() -> str:
            """Get the Windows console's real text encoding.

            ``locale.getpreferredencoding`` returns 'utf-8' in Python's UTF-8
            mode, which does NOT match what the VT-input console actually sends
            (it uses the system ANSI code page — cp936/GBK on Chinese Windows).
            We use GetACP() to get the real code page.
            """
            if sys.platform != "win32":
                return "utf-8"
            try:
                import ctypes
                cp = ctypes.windll.kernel32.GetACP()  # type: ignore[attr-defined]
                if cp:
                    return f"cp{cp}"
            except Exception:
                pass
            return "utf-8"

        def flush_text() -> None:
            if text_run:
                enc = _windows_encoding()
                try:
                    self._on_input(text_run.decode(enc))
                except (UnicodeDecodeError, LookupError):
                    # Fall back: try UTF-8, then latin-1 char by char.
                    try:
                        self._on_input(text_run.decode("utf-8"))
                    except UnicodeDecodeError:
                        for b in text_run:
                            self._on_input(chr(b))
                text_run.clear()

        while i < n:
            b = chunk[i]
            if b == 0x1B and i + 1 < n and chunk[i + 1] in (0x5B, 0x4F):
                # CSI (ESC [) or SS3 (ESC O) — flush pending text first.
                flush_text()
                # Collect the sequence up to a final byte (0x40–0x7E / ~).
                seq = bytearray([b, chunk[i + 1]])
                j = i + 2
                while j < n:
                    c = chunk[j]
                    seq.append(c)
                    j += 1
                    if 0x40 <= c <= 0x7E:  # final byte
                        break
                self._on_input(seq.decode("latin-1"))
                i = j
            else:
                text_run.append(b)
                i += 1
        flush_text()

    # ── Unix raw mode ───────────────────

    def _enable_unix_raw_mode(self) -> None:
        try:
            import termios
            import tty
        except ImportError:
            return  # Not available (shouldn't happen on Unix).
        try:
            self._old_termios = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        except (termios.error, OSError, ValueError):
            self._old_termios = None
        # SIGWINCH for resize.
        try:
            self._old_sigwinch = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, lambda *_: self._on_resize() if self._on_resize else None)
        except (AttributeError, ValueError, OSError):
            pass

    def _disable_unix_raw_mode(self) -> None:
        try:
            import termios
        except ImportError:
            return
        if self._old_termios is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_termios)
            except (termios.error, OSError, ValueError):
                pass
            self._old_termios = None
        if self._old_sigwinch is not None:
            try:
                signal.signal(signal.SIGWINCH, self._old_sigwinch)  # type: ignore[arg-type]
            except (ValueError, OSError):
                pass
            self._old_sigwinch = None

    # ── Windows VT mode ──────────────────────────

    def _enable_windows_vt(self) -> None:
        """Set up Windows console modes for raw input + VT output.

        INPUT side: do NOT set ENABLE_VIRTUAL_TERMINAL_INPUT. We read via
        ReadConsoleInputW, which gives us structured KEY_EVENT_RECORDs
        (vk codes + uChar.UnicodeChar + dwControlKeyState) — this is what
        lets us translate keys back to VT sequences ourselves and, crucially,
        what makes IME work: pinyin letters during preedit do NOT fire
        KEY_EVENTs, only the final CJK character does.

        If VT input IS set, ReadConsoleInputW still returns KEY_EVENT_RECORDs
        but the console has already started translating keys to VT byte
        streams — arrow keys arrive as 3 separate per-byte events
        (uChar='\\x1b', uChar='[', uChar='A'). Our _encode_key_event would
        then forward them as 3 separate inputs, dropping the ESC and leaving
        literal "[A" in the editor. Symptom: pressing Up/Down prints "[A[B"
        as text. So we deliberately leave VT input OFF on stdin.

        OUTPUT side: preserve the caller's existing flags, then enable
        ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT |
        ENABLE_VIRTUAL_TERMINAL_PROCESSING | DISABLE_NEWLINE_AUTO_RETURN so
        our diff renderer's escape sequences are honored without a
        full-width line immediately advancing the physical cursor.
        """
        self._old_input_mode = None
        self._old_output_mode = None
        self._stdin_handle = None
        self._stdout_handle = None
        self._windows_delayed_wrap_enabled = False

        try:
            kernel32 = ctypes.windll.kernel32
            # Get stdin/stdout handles.
            STD_INPUT_HANDLE = -10
            STD_OUTPUT_HANDLE = -11
            self._stdin_handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
            self._stdout_handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)

            # Save old modes.
            input_mode = ctypes.c_uint32(0)
            output_mode = ctypes.c_uint32(0)
            input_mode_read = bool(
                kernel32.GetConsoleMode(self._stdin_handle, ctypes.byref(input_mode))
            )
            output_mode_read = bool(
                kernel32.GetConsoleMode(self._stdout_handle, ctypes.byref(output_mode))
            )
            if input_mode_read:
                self._old_input_mode = input_mode.value
            if output_mode_read:
                self._old_output_mode = output_mode.value

            # Stdin: raw, no line buffering, no echo, NO VT input. We want
            # every key press as a structured KEY_EVENT_RECORD so we can
            # translate arrows/F-keys/modifier combos back to VT sequences
            # ourselves. ENABLE_PROCESSED_INPUT is intentionally left OFF so
            # Ctrl+C arrives as a normal key event (we route it through the
            # input-listener chain instead of letting the console raise SIGINT).
            #   ENABLE_LINE_INPUT  (0x0002)  — OFF (no Enter-to-flush buffering)
            #   ENABLE_ECHO_INPUT  (0x0004)  — OFF (we render ourselves)
            #   ENABLE_PROCESSED_INPUT (0x0001) — OFF (no auto Ctrl+C signal)
            #   ENABLE_WINDOW_INPUT (0x0008) — ON (window events if ever needed)
            if input_mode_read:
                kernel32.SetConsoleMode(self._stdin_handle, 0x0008)

            if output_mode_read:
                output_mode_with_vt = output_mode.value | _WIN_REQUIRED_OUTPUT_MODE
                self._windows_delayed_wrap_enabled = bool(
                    kernel32.SetConsoleMode(self._stdout_handle, output_mode_with_vt)
                )
                if not self._windows_delayed_wrap_enabled:
                    # Some console hosts support VT processing but reject
                    # DISABLE_NEWLINE_AUTO_RETURN. Keep ANSI rendering enabled
                    # and let TUI reserve the final column as its wrap fallback.
                    kernel32.SetConsoleMode(
                        self._stdout_handle,
                        output_mode.value | _WIN_VT_OUTPUT_MODE,
                    )
        except (AttributeError, OSError):
            pass

    def _disable_windows_vt(self) -> None:
        try:
            kernel32 = ctypes.windll.kernel32
            if self._old_input_mode is not None and self._stdin_handle is not None:
                kernel32.SetConsoleMode(self._stdin_handle, self._old_input_mode)
            if self._old_output_mode is not None and self._stdout_handle is not None:
                kernel32.SetConsoleMode(self._stdout_handle, self._old_output_mode)
        except (AttributeError, OSError):
            pass
        finally:
            self._old_input_mode = None
            self._old_output_mode = None
            self._stdin_handle = None
            self._stdout_handle = None
            self._windows_delayed_wrap_enabled = False

    # ── output ───────────────────────────────────

    def write(self, data: str) -> None:
        sys.stdout.write(data)

    def flush(self) -> None:
        sys.stdout.flush()

    def move_by(self, lines: int) -> None:
        """Move cursor by N lines: negative=up, positive=down."""
        if lines > 0:
            self.write(f"{CSI}{lines}B")
        elif lines < 0:
            self.write(f"{CSI}{-lines}A")

    def hide_cursor(self) -> None:
        self.write(f"{CSI}?25l")

    def show_cursor(self) -> None:
        self.write(f"{CSI}?25h")

    def clear_line(self) -> None:
        self.write(f"{CSI}K")

    def clear_from_cursor(self) -> None:
        self.write(f"{CSI}J")

    def clear_screen(self) -> None:
        self.write(f"{CSI}2J{CSI}H")

    def set_title(self, title: str) -> None:
        self.write(f"\x1b]0;{title}\x07")

    def set_progress(self, active: bool) -> None:
        """OSC 9;4 progress indicator."""
        if active:
            self.write("\x1b]9;4;3\x07")
        else:
            self.write("\x1b]9;4;0;\x07")
