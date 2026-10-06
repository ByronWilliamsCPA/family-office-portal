# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Prepare one uploaded image for the chat model.

The model accepts at most one image per request, and an image whose long
edge is over 1024 px breaks the 30-second answer target. Every image is
decoded, turned upright from its EXIF orientation, converted to RGB, shrunk
so its long edge is at most 1024 px, and re-encoded as JPEG. Re-encoding
also drops any metadata the original carried.

Image handling uses Pillow (MIT-CMU license).
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_EDGE_PX = 1024
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# Refuse anything larger before decoding pixels (a decompression bomb).
MAX_SOURCE_PIXELS = 40_000_000
ACCEPTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "GIF", "BMP"})
_JPEG_QUALITY = 85


class ImageError(ValueError):
    """The upload is not an image the portal can send. Messages are plain."""


@dataclass(frozen=True)
class PreparedImage:
    """An image ready to send.

    Attributes:
        data (bytes): JPEG bytes.
        width (int): Width in pixels.
        height (int): Height in pixels.
    """

    data: bytes
    width: int
    height: int

    def data_url(self) -> str:
        """Return the image as a ``data:`` URL for the request body.

        Returns:
            str: ``data:image/jpeg;base64,...``.
        """
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"


def prepare_image(raw: bytes) -> PreparedImage:
    """Decode, shrink and re-encode one uploaded image.

    Blocking (CPU work); async callers run it in a worker thread.

    Args:
        raw (bytes): The uploaded file.

    Returns:
        PreparedImage: JPEG with its long edge at most 1024 px.

    Raises:
        ImageError: If the file is empty, too large, not a supported image,
            or has too many pixels.
    """
    if not raw:
        msg = "The picture was empty."
        raise ImageError(msg)
    if len(raw) > MAX_UPLOAD_BYTES:
        msg = "The picture is too large. Please use one under 10 MB."
        raise ImageError(msg)
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in ACCEPTED_FORMATS:
                msg = "That file is not a picture the portal can read."
                raise ImageError(msg)
            width, height = source.size
            if width * height > MAX_SOURCE_PIXELS:
                msg = "The picture has too many pixels. Please use a smaller one."
                raise ImageError(msg)
            upright = ImageOps.exif_transpose(source).convert("RGB")
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        msg = "That file is not a picture the portal can read."
        raise ImageError(msg) from exc
    upright.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    upright.save(out, format="JPEG", quality=_JPEG_QUALITY)
    return PreparedImage(
        data=out.getvalue(), width=upright.width, height=upright.height
    )
