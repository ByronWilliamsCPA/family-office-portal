# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Prepare one uploaded image for the chat model.

The model accepts at most one image per request, and an image whose long
edge is over 1024 px breaks the 30-second answer target. Every image is
decoded, turned upright from its EXIF orientation, converted to RGB, shrunk
so its long edge is at most 1024 px, and re-encoded as JPEG. Re-encoding
also drops any metadata the original carried.

Image handling uses Pillow (MIT-CMU license).

#EDGE: memory: a 40-megapixel upload decodes to about 120 MB of RGB pixels,
and the conversions hold a few transient copies, so one request can need
several hundred MB and two run at once. #VERIFY the portal container's memory
limit covers two concurrent uploads at ``MAX_SOURCE_PIXELS`` before raising
the limits in this module.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from PIL import Image, ImageOps

MAX_EDGE_PX = 1024
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# Refuse anything larger before decoding pixels (a decompression bomb).
MAX_SOURCE_PIXELS = 40_000_000
# Pillow labels a JPEG with a Multi-Picture header (common on phones: a
# preview, depth or gain-map frame) as MPO. Only the first frame is read, so
# it is handled like a plain JPEG. HEIC is not supported: Pillow needs a
# plugin for it, so an iPhone "HEIC" original is refused with the plain
# "not a picture" sentence.
ACCEPTED_FORMATS = frozenset({"JPEG", "MPO", "PNG", "WEBP", "GIF", "BMP"})
_JPEG_QUALITY = 85
# The JPEG opener returns an MPO image for a Multi-Picture file, and Pillow
# does not list MPO as an opener of its own, so it is left out here.
_OPEN_FORMATS = sorted(ACCEPTED_FORMATS - {"MPO"})
MSG_NOT_A_PICTURE = "That file is not a picture the portal can read."


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


def _decode(raw: bytes) -> Image.Image | str:
    """Decode the file to an upright RGB image, or say why it is refused.

    Args:
        raw (bytes): The uploaded file.

    Returns:
        Image.Image | str: The image, or a plain sentence for a file in a
        format the portal does not accept or with too many pixels.
    """
    with Image.open(io.BytesIO(raw), formats=_OPEN_FORMATS) as source:
        if source.format not in ACCEPTED_FORMATS:
            return MSG_NOT_A_PICTURE
        width, height = source.size
        if width * height > MAX_SOURCE_PIXELS:
            return "The picture has too many pixels. Please use a smaller one."
        return ImageOps.exif_transpose(source).convert("RGB")


def prepare_image(raw: bytes) -> PreparedImage:
    """Decode, shrink and re-encode one uploaded image.

    Blocking (CPU work); async callers run it in a worker thread.

    Args:
        raw (bytes): The uploaded file.

    Returns:
        PreparedImage: JPEG with its long edge at most 1024 px.

    Raises:
        ImageError: If the file is empty, too large, not a supported image
            (including a corrupt one), or has too many pixels.
    """
    if not raw:
        msg = "The picture was empty."
        raise ImageError(msg)
    if len(raw) > MAX_UPLOAD_BYTES:
        msg = "The picture is too large. Please use one under 10 MB."
        raise ImageError(msg)
    try:
        decoded = _decode(raw)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        # UnidentifiedImageError is an OSError. Pillow raises SyntaxError and
        # ValueError for some corrupt PNG headers. The deliberate refusals
        # in ``_decode`` are returned, not raised, so this does not catch
        # ``ImageError`` (a ValueError) by accident.
        raise ImageError(MSG_NOT_A_PICTURE) from exc
    if isinstance(decoded, str):
        raise ImageError(decoded)
    upright = decoded
    upright.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    upright.save(out, format="JPEG", quality=_JPEG_QUALITY)
    return PreparedImage(
        data=out.getvalue(), width=upright.width, height=upright.height
    )
