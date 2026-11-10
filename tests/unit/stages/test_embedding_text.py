"""build_embedding_text: the one text every recipe embedding is made from (Cayenne AI search §6)."""
from recipeparser.core.stages.embed import DIRECTIONS_CHARS, build_embedding_text


def test_text_parts_in_order() -> None:
    assert build_embedding_text(
        "Pad Thai",
        ["Thai", "Noodles"],
        ["200 g rice noodles", "2 eggs"],
        ["Soak the noodles.", "Fry."],
    ) == "Pad Thai\nThai, Noodles\n200 g rice noodles 2 eggs\nSoak the noodles. Fry."


def test_directions_cut_at_400() -> None:
    text = build_embedding_text("Toast", [], ["1 slice bread"], ["x" * 600, "y" * 400])
    assert DIRECTIONS_CHARS == 400
    assert len(text.split("\n")[-1].strip()) == DIRECTIONS_CHARS


def test_text_omits_empty_parts() -> None:
    assert build_embedding_text("Toast", [], [], []) == "Toast"
    assert build_embedding_text("Toast", [], ["1 slice bread"], []) == "Toast\n1 slice bread"


def test_names_trimmed_and_blank_dropped() -> None:
    assert build_embedding_text("Toast", [" Thai ", ""], [], []) == "Toast\nThai"
