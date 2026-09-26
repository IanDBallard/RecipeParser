"""Bring a picture to Cayenne's stored size before it is kept.

The app draws a recipe's picture at 160 px in the library and editor and at 400 in the kitchen, so
Cayenne's editor already shrinks a picture a cook chooses to at most 1600 px on its long edge, as a
JPEG at quality 85 (cayenne-web `domain/recipeImage.ts`, `services/pictureScaler.ts`). Everything
the ingestor stores — a Paprika library's embedded photos, a photo imported as a recipe, a page's
hero image — arrives at whatever size the source had, 3–8 MB for a phone photo. This applies the
same rule here, so every stored picture is the same size whoever supplied it, and a library does
not spend the storage quota and every device's download on pixels nothing shows.

The numbers are Cayenne's; change them there and here together.
"""
from __future__ import annotations

import io
import logging
from typing import Tuple

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

#: The longest edge a stored picture keeps (Cayenne MAX_PICTURE_EDGE).
MAX_PICTURE_EDGE = 1600
#: JPEG quality for a re-encoded picture (Cayenne PICTURE_QUALITY, 0.85).
PICTURE_QUALITY = 85
#: A picture at or under this, and inside the edge, is kept as it is (Cayenne PICTURE_AS_IS_BYTES):
#: re-encoding a small PNG would lose its transparency and add a generation of loss for nothing.
PICTURE_AS_IS_BYTES = 1_000_000


def _scaled_size(width: int, height: int) -> Tuple[int, int]:
    """Cayenne's scaledSize: inside the edge, unchanged; else the edge, never below one pixel."""
    longest = max(width, height)
    if longest <= MAX_PICTURE_EDGE or longest == 0:
        return width, height
    ratio = MAX_PICTURE_EDGE / longest
    return max(1, round(width * ratio)), max(1, round(height * ratio))


def scale_picture(data: bytes, content_type: str) -> Tuple[bytes, str]:
    """The picture to store and its content type.

    Unchanged when it is small and inside the edge, or when it cannot be decoded (a picture is
    never lost to this step); otherwise upright, at most MAX_PICTURE_EDGE on its long edge, flattened
    onto white and re-encoded as a JPEG.
    """
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            if getattr(source, "is_animated", False):
                return data, content_type  # an animated GIF would lose its frames
            width, height = source.size
            if len(data) <= PICTURE_AS_IS_BYTES and max(width, height) <= MAX_PICTURE_EDGE:
                return data, content_type
            upright = ImageOps.exif_transpose(source)
            if upright.mode in ("RGBA", "LA") or (upright.mode == "P" and "transparency" in upright.info):
                rgba = upright.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
            else:
                flat = upright.convert("RGB")
            scaled = flat.resize(_scaled_size(*flat.size), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            scaled.save(out, "JPEG", quality=PICTURE_QUALITY, optimize=True)
    except Exception:  # noqa: BLE001 — not a picture Pillow can read: keep the bytes as they came
        log.info("picture_scale: could not decode a %s picture — storing it as it is.", content_type)
        return data, content_type
    return out.getvalue(), "image/jpeg"
