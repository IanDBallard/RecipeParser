"""REFINE stores every title in house title case (the title-case titles ruling, 2026-10-03)."""
from unittest.mock import MagicMock, patch

from recipeparser.core.pipeline import _pre_parsed_to_refinement
from recipeparser.core.stages.refine import refine
from recipeparser.models import CayenneRecipe, CayenneRefinement, RecipeExtraction, TokenizedDirection

_PATCH = "recipeparser.core.stages.refine.refine_recipe_for_cayenne"

RAW = RecipeExtraction(name="CHICKEN TIKKA MASALA", ingredients=[], directions=["Cook."])


def _refinement(title: str) -> CayenneRefinement:
    return CayenneRefinement(
        title=title, base_servings=4, structured_ingredients=[],
        tokenized_directions=[TokenizedDirection(step=1, text="Cook.")],
    )


def test_an_all_caps_title_is_stored_in_title_case():
    with patch(_PATCH, return_value=_refinement("CHICKEN TIKKA MASALA")):
        result = refine(RAW, client=MagicMock())
    assert result.title == "Chicken Tikka Masala"


def test_a_mixed_case_title_is_normalised_too():
    with patch(_PATCH, return_value=_refinement("chicken tikka Masala with BBQ naan")):
        result = refine(RAW, client=MagicMock())
    assert result.title == "Chicken Tikka Masala with BBQ Naan"


def test_a_paprika_restore_skips_refine_and_keeps_the_cooks_title():
    pr = CayenneRecipe(
        title="CHICKEN TIKKA MASALA", structured_ingredients=[],
        tokenized_directions=[TokenizedDirection(step=1, text="Cook.")],
    )
    assert _pre_parsed_to_refinement(pr).title == "CHICKEN TIKKA MASALA"


def test_a_restored_recipe_keeps_its_tins_and_bake():
    from recipeparser.models import RecipePan

    tin = RecipePan(id="pan_01", raw="20cm round tin", shape="round", family="round-cake")
    pr = CayenneRecipe(
        title="Cake", structured_ingredients=[],
        tokenized_directions=[TokenizedDirection(step=1, text="Bake in a {{pan_01|20cm round tin}}.")],
        pans=[tin], bake_type="sponge", bake_step=0,
    )
    out = _pre_parsed_to_refinement(pr)
    assert [p.id for p in out.pans] == ["pan_01"]
    assert out.bake_type == "sponge" and out.bake_step == 0


def test_a_restored_recipe_without_tins_has_none():
    pr = CayenneRecipe(
        title="Cake", structured_ingredients=[],
        tokenized_directions=[TokenizedDirection(step=1, text="Cook.")],
    )
    out = _pre_parsed_to_refinement(pr)
    assert out.pans == [] and out.bake_type is None and out.bake_step is None


def _restored(spec):
    pr = CayenneRecipe(title="Pie", structured_ingredients=[],
                       tokenized_directions=[TokenizedDirection(step=1, text="Bake.")], yield_spec=spec)
    return _pre_parsed_to_refinement(pr)


def test_a_restored_yield_with_a_known_kind_is_kept():
    spec = {"kind": "servings", "container": None, "area": None, "pieces": None, "edited": True}
    assert _restored(spec).yield_checked == spec


def test_a_restored_yield_with_an_unknown_kind_is_dropped_and_logged(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        assert _restored({"kind": "bushels", "edited": False}).yield_checked is None
        assert _restored({"container": None}).yield_checked is None
    assert any("yield_spec" in r.getMessage() for r in caplog.records)


def test_a_restore_with_no_yield_stays_none():
    assert _restored(None).yield_checked is None


def test_an_archive_yield_spec_that_is_not_an_object_costs_only_the_field(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        for junk in ("servings", ["servings"]):
            pr = CayenneRecipe(title="Pie", structured_ingredients=[],
                               tokenized_directions=[TokenizedDirection(step=1, text="Bake.")], yield_spec=junk)
            assert pr.yield_spec is None and pr.title == "Pie"
    assert any("yield_spec" in r.getMessage() for r in caplog.records)
