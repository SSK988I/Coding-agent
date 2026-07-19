"""PNG/image → terminal half-block renderer.

Renders an image file as colored half-block characters (▀) using 24-bit
truecolor ANSI. Each terminal cell holds TWO image rows (upper pixel as the
foreground, lower pixel as the background), so the image occupies half as
many terminal lines as it does pixel rows.

Requires Pillow. Does NOT need any terminal image protocol (Kitty/iTerm2) —
works anywhere truecolor is supported.

Usage::

    from agent_tui.image_render import render_image_to_lines
    lines = render_image_to_lines("baby.png", width=40)
    # -> list[str], each an ANSI-colored line of half-blocks
"""
from __future__ import annotations

from pathlib import Path


def render_image_to_lines(
    path: str | Path,
    width: int = 40,
    max_height: int = 20,
) -> list[str]:
    """Render an image file to ANSI truecolor half-block lines.

    Args:
        path: Path to the image (PNG/JPG/etc).
        width: Target width in terminal cells (characters).
        max_height: Cap on terminal lines (each line = 2 pixel rows).

    Returns:
        List of strings, each one terminal line of colored half-blocks.
        Empty list if the image can't be loaded.
    """
    try:
        from PIL import Image
    except ImportError:
        return ["[image rendering needs Pillow]"]

    p = Path(path)
    if not p.is_file():
        return [f"[image not found: {p}]"]

    try:
        img = Image.open(p).convert("RGB")
    except Exception as e:  # noqa: BLE001
        return [f"[failed to load image: {e}]"]

    # Scale to target cell width. Two pixel rows pack into one terminal line
    # via ▀ (upper pixel as fg, lower pixel as bg), so pixel height = lines*2.
    iw, ih = img.size
    new_h = max(2, int(ih * width / iw / 2) * 2)  # even, at least 2
    new_h = min(new_h, max_height * 2)
    img = img.resize((width, new_h))

    pixels = img.load()
    lines: list[str] = []
    for y in range(0, new_h, 2):
        buf = []
        for x in range(width):
            up = pixels[x, y]
            # Lower pixel: if last odd row missing, treat as black.
            dn = pixels[x, y + 1] if y + 1 < new_h else (0, 0, 0)
            ur, ug, ub = up
            dr, dg, db = dn
            # ▀ = upper-half block: fg = upper pixel, bg = lower pixel.
            buf.append(f"\x1b[38;2;{ur};{ug};{ub};48;2;{dr};{dg};{db}m▀")
        lines.append("".join(buf) + "\x1b[0m")
    return lines
