"""Tests for recipeparser.gemini — API calls, retries, normalisation."""
import pytest
from unittest.mock import MagicMock

from tests.conftest import make_recipe, make_mock_client
from recipeparser.models import RecipeExtraction, RecipeList
from recipeparser.gemini import (
    extract_recipes,
    needs_table_normalisation,
    normalise_baker_table,
    _UNITS_RULES,
)


# ---------------------------------------------------------------------------
# needs_table_normalisation  (pure Python — no mocks needed)
# ---------------------------------------------------------------------------

class TestNeedsTableNormalisation:

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

    def test_regular_baking_recipe_not_triggered(self):
        text = (
            "Sandy Almond Sugar Cookies\nMAKES ABOUT 50\n"
            "14 tablespoons/200g unsalted butter\n"
            "3/4 cup/100g confectioners sugar\n"
            "2 cups/250g all-purpose flour\n"
            "Preheat oven to 350F. Bake for 12 minutes."
        )
        assert needs_table_normalisation(text) is False

    def test_classic_list_recipe_not_triggered(self):
        text = (
            "Mulligatawny Soup\nSERVES 4\n"
            "1/2 pound boneless lamb\n"
            "2 tablespoons vegetable oil\n"
            "1/2 teaspoon ground coriander\n"
            "Simmer for 30 minutes."
        )
        assert needs_table_normalisation(text) is False

    def test_prose_recipe_not_triggered(self):
        text = (
            "PICCATE AL MARSALA\nAllow 3 or 4 little slices to each person; "
            "beat them out flat, season with salt, pepper and lemon juice, "
            "and dust lightly with flour. Add 2 tablespoonfuls of Marsala."
        )
        assert needs_table_normalisation(text) is False

    def test_empty_string(self):
        assert needs_table_normalisation("") is False

    def test_text_containing_word_baker_without_percent(self):
        assert needs_table_normalisation("The baker added flour to the dough.") is False

    def test_mangled_apostrophe_replacement_character(self):
        """EPUB stripping sometimes replaces the apostrophe with U+FFFD; must still trigger."""
        assert needs_table_normalisation("INGREDIENT\nQUANTITY\nBAKER\uFFFDS %\nFlour\n500g") is True

    def test_mangled_apostrophe_percentage_spelled_out(self):
        assert needs_table_normalisation("BAKER\uFFFDS PERCENTAGE\nFlour 100%") is True


# ---------------------------------------------------------------------------
# normalise_baker_table  (mocked — no live API calls)
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
        client = make_mock_client(return_value=mock_response)

        result = normalise_baker_table(FORKISH_STYLE_CHUNK, client)

        assert result == NORMALISED_CHUNK.strip()

    def test_returns_original_on_api_exception(self):
        client = make_mock_client(side_effect=Exception("API error"))
        result = normalise_baker_table(FORKISH_STYLE_CHUNK, client)
        assert result == FORKISH_STYLE_CHUNK

    def test_returns_original_on_empty_response(self):
        mock_response = MagicMock()
        mock_response.text = ""
        client = make_mock_client(return_value=mock_response)

        result = normalise_baker_table(FORKISH_STYLE_CHUNK, client)
        assert result == FORKISH_STYLE_CHUNK

    def test_returns_original_on_whitespace_only_response(self):
        mock_response = MagicMock()
        mock_response.text = "   \n  "
        client = make_mock_client(return_value=mock_response)

        result = normalise_baker_table(FORKISH_STYLE_CHUNK, client)
        assert result == FORKISH_STYLE_CHUNK

    def test_prompt_includes_original_text(self):
        mock_response = MagicMock()
        mock_response.text = NORMALISED_CHUNK
        sentinel = "UNIQUE_SENTINEL_XYZ_123"
        client = make_mock_client(return_value=mock_response)

        normalise_baker_table(sentinel, client)

        call_kwargs = client.models.generate_content.call_args
        assert sentinel in call_kwargs.kwargs["contents"]

    def test_temperature_zero(self):
        mock_response = MagicMock()
        mock_response.text = NORMALISED_CHUNK
        client = make_mock_client(return_value=mock_response)

        normalise_baker_table(FORKISH_STYLE_CHUNK, client)

        call_kwargs = client.models.generate_content.call_args
        assert call_kwargs.kwargs["config"]["temperature"] == 0

    def test_trigger_then_normalise_then_extract_integration(self):
        """End-to-end mocked flow: detect table → normalise → extract."""
        assert needs_table_normalisation(FORKISH_STYLE_CHUNK) is True

        norm_response = MagicMock()
        norm_response.text = NORMALISED_CHUNK

        recipe = make_recipe("Saturday Pizza Dough")
        extract_response = MagicMock()
        extract_response.parsed = RecipeList(recipes=[recipe])

        client = MagicMock()
        client.models.generate_content.side_effect = [norm_response, extract_response]

        normalised = normalise_baker_table(FORKISH_STYLE_CHUNK, client)
        result = extract_recipes(normalised, client)

        assert result is not None
        assert len(result.recipes) == 1
        assert result.recipes[0].name == "Saturday Pizza Dough"


# ---------------------------------------------------------------------------
# extract_recipes  (mocked — no live API calls)
# ---------------------------------------------------------------------------

class TestExtractRecipes:

    def _make_recipe_list(self, *recipes: RecipeExtraction) -> RecipeList:
        return RecipeList(recipes=list(recipes))

    def test_valid_response_returned_as_recipe_list(self):
        expected = self._make_recipe_list(make_recipe("Chocolate Cake", photo="cake.jpg"))
        mock_response = MagicMock()
        mock_response.parsed = expected
        client = make_mock_client(return_value=mock_response)

        result = extract_recipes("some chunk of text", client)

        assert result is not None
        assert len(result.recipes) == 1
        assert result.recipes[0].name == "Chocolate Cake"
        assert result.recipes[0].photo_filename == "cake.jpg"

    def test_api_exception_returns_none(self):
        client = make_mock_client(side_effect=Exception("503 Service Unavailable"))
        result = extract_recipes("some chunk of text", client)
        assert result is None

    def test_empty_recipe_list_returned_cleanly(self):
        mock_response = MagicMock()
        mock_response.parsed = RecipeList(recipes=[])
        client = make_mock_client(return_value=mock_response)

        result = extract_recipes("Introduction to the author.", client)

        assert result is not None
        assert result.recipes == []

    def test_multiple_recipes_in_one_chunk(self):
        expected = self._make_recipe_list(
            make_recipe("Pasta Primavera"),
            make_recipe("Caesar Salad"),
            make_recipe("Tiramisu"),
        )
        mock_response = MagicMock()
        mock_response.parsed = expected
        client = make_mock_client(return_value=mock_response)

        result = extract_recipes("chunk with three recipes", client)

        assert result is not None
        assert len(result.recipes) == 3
        assert result.recipes[1].name == "Caesar Salad"

    def test_correct_model_and_config_passed_to_api(self):
        mock_response = MagicMock()
        mock_response.parsed = RecipeList(recipes=[])
        client = make_mock_client(return_value=mock_response)

        extract_recipes("any text", client)

        call_kwargs = client.models.generate_content.call_args
        assert call_kwargs.kwargs["model"] == "gemini-2.5-flash"
        config = call_kwargs.kwargs["config"]
        assert config["response_mime_type"] == "application/json"
        assert config["temperature"] == 0.1

    def test_text_chunk_included_in_prompt(self):
        mock_response = MagicMock()
        mock_response.parsed = RecipeList(recipes=[])
        sentinel = "UNIQUE_SENTINEL_STRING_XYZ"
        client = make_mock_client(return_value=mock_response)

        extract_recipes(sentinel, client)

        call_kwargs = client.models.generate_content.call_args
        assert sentinel in call_kwargs.kwargs["contents"]

    def test_unicode_fraction_fields_preserved(self):
        recipe_with_fractions = RecipeExtraction(
            name="Scones",
            ingredients=["1/2 cup butter", "3/4 cup milk"],
            directions=["Mix.", "Bake."],
        )
        mock_response = MagicMock()
        mock_response.parsed = RecipeList(recipes=[recipe_with_fractions])
        client = make_mock_client(return_value=mock_response)

        result = extract_recipes("scone text", client)

        assert result.recipes[0].ingredients[0] == "1/2 cup butter"
        assert result.recipes[0].ingredients[1] == "3/4 cup milk"

    def test_none_parsed_response_handled(self):
        mock_response = MagicMock()
        mock_response.parsed = None
        client = make_mock_client(return_value=mock_response)

        result = extract_recipes("any text", client)

        assert result is None


# ---------------------------------------------------------------------------
# units preference — prompt rule injection
# ---------------------------------------------------------------------------

DUAL_UOM_CHUNK = (
    "Sandy Almond Sugar Cookies\n"
    "MAKES ABOUT 55 COOKIES\n"
    "14 tablespoons/200g unsalted butter, softened\n"
    "3/4 cup/100g confectioners sugar\n"
    "2 cups/250g all-purpose flour\n"
    "1 teaspoon vanilla extract\n"
    "Preheat oven to 350F/180C. Mix butter and sugar. "
    "Stir in flour. Bake 12 minutes."
)


class TestUnitsPreference:
    """Verify that the units preference is wired into the extraction prompt."""

    def _run_extract(self, units: str) -> str:
        """Return the prompt string sent to the API for a given units value."""
        mock_response = MagicMock()
        mock_response.parsed = RecipeList(recipes=[])
        client = make_mock_client(return_value=mock_response)
        extract_recipes(DUAL_UOM_CHUNK, client, units=units)
        return client.models.generate_content.call_args.kwargs["contents"]

    def test_book_default_has_no_units_rule(self):
        prompt = self._run_extract("book")
        assert "dual" not in prompt.lower()
        assert "metric" not in prompt.lower()
        assert "cup/tbsp" not in prompt.lower()

    def test_metric_rule_in_prompt(self):
        prompt = self._run_extract("metric")
        assert "metric" in prompt.lower()
        assert "gram" in prompt.lower() or "ml" in prompt.lower()

    def test_us_rule_in_prompt(self):
        prompt = self._run_extract("us")
        assert "us" in prompt.lower() or "cup" in prompt.lower()

    def test_imperial_rule_in_prompt(self):
        prompt = self._run_extract("imperial")
        assert "imperial" in prompt.lower() or "oz" in prompt.lower() or "ounce" in prompt.lower()

    def test_all_units_choices_defined(self):
        for choice in ("metric", "us", "imperial", "book"):
            assert choice in _UNITS_RULES, f"'{choice}' missing from _UNITS_RULES"

    def test_unknown_units_falls_back_to_book(self):
        prompt_book = self._run_extract("book")
        prompt_unknown = self._run_extract("xyzzy")
        assert prompt_book == prompt_unknown

    def test_chunk_always_in_prompt(self):
        sentinel = "UNIQUE_CHUNK_SENTINEL_ABC"
        mock_response = MagicMock()
        mock_response.parsed = RecipeList(recipes=[])
        client = make_mock_client(return_value=mock_response)
        extract_recipes(sentinel, client, units="metric")
        prompt = client.models.generate_content.call_args.kwargs["contents"]
        assert sentinel in prompt


class TestPhaseInstructions:
    """Verify that multi-phase recipe handling is explicit in the prompt."""

    def _get_prompt(self) -> str:
        mock_response = MagicMock()
        mock_response.parsed = RecipeList(recipes=[])
        client = make_mock_client(return_value=mock_response)
        extract_recipes("some text", client)
        return client.models.generate_content.call_args.kwargs["contents"]

    def test_phase_instruction_in_prompt(self):
        prompt = self._get_prompt()
        assert "phase" in prompt.lower()

    def test_phase_label_bold_markdown_in_prompt(self):
        """The prompt must instruct Gemini to use **bold** Markdown for phase headings."""
        prompt = self._get_prompt()
        assert "**Phase" in prompt or "**phase" in prompt.lower()

    def test_do_not_merge_phases_instruction(self):
        prompt = self._get_prompt()
        assert "flatten" in prompt.lower() or "merge" in prompt.lower()

    def test_phase_label_is_separate_list_item(self):
        """The prompt must make clear the bold label is its own list entry."""
        prompt = self._get_prompt()
        assert "separate list item" in prompt.lower() or "own separate" in prompt.lower()


# ---------------------------------------------------------------------------
# extract_text_via_vision  (mocked — no live API calls, no real PDF needed)
# ---------------------------------------------------------------------------

class TestExtractTextViaVision:
    """
    Unit tests for gemini.extract_text_via_vision().

    Strategy: mock the fitz.Document object so no real PDF file is needed,
    and mock the Gemini client so no real API calls are made.
    """

    def _make_doc(self, page_count: int = 1, pixmap_bytes: bytes = b"PNG_BYTES"):
        """Return a minimal mock fitz.Document with `page_count` pages."""
        doc = MagicMock()
        doc.page_count = page_count

        # Each page returns a pixmap whose tobytes() returns pixmap_bytes
        pixmap = MagicMock()
        pixmap.tobytes.return_value = pixmap_bytes
        page = MagicMock()
        page.get_pixmap.return_value = pixmap
        doc.__getitem__ = MagicMock(return_value=page)

        return doc

    def _make_vision_client(self, page_texts):
        """
        Return a mock Gemini client whose generate_content returns successive
        page transcripts from `page_texts` (one per call).
        """
        responses = []
        for text in page_texts:
            r = MagicMock()
            r.text = text
            responses.append(r)
        client = MagicMock()
        client.models.generate_content.side_effect = responses
        return client

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_single_page_returns_text(self):
        from recipeparser.gemini import extract_text_via_vision
        doc = self._make_doc(page_count=1)
        client = self._make_vision_client(["Chocolate Cake\n1 cup flour\nMix and bake."])

        result = extract_text_via_vision(doc, client)

        assert "Chocolate Cake" in result
        assert "1 cup flour" in result

    def test_multi_page_concatenated_with_double_newline(self):
        from recipeparser.gemini import extract_text_via_vision
        doc = self._make_doc(page_count=3)
        client = self._make_vision_client(["Page 1 text", "Page 2 text", "Page 3 text"])

        result = extract_text_via_vision(doc, client)

        assert result == "Page 1 text\n\nPage 2 text\n\nPage 3 text"

    def test_gemini_called_once_per_page(self):
        from recipeparser.gemini import extract_text_via_vision
        doc = self._make_doc(page_count=4)
        client = self._make_vision_client(["text"] * 4)

        extract_text_via_vision(doc, client)

        assert client.models.generate_content.call_count == 4

    def test_pixmap_rendered_at_2x_scale(self):
        """Verify get_pixmap is called with a 2× Matrix (144 DPI)."""
        import fitz
        from recipeparser.gemini import extract_text_via_vision
        doc = self._make_doc(page_count=1)
        client = self._make_vision_client(["some text"])

        extract_text_via_vision(doc, client)

        page = doc[0]
        call_kwargs = page.get_pixmap.call_args
        matrix = call_kwargs.kwargs.get("matrix") or call_kwargs.args[0]
        # fitz.Matrix(2, 2) has .a == 2.0 and .d == 2.0
        assert matrix.a == pytest.approx(2.0)
        assert matrix.d == pytest.approx(2.0)

    def test_image_sent_as_png_mime_type(self):
        """
        Verify the Gemini call includes a Part with mime_type='image/png'.

        Strategy: inspect the ``contents`` list passed to generate_content.
        The first element must be a Part whose inline_data.mime_type is 'image/png'.
        We use a real (non-mocked) fitz document so the pixmap path runs normally,
        and a mock client so no real API call is made.
        """
        from recipeparser.gemini import extract_text_via_vision
        from google.genai import types as genai_types

        doc = self._make_doc(page_count=1, pixmap_bytes=b"\x89PNG_FAKE_DATA")
        client = self._make_vision_client(["recipe text"])

        extract_text_via_vision(doc, client)

        call_args = client.models.generate_content.call_args
        contents = call_args.kwargs.get("contents") or call_args.args[0]
        # contents is [Part, prompt_string]
        assert isinstance(contents, list) and len(contents) >= 1
        part = contents[0]
        # Part.from_bytes sets inline_data.mime_type
        assert part.inline_data.mime_type == "image/png"

    def test_ocr_prompt_instructs_transcription(self):
        """Verify the prompt sent to Gemini mentions OCR / transcription."""
        from recipeparser.gemini import extract_text_via_vision
        doc = self._make_doc(page_count=1)
        client = self._make_vision_client(["text"])

        extract_text_via_vision(doc, client)

        call_args = client.models.generate_content.call_args
        contents = call_args.kwargs.get("contents") or call_args.args[0]
        # contents is a list: [Part, prompt_string]
        prompt_str = contents[1] if isinstance(contents, list) else str(contents)
        assert "transcribe" in prompt_str.lower() or "ocr" in prompt_str.lower()

    def test_temperature_zero_for_ocr(self):
        """OCR should use temperature=0 for deterministic output."""
        from recipeparser.gemini import extract_text_via_vision
        doc = self._make_doc(page_count=1)
        client = self._make_vision_client(["text"])

        extract_text_via_vision(doc, client)

        call_kwargs = client.models.generate_content.call_args.kwargs
        assert call_kwargs["config"]["temperature"] == 0

    # ------------------------------------------------------------------
    # Partial failure — some pages succeed, some fail
    # ------------------------------------------------------------------

    def test_failed_page_skipped_others_returned(self):
        """If one page raises, the others should still be returned."""
        from recipeparser.gemini import extract_text_via_vision
        doc = self._make_doc(page_count=3)

        r1 = MagicMock()
        r1.text = "Page 1 text"
        r3 = MagicMock()
        r3.text = "Page 3 text"
        client = MagicMock()
        client.models.generate_content.side_effect = [r1, Exception("API error"), r3]

        result = extract_text_via_vision(doc, client)

        assert "Page 1 text" in result
        assert "Page 3 text" in result

    def test_empty_response_page_skipped(self):
        """Pages where Gemini returns empty string are silently skipped."""
        from recipeparser.gemini import extract_text_via_vision
        doc = self._make_doc(page_count=2)
        client = self._make_vision_client(["", "Real recipe text"])

        result = extract_text_via_vision(doc, client)

        assert result == "Real recipe text"

    def test_whitespace_only_response_page_skipped(self):
        """Pages where Gemini returns only whitespace are silently skipped."""
        from recipeparser.gemini import extract_text_via_vision
        doc = self._make_doc(page_count=2)
        client = self._make_vision_client(["   \n  ", "Actual content"])

        result = extract_text_via_vision(doc, client)

        assert result == "Actual content"

    # ------------------------------------------------------------------
    # Total failure — all pages fail
    # ------------------------------------------------------------------

    def test_all_pages_fail_raises_runtime_error(self):
        """If every page fails, RuntimeError is raised (not a silent empty string)."""
        from recipeparser.gemini import extract_text_via_vision
        doc = self._make_doc(page_count=2)
        client = MagicMock()
        client.models.generate_content.side_effect = Exception("Vision API down")

        with pytest.raises(RuntimeError, match="no text for any page"):
            extract_text_via_vision(doc, client)

    def test_all_pages_empty_raises_runtime_error(self):
        """If every page returns empty text, RuntimeError is raised."""
        from recipeparser.gemini import extract_text_via_vision
        doc = self._make_doc(page_count=3)
        client = self._make_vision_client(["", "", ""])

        with pytest.raises(RuntimeError, match="no text for any page"):
            extract_text_via_vision(doc, client)
