"""A book chunk carries the bytes of the photos its text marks — the temp dir does not outlive read()."""
from __future__ import annotations

from pathlib import Path

import fitz  # type: ignore[import-untyped]
from ebooklib import epub

from recipeparser.config import MIN_PHOTO_BYTES
from recipeparser.io.readers.book_images import images_named_in, inject_hero_markers
from recipeparser.io.readers.epub import EpubReader
from recipeparser.io.readers.pdf import PdfReader

RECIPE = (
    "<h1>Scones</h1><h2>Ingredients</h2><p>2 cups flour</p><p>1 tbsp sugar</p>"
    "<h2>Method</h2><p>Stir, then bake for 12 minutes.</p>"
)
PHOTO = b"\xff\xd8" + b"p" * MIN_PHOTO_BYTES
OTHER = b"\xff\xd8" + b"o" * MIN_PHOTO_BYTES
RECIPE_TEXT = "Scones\nIngredients\n2 cups flour\n1 tbsp sugar\nMethod\nStir, then bake."


def _epub(tmp_path: Path, chapters, images) -> str:
    book = epub.EpubBook()
    book.set_identifier("book-images-test")
    book.set_title("Test")
    book.set_language("en")
    items = []
    for i, html in enumerate(chapters):
        item = epub.EpubHtml(title=f"c{i}", file_name=f"c{i}.xhtml", lang="en")
        item.content = html
        book.add_item(item)
        items.append(item)
    for name, data in images.items():
        img = epub.EpubImage()
        img.file_name = f"images/{name}"
        img.media_type = "image/jpeg"
        img.content = data
        book.add_item(img)
    book.toc = tuple(items)
    book.spine = ["nav", *items]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    path = tmp_path / "book.epub"
    epub.write_epub(str(path), book)
    return str(path)


class TestInjectHeroMarkers:
    def test_a_photo_only_page_hands_its_photo_to_the_next_chunk(self):
        out = inject_hero_markers(["[IMAGE: hero.jpg]\n", RECIPE_TEXT])
        assert out[1] == "[HERO IMAGE: hero.jpg]\n" + RECIPE_TEXT
        assert out[0] == "[IMAGE: hero.jpg]\n"

    def test_a_page_with_real_text_keeps_its_photo(self):
        prose = "[IMAGE: map.jpg]\n" + "A long essay about the history of Roman dining. " * 5
        assert inject_hero_markers([prose, RECIPE_TEXT]) == [prose, RECIPE_TEXT]

    def test_a_recipe_page_never_donates_its_photo(self):
        page = "[IMAGE: a.jpg]\n" + RECIPE_TEXT
        assert inject_hero_markers([page, RECIPE_TEXT]) == [page, RECIPE_TEXT]

    def test_the_last_chunk_has_no_one_to_donate_to(self):
        assert inject_hero_markers(["[IMAGE: a.jpg]"]) == ["[IMAGE: a.jpg]"]


def test_images_named_in_reads_only_marked_files_that_exist(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"A")
    (tmp_path / "b.jpg").write_bytes(b"B")
    (tmp_path / "unmarked.jpg").write_bytes(b"U")
    text = "[IMAGE: a.jpg]\n[HERO IMAGE: b.jpg]\n[IMAGE: missing.jpg]\n[IMAGE: a.jpg]"
    assert images_named_in(text, str(tmp_path)) == {"a.jpg": b"A", "b.jpg": b"B"}


class TestEpubReaderImages:
    def test_each_chunk_carries_the_bytes_of_the_photos_it_marks(self, tmp_path):
        source = _epub(
            tmp_path,
            ['<p><img src="images/a.jpg"/></p>' + RECIPE, '<p><img src="images/b.jpg"/></p>' + RECIPE],
            {"a.jpg": PHOTO, "b.jpg": OTHER},
        )
        chunks = EpubReader().read(source)
        assert [c.images for c in chunks] == [{"a.jpg": PHOTO}, {"b.jpg": OTHER}]

    def test_a_photo_only_chapter_becomes_the_next_recipes_hero(self, tmp_path):
        source = _epub(tmp_path, ['<p><img src="images/hero.jpg"/></p>', RECIPE], {"hero.jpg": PHOTO})
        [chunk] = EpubReader().read(source)
        assert chunk.text.startswith("[HERO IMAGE: hero.jpg]")
        assert chunk.images == {"hero.jpg": PHOTO}

    def test_a_decorative_image_is_not_carried(self, tmp_path):
        source = _epub(tmp_path, ['<p><img src="images/rule.jpg"/></p>' + RECIPE], {"rule.jpg": b"tiny"})
        [chunk] = EpubReader().read(source)
        assert chunk.images == {}


def test_pdf_reader_chunks_carry_their_page_photo(tmp_path):
    pixmap = fitz.Pixmap(fitz.csRGB, 160, 160, bytes(range(256)) * 300, False)
    jpeg = pixmap.tobytes("jpeg")
    assert len(jpeg) >= MIN_PHOTO_BYTES, "fixture photo must clear the decorative-image bar"
    doc = fitz.open()
    for _ in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), "Scones. Ingredients: 2 cups flour, 1 tbsp sugar. Stir, then bake. " * 3)
    doc[1].insert_image(fitz.Rect(72, 200, 232, 360), stream=jpeg)
    path = tmp_path / "book.pdf"
    doc.save(str(path))

    chunks = PdfReader().read(str(path))
    carried = [list(c.images) for c in chunks]
    assert carried == [[], ["page2_img1.jpeg"], []]
    assert len(chunks[1].images["page2_img1.jpeg"]) == len(jpeg)
