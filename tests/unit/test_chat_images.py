# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for image preparation (app.chat.images)."""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from app.chat import images
from app.chat.images import MAX_EDGE_PX, ImageError, prepare_image


def _image_bytes(size: tuple[int, int], fmt: str = "PNG", mode: str = "RGBA") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, (10, 20, 30, 255) if mode == "RGBA" else 0).save(
        out, format=fmt
    )
    return out.getvalue()


def test_large_image_is_shrunk_to_1024_long_edge() -> None:
    """A 3000 x 1500 picture comes out 1024 x 512, as JPEG."""
    prepared = prepare_image(_image_bytes((3000, 1500)))
    assert (prepared.width, prepared.height) == (MAX_EDGE_PX, 512)
    with Image.open(io.BytesIO(prepared.data)) as out:
        assert out.format == "JPEG"
        assert max(out.size) <= MAX_EDGE_PX


def test_tall_image_is_shrunk_on_its_height() -> None:
    """The long edge may be the height."""
    prepared = prepare_image(_image_bytes((600, 2048), fmt="JPEG", mode="RGB"))
    assert (prepared.width, prepared.height) == (300, MAX_EDGE_PX)


def test_small_image_keeps_its_size() -> None:
    """A picture already under the limit is not enlarged."""
    prepared = prepare_image(_image_bytes((200, 100)))
    assert (prepared.width, prepared.height) == (200, 100)


def test_data_url_is_base64_jpeg() -> None:
    """The data URL decodes back to the JPEG bytes."""
    prepared = prepare_image(_image_bytes((20, 20)))
    url = prepared.data_url()
    prefix = "data:image/jpeg;base64,"
    assert url.startswith(prefix)
    assert base64.b64decode(url[len(prefix) :]) == prepared.data


def test_empty_upload_is_rejected() -> None:
    """An empty file is a plain error."""
    with pytest.raises(ImageError, match="empty"):
        prepare_image(b"")


def test_oversized_upload_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A file over the byte limit is rejected before decoding."""
    monkeypatch.setattr(images, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(ImageError, match="too large"):
        prepare_image(b"x" * 11)


def test_non_image_is_rejected() -> None:
    """Text is not a picture."""
    with pytest.raises(ImageError, match="not a picture"):
        prepare_image(b"not an image at all")


def test_unsupported_format_is_rejected() -> None:
    """A format outside the accepted list is refused."""
    data = _image_bytes((10, 10), fmt="TIFF", mode="RGB")
    with pytest.raises(ImageError, match="not a picture"):
        prepare_image(data)


def test_too_many_pixels_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A picture over the pixel cap is refused before its pixels load."""
    monkeypatch.setattr(images, "MAX_SOURCE_PIXELS", 99)
    data = _image_bytes((10, 10))
    with pytest.raises(ImageError, match="too many pixels"):
        prepare_image(data)


def test_truncated_image_is_rejected() -> None:
    """A cut-off file is a plain error, not a crash."""
    data = _image_bytes((300, 300), fmt="JPEG", mode="RGB")
    with pytest.raises(ImageError, match="not a picture"):
        prepare_image(data[:200])


@pytest.mark.parametrize(
    ("offset", "pillow_error"),
    [(11, ValueError), (36, SyntaxError)],
)
def test_corrupt_png_is_a_plain_error(
    offset: int, pillow_error: type[Exception]
) -> None:
    """Pillow's ValueError and SyntaxError for broken PNGs become plain errors.

    Zeroing byte 11 of a small PNG makes Pillow raise ``ValueError`` ("Truncated
    IHDR chunk"), and byte 36 makes it raise ``SyntaxError`` ("broken PNG
    file"). Both used to escape past the caller as a server error.
    """
    data = bytearray(_image_bytes((30, 30), mode="RGB"))
    data[offset] = 0
    with pytest.raises(pillow_error), Image.open(io.BytesIO(bytes(data))) as raw:
        raw.load()
    with pytest.raises(ImageError, match="not a picture") as info:
        prepare_image(bytes(data))
    assert isinstance(info.value.__cause__, pillow_error)


def _two_frame_mpo() -> bytes:
    first = Image.new("RGB", (40, 30), (200, 10, 10))
    second = Image.new("RGB", (40, 30), (10, 10, 200))
    out = io.BytesIO()
    first.save(out, format="MPO", save_all=True, append_images=[second])
    return out.getvalue()


def test_multi_picture_phone_jpeg_is_accepted() -> None:
    """A JPEG that Pillow labels MPO (a phone photo) is read from its first frame."""
    data = _two_frame_mpo()
    with Image.open(io.BytesIO(data)) as probe:
        assert probe.format == "MPO"
    prepared = prepare_image(data)
    assert (prepared.width, prepared.height) == (40, 30)
    with Image.open(io.BytesIO(prepared.data)) as out:
        assert out.format == "JPEG"
        red, _green, blue = out.convert("RGB").getpixel((20, 15))
        assert red > blue


def test_exif_orientation_is_applied_and_metadata_dropped() -> None:
    """The picture comes out upright, with no EXIF or GPS data left."""
    source = Image.new("RGB", (60, 20), (90, 90, 90))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to display
    exif[0x010F] = "SyntheticMaker"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    out = io.BytesIO()
    source.save(out, format="JPEG", exif=exif)
    prepared = prepare_image(out.getvalue())
    assert (prepared.width, prepared.height) == (20, 60)
    with Image.open(io.BytesIO(prepared.data)) as result:
        assert not result.getexif()
    assert b"SyntheticMaker" not in prepared.data
