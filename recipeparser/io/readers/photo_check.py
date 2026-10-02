"""Whether a book image looks like a photograph of a dish (Fix Roadmap F-203).

EpubReader and PdfReader offer the model every image on a page over
``MIN_PHOTO_BYTES`` as a candidate photo. Byte size let through a blank white
page crop and a black ornament, both well over it, and the model can name one
as the recipe's photo when it is the only marker on the page. This looks at
the pixels instead.
"""
from __future__ import annotations

import io
import logging
from collections import Counter
from typing import Any, List, Optional

from PIL import Image

from recipeparser.config import (
    LINE_ART_EXTREMES_SHARE,
    LINE_ART_TOP_TWO_SHARE,
    MAX_PHOTO_ASPECT,
    MIN_PHOTO_COLOURS,
    MIN_PHOTO_CONTRAST,
    MIN_PHOTO_EDGE_PX,
)

log = logging.getLogger(__name__)

#: The side an image is reduced to before its colours are counted: enough to see
#: a photograph's spread, small enough to cost nothing per image.
_SAMPLE_EDGE = 64
#: Colour bins per channel when counting colours (8 levels, 512 bins).
_LEVELS_SHIFT = 5
#: A colour holds "a real share" of the image at this fraction of its pixels.
_REAL_SHARE = 0.001


def _flattened(img: "Image.Image") -> List[Any]:
    # getdata() is deprecated from Pillow 12 (removed in 14); its replacement is new in 12.
    getter = getattr(img, "get_flattened_data", None) or img.getdata
    return list(getter())


def photo_refusal(data: bytes) -> Optional[str]:
    """Why a book image is not a photograph of a dish, or None when it may be one.

    Only an image this can decode is judged: bytes Pillow cannot read (JPEG 2000 without
    its codec, say) are kept, as they always were, rather than lose a photograph
    to a missing codec. Transparency is laid on white, as the app shows it.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            if min(width, height) < MIN_PHOTO_EDGE_PX:
                return f"too small ({width}x{height} px)"
            if max(width, height) / min(width, height) > MAX_PHOTO_ASPECT:
                return f"too thin to be a photograph ({width}x{height} px)"
            img.draft("RGB", (_SAMPLE_EDGE * 4, _SAMPLE_EDGE * 4))
            rgba = img.convert("RGBA")
    except Exception as exc:  # noqa: BLE001 -- any decoder failure means "cannot judge"
        log.debug("photo_refusal: could not decode an image (%s); keeping it.", exc)
        return None

    flat = Image.new("RGB", rgba.size, (255, 255, 255))
    flat.paste(rgba, mask=rgba.getchannel("A"))
    sample = flat.resize((_SAMPLE_EDGE, _SAMPLE_EDGE))

    pixels = list(_flattened(sample))
    luminance = list(_flattened(sample.convert("L")))
    mean = sum(luminance) / len(luminance)
    spread = (sum((v - mean) ** 2 for v in luminance) / len(luminance)) ** 0.5
    if spread < MIN_PHOTO_CONTRAST:
        return f"blank or nearly uniform (contrast {spread:.1f})"

    counts = Counter((r >> _LEVELS_SHIFT, g >> _LEVELS_SHIFT, b >> _LEVELS_SHIFT) for r, g, b in pixels)
    total = len(luminance)
    top_two = sum(n for _, n in counts.most_common(2)) / total
    colours = sum(1 for n in counts.values() if n / total >= _REAL_SHARE)
    extremes = sum(1 for v in luminance if v < 64 or v >= 192) / total
    if extremes >= LINE_ART_EXTREMES_SHARE and top_two >= LINE_ART_TOP_TWO_SHARE and colours < MIN_PHOTO_COLOURS:
        return f"line art, not a photograph ({colours} colours, {top_two:.0%} in two)"
    return None


def keep_as_photo(name: str, data: bytes) -> bool:
    """True when a book image should be offered to the model as a photo; logs why not."""
    reason = photo_refusal(data)
    if reason is None:
        return True
    log.info("Skipping image '%s': %s.", name, reason)
    return False
