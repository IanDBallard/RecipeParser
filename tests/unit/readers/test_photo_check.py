"""A book image is offered to the model as a photo only when it looks like one (Fix Roadmap F-203).

``coffee_cc0.jpg`` is scikit-image's ``coffee`` sample, "No copyright restrictions. CC0 by the
photographer (Rachel Michetti)", scaled to 450x300: a real photograph, so the thresholds are held
against one rather than against noise that only looks like a photo to the test that made it.
"""
from __future__ import annotations

import io
import math
from pathlib import Path

import fitz  # type: ignore[import-untyped]
from PIL import Image, ImageDraw, ImageEnhance

from recipeparser.config import MIN_PHOTO_BYTES, MIN_PHOTO_EDGE_PX
from recipeparser.io.readers.pdf import PdfReader
from recipeparser.io.readers.photo_check import keep_as_photo, photo_refusal

COFFEE = (Path(__file__).resolve().parents[2] / "fixtures" / "coffee_cc0.jpg").read_bytes()


def _encode(img: Image.Image, fmt: str = "JPEG") -> bytes:
    out = io.BytesIO()
    img.save(out, fmt, **({"quality": 95} if fmt == "JPEG" else {}))
    return out.getvalue()


def _coffee() -> Image.Image:
    return Image.open(io.BytesIO(COFFEE)).convert("RGB")


def _spiky_ornament(size: int = 500, ink=(0, 0, 0), paper=(255, 255, 255), mode: str = "RGB") -> Image.Image:
    """A sixteen-point black star on white, antialiased: the blob a cookbook PDF put beside a minestrone."""
    big = Image.new(mode, (size * 4, size * 4), paper)
    centre = size * 2
    points = []
    for i in range(32):
        radius = size * (1.8 if i % 2 == 0 else 0.6)
        angle = math.pi * i / 16
        points.append((centre + radius * math.cos(angle), centre + radius * math.sin(angle)))
    ImageDraw.Draw(big).polygon(points, fill=ink)
    return big.resize((size, size), Image.LANCZOS)


class TestPhotographsAreKept:
    def test_a_photograph(self) -> None:
        assert photo_refusal(COFFEE) is None

    def test_a_black_and_white_photograph(self) -> None:
        assert photo_refusal(_encode(_coffee().convert("L"))) is None

    def test_a_flat_low_contrast_photograph(self) -> None:
        assert photo_refusal(_encode(ImageEnhance.Contrast(_coffee()).enhance(0.35))) is None

    def test_a_photograph_small_on_a_white_page(self) -> None:
        page = Image.new("RGB", (1200, 800), "white")
        page.paste(_coffee(), (375, 250))
        assert photo_refusal(_encode(page)) is None

    def test_a_panorama_up_to_four_to_one(self) -> None:
        assert photo_refusal(_encode(_coffee().resize((MIN_PHOTO_EDGE_PX * 4, MIN_PHOTO_EDGE_PX)))) is None

    def test_bytes_that_do_not_decode_are_kept_rather_than_judged(self) -> None:
        # A JPEG 2000 without its codec, say: losing a photograph to a missing codec is worse.
        assert photo_refusal(b"\xff\xd8" + b"p" * MIN_PHOTO_BYTES) is None


class TestWhatIsNotAPhotograph:
    def test_a_blank_page(self) -> None:
        assert "blank" in (photo_refusal(_encode(Image.new("RGB", (1200, 1600), "white"))) or "")

    def test_a_solid_block(self) -> None:
        assert "blank" in (photo_refusal(_encode(Image.new("RGB", (600, 600), "black"))) or "")

    def test_a_paper_texture(self) -> None:
        paper = Image.effect_noise((1200, 1600), 4).point(lambda v: 235 + v // 25).convert("RGB")
        assert "blank" in (photo_refusal(_encode(paper)) or "")

    def test_a_black_ornament(self) -> None:
        assert "line art" in (photo_refusal(_encode(_spiky_ornament())) or "")

    def test_an_ornament_on_transparency_is_judged_as_the_app_shows_it_on_white(self) -> None:
        ornament = _spiky_ornament(ink=(0, 0, 0, 255), paper=(0, 0, 0, 0), mode="RGBA")
        assert "line art" in (photo_refusal(_encode(ornament, "PNG")) or "")

    def test_a_block_of_text(self) -> None:
        block = Image.new("RGB", (900, 400), "white")
        draw = ImageDraw.Draw(block)
        for y in range(20, 380, 30):
            draw.text((20, y), "Serves 4. Preheat the oven to 180C and butter a tin. " * 2, fill="black")
        assert "line art" in (photo_refusal(_encode(block)) or "")

    def test_anything_under_the_minimum_edge(self) -> None:
        small = _coffee().resize((MIN_PHOTO_EDGE_PX - 1, MIN_PHOTO_EDGE_PX - 1))
        assert "too small" in (photo_refusal(_encode(small)) or "")

    def test_a_rule_or_a_border_strip(self) -> None:
        strip = _coffee().resize((1600, 300))
        assert "too thin" in (photo_refusal(_encode(strip)) or "")


def test_keep_as_photo_logs_the_reason(caplog) -> None:
    caplog.set_level("INFO")
    assert keep_as_photo("page3_img2.jpeg", _encode(_spiky_ornament())) is False
    assert "page3_img2.jpeg" in caplog.text and "line art" in caplog.text
    assert keep_as_photo("page3_img1.jpeg", COFFEE) is True


def test_a_pdf_page_offers_the_model_its_photo_and_not_its_ornament(tmp_path: Path) -> None:
    ornament = _encode(_spiky_ornament(700))
    assert len(ornament) >= MIN_PHOTO_BYTES and len(COFFEE) >= MIN_PHOTO_BYTES  # both reach the new check
    doc = fitz.open()
    page = doc.new_page()
    method = "Soften the onion, carrot and celery in the oil for ten minutes, add the stock and simmer.\n"
    text = "Perfect Minestrone\nIngredients\n1 onion\n2 carrots\n2 sticks celery\n1 litre stock\nMethod\n" + method * 4
    page.insert_textbox(fitz.Rect(72, 320, 540, 780), text, fontsize=9)
    page.insert_image(fitz.Rect(72, 150, 300, 300), stream=COFFEE)
    page.insert_image(fitz.Rect(320, 150, 420, 250), stream=ornament)
    path = tmp_path / "book.pdf"
    doc.save(str(path))
    doc.close()

    chunks = PdfReader().read(str(path))
    named = [name for chunk in chunks for name in chunk.images]
    assert len(named) == 1
    assert chunks[0].images[named[0]] == COFFEE
    assert all("img2" not in chunk.text for chunk in chunks)
