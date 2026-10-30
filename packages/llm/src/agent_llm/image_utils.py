"""Bounded validation shared by user attachments, image tools and request gates.

Keep decoded pixels and base64 out of the validation cache. Only a digest and
the inspected format/dimensions are retained; repeated conversation turns need
not decode the same image again.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import threading
import warnings
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

from PIL import Image, UnidentifiedImageError

from agent_llm.types import ImageContent, Model

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_FRAMES = 64
MAX_IMAGES_PER_TURN = 4
MAX_IMAGE_TURN_BYTES = 12 * 1024 * 1024
MAX_REQUEST_IMAGES = 24
MAX_REQUEST_IMAGE_BYTES = 24 * 1024 * 1024
# Includes all history, text, tools and encoded images, not just this turn.
MAX_IMAGE_REQUEST_BODY_BYTES = 40 * 1024 * 1024
IMAGE_FORMAT_MIMES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}
# MIME, maximum frame width/height, cumulative decoded pixels, frame count.
_inspected_images: OrderedDict[str, tuple[str, int, int, int, int]] = OrderedDict()
_inspection_lock = threading.Lock()


class ImageInputError(ValueError):
    """User-actionable image error without including private image content."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _image_bytes(image: ImageContent) -> bytes:
    if not isinstance(image.data, str) or not image.data:
        raise ImageInputError("INVALID_IMAGE", "图片需要非空 Base64 数据")
    if not isinstance(image.mime_type, str) or image.mime_type not in IMAGE_FORMAT_MIMES.values():
        raise ImageInputError("INVALID_IMAGE", "仅支持 PNG、JPEG、GIF 和 WebP 图片")
    if len(image.data) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ImageInputError("IMAGE_TOO_LARGE", "单张图片不能超过 5 MiB")
    try:
        raw = base64.b64decode(image.data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageInputError("INVALID_IMAGE", "图片 Base64 数据无效") from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ImageInputError("IMAGE_TOO_LARGE", "图片不能为空，且单张不能超过 5 MiB")
    if base64.b64encode(raw).decode("ascii") != image.data:
        raise ImageInputError("INVALID_IMAGE", "图片需要标准 Base64 编码，不能包含多余填充或非规范数据")
    return raw


def _check_frame_budget(width: int, height: int, total_pixels: int, frame_count: int) -> None:
    if frame_count > MAX_IMAGE_FRAMES:
        raise ImageInputError("IMAGE_FRAMES_EXCEEDED", "动画图片不能超过 64 帧；请截取静态图片或缩短动画")
    if (width < 1 or height < 1 or max(width, height) > MAX_IMAGE_DIMENSION
            or total_pixels > MAX_IMAGE_PIXELS):
        raise ImageInputError(
            "IMAGE_DIMENSIONS_EXCEEDED",
            "图片每边不能超过 8192 像素，且所有帧累计不能超过 1600 万像素；请裁剪或分段上传",
        )


def _preflight_gif(raw: bytes) -> None:
    """Bound GIF canvases before Pillow seek can allocate disposal buffers.

    Scan only descriptors and bounded byte subblocks, never LZW pixels or
    n_frames. The input has already passed the 5 MiB encoded-file limit.
    """
    if not raw.startswith((b"GIF87a", b"GIF89a")):
        return

    def require(end: int) -> None:
        if end > len(raw):
            raise ImageInputError("INVALID_IMAGE", "GIF 图片已损坏或截断")

    def skip_subblocks(position: int) -> int:
        while True:
            require(position + 1)
            size = raw[position]
            position += 1
            require(position + size)
            if not size:
                return position
            position += size

    require(13)
    canvas_width = int.from_bytes(raw[6:8], "little")
    canvas_height = int.from_bytes(raw[8:10], "little")
    _check_frame_budget(canvas_width, canvas_height, canvas_width * canvas_height, 0)
    position = 13
    if raw[10] & 0x80:  # Global color table.
        position += 3 * (1 << ((raw[10] & 0x07) + 1))
    require(position)
    frame_count = total_pixels = 0
    while True:
        require(position + 1)
        marker = raw[position]
        position += 1
        if marker == 0x3B:  # Trailer.
            return
        if marker == 0x00:  # Tolerate padding accepted by GIF readers.
            continue
        if marker == 0x21:  # Extension label, followed by byte subblocks.
            require(position + 1)
            position = skip_subblocks(position + 1)
            continue
        if marker != 0x2C:
            raise ImageInputError("INVALID_IMAGE", "GIF 图片包含无效数据块")
        require(position + 9)
        left = int.from_bytes(raw[position:position + 2], "little")
        top = int.from_bytes(raw[position + 2:position + 4], "little")
        width = int.from_bytes(raw[position + 4:position + 6], "little")
        height = int.from_bytes(raw[position + 6:position + 8], "little")
        flags = raw[position + 8]
        if width < 1 or height < 1:
            raise ImageInputError("INVALID_IMAGE", "GIF 帧尺寸无效")
        canvas_width = max(canvas_width, left + width)
        canvas_height = max(canvas_height, top + height)
        frame_count += 1
        total_pixels += canvas_width * canvas_height
        _check_frame_budget(canvas_width, canvas_height, total_pixels, frame_count)
        position += 9
        if flags & 0x80:  # Local color table.
            position += 3 * (1 << ((flags & 0x07) + 1))
        require(position + 1)
        position = skip_subblocks(position + 1)  # LZW code-size byte, then data.


def validate_image_content(image: ImageContent) -> ImageContent:
    """Validate actual image bytes; never launch a file helper or external URL."""
    raw = _image_bytes(image)
    digest = hashlib.sha256(raw).hexdigest()
    with _inspection_lock:
        inspected = _inspected_images.get(digest)
        if inspected is not None:
            _inspected_images.move_to_end(digest)
    if inspected is None:
        try:
            _preflight_gif(raw)
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw), formats=list(IMAGE_FORMAT_MIMES)) as decoded:
                    detected_mime = IMAGE_FORMAT_MIMES.get(decoded.format or "")
                    decoded.verify()
                # Do not use n_frames: some decoders scan the entire animation
                # to compute it. Visit at most MAX_IMAGE_FRAMES + 1 headers and
                # check each frame's canvas before decoding its pixels.
                max_width = max_height = total_pixels = frame_count = 0
                with Image.open(io.BytesIO(raw), formats=list(IMAGE_FORMAT_MIMES)) as decoded:
                    while True:
                        width, height = decoded.size
                        total_pixels += width * height
                        _check_frame_budget(width, height, total_pixels, frame_count + 1)
                        max_width = max(max_width, width)
                        max_height = max(max_height, height)
                        decoded.load()
                        frame_count += 1
                        try:
                            decoded.seek(frame_count)
                        except EOFError:
                            break
            if detected_mime is None:
                raise ImageInputError("INVALID_IMAGE", "图片格式不受支持")
            inspected = (detected_mime, max_width, max_height, total_pixels, frame_count)
        except ImageInputError:
            raise
        except (OSError, ValueError, SyntaxError, UnidentifiedImageError,
                Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise ImageInputError("INVALID_IMAGE", "图片已损坏或无法安全解码，请重新导出图片") from exc
        with _inspection_lock:
            _inspected_images[digest] = inspected
            _inspected_images.move_to_end(digest)
            while len(_inspected_images) > 128:
                _inspected_images.popitem(last=False)
    if inspected[0] != image.mime_type:
        raise ImageInputError("INVALID_IMAGE", "图片声明格式与实际内容不一致")
    if (max(inspected[1:3]) > MAX_IMAGE_DIMENSION
            or inspected[3] > MAX_IMAGE_PIXELS):
        raise ImageInputError("IMAGE_DIMENSIONS_EXCEEDED", "图片尺寸超限；请裁剪或分段上传")
    if inspected[4] > MAX_IMAGE_FRAMES:
        raise ImageInputError("IMAGE_FRAMES_EXCEEDED", "动画图片帧数超限；请截取静态图片或缩短动画")
    return image


def validate_image_messages(messages: Iterable[Any], model: Model | None) -> None:
    """Gate every model request, including images from persisted/tool messages."""
    count = 0
    total_bytes = 0
    for message in messages:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, ImageContent):
                continue
            if model is None or "image" not in model.input:
                raise ImageInputError(
                    "IMAGE_INPUT_UNSUPPORTED",
                    "当前模型不支持图片输入；请切换支持图片的模型，或新建纯文本会话",
                )
            validate_image_content(block)
            count += 1
            # Strict validation above guarantees standard padded Base64.
            padding = len(block.data) - len(block.data.rstrip("="))
            total_bytes += len(block.data) // 4 * 3 - padding
            if count > MAX_REQUEST_IMAGES or total_bytes > MAX_REQUEST_IMAGE_BYTES:
                raise ImageInputError(
                    "IMAGE_REQUEST_TOO_LARGE",
                    "当前上下文图片过多（最多 24 张、合计 24 MiB）；请先压缩上下文或新建会话",
                )


def validate_image_request_size(params: dict[str, Any]) -> None:
    """Bound the complete Responses JSON body, including text and tool schemas."""
    def has_image(value: Any) -> bool:
        if isinstance(value, dict):
            return value.get("type") == "input_image" or any(has_image(item) for item in value.values())
        return isinstance(value, list) and any(has_image(item) for item in value)

    # ASCII JSON has equal character and UTF-8 byte lengths; avoid another full copy.
    if has_image(params.get("input")) and len(
        json.dumps(params, ensure_ascii=True, separators=(",", ":"))
    ) > MAX_IMAGE_REQUEST_BODY_BYTES:
        raise ImageInputError(
            "IMAGE_REQUEST_TOO_LARGE",
            "包含历史图片的完整请求超过 40 MiB；请先压缩上下文或新建会话",
        )
