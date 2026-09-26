"""The picture scaler: every stored picture at most 1600 px on its long edge (Cayenne's rule)."""
from __future__ import annotations

import io

from PIL import Image

from recipeparser.io.writers.picture_scale import MAX_PICTURE_EDGE, PICTURE_AS_IS_BYTES, scale_picture


def _jpeg(width: int, height: int, *, exif_orientation: int | None = None) -> bytes:
    img = Image.new("RGB", (width, height), (180, 90, 40))
    buf = io.BytesIO()
    if exif_orientation is None:
        img.save(buf, "JPEG", quality=95)
    else:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        img.save(buf, "JPEG", quality=95, exif=exif.tobytes())
    return buf.getvalue()


def _png(width: int, height: int, *, alpha: bool = False) -> bytes:
    img = Image.new("RGBA" if alpha else "RGB", (width, height), (10, 200, 30, 0) if alpha else (10, 200, 30))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _size(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


def test_a_phone_photo_is_scaled_to_the_edge_as_a_jpeg():
    data, content_type = scale_picture(_jpeg(4032, 3024), "image/jpeg")
    assert content_type == "image/jpeg"
    assert _size(data) == (1600, 1200)


def test_a_portrait_photo_is_turned_upright_before_it_is_scaled():
    # Stored landscape with EXIF orientation 6 (rotate 90°): the phone held upright.
    data, _ = scale_picture(_jpeg(4000, 3000, exif_orientation=6), "image/jpeg")
    assert _size(data) == (1200, 1600)


def test_a_small_picture_is_stored_as_it_is():
    original = _png(400, 300)
    assert len(original) <= PICTURE_AS_IS_BYTES
    assert scale_picture(original, "image/png") == (original, "image/png")


def test_a_large_transparent_png_becomes_an_opaque_jpeg_at_the_edge():
    data, content_type = scale_picture(_png(3200, 1600, alpha=True), "image/png")
    assert content_type == "image/jpeg"
    img = Image.open(io.BytesIO(data))
    assert img.size == (MAX_PICTURE_EDGE, 800)
    assert img.mode == "RGB"


def test_bytes_that_are_not_a_picture_are_stored_as_they_are():
    assert scale_picture(b"not a picture", "image/jpeg") == (b"not a picture", "image/jpeg")


def test_a_panorama_keeps_at_least_one_pixel_of_height():
    data, _ = scale_picture(_jpeg(8000, 2), "image/jpeg")
    assert _size(data) == (1600, 1)
