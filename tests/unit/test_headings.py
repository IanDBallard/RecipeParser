"""The heading text rules (Cayenne ingredient headings design D3, backfill rules)."""
import pytest

from recipeparser.core.headings import heading_name, heading_shape


@pytest.mark.parametrize("line, name", [
    ("For the sauce:", "For the sauce"),
    ("**For the Ricotta:**", "For the Ricotta"),
    ("**For the dough**", "For the dough"),
    ("FOR THE FILLING:", "FOR THE FILLING"),
    ("PUDDING", "PUDDING"),
    ("‘‘EXTRA’’ SAUCE", "‘‘EXTRA’’ SAUCE"),
    ("  To serve:  ", "To serve"),
    ("**PHASE 1 — Levain**", "PHASE 1 — Levain"),
])
def test_heading_name_strips_emphasis_and_trailing_colon(line, name):
    assert heading_name(line) == name


def test_heading_name_never_returns_empty():
    assert heading_name("**:**") == "**:**"


@pytest.mark.parametrize("line", [
    "For the sauce:", "To Grill:", "Optional:", "**For the Spinach:**", "**Five Spice Rub**",
    "FOR THE COBBLER TOPPING:", "STICKY TOFFEE TOPPING", "CRUST", "For the marinade",
    "For the béchamel", "To serve", "Garnish", "To finish",
])
def test_heading_shapes(line):
    assert heading_shape(line) == "heading"


@pytest.mark.parametrize("line", ["Lemon Sauce", "Sponge Sheet Cake", "Sugar", "Chocolate Ganache"])
def test_short_title_case_lines_are_unsure(line):
    assert heading_shape(line) == "unsure"


@pytest.mark.parametrize("line", [
    "To serve Maneesh (see here) or pitta breads (see here)",  # a food, not a heading
    "Butter for greasing",
    "Egg whites",
    "2 cups flour",
    "INGREDIENTS (US CUP = 240ML )",  # digits: never a heading by rule
    "(450g, cut into 1-inch pieces; your butcher can do this for you!)",
    "other ingredients",
    "",
])
def test_not_headings(line):
    assert heading_shape(line) is None
