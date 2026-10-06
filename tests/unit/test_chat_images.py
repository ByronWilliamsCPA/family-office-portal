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
    with pytest.raises(ImageError, match="not a picture"):
        prepare_image(_image_bytes((10, 10), fmt="TIFF", mode="RGB"))


def test_too_many_pixels_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A picture over the pixel cap is refused before its pixels load."""
    monkeypatch.setattr(images, "MAX_SOURCE_PIXELS", 99)
    with pytest.raises(ImageError, match="too many pixels"):
        prepare_image(_image_bytes((10, 10)))


def test_truncated_image_is_rejected() -> None:
    """A cut-off file is a plain error, not a crash."""
    data = _image_bytes((300, 300), fmt="JPEG", mode="RGB")
    with pytest.raises(ImageError, match="not a picture"):
        prepare_image(data[:200])
