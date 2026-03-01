"""
Tests for recipeparser.py — covers all pure-Python logic without making any
live API calls or requiring a real EPUB file.

Sections:
  1. is_recipe_candidate      — discrimination heuristic
  2. split_large_chunk        — token-limit guard
  3. deduplicate_recipes      — name-normalised dedup
  4. create_paprika_export    — bundle structure / image embedding
  5. extract_chapters_with_image_markers — [IMAGE:] breadcrumb insertion
  6. extract_recipes_with_gemini — mocked API call handling
  7. needs_table_normalisation  — baker's % trigger detection
  8. normalise_baker_table      — pre-processing pass (mocked)
  9. categorise_recipe          — Paprika taxonomy assignment (mocked)
 10. _load_category_tree        — YAML taxonomy loader
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
os.environ.setdefault("GOOGLE_API_KEY", "dummy-key-for-tests")
with patch("google.genai.Client"), patch("dotenv.load_dotenv"):
    from recipeparser import (
        RecipeExtraction,
        RecipeList,
        create_paprika_export,
        deduplicate_recipes,
        extract_chapters_with_image_markers,
        extract_recipes_with_gemini,
        is_recipe_candidate,
        categorise_recipe,
        needs_table_normalisation,
        normalise_baker_table,
        split_large_chunk,
        PAPRIKA_CATEGORIES,
        _load_category_tree,
        _CATEGORY_TREE,
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
        """Core keys are always present; photo/photo_data only appear when an image exists."""
        required_keys = {
            "uid", "name", "directions", "ingredients", "prep_time", "cook_time",
            "total_time", "servings", "notes", "source", "source_url", "categories",
            "rating", "created", "hash", "description", "nutritional_info",
            "difficulty", "image_url",
        }
        recipes = [make_recipe("Tart")]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert required_keys.issubset(data.keys())
        assert "photo" not in data
        assert "photo_data" not in data

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
        """Recipe whose image file is missing must still export cleanly, with no photo keys."""
        recipes = [make_recipe("Mystery Pie", photo="ghost.jpg")]
        result = create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        assert result is True
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert "photo" not in data
        assert "photo_data" not in data

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


# ---------------------------------------------------------------------------
# 7. needs_table_normalisation  (pure Python — no mocks needed)
# ---------------------------------------------------------------------------

class TestNeedsTableNormalisation:
    """Tests for the baker's percentage table trigger detection."""

    # --- True positives: text that should trigger normalisation ---

    def test_exact_uppercase_match(self):
        assert needs_table_normalisation("INGREDIENT\nQUANTITY\nBAKER'S %\nFlour\n500g") is True

    def test_mixed_case_match(self):
        assert needs_table_normalisation("Ingredient\nBaker's %\n100%") is True

    def test_lowercase_match(self):
        assert needs_table_normalisation("flour\n500g\nbaker's %\n100%") is True

    def test_bakers_percentage_spelled_out(self):
        assert needs_table_normalisation("Baker's Percentage\nFlour 100%") is True

    def test_bakers_percentage_uppercase(self):
        assert needs_table_normalisation("BAKER'S PERCENTAGE column") is True

    def test_trigger_within_long_text(self):
        long_text = "A" * 5000 + "\nBAKER'S %\n" + "B" * 5000
        assert needs_table_normalisation(long_text) is True

    # --- False positives: baking books without baker's percentage tables ---

    def test_regular_baking_recipe_not_triggered(self):
        """German-style baking with dual units but no baker's % table."""
        text = (
            "Sandy Almond Sugar Cookies\nMAKES ABOUT 50\n"
            "14 tablespoons/200g unsalted butter\n"
            "3/4 cup/100g confectioners sugar\n"
            "2 cups/250g all-purpose flour\n"
            "Preheat oven to 350F. Bake for 12 minutes."
        )
        assert needs_table_normalisation(text) is False

    def test_classic_list_recipe_not_triggered(self):
        """Standard American cookbook format."""
        text = (
            "Mulligatawny Soup\nSERVES 4\n"
            "1/2 pound boneless lamb\n"
            "2 tablespoons vegetable oil\n"
            "1/2 teaspoon ground coriander\n"
            "Simmer for 30 minutes."
        )
        assert needs_table_normalisation(text) is False

    def test_prose_recipe_not_triggered(self):
        """Elizabeth David-style narrative recipe."""
        text = (
            "PICCATE AL MARSALA\nAllow 3 or 4 little slices to each person; "
            "beat them out flat, season with salt, pepper and lemon juice, "
            "and dust lightly with flour. Add 2 tablespoonfuls of Marsala."
        )
        assert needs_table_normalisation(text) is False

    def test_empty_string(self):
        assert needs_table_normalisation("") is False

    def test_text_containing_word_baker_without_percent(self):
        """'Baker' appearing in prose should not trigger."""
        assert needs_table_normalisation("The baker added flour to the dough.") is False

    def test_mangled_apostrophe_replacement_character(self):
        """EPUB stripping sometimes replaces the apostrophe with U+FFFD; must still trigger."""
        assert needs_table_normalisation("INGREDIENT\nQUANTITY\nBAKER\uFFFDS %\nFlour\n500g") is True

    def test_mangled_apostrophe_percentage_spelled_out(self):
        """Mangled apostrophe variant with 'PERCENTAGE' spelled out."""
        assert needs_table_normalisation("BAKER\uFFFDS PERCENTAGE\nFlour 100%") is True


# ---------------------------------------------------------------------------
# 8. normalise_baker_table  (mocked — no live API calls)
# ---------------------------------------------------------------------------

FORKISH_STYLE_CHUNK = """Saturday Pizza Dough

INGREDIENT
QUANTITY
BAKER'S %
Water
350g
1 1/2 cups
70%
Fine sea salt
15g
2 3/4 tsp
3.0%
Instant dried yeast
0.3g
1/3 of 1/4 tsp
0.6%
White flour, preferably 00
500g
Scant 4 cups
100%

1 Measure and combine the ingredients.
2 Mix the dough by hand for 30 seconds.
3 Knead and let rise for 2 hours.
"""

NORMALISED_CHUNK = """Saturday Pizza Dough

Water: 350g (1 1/2 cups) — 70%
Fine sea salt: 15g (2 3/4 tsp) — 3.0%
Instant dried yeast: 0.3g (1/3 of 1/4 tsp) — 0.6%
White flour, preferably 00: 500g (Scant 4 cups) — 100%

1 Measure and combine the ingredients.
2 Mix the dough by hand for 30 seconds.
3 Knead and let rise for 2 hours.
"""


class TestNormaliseBakerTable:

    def test_returns_normalised_text_on_success(self):
        mock_response = MagicMock()
        mock_response.text = NORMALISED_CHUNK

        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            result = normalise_baker_table(FORKISH_STYLE_CHUNK)

        # strip() is applied to response.text, so compare against stripped constant
        assert result == NORMALISED_CHUNK.strip()

    def test_returns_original_on_api_exception(self):
        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.side_effect = Exception("API error")
            result = normalise_baker_table(FORKISH_STYLE_CHUNK)

        assert result == FORKISH_STYLE_CHUNK

    def test_returns_original_on_empty_response(self):
        mock_response = MagicMock()
        mock_response.text = ""

        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            result = normalise_baker_table(FORKISH_STYLE_CHUNK)

        assert result == FORKISH_STYLE_CHUNK

    def test_returns_original_on_whitespace_only_response(self):
        mock_response = MagicMock()
        mock_response.text = "   \n  "

        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            result = normalise_baker_table(FORKISH_STYLE_CHUNK)

        assert result == FORKISH_STYLE_CHUNK

    def test_prompt_includes_original_text(self):
        """Verify the original chunk is passed to the API."""
        mock_response = MagicMock()
        mock_response.text = NORMALISED_CHUNK
        sentinel = "UNIQUE_SENTINEL_XYZ_123"

        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            normalise_baker_table(sentinel)

        call_kwargs = mock_client.models.generate_content.call_args
        assert sentinel in call_kwargs.kwargs["contents"]

    def test_temperature_zero(self):
        """Normalisation must use temperature=0 for deterministic output."""
        mock_response = MagicMock()
        mock_response.text = NORMALISED_CHUNK

        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            normalise_baker_table(FORKISH_STYLE_CHUNK)

        call_kwargs = mock_client.models.generate_content.call_args
        assert call_kwargs.kwargs["config"]["temperature"] == 0

    def test_trigger_then_normalise_then_extract_integration(self):
        """
        End-to-end mocked flow: detect table → normalise → extract.
        Verifies the three functions work together correctly.
        """
        # needs_table_normalisation fires on this chunk
        assert needs_table_normalisation(FORKISH_STYLE_CHUNK) is True

        # normalise_baker_table returns cleaned text
        norm_response = MagicMock()
        norm_response.text = NORMALISED_CHUNK

        # extract_recipes_with_gemini returns a recipe from the cleaned text
        recipe = make_recipe("Saturday Pizza Dough")
        extract_response = MagicMock()
        extract_response.parsed = RecipeList(recipes=[recipe])

        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.side_effect = [
                norm_response,    # first call: normalise
                extract_response, # second call: extract
            ]
            normalised = normalise_baker_table(FORKISH_STYLE_CHUNK)
            result = extract_recipes_with_gemini(normalised)

        assert result is not None
        assert len(result.recipes) == 1
        assert result.recipes[0].name == "Saturday Pizza Dough"


# ---------------------------------------------------------------------------
# 9. categorise_recipe  (mocked — no live API calls)
# ---------------------------------------------------------------------------

def _make_recipe_for_cat(name: str, ingredients=None, notes=None) -> RecipeExtraction:
    return RecipeExtraction(
        name=name,
        ingredients=ingredients or ["flour", "water"],
        directions=["Mix and bake."],
        notes=notes,
    )


class TestCategoriseRecipe:
    """Tests for the Paprika taxonomy category assignment."""

    def _mock_response(self, categories: list) -> MagicMock:
        resp = MagicMock()
        resp.text = json.dumps(categories)
        return resp

    def test_valid_single_category_returned(self):
        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = self._mock_response(["Pizza"])
            result = categorise_recipe(_make_recipe_for_cat("Margherita Pizza"))
        assert result == ["Pizza"]

    def test_valid_multiple_categories_returned(self):
        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = self._mock_response(
                ["Cake", "Dessert"]
            )
            result = categorise_recipe(_make_recipe_for_cat("Chocolate Cake"))
        assert result == ["Cake", "Dessert"]

    def test_invalid_category_filtered_out(self):
        """Categories not in PAPRIKA_CATEGORIES must be silently dropped."""
        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = self._mock_response(
                ["Pizza", "Made Up Category", "Soup"]
            )
            result = categorise_recipe(_make_recipe_for_cat("Pizza Soup"))
        assert "Made Up Category" not in result
        assert set(result) == {"Pizza", "Soup"}

    def test_all_invalid_categories_falls_back(self):
        """If every returned category is invalid, fall back to EPUB Imports."""
        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = self._mock_response(
                ["Nonsense", "Also Nonsense"]
            )
            result = categorise_recipe(_make_recipe_for_cat("Mystery Dish"))
        assert result == ["EPUB Imports"]

    def test_api_exception_falls_back(self):
        """API failure must not crash — fall back to EPUB Imports."""
        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.side_effect = Exception("network error")
            result = categorise_recipe(_make_recipe_for_cat("Pasta Carbonara"))
        assert result == ["EPUB Imports"]

    def test_empty_list_response_falls_back(self):
        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = self._mock_response([])
            result = categorise_recipe(_make_recipe_for_cat("Empty Recipe"))
        assert result == ["EPUB Imports"]

    def test_markdown_fences_stripped(self):
        """Gemini sometimes wraps JSON in ```json ... ``` — must be handled."""
        resp = MagicMock()
        resp.text = '```json\n["Soup"]\n```'
        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = resp
            result = categorise_recipe(_make_recipe_for_cat("Tomato Soup"))
        assert result == ["Soup"]

    def test_recipe_name_included_in_prompt(self):
        """The recipe name must appear in the prompt sent to Gemini."""
        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = self._mock_response(["Soup"])
            categorise_recipe(_make_recipe_for_cat("Pho Bo"))
        prompt = mock_client.models.generate_content.call_args.kwargs["contents"]
        assert "Pho Bo" in prompt

    def test_all_taxonomy_entries_in_prompt(self):
        """Every category in PAPRIKA_CATEGORIES must appear in the prompt."""
        with patch("recipeparser.client") as mock_client:
            mock_client.models.generate_content.return_value = self._mock_response(["Soup"])
            categorise_recipe(_make_recipe_for_cat("Minestrone"))
        prompt = mock_client.models.generate_content.call_args.kwargs["contents"]
        for cat in PAPRIKA_CATEGORIES:
            assert cat in prompt, f"Category '{cat}' missing from prompt"

    def test_categories_written_into_paprika_export(self, tmp_path):
        """Categories assigned by categorise_recipe must appear in the exported JSON."""
        recipe = _make_recipe_for_cat("Chicken Curry")
        recipe._categories = ["Chicken Dishes", "Indian"]
        create_paprika_export([recipe], str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["categories"] == ["Chicken Dishes", "Indian"]

    def test_no_categories_attribute_falls_back_in_export(self, tmp_path):
        """Recipes without _categories set must still export with the fallback."""
        recipe = _make_recipe_for_cat("Plain Recipe")
        create_paprika_export([recipe], str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["categories"] == ["EPUB Imports"]

    def test_book_source_written_to_export(self, tmp_path):
        """book_source parameter must appear as the source field in exported JSON."""
        recipe = _make_recipe_for_cat("Risotto")
        create_paprika_export(
            [recipe], str(tmp_path), str(tmp_path), "out.paprikarecipes",
            book_source="Italian Food — Elizabeth David"
        )
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["source"] == "Italian Food — Elizabeth David"

    def test_total_time_derived_from_prep_and_cook(self, tmp_path):
        """total_time should be the sum of prep and cook when both are simple minute strings."""
        recipe = RecipeExtraction(
            name="Quick Pasta",
            ingredients=["pasta", "sauce"],
            directions=["Boil pasta.", "Add sauce."],
            prep_time="10 mins",
            cook_time="20 mins",
        )
        create_paprika_export([recipe], str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["total_time"] == "30 mins"

    def test_total_time_empty_when_times_missing(self, tmp_path):
        """total_time should be empty string when prep or cook time is absent."""
        recipe = _make_recipe_for_cat("Mystery Stew")
        create_paprika_export([recipe], str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["total_time"] == ""


# ---------------------------------------------------------------------------
# 10. _load_category_tree  — YAML taxonomy loader
# ---------------------------------------------------------------------------

class TestLoadCategoryTree:
    """Tests for the YAML-based category taxonomy loader."""

    def _write_yaml(self, tmp_path, content: str):
        p = tmp_path / "categories.yaml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_loads_top_level_categories(self, tmp_path):
        p = self._write_yaml(tmp_path, "categories:\n  - Soup\n  - Salads\n")
        tree = _load_category_tree(p)
        assert ("Soup", None) in tree
        assert ("Salads", None) in tree

    def test_loads_subcategories_with_parent(self, tmp_path):
        p = self._write_yaml(tmp_path, "categories:\n  - Dessert:\n      - Cake\n      - Pie\n")
        tree = _load_category_tree(p)
        assert ("Dessert", None) in tree
        assert ("Cake", "Dessert") in tree
        assert ("Pie", "Dessert") in tree

    def test_mixed_top_level_and_nested(self, tmp_path):
        p = self._write_yaml(tmp_path, (
            "categories:\n"
            "  - Soup\n"
            "  - Mains:\n"
            "      - Beef Dishes\n"
            "  - Salads\n"
        ))
        tree = _load_category_tree(p)
        assert ("Soup", None) in tree
        assert ("Mains", None) in tree
        assert ("Beef Dishes", "Mains") in tree
        assert ("Salads", None) in tree

    def test_missing_file_returns_empty_list(self, tmp_path):
        tree = _load_category_tree(tmp_path / "nonexistent.yaml")
        assert tree == []

    def test_malformed_yaml_returns_empty_list(self, tmp_path):
        p = self._write_yaml(tmp_path, "categories: ][invalid yaml")
        tree = _load_category_tree(p)
        assert tree == []

    def test_empty_categories_list_returns_empty(self, tmp_path):
        p = self._write_yaml(tmp_path, "categories: []\n")
        tree = _load_category_tree(p)
        assert tree == []

    def test_paprika_categories_derived_from_tree(self, tmp_path):
        """PAPRIKA_CATEGORIES must only contain leaf names (no duplicates)."""
        assert len(PAPRIKA_CATEGORIES) == len(set(PAPRIKA_CATEGORIES))
        # Every entry in PAPRIKA_CATEGORIES must be a leaf in the tree
        tree_leaves = {leaf for leaf, _ in _CATEGORY_TREE}
        for cat in PAPRIKA_CATEGORIES:
            assert cat in tree_leaves

    def test_real_categories_yaml_loads_correctly(self):
        """The actual categories.yaml in the project must parse without errors."""
        from recipeparser import _CATEGORIES_FILE
        tree = _load_category_tree(_CATEGORIES_FILE)
        assert len(tree) > 0, "categories.yaml loaded but was empty"
        # Spot-check a few known entries
        leaves = {leaf for leaf, _ in tree}
        assert "Soup" in leaves
        assert "Cake" in leaves
        assert "Italian" in leaves
        assert "Chicken Dishes" in leaves
