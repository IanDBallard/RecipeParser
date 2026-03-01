"""Tests for recipeparser.epub — EPUB parsing, chunking, image extraction."""
import ebooklib
import pytest
from unittest.mock import MagicMock

from recipeparser.epub import (
    extract_all_images,
    extract_chapters_with_image_markers,
    is_recipe_candidate,
    split_large_chunk,
)
from recipeparser.config import MIN_PHOTO_BYTES


# ---------------------------------------------------------------------------
# is_recipe_candidate
# ---------------------------------------------------------------------------

class TestIsRecipeCandidate:
    """True positives — text that should be flagged as recipe content."""

    def test_clear_recipe_passes(self):
        text = (
            "Chocolate Chip Cookies\n"
            "Ingredients\n"
            "2 cups flour\n"
            "1 tsp baking soda\n"
            "1 tbsp vanilla extract\n"
            "Directions\n"
            "Preheat oven to 375F. Mix dry ingredients. Bake for 12 mins."
        )
        assert is_recipe_candidate(text) is True

    def test_metric_recipe_passes(self):
        text = (
            "Vegetable Soup\n"
            "200 gram carrots\n"
            "500 ml stock\n"
            "Method\n"
            "Simmer for 20 minutes. Stir occasionally."
        )
        assert is_recipe_candidate(text) is True

    def test_recipe_with_imperial_units_passes(self):
        text = (
            "Roast Chicken\n"
            "1 lb chicken\n"
            "2 oz butter\n"
            "Directions\n"
            "Roast at 400F for 1 hour."
        )
        assert is_recipe_candidate(text) is True

    def test_table_of_contents_rejected(self):
        text = (
            "Table of Contents\n"
            "Chapter 1: Introduction ........ 3\n"
            "Chapter 2: Breakfast ........... 10\n"
            "Chapter 3: Desserts ............ 45\n"
        )
        assert is_recipe_candidate(text) is False

    def test_author_bio_rejected(self):
        text = (
            "About the Author\n"
            "Jane Smith is an award-winning food writer who has spent twenty years "
            "travelling the world in search of great flavours. She lives in Vermont "
            "with her husband and two cats."
        )
        assert is_recipe_candidate(text) is False

    def test_copyright_page_rejected(self):
        text = (
            "Copyright © 2024 Jane Smith. All rights reserved.\n"
            "Published by Culinary Press. ISBN 978-0-000-00000-0.\n"
            "No part of this publication may be reproduced without permission."
        )
        assert is_recipe_candidate(text) is False

    def test_only_quantity_keywords_no_structure_rejected(self):
        """Grocery list: has units but no cooking verbs or section headings."""
        text = (
            "Shopping list:\n"
            "2 cups milk\n"
            "1 lb butter\n"
            "3 oz cheese\n"
        )
        assert is_recipe_candidate(text) is False

    def test_only_structure_keywords_no_quantities_rejected(self):
        """Essay about baking: has verbs but no measurement units."""
        text = (
            "The art of baking is all about technique. You preheat the oven, "
            "you bake the dough, you let it simmer in its own warmth. "
            "Great bakers stir with intention and fold with care."
        )
        assert is_recipe_candidate(text) is False

    def test_exactly_threshold_passes(self):
        """Exactly 2 quantity keywords and 1 structure keyword — should pass."""
        text = "Use 1 cup flour and 1 tbsp oil. Then bake until golden."
        assert is_recipe_candidate(text) is True

    def test_one_quantity_keyword_fails(self):
        """Only 1 quantity keyword — should fail even with structure keywords."""
        text = "Add 1 cup of love. Bake with care. Stir the soul."
        assert is_recipe_candidate(text) is False

    def test_case_insensitive(self):
        """Keywords must be detected regardless of case."""
        text = "Use 2 CUPS flour and 1 TBSP sugar. PREHEAT the oven."
        assert is_recipe_candidate(text) is True


# ---------------------------------------------------------------------------
# split_large_chunk
# ---------------------------------------------------------------------------

class TestSplitLargeChunk:

    def test_short_chunk_unchanged(self):
        text = "Short text that fits in one chunk."
        result = split_large_chunk(text, max_chars=1000)
        assert result == [text]

    def test_chunk_at_exact_limit_unchanged(self):
        text = "x" * 100
        result = split_large_chunk(text, max_chars=100)
        assert result == [text]

    def test_oversized_chunk_splits(self):
        para_a = "A" * 60
        para_b = "B" * 60
        para_c = "C" * 60
        text = f"{para_a}\n\n{para_b}\n\n{para_c}"
        result = split_large_chunk(text, max_chars=100)
        assert len(result) > 1

    def test_all_content_preserved_after_split(self):
        """Joining all split parts should reconstruct the original (minus joining separators)."""
        paras = ["Paragraph number " + str(i) + ". " + ("word " * 20) for i in range(20)]
        text = "\n\n".join(paras)
        parts = split_large_chunk(text, max_chars=200)
        reconstructed = "\n\n".join(parts)
        for para in paras:
            assert para in reconstructed

    def test_no_part_exceeds_max_chars(self):
        """As long as individual paragraphs are smaller than max_chars, no part should exceed it."""
        paras = ["word " * 30 for _ in range(50)]
        text = "\n\n".join(paras)
        max_chars = 500
        parts = split_large_chunk(text, max_chars=max_chars)
        for part in parts:
            assert len(part) <= max_chars + 200

    def test_single_oversized_paragraph_stays_intact(self):
        """A single paragraph larger than max_chars cannot be split further — it stays whole."""
        text = "word " * 1000
        parts = split_large_chunk(text, max_chars=100)
        assert len(parts) == 1
        assert parts[0] == text


# ---------------------------------------------------------------------------
# extract_chapters_with_image_markers
# ---------------------------------------------------------------------------

class TestExtractChaptersWithImageMarkers:

    def _make_mock_book(self, items):
        mock_book = MagicMock()
        mock_items = []
        for (item_type, content) in items:
            item = MagicMock()
            item.get_type.return_value = item_type
            item.get_body_content.return_value = content.encode("utf-8")
            mock_items.append(item)
        mock_book.get_items.return_value = mock_items
        return mock_book

    def test_img_tag_replaced_with_marker(self):
        html = '<html><body><img src="images/photo.jpg"/><p>Some text</p></body></html>'
        book = self._make_mock_book([(ebooklib.ITEM_DOCUMENT, html)])
        chunks = extract_chapters_with_image_markers(book)
        assert len(chunks) == 1
        assert "[IMAGE: photo.jpg]" in chunks[0]
        assert "<img" not in chunks[0]

    def test_src_path_stripped_to_basename(self):
        html = '<html><body><img src="../../OEBPS/images/deep/nested/photo.png"/></body></html>'
        book = self._make_mock_book([(ebooklib.ITEM_DOCUMENT, html)])
        chunks = extract_chapters_with_image_markers(book)
        assert "[IMAGE: photo.png]" in chunks[0]

    def test_img_without_src_not_inserted(self):
        html = '<html><body><img alt="decorative"/><p>Some text</p></body></html>'
        book = self._make_mock_book([(ebooklib.ITEM_DOCUMENT, html)])
        chunks = extract_chapters_with_image_markers(book)
        assert "[IMAGE:" not in chunks[0]

    def test_non_document_items_skipped(self):
        items = [
            (ebooklib.ITEM_IMAGE, ""),
            (ebooklib.ITEM_DOCUMENT, "<html><body><p>Recipe text</p></body></html>"),
        ]
        book = self._make_mock_book(items)
        chunks = extract_chapters_with_image_markers(book)
        assert len(chunks) == 1

    def test_empty_document_excluded(self):
        html = "<html><body>   </body></html>"
        book = self._make_mock_book([(ebooklib.ITEM_DOCUMENT, html)])
        chunks = extract_chapters_with_image_markers(book)
        assert chunks == []

    def test_multiple_images_all_replaced(self):
        html = (
            '<html><body>'
            '<img src="img1.jpg"/>'
            '<p>Pasta recipe here</p>'
            '<img src="img2.jpg"/>'
            '<p>Salad recipe here</p>'
            '</body></html>'
        )
        book = self._make_mock_book([(ebooklib.ITEM_DOCUMENT, html)])
        chunks = extract_chapters_with_image_markers(book)
        assert "[IMAGE: img1.jpg]" in chunks[0]
        assert "[IMAGE: img2.jpg]" in chunks[0]

    def test_qualifying_filter_suppresses_small_image(self):
        """Images not in qualifying_images set must be silently removed."""
        html = (
            '<html><body>'
            '<img src="small_icon.jpg"/>'
            '<img src="hero.jpg"/>'
            '<p>Some recipe text here</p>'
            '</body></html>'
        )
        book = self._make_mock_book([(ebooklib.ITEM_DOCUMENT, html)])
        chunks = extract_chapters_with_image_markers(book, qualifying_images={"hero.jpg"})
        assert "[IMAGE: hero.jpg]" in chunks[0]
        assert "small_icon.jpg" not in chunks[0]

    def test_qualifying_filter_none_passes_all(self):
        """Without a qualifying set, all images get markers (backward-compat)."""
        html = '<html><body><img src="any.jpg"/><p>text</p></body></html>'
        book = self._make_mock_book([(ebooklib.ITEM_DOCUMENT, html)])
        chunks = extract_chapters_with_image_markers(book, qualifying_images=None)
        assert "[IMAGE: any.jpg]" in chunks[0]


# ---------------------------------------------------------------------------
# extract_all_images
# ---------------------------------------------------------------------------

def _make_epub_with_images(image_sizes: dict) -> MagicMock:
    items = []
    for filename, size in image_sizes.items():
        item = MagicMock()
        item.get_type.return_value = ebooklib.ITEM_IMAGE
        item.file_name = filename
        item.get_content.return_value = b"x" * size
        items.append(item)
    book = MagicMock()
    book.get_items.return_value = items
    return book


class TestExtractAllImages:

    def test_large_images_are_saved(self, tmp_path):
        book = _make_epub_with_images({"recipe.jpg": MIN_PHOTO_BYTES})
        extract_all_images(book, str(tmp_path))
        assert (tmp_path / "images" / "recipe.jpg").exists()

    def test_small_images_are_skipped(self, tmp_path):
        book = _make_epub_with_images({"separator.jpg": MIN_PHOTO_BYTES - 1})
        extract_all_images(book, str(tmp_path))
        assert not (tmp_path / "images" / "separator.jpg").exists()

    def test_exactly_at_threshold_is_saved(self, tmp_path):
        book = _make_epub_with_images({"border.jpg": MIN_PHOTO_BYTES})
        extract_all_images(book, str(tmp_path))
        assert (tmp_path / "images" / "border.jpg").exists()

    def test_mixed_sizes_only_saves_large(self, tmp_path):
        book = _make_epub_with_images({
            "small.jpg": 5_000,
            "medium.jpg": MIN_PHOTO_BYTES - 1,
            "large.jpg": MIN_PHOTO_BYTES,
            "bigger.jpg": 100_000,
        })
        extract_all_images(book, str(tmp_path))
        img_dir = tmp_path / "images"
        assert not (img_dir / "small.jpg").exists()
        assert not (img_dir / "medium.jpg").exists()
        assert (img_dir / "large.jpg").exists()
        assert (img_dir / "bigger.jpg").exists()

    def test_image_dir_created(self, tmp_path):
        book = _make_epub_with_images({})
        extract_all_images(book, str(tmp_path))
        assert (tmp_path / "images").is_dir()

    def test_returns_image_dir_path(self, tmp_path):
        book = _make_epub_with_images({})
        image_dir, qualifying = extract_all_images(book, str(tmp_path))
        assert image_dir == str(tmp_path / "images")

    def test_returns_qualifying_set(self, tmp_path):
        book = _make_epub_with_images({
            "small.jpg": MIN_PHOTO_BYTES - 1,
            "hero.jpg": MIN_PHOTO_BYTES,
        })
        _, qualifying = extract_all_images(book, str(tmp_path))
        assert "hero.jpg" in qualifying
        assert "small.jpg" not in qualifying

    def test_qualifying_set_empty_when_no_images(self, tmp_path):
        book = _make_epub_with_images({})
        _, qualifying = extract_all_images(book, str(tmp_path))
        assert qualifying == set()
