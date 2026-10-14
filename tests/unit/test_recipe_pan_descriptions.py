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
