"""Unit tests for recipeparser.utils — title_case() function."""
import pytest

from recipeparser.utils import title_case


class TestTitleCase:
    """Tests for the title_case() utility function."""

    # ------------------------------------------------------------------
    # Basic capitalisation
    # ------------------------------------------------------------------

    def test_simple_title_case(self):
        assert title_case("chocolate chip cookies") == "Chocolate Chip Cookies"

    def test_all_caps_input(self):
        assert title_case("CHOCOLATE CHIP COOKIES") == "Chocolate Chip Cookies"

    def test_already_correct(self):
        assert title_case("Chocolate Chip Cookies") == "Chocolate Chip Cookies"

    def test_mixed_case_input(self):
        assert title_case("cHoCoLaTe ChIp CoOkIeS") == "Chocolate Chip Cookies"

    # ------------------------------------------------------------------
    # Stop words
    # ------------------------------------------------------------------

    def test_stop_word_in_middle(self):
        assert title_case("macaroni and cheese") == "Macaroni and Cheese"

    def test_multiple_stop_words(self):
        assert title_case("bread and butter with jam") == "Bread and Butter with Jam"

    def test_stop_word_at_start_is_capitalised(self):
        assert title_case("a tale of two cities") == "A Tale of Two Cities"

    def test_stop_word_at_end_is_capitalised(self):
        # "of" is a stop word but must be capitalised when it is the last word
        assert title_case("what dreams are made of") == "What Dreams Are Made Of"

    def test_preposition_in_middle(self):
        assert title_case("chicken in a pot") == "Chicken in a Pot"

    def test_conjunction_in_middle(self):
        assert title_case("fish or chips") == "Fish or Chips"

    # ------------------------------------------------------------------
    # Acronym / abbreviation preservation
    # ------------------------------------------------------------------

    def test_all_caps_abbreviation_preserved(self):
        assert title_case("BBQ chicken wings") == "BBQ Chicken Wings"

    def test_city_abbreviation_preserved(self):
        assert title_case("NYC style pizza") == "NYC Style Pizza"

    def test_abbreviation_in_all_caps_source(self):
        # Source is all-caps but BBQ should stay as-is
        assert title_case("BBQ CHICKEN WINGS") == "BBQ Chicken Wings"

    # ------------------------------------------------------------------
    # Hyphenated compounds
    # ------------------------------------------------------------------

    def test_hyphenated_compound(self):
        assert title_case("pan-fried chicken") == "Pan-Fried Chicken"

    def test_hyphenated_all_caps(self):
        assert title_case("PAN-FRIED CHICKEN") == "Pan-Fried Chicken"

    def test_hyphenated_stop_word_in_compound(self):
        # "in" is a stop word but inside a hyphenated compound it should be capitalised
        # (it's not the first word of the compound, but it's a meaningful part)
        result = title_case("stir-in sauce")
        # "stir-in" — "in" is a stop word but it's part of a compound; first part capitalised
        assert result == "Stir-In Sauce"

    def test_multiple_hyphens(self):
        assert title_case("slow-and-low BBQ ribs") == "Slow-And-Low BBQ Ribs"

    # ------------------------------------------------------------------
    # Whitespace handling
    # ------------------------------------------------------------------

    def test_leading_trailing_whitespace(self):
        assert title_case("  chocolate chip cookies  ") == "Chocolate Chip Cookies"

    def test_multiple_internal_spaces(self):
        assert title_case("chocolate  chip   cookies") == "Chocolate Chip Cookies"

    def test_single_word(self):
        assert title_case("cookies") == "Cookies"

    def test_single_word_all_caps(self):
        assert title_case("COOKIES") == "Cookies"

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_empty_string(self):
        assert title_case("") == ""

    def test_whitespace_only(self):
        assert title_case("   ") == "   "

    def test_single_character(self):
        assert title_case("a") == "A"

    def test_numbers_in_title(self):
        assert title_case("30-minute chicken soup") == "30-Minute Chicken Soup"

    def test_apostrophe_in_word(self):
        # Apostrophes inside words should not break capitalisation
        result = title_case("grandma's apple pie")
        assert result == "Grandma's Apple Pie"

    def test_book_source_string(self):
        """Simulates the book_source 'Title — Author' pattern used in export.py."""
        assert title_case("THE JOY OF COOKING — IRMA S. ROMBAUER") == "The Joy of Cooking — Irma S. Rombauer"

    def test_real_world_all_caps_recipe(self):
        assert title_case("CLASSIC BEEF STEW WITH VEGETABLES") == "Classic Beef Stew with Vegetables"

    def test_real_world_lowercase_url_recipe(self):
        assert title_case("easy one-pot pasta with garlic and olive oil") == "Easy One-Pot Pasta with Garlic and Olive Oil"

    def test_two_word_title(self):
        assert title_case("beef stew") == "Beef Stew"

    def test_stop_word_only_title(self):
        # A title that is just a stop word — must still be capitalised (first == last)
        assert title_case("a") == "A"
