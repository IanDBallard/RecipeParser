"""RecipePan's field descriptions list exactly core.pans' vocabularies, so they cannot drift."""

from recipeparser.core.pans import PAN_FAMILIES, PAN_SHAPES
from recipeparser.models import RecipePan


def test_shape_description_lists_every_shape():
    description = RecipePan.model_fields["shape"].description
    for value in PAN_SHAPES:
        assert value in description


def test_family_description_lists_every_family():
    description = RecipePan.model_fields["family"].description
    for value in PAN_FAMILIES:
        assert value in description


def test_bake_type_description_counts_every_bake_type():
    from recipeparser.core.pans import BAKE_TYPES
    from recipeparser.models import _BAKE_TYPE_DESC

    assert f"list of {len(BAKE_TYPES)} bake types" in _BAKE_TYPE_DESC


# Cayenne pie tins (D57, D59): a pie plate is its own family; a crust alone is a bake type.
def test_pie_shell_is_a_bake_type_and_pie_a_family():
    from recipeparser.core.pans import BAKE_TYPES

    assert "pie-shell" in BAKE_TYPES
    assert "pie" in PAN_FAMILIES


def test_a_pie_family_tin_is_kept_and_an_unknown_family_is_refused():
    from recipeparser.core.pans import normalise_pans

    def tin(family):
        return RecipePan.model_validate(
            {"id": "pan_01", "raw": "9-inch pie plate", "shape": "round", "family": family, "dims_mm": {"d": 229}}
        )

    assert normalise_pans([tin("pie")])[0][0].family == "pie"
    assert normalise_pans([tin("pastry")])[0][0].family is None


def test_check_bake_keeps_pie_shell():
    from recipeparser.core.pans import check_bake
    from recipeparser.models import TokenizedDirection

    d = [TokenizedDirection(step=1, text="Line the plate."), TokenizedDirection(step=2, text="Bake 20 min.")]
    assert check_bake("pie-shell", 1, d, set()) == ("pie-shell", 1)
