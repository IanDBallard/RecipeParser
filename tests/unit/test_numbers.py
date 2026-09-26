"""The numbers a text writes, by value (verbatim-ingestion design D2, D3)."""
import pytest

from recipeparser.core.numbers import unmatched_numbers, written_values

# The NYT Cooking page as r.jina.ai rendered it on 2026-09-25 (spec, Why).
NYT_PAGE = (
    "Spaghetti Carbonara\n"
    "Salt\n"
    "2 large eggs and 2 large yolks, room temperature\n"
    "1 ounce (about ⅓ packed cup) grated pecorino Romano, plus additional for serving\n"
    "1 ounce (about ⅓ packed cup) grated Parmesan\n"
    "Coarsely ground black pepper\n"
    "1 tablespoon olive oil\n"
    "3 ½ ounces of slab guanciale (see recipe), pancetta or bacon, sliced into pieces about ¼ inch thick by 1⅓ inch square\n"
    "12 ounces spaghetti (about ¾ box)\n"
)
VERBATIM = [
    "Salt",
    "2 large eggs and 2 large yolks, room temperature",
    "1 ounce (about 1/3 packed cup) grated pecorino Romano, plus additional for serving",
    "1 ounce (about 1/3 packed cup) grated Parmesan",
    "Coarsely ground black pepper",
    "1 tablespoon olive oil",
    "3 1/2 ounces of slab guanciale (see recipe), pancetta or bacon, sliced into pieces about 1/4 inch thick by 1 1/3 inch square",
    "12 ounces spaghetti (about 3/4 box)",
]
# What the library stored (spec, Why).
REWRITTEN = [
    "Salt",
    "2 large eggs and 2 large yolks, room temperature",
    "28g grated pecorino Romano, plus additional for serving",
    "28g grated Parmesan",
    "Coarsely ground black pepper",
    "1 tablespoon olive oil",
    "99g of slab guanciale (see recipe), pancetta or bacon, sliced into pieces about 0.6cm thick by 3.4cm square",
    "340g spaghetti (about 3/4 box)",
]


def test_the_verbatim_carbonara_writes_nothing_the_page_does_not():
    assert unmatched_numbers(NYT_PAGE, VERBATIM) == []


def test_the_stored_carbonara_is_caught_by_every_converted_number():
    assert unmatched_numbers(NYT_PAGE, REWRITTEN) == ["28", "28", "99", "0.6", "3.4", "340"]


@pytest.mark.parametrize(
    "source, line",
    [
        ("1½ cups flour", "1 1/2 cups flour"),
        ("1 ½ cups flour", "1 1/2 cups flour"),
        ("3⁄4 cup milk", "3/4 cup milk"),        # U+2044 fraction slash
        ("1,5 dl grädde", "1.5 dl grädde"),       # a decimal comma
        ("2 cups flour, sifted\nwith 1 tsp salt", "2 cups flour, sifted with 1 tsp salt"),  # a PDF line wrap
        ("⅛ tsp cayenne", "1/8 tsp cayenne"),
    ],
)
def test_the_one_permitted_rewrite_and_layout_changes_pass(source, line):
    assert unmatched_numbers(source, [line]) == []


def test_a_line_break_that_joins_a_number_to_a_fraction_is_not_a_rewrite():
    # Review Focus 1: whitespace collapses "Serves 4" and "½ cup" into the mixed number "4 1/2".
    assert unmatched_numbers("Serves 4\n½ cup sugar", ["1/2 cup sugar"]) == []


def test_a_rounded_number_is_a_rewrite():
    assert unmatched_numbers("1 1/2 cups flour", ["1.5 cups flour"]) == []  # the same value
    assert unmatched_numbers("1 1/3 cups flour", ["1.3 cups flour"]) == ["1.3"]


def test_written_values_reads_fractions_and_mixed_numbers_by_value():
    assert written_values("1 ounce (about 1/3 packed cup)") == pytest.approx([1.0, 1 / 3])
    assert written_values("3 1/2 ounces") == pytest.approx([3.5, 3.0, 0.5])


def test_a_zero_denominator_never_matches():
    assert unmatched_numbers("1/0 cup", ["1/0 cup"]) == ["1/0"]


# Old books spell their numbers; D2 checks presence by value, so a number word in the source
# states its value (Task 8 ruling). The four gutenberg-multi.epub (Apicius) recipes it came from.
@pytest.mark.parametrize(
    "source, line",
    [
        ("TAKE TWO COOKED BRAINS AND HALF A POUND\nOF MEAT", "1/2 pound of meat (ground as for forcemeat)"),
        ("A PINT OF\nWATER, THAT IS, A THIRD PART", "WATER (A pint, or 1/3 part)"),
        ("ADDING] [1] HALF AN\nOUNCE OF PEPPER", "1/2 ounce of pepper"),
        ("SPRINKLE\nHALF AN OUNCE OF PEPPER", "1/2 ounce of pepper"),
        ("A THIRD PART of the paste", "1/3 of the paste"),
        ("two thirds cup sugar", "2/3 cup sugar"),
        ("three-quarters cup milk", "3/4 cup milk"),
        ("a dozen eggs", "12 eggs"),
        ("Twenty almonds", "20 almonds"),
    ],
)
def test_a_number_the_source_spells_as_a_word_counts_by_value(source, line):
    assert unmatched_numbers(source, [line]) == []


def test_a_spelled_number_still_catches_a_different_value():
    assert unmatched_numbers("half a cup", ["1/3 cup"]) == ["1/3"]


@pytest.mark.parametrize("source, line", [("often", "10 often"), ("tone", "1 tone"), ("none", "1 none")])
def test_a_number_word_inside_another_word_does_not_count(source, line):
    assert unmatched_numbers(source, [line]) == [line.split()[0]]


def test_written_values_reads_numerals_only():
    assert written_values("half a cup and 2 eggs") == [2.0]
