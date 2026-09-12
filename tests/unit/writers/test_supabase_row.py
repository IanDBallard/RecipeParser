"""write_recipe_to_supabase sends raw body, bookkeeping and duration columns (spec 5.7)."""
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.io.writers.supabase import write_recipe_to_supabase
from recipeparser.models import IngestResponse, StructuredIngredient, TokenizedDirection


def _recipe() -> IngestResponse:
    return IngestResponse(
        title="Cake",
        base_servings=2,
        structured_ingredients=[StructuredIngredient(id="ing_01", name="flour",
                                                     fallback_string="1 cup flour", line_index=0)],
        tokenized_directions=[TokenizedDirection(step=1, text="Mix {{ing_01|flour}}.")],
        embedding=[0.0] * 1536,
        ingredient_lines=["1 cup flour"],
        direction_steps=["Mix flour."],
        prep_time="15 mins",
        prep_min_minutes=15, prep_max_minutes=15, prep_note=None,
        servings_min=2, servings_max=4, servings_note=None,
        source_kind="book", source_key="cake book", source_title="Cake Book", source_author="A. Baker",
    )


@pytest.fixture()
def posted(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    monkeypatch.setenv("ALLOW_LIVE_WRITES_IN_TESTS", "1")
    resp = MagicMock(status_code=201, text="")
    with patch("recipeparser.io.writers.supabase.httpx.post", return_value=resp) as post:
        write_recipe_to_supabase(_recipe(), user_id="u1", recipe_id="r1")
        yield post.call_args.kwargs["json"]


def test_row_has_raw_body_and_bookkeeping(posted):
    assert posted["ingredient_lines"] == ["1 cup flour"]
    assert posted["direction_steps"] == ["Mix flour."]
    assert posted["body_rev"] == 0
    assert posted["derived_rev"] == 0
    assert posted["amount_overrides"] == {}


def test_row_has_structured_durations(posted):
    assert posted["prep_time"] == "15 mins"
    assert (posted["prep_min_minutes"], posted["prep_max_minutes"], posted["prep_note"]) == (15, 15, None)
    assert (posted["servings_min"], posted["servings_max"]) == (2, 4)
    assert posted["cook_min_minutes"] is None


def test_line_index_serialised(posted):
    assert posted["structured_ingredients"][0]["line_index"] == 0


def test_row_has_the_citation_columns(posted):
    assert (posted["source_kind"], posted["source_key"], posted["source_title"], posted["source_author"]) == (
        "book", "cake book", "Cake Book", "A. Baker")
