"""Tests for recipeparser.toc — extract_toc_epub, extract_toc_pdf, segment_by_toc, run_recon."""
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.toc import extract_toc_epub, extract_toc_pdf, run_recon, segment_by_toc, filter_toc_to_recipe_entries


# ---------------------------------------------------------------------------
# extract_toc_epub
# ---------------------------------------------------------------------------


class TestExtractTocEpub:
    def test_epub_programmatic_toc_returns_entries(self, tmp_path):
        """When book has nav/NCX TOC with enough entries, returns them after AI filter."""
        epub_path = tmp_path / "book.epub"
        epub_path.write_bytes(b"PK\x03\x04")
        section1 = MagicMock(title="Chicken Soup", href="ch1.xhtml")
        section2 = MagicMock(title="Beef Stew", href="ch2.xhtml")
        mock_toc = [(section1, []), (section2, [])]

        with patch("ebooklib.epub.read_epub") as mock_read, \
             patch("recipeparser.toc.filter_toc_to_recipe_entries", side_effect=lambda entries, _: entries):
            mock_book = MagicMock()
            mock_book.toc = mock_toc
            mock_read.return_value = mock_book
            result = extract_toc_epub(str(epub_path), ["chunk1", "chunk2"], MagicMock())

        assert len(result) == 2
        assert result[0][0] == "Chicken Soup"
        assert result[1][0] == "Beef Stew"

    def test_epub_empty_toc_falls_back_to_ai(self, tmp_path):
        """When book has empty TOC, falls back to AI parse of first chunks."""
        epub_path = tmp_path / "book.epub"
        epub_path.write_bytes(b"PK\x03\x04")
        mock_book = MagicMock()
        mock_book.toc = []

        with patch("ebooklib.epub.read_epub", return_value=mock_book), \
             patch("recipeparser.toc._parse_toc_from_text_fallback") as mock_fallback, \
             patch("recipeparser.toc.filter_toc_to_recipe_entries", side_effect=lambda entries, _: entries):
            mock_fallback.return_value = [("Recipe A", 1), ("Recipe B", 2)]
            result = extract_toc_epub(str(epub_path), ["chunk1", "chunk2"], MagicMock())

        mock_fallback.assert_called_once()
        assert result == [("Recipe A", 1), ("Recipe B", 2)]

    def test_epub_nested_toc_leaves_only_vs_all_levels(self, tmp_path):
        """Nested TOC: leaves-only returns only bottom-level entries; all-levels includes sections."""
        from recipeparser.toc import _flatten_epub_toc, _flatten_epub_toc_leaves_only

        part = MagicMock(title="Part One: The Dough", href="part1.xhtml")
        ch1 = MagicMock(title="Egg Pastas", href="ch1.xhtml")
        r1 = MagicMock(title="Egg Dough", href="r1.xhtml")
        r2 = MagicMock(title="Ravioli", href="r2.xhtml")
        ch2 = MagicMock(title="Extruded", href="ch2.xhtml")
        r3 = MagicMock(title="Spaghetti", href="r3.xhtml")
        mock_toc = [
            (part, [
                (ch1, [(r1, []), (r2, [])]),
                (ch2, [(r3, [])]),
            ]),
        ]

        all_entries = _flatten_epub_toc(mock_toc)
        leaf_entries = _flatten_epub_toc_leaves_only(mock_toc)

        assert len(all_entries) == 6
        assert len(leaf_entries) == 3
        assert [t for t, _ in leaf_entries] == ["Egg Dough", "Ravioli", "Spaghetti"]
        assert [t for t, _ in all_entries] == [
            "Part One: The Dough", "Egg Pastas", "Egg Dough", "Ravioli", "Extruded", "Spaghetti",
        ]


# ---------------------------------------------------------------------------
# filter_toc_to_recipe_entries
# ---------------------------------------------------------------------------


class TestFilterTocToRecipeEntries:
    def test_filters_to_recipe_indices_only(self):
        """AI filter returns only entries at indices classified as recipe titles."""
        entries = [
            ("Chicken Soup", 1),
            ("Introduction", None),
            ("Beef Stew", 5),
        ]
        with patch("recipeparser.toc._classify_toc_recipe_indices", return_value=[0, 2]):
            result = filter_toc_to_recipe_entries(entries, MagicMock())
        assert result == [("Chicken Soup", 1), ("Beef Stew", 5)]

    def test_returns_entries_unchanged_on_classification_failure(self):
        """On API failure, return full list so recon still runs."""
        entries = [("Recipe A", 1), ("Recipe B", 2)]
        with patch("recipeparser.toc._classify_toc_recipe_indices", return_value=None):
            result = filter_toc_to_recipe_entries(entries, MagicMock())
        assert result == entries

    def test_empty_entries_returns_empty(self):
        with patch("recipeparser.toc._classify_toc_recipe_indices") as mock_classify:
            result = filter_toc_to_recipe_entries([], MagicMock())
        assert result == []
        mock_classify.assert_not_called()


# ---------------------------------------------------------------------------
# extract_toc_pdf
# ---------------------------------------------------------------------------


class TestExtractTocPdf:
    def test_pdf_programmatic_toc_returns_entries(self, tmp_path):
        """When PDF has outline with enough entries, returns them after AI filter."""
        pdf_path = tmp_path / "book.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")  # minimal PDF header
        mock_toc = [[1, "Chicken Soup", 10], [1, "Beef Stew", 20]]

        with patch("fitz.open") as mock_open, \
             patch("recipeparser.toc.filter_toc_to_recipe_entries", side_effect=lambda entries, _: entries):
            mock_doc = MagicMock()
            mock_doc.get_toc.return_value = mock_toc
            mock_doc.__enter__ = MagicMock(return_value=mock_doc)
            mock_doc.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_doc
            result = extract_toc_pdf(str(pdf_path), ["page1", "page2"], MagicMock())

        assert len(result) == 2
        assert result[0] == ("Chicken Soup", 10)
        assert result[1] == ("Beef Stew", 20)


# ---------------------------------------------------------------------------
# segment_by_toc
# ---------------------------------------------------------------------------


class TestSegmentByToc:
    def test_empty_toc_returns_empty_segments(self):
        chunks = ["Page 1\nChicken Soup\nIngredients...", "Page 2\nBeef Stew..."]
        segments, ratio = segment_by_toc(chunks, [])
        assert segments == []
        assert ratio == 0.0

    def test_empty_chunks_returns_empty_segments(self):
        segments, ratio = segment_by_toc([], [("Chicken Soup", 1), ("Beef Stew", 2)])
        assert segments == []
        assert ratio == 0.0

    def test_finds_titles_and_segments(self):
        raw = [
            "Intro text\n\nChicken Soup\nIngredients: 1 cup broth\nDirections: heat\n",
            "\n\nBeef Stew\nIngredients: beef\nDirections: simmer\n\nDessert\nRecipe for cake",
        ]
        toc = [("Chicken Soup", 1), ("Beef Stew", 2), ("Dessert", 3)]
        segments, ratio = segment_by_toc(raw, toc)
        assert ratio == 1.0
        assert len(segments) >= 2  # at least Chicken Soup and Beef Stew segments
        full = " ".join(segments).lower()
        assert "chicken soup" in full
        assert "beef stew" in full

    def test_partial_match_ratio(self):
        raw = ["Some text\nChicken Soup\nIngredients...\n"]
        toc = [("Chicken Soup", 1), ("Not In Text", None), ("Also Missing", None)]
        segments, ratio = segment_by_toc(raw, toc)
        assert 0.0 < ratio < 1.0
        assert ratio == pytest.approx(1 / 3, rel=0.01)


# ---------------------------------------------------------------------------
# run_recon
# ---------------------------------------------------------------------------


class TestRunRecon:
    def test_all_matched(self):
        toc = [("Chicken Soup", 1), ("Beef Stew", 2)]
        extracted = ["Chicken Soup", "Beef Stew"]
        matched, missing, extra = run_recon(toc, extracted)
        assert len(matched) == 2
        assert len(missing) == 0
        assert len(extra) == 0

    def test_case_insensitive_match(self):
        toc = [("Chicken Soup", 1)]
        extracted = ["chicken soup"]
        matched, missing, extra = run_recon(toc, extracted)
        assert len(matched) == 1
        assert len(missing) == 0

    def test_missing_from_extraction(self):
        toc = [("Chicken Soup", 1), ("Beef Stew", 2), ("Dessert", 3)]
        extracted = ["Chicken Soup", "Dessert"]
        matched, missing, extra = run_recon(toc, extracted)
        assert len(matched) == 2
        assert "Beef Stew" in missing
        assert len(missing) == 1

    def test_extra_extracted(self):
        toc = [("Chicken Soup", 1)]
        extracted = ["Chicken Soup", "Bonus Recipe"]
        matched, missing, extra = run_recon(toc, extracted)
        assert len(matched) == 1
        assert "Bonus Recipe" in extra
        assert len(extra) == 1

    def test_empty_toc(self):
        matched, missing, extra = run_recon([], ["A", "B"])
        assert matched == []
        assert missing == []
        assert len(extra) == 2

    def test_empty_extracted(self):
        toc = [("Chicken Soup", 1)]
        matched, missing, extra = run_recon(toc, [])
        assert matched == []
        assert len(missing) == 1
        assert extra == []
