"""Read a local image into a multimodal tool result, without OCR or a shell."""
from __future__ import annotations

import base64
from io import BytesIO
import os
from pathlib import Path
import stat
from typing import Any
import warnings

from PIL import Image, UnidentifiedImageError

from agent_llm import ImageContent, TextContent
from agent_llm.image_utils import (
    IMAGE_FORMAT_MIMES,
    MAX_IMAGE_BYTES,
    preflight_image_bytes,
    validate_image_content,
)

from agent_core.types import AgentToolResult, PlanAccess


VIEW_IMAGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to a local PNG, JPEG, GIF or WebP image (relative or absolute).",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}


class ViewImageTool:
    """Return actual image content under the host's normal read approval policy."""

    name: str = "view_image"
    effect: str = "read"
    plan_access: PlanAccess = "observe"
    label: str = "view image"
    description: str = "View a local image using the current model's vision capability. Does not perform OCR separately."
    parameters: dict = VIEW_IMAGE_SCHEMA
    prompt_snippet: str = "View local images"
    prompt_guidelines: list[str] = [
        "Use view_image to inspect image pixels; read only reads files as text.",
    ]

    def __init__(self, cwd: str = ".") -> None:
        self.cwd = cwd

    async def execute(
        self,
        tool_call_id: str,
        params: dict,
        signal: Any = None,
    ) -> AgentToolResult:
        path = Path(params["path"])
        full_path = (path if path.is_absolute() else Path(self.cwd) / path).resolve(strict=True)
        if not full_path.is_file():
            raise ValueError("view_image requires a regular local image file")

        # Bound the read even if the file changes after the metadata check. On
        # POSIX, nonblocking open also avoids hanging if replaced by a FIFO.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(full_path, flags)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("view_image requires a regular local image file")
            raw = handle.read(MAX_IMAGE_BYTES + 1)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds the 5 MiB size limit")

        preflight_image_bytes(raw)
        # Detect MIME from actual bytes, not an untrusted filename extension.
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(raw), formats=list(IMAGE_FORMAT_MIMES)) as image:
                    mime_type = IMAGE_FORMAT_MIMES.get(image.format or "", "")
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
                Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise ValueError("File is not a supported, valid image") from exc
        content = validate_image_content(ImageContent(
            data=base64.b64encode(raw).decode("ascii"), mime_type=mime_type,
        ))
        return AgentToolResult(
            content=[TextContent(text=f"Image: {path}"), content],
            details={"path": str(full_path), "mimeType": content.mime_type, "sizeBytes": len(raw)},
        )
