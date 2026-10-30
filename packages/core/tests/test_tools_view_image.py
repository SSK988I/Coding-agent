"""Local image reads preserve pixels and reject unsafe/unsupported payloads."""
from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest

from agent_core import ViewImageTool
from agent_llm import ImageContent, TextContent
from agent_llm.image_utils import ImageInputError, MAX_IMAGE_BYTES


def _image_bytes(format: str = "PNG", size: tuple[int, int] = (8, 6)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (25, 70, 120)).save(output, format=format)
    return output.getvalue()


@pytest.mark.parametrize(
    ("format", "mime_type"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp")],
)
def test_view_image_returns_actual_image_content(
    tmp_path: Path, format: str, mime_type: str,
) -> None:
    raw = _image_bytes(format)
    # Extension intentionally disagrees: trust the image header, not the path.
    (tmp_path / "picture.txt").write_bytes(raw)
    tool = ViewImageTool(cwd=str(tmp_path))
    result = asyncio.run(tool.execute("view-1", {"path": "picture.txt"}))

    assert isinstance(result.content[0], TextContent)
    image = result.content[1]
    assert isinstance(image, ImageContent)
    assert image.mime_type == mime_type
    assert base64.b64decode(image.data) == raw
    assert result.details["path"] == str((tmp_path / "picture.txt").resolve())
    assert result.details["sizeBytes"] == len(raw)
    assert not result.terminate


def test_view_image_accepts_absolute_path(tmp_path: Path) -> None:
    path = tmp_path / "image.png"
    path.write_bytes(_image_bytes())
    result = asyncio.run(ViewImageTool(cwd=str(tmp_path / "unused")).execute(
        "view-1", {"path": str(path)},
    ))
    assert isinstance(result.content[1], ImageContent)


@pytest.mark.parametrize("format", ["GIF", "WEBP"])
def test_view_image_preserves_supported_animation(tmp_path: Path, format: str) -> None:
    output = BytesIO()
    frames = [Image.new("RGB", (2, 3), color) for color in ("red", "blue")]
    frames[0].save(output, format=format, save_all=True, append_images=frames[1:], duration=40, loop=0)
    raw = output.getvalue()
    (tmp_path / "animation.txt").write_bytes(raw)
    result = asyncio.run(ViewImageTool(cwd=str(tmp_path)).execute("view", {"path": "animation.txt"}))
    image = result.content[1]
    assert isinstance(image, ImageContent)
    assert image.mime_type == f"image/{format.lower()}"
    assert base64.b64decode(image.data) == raw


@pytest.mark.parametrize("raw", [b"", b"not an image", b"\x89PNG\r\n\x1a\n"])
def test_view_image_rejects_invalid_files(tmp_path: Path, raw: bytes) -> None:
    (tmp_path / "fake.png").write_bytes(raw)
    with pytest.raises(ValueError):
        asyncio.run(ViewImageTool(cwd=str(tmp_path)).execute("bad", {"path": "fake.png"}))


def test_view_image_rejects_unsupported_image_format(tmp_path: Path) -> None:
    (tmp_path / "picture.bmp").write_bytes(_image_bytes("BMP"))
    with pytest.raises(ValueError):
        asyncio.run(ViewImageTool(cwd=str(tmp_path)).execute("bad", {"path": "picture.bmp"}))


def test_view_image_rejects_truncated_image(tmp_path: Path) -> None:
    (tmp_path / "broken.png").write_bytes(_image_bytes()[:40])
    with pytest.raises(ValueError):
        asyncio.run(ViewImageTool(cwd=str(tmp_path)).execute("bad", {"path": "broken.png"}))


def test_view_image_rejects_oversized_file_before_decoding(tmp_path: Path) -> None:
    (tmp_path / "large.png").write_bytes(b"x" * (MAX_IMAGE_BYTES + 1))
    with pytest.raises(ValueError, match="5 MiB"):
        asyncio.run(ViewImageTool(cwd=str(tmp_path)).execute("bad", {"path": "large.png"}))


def test_view_image_rejects_excessive_dimensions(tmp_path: Path) -> None:
    (tmp_path / "wide.png").write_bytes(_image_bytes(size=(8193, 1)))
    with pytest.raises(ValueError):
        asyncio.run(ViewImageTool(cwd=str(tmp_path)).execute("bad", {"path": "wide.png"}))


def test_view_image_webp_canvas_is_bounded_before_mime_detection(tmp_path: Path, monkeypatch) -> None:
    from PIL import WebPImagePlugin

    output = BytesIO()
    frames = [Image.new("RGB", (2, 3), color) for color in ("red", "blue")]
    frames[0].save(output, format="WEBP", save_all=True, append_images=frames[1:], duration=40, loop=0)
    raw = bytearray(output.getvalue())
    assert raw[12:16] == b"VP8X"
    raw[24:27] = (8000 - 1).to_bytes(3, "little")
    raw[27:30] = (3000 - 1).to_bytes(3, "little")
    assert len(raw) < 1024
    (tmp_path / "unsafe.txt").write_bytes(raw)
    monkeypatch.setattr(WebPImagePlugin._webp, "WebPAnimDecoder",
                        lambda *_args: pytest.fail("unsafe WebP reached native MIME detection"))
    with pytest.raises(ImageInputError) as error:
        asyncio.run(ViewImageTool(cwd=str(tmp_path)).execute("bad", {"path": "unsafe.txt"}))
    assert error.value.code == "IMAGE_DIMENSIONS_EXCEEDED"


def test_view_image_rejects_non_regular_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="regular"):
        asyncio.run(ViewImageTool(cwd=str(tmp_path)).execute("bad", {"path": "."}))


def test_view_image_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        asyncio.run(ViewImageTool(cwd=str(tmp_path)).execute("bad", {"path": "missing.png"}))
