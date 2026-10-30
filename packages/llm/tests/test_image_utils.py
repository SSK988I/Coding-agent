from __future__ import annotations

import base64
import hashlib
import json
from io import BytesIO

from PIL import Image
import pytest

from agent_llm import ImageContent, UserMessage, ToolResultMessage
from agent_llm import image_utils
from agent_llm.image_utils import ImageInputError, validate_image_content, validate_image_messages
from agent_llm.providers.deepseek_models import DEEPSEEK_MODELS


def image_content(format: str = "PNG", size: tuple[int, int] = (2, 3)) -> ImageContent:
    buffer = BytesIO()
    Image.new("RGB", size, "red").save(buffer, format=format)
    return ImageContent(data=base64.b64encode(buffer.getvalue()).decode("ascii"),
                        mime_type=image_utils.IMAGE_FORMAT_MIMES[format])


@pytest.mark.parametrize("format", ["PNG", "JPEG", "GIF", "WEBP"])
def test_supported_image_content_is_validated_without_rewriting(format: str) -> None:
    image = image_content(format)
    assert validate_image_content(image) is image


@pytest.mark.parametrize("data", ["", "!bad!", "data:image/png;base64,AAAA", "中文", "YWJj=", "YWJj===="])
def test_invalid_or_noncanonical_base64_is_rejected(data: str) -> None:
    with pytest.raises(ImageInputError):
        validate_image_content(ImageContent(data=data, mime_type="image/png"))


def test_mime_spoofing_and_broken_image_bytes_are_rejected() -> None:
    image = image_content()
    image.mime_type = "image/jpeg"
    with pytest.raises(ImageInputError, match="实际内容"):
        validate_image_content(image)
    with pytest.raises(ImageInputError, match="损坏"):
        validate_image_content(ImageContent(data=base64.b64encode(b"not a png").decode(), mime_type="image/png"))


def test_size_and_pixel_limits_are_enforced_before_decoding(monkeypatch) -> None:
    image = image_content()
    monkeypatch.setattr(image_utils, "MAX_IMAGE_BYTES", 1)
    with pytest.raises(ImageInputError) as error:
        validate_image_content(image)
    assert error.value.code == "IMAGE_TOO_LARGE"


def test_cached_images_still_obey_current_dimension_limits(monkeypatch) -> None:
    image = image_content(size=(4, 5))
    validate_image_content(image)
    monkeypatch.setattr(image_utils, "MAX_IMAGE_DIMENSION", 3)
    with pytest.raises(ImageInputError) as error:
        validate_image_content(image)
    assert error.value.code == "IMAGE_DIMENSIONS_EXCEEDED"


def test_real_pixel_decode_catches_truncated_images() -> None:
    image = image_content("JPEG", (40, 40))
    data = base64.b64decode(image.data)
    image.data = base64.b64encode(data[:-10]).decode()
    with pytest.raises(ImageInputError, match="损坏"):
        validate_image_content(image)


def test_request_budget_counts_history_and_tool_images(monkeypatch) -> None:
    image = image_content()
    messages = [UserMessage(content=[image]), ToolResultMessage(content=[image])]
    validate_image_messages(messages, DEEPSEEK_MODELS["deepseek-flash"])
    monkeypatch.setattr(image_utils, "MAX_REQUEST_IMAGES", 1)
    with pytest.raises(ImageInputError) as error:
        validate_image_messages(messages, DEEPSEEK_MODELS["deepseek-flash"])
    assert error.value.code == "IMAGE_REQUEST_TOO_LARGE"


def test_request_budget_counts_decoded_bytes(monkeypatch) -> None:
    image = image_content()
    monkeypatch.setattr(image_utils, "MAX_REQUEST_IMAGE_BYTES", len(base64.b64decode(image.data)))
    with pytest.raises(ImageInputError):
        validate_image_messages([UserMessage(content=[image, image])], DEEPSEEK_MODELS["deepseek-flash"])


def test_history_images_are_not_silently_dropped_for_text_models() -> None:
    with pytest.raises(ImageInputError) as error:
        validate_image_messages([UserMessage(content=[image_content()])], DEEPSEEK_MODELS["deepseek-v4-pro"])
    assert error.value.code == "IMAGE_INPUT_UNSUPPORTED"


def test_complete_request_body_includes_text_history(monkeypatch) -> None:
    monkeypatch.setattr(image_utils, "MAX_IMAGE_REQUEST_BODY_BYTES", 100)
    params = {"input": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}],
              "instructions": "x" * 100}
    with pytest.raises(ImageInputError) as error:
        image_utils.validate_image_request_size(params)
    assert error.value.code == "IMAGE_REQUEST_TOO_LARGE"
    # The new image-specific budget does not change pure text requests.
    image_utils.validate_image_request_size({"input": "x" * 1000})


@pytest.mark.parametrize("text", ["plain", "中文", "风景 😀", 'line\n"quoted" \\ path\t', "\ud800"])
def test_image_request_body_limit_preserves_exact_utf8_boundary(text: str, monkeypatch) -> None:
    params = {
        "input": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}],
        "instructions": text,
    }
    serialized = json.dumps(params, ensure_ascii=True, separators=(",", ":"))
    byte_size = len(serialized.encode("utf-8"))
    assert serialized.isascii()
    assert len(serialized) == byte_size

    monkeypatch.setattr(image_utils, "MAX_IMAGE_REQUEST_BODY_BYTES", byte_size)
    image_utils.validate_image_request_size(params)

    monkeypatch.setattr(image_utils, "MAX_IMAGE_REQUEST_BODY_BYTES", byte_size - 1)
    with pytest.raises(ImageInputError) as error:
        image_utils.validate_image_request_size(params)
    assert error.value.code == "IMAGE_REQUEST_TOO_LARGE"


def test_text_only_request_skips_image_body_serialization(monkeypatch) -> None:
    def unexpected_serialization(*args, **kwargs):
        raise AssertionError("Text-only requests do not need the image body budget")

    monkeypatch.setattr(image_utils, "MAX_IMAGE_REQUEST_BODY_BYTES", 1)
    monkeypatch.setattr(image_utils.json, "dumps", unexpected_serialization)
    image_utils.validate_image_request_size({
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "中文 😀"}]}],
        "instructions": "x" * 1000,
    })


def animated_image_content(
    format: str = "GIF", size: tuple[int, int] = (2, 3), frames: int = 2,
) -> ImageContent:
    images = [Image.new("RGB", size, (index, 255 - index, 40)) for index in range(frames)]
    output = BytesIO()
    images[0].save(output, format=format, save_all=True, append_images=images[1:],
                   duration=40, loop=0, optimize=False, lossless=True)
    return ImageContent(data=base64.b64encode(output.getvalue()).decode(),
                        mime_type=image_utils.IMAGE_FORMAT_MIMES[format])


@pytest.mark.parametrize("format", ["GIF", "WEBP"])
def test_small_animations_are_validated_without_rewriting(format: str) -> None:
    image = animated_image_content(format)
    assert validate_image_content(image) is image
    metadata = image_utils._inspected_images[hashlib.sha256(base64.b64decode(image.data)).hexdigest()]
    assert metadata == (image.mime_type, 2, 3, 12, 2)


@pytest.mark.parametrize("size", [(8000, 3000), (8193, 1), (3000, 3000)])
def test_webp_canvas_is_bounded_before_native_decoder(size, monkeypatch) -> None:
    from PIL import WebPImagePlugin

    raw = bytearray(base64.b64decode(animated_image_content("WEBP").data))
    assert raw[12:16] == b"VP8X"
    raw[24:27] = (size[0] - 1).to_bytes(3, "little")
    raw[27:30] = (size[1] - 1).to_bytes(3, "little")
    assert len(raw) < 1024
    image = ImageContent(data=base64.b64encode(raw).decode(), mime_type="image/webp")
    monkeypatch.setattr(WebPImagePlugin._webp, "WebPAnimDecoder",
                        lambda *_args: pytest.fail("unsafe WebP reached native decoder"))
    with pytest.raises(ImageInputError) as error:
        validate_image_content(image)
    assert error.value.code == "IMAGE_DIMENSIONS_EXCEEDED"


@pytest.mark.parametrize("lossless", [False, True])
def test_static_webp_bitstream_dimensions_are_bounded_before_native_decoder(lossless, monkeypatch) -> None:
    from PIL import WebPImagePlugin

    output = BytesIO()
    Image.new("RGB", (2, 3), "red").save(output, format="WEBP", lossless=lossless)
    raw = bytearray(output.getvalue())
    if lossless:
        assert raw[12:16] == b"VP8L"
        raw[21:25] = ((8000 - 1) | ((3000 - 1) << 14)).to_bytes(4, "little")
    else:
        assert raw[12:16] == b"VP8 "
        raw[26:28] = (8000).to_bytes(2, "little")
        raw[28:30] = (3000).to_bytes(2, "little")
    image = ImageContent(data=base64.b64encode(raw).decode(), mime_type="image/webp")
    monkeypatch.setattr(WebPImagePlugin._webp, "WebPAnimDecoder",
                        lambda *_args: pytest.fail("unsafe static WebP reached native decoder"))
    with pytest.raises(ImageInputError) as error:
        validate_image_content(image)
    assert error.value.code == "IMAGE_DIMENSIONS_EXCEEDED"


def test_webp_frame_count_is_bounded_before_native_decoder(monkeypatch) -> None:
    from PIL import WebPImagePlugin

    image = animated_image_content("WEBP", frames=65)
    with monkeypatch.context() as patch:
        patch.setattr(WebPImagePlugin._webp, "WebPAnimDecoder",
                      lambda *_args: pytest.fail("excessive WebP frames reached native decoder"))
        with pytest.raises(ImageInputError) as error:
            validate_image_content(image)
        assert error.value.code == "IMAGE_FRAMES_EXCEEDED"
    assert validate_image_content(animated_image_content("WEBP", frames=64))


@pytest.mark.parametrize("corruption", ["truncated", "chunk_bounds", "short_vp8x", "short_anmf"])
def test_webp_invalid_headers_are_rejected_before_native_decoder(corruption, monkeypatch) -> None:
    from PIL import WebPImagePlugin

    raw = bytearray(base64.b64decode(animated_image_content("WEBP").data))
    if corruption == "truncated":
        del raw[-1]
    elif corruption == "chunk_bounds":
        raw[16:20] = (0xFFFFFFF0).to_bytes(4, "little")
    elif corruption == "short_vp8x":
        raw[16:20] = (9).to_bytes(4, "little")
    else:
        frame = raw.index(b"ANMF")
        raw[frame + 4:frame + 8] = (15).to_bytes(4, "little")
    image = ImageContent(data=base64.b64encode(raw).decode(), mime_type="image/webp")
    monkeypatch.setattr(WebPImagePlugin._webp, "WebPAnimDecoder",
                        lambda *_args: pytest.fail("invalid WebP reached native decoder"))
    with pytest.raises(ImageInputError) as error:
        validate_image_content(image)
    assert error.value.code == "INVALID_IMAGE"


@pytest.mark.parametrize("lossless", [False, True])
def test_webp_preflight_accepts_static_metadata_and_padded_unknown_chunks(lossless) -> None:
    output = BytesIO()
    Image.new("RGB", (3, 4), "red").save(output, format="WEBP", lossless=lossless, exif=b"metadata")
    raw = bytearray(output.getvalue())
    raw.extend(b"TEST\x03\x00\x00\x00abc\x00")
    raw[4:8] = (len(raw) - 8).to_bytes(4, "little")
    image = ImageContent(data=base64.b64encode(raw).decode(), mime_type="image/webp")
    assert validate_image_content(image) is image


def expanding_gif(size: tuple[int, int], offset: tuple[int, int] = (0, 0)) -> bytes:
    palette = [255, 0, 0, 0, 0, 255] + [0] * 762
    encoded = []
    for frame_size, color in [((2, 2), 0), (size, 1)]:
        frame = Image.new("P", frame_size, color)
        frame.putpalette(palette)
        output = BytesIO()
        frame.save(output, format="GIF", optimize=False)
        encoded.append(output.getvalue())
    # Fixed GIF header (13 bytes), then the complete 256-entry global palette.
    second_descriptor = 13 + 256 * 3
    assert encoded[1][second_descriptor:second_descriptor + 1] == b","
    second_frame = bytearray(encoded[1][second_descriptor:])
    second_frame[1:3] = offset[0].to_bytes(2, "little")
    second_frame[3:5] = offset[1].to_bytes(2, "little")
    # Disposal 2 makes Pillow allocate a canvas during seek(), before load().
    disposal_extension = b"!\xf9\x04\x08\x00\x00\x00\x00"
    return encoded[0][:-1] + disposal_extension + second_frame


def test_gif_later_frame_cannot_expand_beyond_dimension_budget() -> None:
    # Pillow expands the canvas when seeking, so checking only frame 0 would
    # accept this tiny file despite its 8193-pixel-wide second frame.
    raw = expanding_gif((8193, 1))
    with Image.open(BytesIO(raw)) as decoded:
        assert decoded.size == (2, 2)
        decoded.seek(1)
        assert decoded.size == (8193, 2)
    image = ImageContent(data=base64.b64encode(raw).decode(), mime_type="image/gif")
    with pytest.raises(ImageInputError) as error:
        validate_image_content(image)
    assert error.value.code == "IMAGE_DIMENSIONS_EXCEEDED"


def test_gif_preflight_accepts_extensions_and_local_color_tables() -> None:
    raw = bytearray(expanding_gif((3, 3)))
    disposal_extension = b"!\xf9\x04\x08\x00\x00\x00\x00"
    descriptor = raw.index(disposal_extension) + len(disposal_extension)
    raw[descriptor + 9] |= 0x87  # Local 256-entry palette follows descriptor.
    raw[descriptor + 10:descriptor + 10] = raw[13:781]
    comment = b"A legal GIF comment. " * 20
    extension = b"!\xfe"
    for start in range(0, len(comment), 255):
        chunk = comment[start:start + 255]
        extension += bytes([len(chunk)]) + chunk
    extension += b"\x00"
    raw[781:781] = extension
    image = ImageContent(data=base64.b64encode(raw).decode(), mime_type="image/gif")
    assert validate_image_content(image) is image


@pytest.mark.parametrize("size,offset", [
    ((4097, 4096), (0, 0)), ((5, 2), (8190, 0)), ((2, 5), (0, 8190)),
])
def test_gif_disposal_canvas_is_rejected_before_pillow_opens_it(size, offset, monkeypatch) -> None:
    raw = expanding_gif(size, offset)
    image = ImageContent(data=base64.b64encode(raw).decode(), mime_type="image/gif")
    monkeypatch.setattr(Image, "open", lambda *_args, **_kwargs: pytest.fail("unsafe GIF reached Pillow"))
    with pytest.raises(ImageInputError) as error:
        validate_image_content(image)
    assert error.value.code == "IMAGE_DIMENSIONS_EXCEEDED"


def test_animation_frame_count_is_bounded_before_decoding_extra_frame(monkeypatch) -> None:
    from PIL.GifImagePlugin import GifImageFile

    image = animated_image_content(frames=65)
    loaded = []
    original_load = GifImageFile.load

    def record_load(self, *args, **kwargs):
        loaded.append(self.tell())
        return original_load(self, *args, **kwargs)

    monkeypatch.setattr(GifImageFile, "load", record_load)
    with pytest.raises(ImageInputError) as error:
        validate_image_content(image)
    assert error.value.code == "IMAGE_FRAMES_EXCEEDED"
    assert not loaded  # Header preflight rejects before any Pillow decode.
    assert validate_image_content(animated_image_content(frames=64))
    assert loaded and max(loaded) < 64


def test_animation_validation_never_requests_unbounded_frame_scan(monkeypatch) -> None:
    from PIL.GifImagePlugin import GifImageFile

    image = animated_image_content(size=(9, 4))

    def unexpected_frame_scan(self):
        raise AssertionError("Do not request n_frames on untrusted animations")

    monkeypatch.setattr(GifImageFile, "n_frames", property(unexpected_frame_scan))
    assert validate_image_content(image) is image


@pytest.mark.parametrize("format", ["GIF", "WEBP"])
def test_animation_cumulative_pixels_are_bounded(format: str, monkeypatch) -> None:
    image = animated_image_content(format, size=(11, 7))
    monkeypatch.setattr(image_utils, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(ImageInputError) as error:
        validate_image_content(image)
    assert error.value.code == "IMAGE_DIMENSIONS_EXCEEDED"


@pytest.mark.parametrize("limit", ["MAX_IMAGE_PIXELS", "MAX_IMAGE_FRAMES"])
def test_cached_animation_obeys_updated_limits(limit: str, monkeypatch) -> None:
    image = animated_image_content(size=(3, 5))
    validate_image_content(image)
    monkeypatch.setattr(image_utils, limit, 20 if limit == "MAX_IMAGE_PIXELS" else 1)
    with pytest.raises(ImageInputError) as error:
        validate_image_content(image)
    assert error.value.code == (
        "IMAGE_DIMENSIONS_EXCEEDED" if limit == "MAX_IMAGE_PIXELS" else "IMAGE_FRAMES_EXCEEDED"
    )
