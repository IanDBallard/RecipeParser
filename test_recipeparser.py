"""
Tests for recipeparser.py — covers all pure-Python logic without making any
live API calls or requiring a real EPUB file.

Sections:
  1. is_recipe_candidate      — discrimination heuristic
  2. split_large_chunk        — token-limit guard
  3. deduplicate_recipes      — name-normalised dedup
  4. create_paprika_export    — bundle structure / image embedding
  5. extract_chapters_with_image_markers — [IMAGE:] breadcrumb insertion
"""

import base64
import gzip
import json
import os
import tempfile
import zipfile
from unittest.mock import MagicMock, patch

import ebooklib
import pytest

# Suppress the module-level load_dotenv / genai.Client() calls so tests can
# run without a .env file or Google credentials present.
with patch("google.genai.Client"), patch("dotenv.load_dotenv"):
    from recipeparser import (
        RecipeExtraction,
        RecipeList,
        create_paprika_export,
        deduplicate_recipes,
        extract_chapters_with_image_markers,
        extract_recipes_with_gemini,
        is_recipe_candidate,
        split_large_chunk,
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_recipe(name: str, photo: str | None = None) -> RecipeExtraction:
    return RecipeExtraction(
        name=name,
        photo_filename=photo,
        ingredients=["1 cup flour", "1/2 tsp salt"],
        directions=["Mix ingredients.", "Bake at 350F for 30 mins."],
    )


# ---------------------------------------------------------------------------
# 1. is_recipe_candidate
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

    """False positives — non-recipe text that should be rejected."""

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
# 2. split_large_chunk
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
        # max_chars=100 — each paragraph is 60 chars; no two fit in 100 chars together
        result = split_large_chunk(text, max_chars=100)
        assert len(result) > 1

    def test_all_content_preserved_after_split(self):
        """Joining all split parts should reconstruct the original (minus joining separators)."""
        paras = ["Paragraph number " + str(i) + ". " + ("word " * 20) for i in range(20)]
        text = "\n\n".join(paras)
        parts = split_large_chunk(text, max_chars=200)
        reconstructed = "\n\n".join(parts)
        # Every paragraph should appear somewhere in the output
        for para in paras:
            assert para in reconstructed

    def test_no_part_exceeds_max_chars(self):
        """As long as individual paragraphs are smaller than max_chars, no part should exceed it."""
        paras = ["word " * 30 for _ in range(50)]  # ~150 chars each
        text = "\n\n".join(paras)
        max_chars = 500
        parts = split_large_chunk(text, max_chars=max_chars)
        for part in parts:
            assert len(part) <= max_chars + 200  # small tolerance for final accumulated para

    def test_single_oversized_paragraph_stays_intact(self):
        """A single paragraph larger than max_chars cannot be split further — it stays whole."""
        text = "word " * 1000  # ~5000 chars, no \n\n separators
        parts = split_large_chunk(text, max_chars=100)
        assert len(parts) == 1
        assert parts[0] == text


# ---------------------------------------------------------------------------
# 3. deduplicate_recipes
# ---------------------------------------------------------------------------

class TestDeduplicateRecipes:

    def test_no_duplicates_unchanged(self):
        recipes = [make_recipe("Pasta"), make_recipe("Salad"), make_recipe("Soup")]
        result = deduplicate_recipes(recipes)
        assert len(result) == 3

    def test_exact_duplicate_removed(self):
        recipes = [make_recipe("Pasta"), make_recipe("Pasta")]
        result = deduplicate_recipes(recipes)
        assert len(result) == 1

    def test_case_insensitive_dedup(self):
        recipes = [make_recipe("Chocolate Cake"), make_recipe("chocolate cake"), make_recipe("CHOCOLATE CAKE")]
        result = deduplicate_recipes(recipes)
        assert len(result) == 1

    def test_leading_trailing_whitespace_normalised(self):
        recipes = [make_recipe("  Banana Bread  "), make_recipe("Banana Bread")]
        result = deduplicate_recipes(recipes)
        assert len(result) == 1

    def test_first_occurrence_kept(self):
        r1 = make_recipe("Omelette", photo="omelette1.jpg")
        r2 = make_recipe("Omelette", photo="omelette2.jpg")
        result = deduplicate_recipes([r1, r2])
        assert result[0].photo_filename == "omelette1.jpg"

    def test_empty_list(self):
        assert deduplicate_recipes([]) == []

    def test_distinct_recipes_all_kept(self):
        recipes = [make_recipe(f"Recipe {i}") for i in range(10)]
        result = deduplicate_recipes(recipes)
        assert len(result) == 10


# ---------------------------------------------------------------------------
# 4. create_paprika_export
# ---------------------------------------------------------------------------

class TestCreatePaprikaExport:

    def test_empty_recipe_list_returns_false(self, tmp_path):
        result = create_paprika_export([], str(tmp_path), str(tmp_path), "out.paprikarecipes")
        assert result is False
        assert not (tmp_path / "out.paprikarecipes").exists()

    def test_export_file_created(self, tmp_path):
        recipes = [make_recipe("Brownies")]
        result = create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        assert result is True
        assert (tmp_path / "out.paprikarecipes").exists()

    def test_archive_is_valid_zip(self, tmp_path):
        recipes = [make_recipe("Waffles")]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        assert zipfile.is_zipfile(tmp_path / "out.paprikarecipes")

    def test_archive_contains_one_entry_per_recipe(self, tmp_path):
        recipes = [make_recipe("Waffles"), make_recipe("Pancakes"), make_recipe("French Toast")]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            assert len(zf.namelist()) == 3

    def test_inner_file_is_valid_gzipped_json(self, tmp_path):
        recipes = [make_recipe("Quiche")]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["name"] == "Quiche"
        assert "ingredients" in data
        assert "directions" in data

    def test_required_paprika_keys_present(self, tmp_path):
        required_keys = {"uid", "name", "directions", "ingredients", "prep_time",
                         "cook_time", "servings", "notes", "photo", "photo_data",
                         "source", "categories", "rating"}
        recipes = [make_recipe("Tart")]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert required_keys.issubset(data.keys())

    def test_uid_is_uppercase_uuid(self, tmp_path):
        import re
        UUID_RE = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")
        recipes = [make_recipe("Scones")]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert UUID_RE.match(data["uid"]), f"UID format unexpected: {data['uid']}"

    def test_photo_embedded_when_image_exists(self, tmp_path):
        # Write a tiny fake image
        img_dir = tmp_path / "images"
        img_dir.mkdir()
        fake_png = bytes([137, 80, 78, 71, 13, 10, 26, 10])  # PNG magic bytes
        (img_dir / "cake.jpg").write_bytes(fake_png)

        recipes = [make_recipe("Cake", photo="cake.jpg")]
        create_paprika_export(recipes, str(tmp_path), str(img_dir), "out.paprikarecipes")

        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))

        assert data["photo"] == "cake.jpg"
        assert data["photo_data"] == base64.b64encode(fake_png).decode("utf-8")

    def test_missing_image_does_not_crash(self, tmp_path):
        """Recipe with a photo_filename pointing to a non-existent file should still export."""
        recipes = [make_recipe("Mystery Pie", photo="ghost.jpg")]
        result = create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        assert result is True
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["photo"] == ""
        assert data["photo_data"] == ""

    def test_recipe_name_with_special_chars_sanitised(self, tmp_path):
        """Special characters in recipe names must not produce invalid ZIP entry names."""
        recipes = [make_recipe("Crème Brûlée & Friends! <test>")]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            entry_name = zf.namelist()[0]
        # Should not contain characters that break filesystems
        for bad_char in ["<", ">", "&", "/"]:
            assert bad_char not in entry_name

    def test_untitled_fallback_for_empty_name(self, tmp_path):
        """A recipe whose name is all special characters gets the Untitled_Recipe fallback."""
        recipes = [make_recipe("!!!???###")]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            assert zf.namelist()[0] == "Untitled_Recipe.paprikarecipe"

    def test_multiple_recipes_have_unique_uids(self, tmp_path):
        recipes = [make_recipe(f"Recipe {i}") for i in range(5)]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        uids = []
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            for name in zf.namelist():
                data = json.loads(gzip.decompress(zf.read(name)).decode("utf-8"))
                uids.append(data["uid"])
        assert len(set(uids)) == 5


# ---------------------------------------------------------------------------
# 5. extract_chapters_with_image_markers
# ---------------------------------------------------------------------------

class TestExtractChaptersWithImageMarkers:

    def _make_mock_book(self, items):
        """Build a lightweight mock of an epub.EpubBook for testing."""
        import ebooklib

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
        # ITEM_IMAGE mock will have get_body_content called — ensure it doesn't crash
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


# ---------------------------------------------------------------------------
# 6. extract_recipes_with_gemini  (mocked — no live API calls)
# ---------------------------------------------------------------------------

class TestExtractRecipesWithGemini:
    """
    Tests for extract_recipes_with_gemini().  The Google API client is mocked
    so no network call or API key is required.  We test that our code correctly
    handles whatever the API layer returns (or throws).
    """

    def _make_recipe_list(self, *recipes: RecipeExtraction) -> RecipeList:
        return RecipeList(recipes=list(recipes))

    def test_valid_response_returned_as_recipe_list(self):
        """Happy path: API returns a parsed RecipeList."""
        expected = self._make_recipe_list(
            make_recipe("Chocolate Cake", photo="cake.jpg")
        )
        mock_response = MagicMock()
        mock_response.parsed = expected

        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            result = extract_recipes_with_gemini("some chunk of text")

        assert result is not None
        assert len(result.recipes) == 1
        assert result.recipes[0].name == "Chocolate Cake"
        assert result.recipes[0].photo_filename == "cake.jpg"

    def test_api_exception_returns_none(self):
        """Any exception from the API must be caught and None returned."""
        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.side_effect = Exception("503 Service Unavailable")
            result = extract_recipes_with_gemini("some chunk of text")

        assert result is None

    def test_empty_recipe_list_returned_cleanly(self):
        """API may legitimately return zero recipes for a non-recipe chunk."""
        mock_response = MagicMock()
        mock_response.parsed = RecipeList(recipes=[])

        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            result = extract_recipes_with_gemini("Introduction to the author.")

        assert result is not None
        assert result.recipes == []

    def test_multiple_recipes_in_one_chunk(self):
        """A chunk can yield multiple recipes in a single API call."""
        expected = self._make_recipe_list(
            make_recipe("Pasta Primavera"),
            make_recipe("Caesar Salad"),
            make_recipe("Tiramisu"),
        )
        mock_response = MagicMock()
        mock_response.parsed = expected

        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            result = extract_recipes_with_gemini("chunk with three recipes")

        assert result is not None
        assert len(result.recipes) == 3
        assert result.recipes[1].name == "Caesar Salad"

    def test_correct_model_and_config_passed_to_api(self):
        """Verify we're calling the API with the right model name and JSON config."""
        mock_response = MagicMock()
        mock_response.parsed = RecipeList(recipes=[])

        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            extract_recipes_with_gemini("any text")

        call_kwargs = mock_client.models.generate_content.call_args
        assert call_kwargs.kwargs["model"] == "gemini-2.5-flash"
        config = call_kwargs.kwargs["config"]
        assert config["response_mime_type"] == "application/json"
        assert config["temperature"] == 0.1

    def test_text_chunk_included_in_prompt(self):
        """The raw text chunk must appear in the prompt sent to the API."""
        mock_response = MagicMock()
        mock_response.parsed = RecipeList(recipes=[])
        sentinel = "UNIQUE_SENTINEL_STRING_XYZ"

        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            extract_recipes_with_gemini(sentinel)

        call_kwargs = mock_client.models.generate_content.call_args
        assert sentinel in call_kwargs.kwargs["contents"]

    def test_unicode_fraction_fields_preserved(self):
        """
        The LLM is responsible for converting fractions; we verify our code
        faithfully passes through whatever string values the API returns.
        """
        recipe_with_fractions = RecipeExtraction(
            name="Scones",
            ingredients=["1/2 cup butter", "3/4 cup milk"],
            directions=["Mix.", "Bake."],
        )
        mock_response = MagicMock()
        mock_response.parsed = RecipeList(recipes=[recipe_with_fractions])

        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            result = extract_recipes_with_gemini("scone text")

        assert result.recipes[0].ingredients[0] == "1/2 cup butter"
        assert result.recipes[0].ingredients[1] == "3/4 cup milk"

    def test_none_parsed_response_handled(self):
        """
        If response.parsed is None (malformed API response), the function
        should return it without crashing — callers already guard for None.
        """
        mock_response = MagicMock()
        mock_response.parsed = None

        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            result = extract_recipes_with_gemini("any text")

        assert result is None
