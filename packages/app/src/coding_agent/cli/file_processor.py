"""把 @file CLI 参数处理为文本内容和图片附件。

每个 ``@file`` 参数都会基于当前工作目录解析并读取，然后：
  - 作为文本内嵌，并由 ``<file name="...">...</file>`` 包裹；或
  - 作为 ``ImageContent``（base64）和 ``<file>`` 文本引用内嵌。

图片按原内容内嵌。空文件会被静默跳过；文件不存在时程序退出。

"""
from __future__ import annotations

import base64
import sys
from dataclasses import dataclass, field
from pathlib import Path

from agent_llm import ImageContent

from coding_agent.utils.paths import resolve_read_path

#: Image extensions recognized as image attachments.
_IMAGE_EXTENSIONS: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


@dataclass
class ProcessedFiles:
    """@file 参数处理结果。"""

    text: str = ""
    images: list[ImageContent] = field(default_factory=list)


def _detect_image_mime(path: Path) -> str | None:
    """如果 ``path`` 是支持的图片则返回 MIME 类型，否则返回 ``None``。"""
    return _IMAGE_EXTENSIONS.get(path.suffix.lower())


def process_file_arguments(
    file_args: list[str],
    cwd: str,
    *,
    auto_resize_images: bool = True,
) -> ProcessedFiles:
    """Process ``@file`` arguments into text content and image attachments.

    Args:
        file_args: List of file paths (already stripped of the leading ``@``).
        cwd: Working directory to resolve relative paths against.
        auto_resize_images: Reserved for image resizing; currently ignored.
            (no resizing is performed — images are inlined verbatim).

    Returns:
        ``ProcessedFiles`` with accumulated text and image attachments.
    """
    del auto_resize_images  # Reserved option; images are currently kept at source size.
    result = ProcessedFiles()

    for file_arg in file_args:
        absolute_path = resolve_read_path(file_arg, cwd)
        path = Path(absolute_path)

        if not path.exists():
            print(f"错误：找不到文件：{absolute_path}", file=sys.stderr)
            sys.exit(1)

        # Skip empty files silently.
        if path.stat().st_size == 0:
            continue

        mime_type = _detect_image_mime(path)

        if mime_type is not None:
            # Image branch — keep source dimensions and omit generated hints.
            try:
                data = path.read_bytes()
            except OSError as e:
                # Fall back to a text note instead of an attachment.
                result.text += f'<file name="{absolute_path}">{e}</file>\n'
                continue
            result.images.append(ImageContent(
                type="image",
                mime_type=mime_type,
                data=base64.b64encode(data).decode("ascii"),
            ))
            # Text reference (no inner newline for the image-reference form).
            result.text += f'<file name="{absolute_path}"></file>\n'
        else:
            # Text branch.
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                print(f"错误：无法读取文件 {absolute_path}：{e}", file=sys.stderr)
                sys.exit(1)
            # Note the inner newlines, distinct from the image-reference form.
            result.text += f'<file name="{absolute_path}">\n{content}\n</file>\n'

    return result
