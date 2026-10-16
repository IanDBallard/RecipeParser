"""write_recipe_to_supabase sends raw body, bookkeeping and duration columns (spec 5.7)."""
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.io.writers.supabase import write_recipe_to_supabase
from recipeparser.models import IngestResponse, RecipePan, StructuredIngredient, TokenizedDirection


def _recipe() -> IngestResponse:
    return IngestResponse(
        title="Cake",
        base_servings=2,
        structured_ingredients=[StructuredIngredient(id="ing_01", kind="ingredient", name="flour",
                                                     fallback_string="1 cup flour", line_index=0)],
        tokenized_directions=[TokenizedDirection(step=1, text="Mix {{ing_01|flour}}.")],
        embedding=[0.0] * 1536,
        ingredient_lines=["1 cup flour"],
        direction_steps=["Mix flour."],
        prep_time="15 mins",
        prep_min_minutes=15, prep_max_minutes=15, prep_note=None,
        servings_min=2, servings_max=4, servings_note=None,
        source_kind="book", source_key="cake book", source_title="Cake Book", source_author="A. Baker",
        pans=[RecipePan(id="pan_01", raw="a 20 cm tin", shape="round", family="round-cake", dims_mm={"d": 200})],
        bake_type="sponge",
        bake_step=0,
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


def test_row_has_the_tins_bake_type_and_bake_step(posted):
    assert isinstance(posted["pans"], list)  # a real list for jsonb, never a JSON string
    assert posted["pans"][0]["id"] == "pan_01"
    assert posted["pans"][0]["dims_mm"]["d"] == 200
    assert posted["pans"][0]["edited"] is False
    assert (posted["bake_type"], posted["bake_step"]) == ("sponge", 0)


# --- Cayenne restore (plan B task 3) -----------------------------------------


def _row(**over):
    from recipeparser.io.writers.supabase import _recipe_row

    rec = _recipe()
    for k, v in over.items():
        setattr(rec, k, v)
    return _recipe_row(rec, "u1", "r1")


def test_row_writes_favourite_uom_image_source():
    row = _row(is_favourite=True, source_uom_system="UK", image_source="generated")
    assert (row["is_favourite"], row["source_uom_system"], row["image_source"]) == (True, "UK", "generated")


def test_row_defaults_and_invalid_values_are_safe():
    row = _row(is_favourite=None, source_uom_system="metric", image_source="uploaded")
    assert row["is_favourite"] is False
    assert row["source_uom_system"] is None
    assert row["image_source"] is None


def test_row_stale_restore_bumps_body_rev():
    stale = _row(stale=True)
    assert (stale["body_rev"], stale["derived_rev"]) == (1, 0)
    fresh = _row(stale=False)
    assert (fresh["body_rev"], fresh["derived_rev"]) == (0, 0)


def test_row_keeps_removed_tin():
    pans = [RecipePan(id="pan_02", raw="a 23 cm tin", shape="round", family="round-cake",
                      dims_mm={"d": 230}, removed=True)]
    assert _row(pans=pans)["pans"][0]["removed"] is True


class _NoAiClient:
    def __getattr__(self, name):
        raise AssertionError(f"a restore must not call the AI client ({name})")


def test_contract_round_trip_rows():
    from pathlib import Path

    from recipeparser.core.fsm import PipelineController
    from recipeparser.core.pipeline import RecipePipeline, chunk_stages
    from recipeparser.core.rate_limiter import GlobalRateLimiter
    from recipeparser.io.readers.paprika import PaprikaReader
    from recipeparser.io.writers.supabase import _recipe_row
    from tests.fixtures.cayenne_contract_rows import CONTRACT_ROWS

    path = Path(__file__).resolve().parents[2] / "fixtures" / "cayenne-export-contract.paprikarecipes"
    chunks = PaprikaReader().read(str(path))
    assert len(chunks) == len(CONTRACT_ROWS)

    GlobalRateLimiter().reset()
    pipeline = RecipePipeline(
        client=_NoAiClient(), controller=PipelineController(), category_source=MagicMock(), rpm=9999,
    )
    seen = set()
    for chunk in chunks:
        result = pipeline._process_chunk(chunk, chunk_stages(chunk)).results[0]
        row = _recipe_row(result, "u1", "r1")
        want = CONTRACT_ROWS[row["title"]]
        seen.add(row["title"])

        for key in (
            "title", "base_servings", "prep_min_minutes", "prep_max_minutes", "prep_note",
            "cook_min_minutes", "cook_max_minutes", "cook_note", "servings_min", "servings_max",
            "servings_note", "rating", "is_favourite", "source_uom_system", "source_uom_system_detected",
            "image_source", "ingredient_lines", "direction_steps", "body_rev", "derived_rev",
        ):
            assert row[key] == want[key], (row["title"], key)
        assert len(row["embedding"]) == want["embedding_length"]
        assert row["tokenized_directions"] == want["tokenized_directions"]
        assert row["structured_ingredients"] == want["structured_ingredients"]
        assert len(row["pans"]) == len(want["pans"])
        for got, exp in zip(row["pans"], want["pans"]):
            assert {k: got[k] for k in exp if k != "dims_mm"} == {k: v for k, v in exp.items() if k != "dims_mm"}
            assert {k: v for k, v in got["dims_mm"].items() if v is not None} == exp["dims_mm"]
            assert got.get("removed", False) == exp.get("removed", False)
    assert seen == set(CONTRACT_ROWS)
