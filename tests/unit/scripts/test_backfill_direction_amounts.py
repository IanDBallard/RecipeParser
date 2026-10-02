"""The direction-amounts backfill's decisions, with no network and no model."""
from __future__ import annotations

import json
from typing import Any, Dict, List

from recipeparser.core.fat_tokens import strip_fat_tokens
from recipeparser.models import DirectionMention
from scripts.backfill_direction_amounts import plan_row, summarise

INGREDIENTS = [
    {"id": "ing_01", "amount": 2, "unit": "cups", "name": "ricotta", "fallback_string": "2 cups ricotta"},
    {"id": "ing_02", "amount": 0.75, "unit": "cup", "name": "flour", "fallback_string": "3/4 cup flour"},
]
STEPS = ["Drain the ricotta.", "Add about 1/2 cup flour; add more flour until sticky."]
LEGACY = [
    {"step": 1, "text": "Drain the {{ing_01|ricotta}}."},
    {"step": 2, "text": "Add about 1/2 cup {{ing_02|flour}}; add more {{ing_02|flour}} until sticky."},
]
MENTIONS = [
    DirectionMention(step=1, quote="ricotta", ingredient_id="ing_01", use="all"),
    DirectionMention(step=2, quote="1/2 cup", ingredient_id="ing_02", use="0.5 cup"),
    DirectionMention(step=2, quote="flour", context="add more flour until", ingredient_id="ing_02", use="none"),
]


def _row(**over: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "id": "r1", "title": "Gnocchi", "structured_ingredients": INGREDIENTS, "tokenized_directions": LEGACY,
        "direction_steps": STEPS, "body_rev": 2, "derived_rev": 2,
    }
    row.update(over)
    return row


def _tag(calls: List[int]):
    def tag(ingredients, steps):
        calls.append(1)
        return MENTIONS
    return tag


def test_tags_a_legacy_recipe_and_keeps_every_word():
    result = plan_row(_row(), _tag([]))
    assert result.status == "tagged"
    assert [d.text for d in result.directions] == [
        "Drain the {{ing_01|ricotta|all}}.",
        "Add about {{ing_02|1/2 cup|0.5 cup}} flour; add more {{ing_02|flour|none}} until sticky.",
    ]
    assert [strip_fat_tokens(d.text) for d in result.directions] == STEPS
    assert not result.text_changed
    assert result.before == LEGACY


def test_never_calls_the_model_for_a_row_it_skips():
    calls: List[int] = []
    tagged = [{"step": 1, "text": "Drain the {{ing_01|ricotta|all}}."}]
    assert plan_row(_row(tokenized_directions=tagged), _tag(calls)).status == "already-tagged"
    assert plan_row(_row(body_rev=3), _tag(calls)).status == "stale"
    assert plan_row(_row(direction_steps=[]), _tag(calls)).status == "empty"
    assert plan_row(_row(structured_ingredients=json.dumps(INGREDIENTS)), _tag(calls)).status == "malformed"
    assert plan_row(_row(direction_steps=None), _tag(calls)).status == "malformed"
    assert calls == []


def test_force_re_tags_a_tagged_recipe():
    tagged = [{"step": 1, "text": "Drain the {{ing_01|ricotta|all}}."}]
    assert plan_row(_row(tokenized_directions=tagged), _tag([]), force=True).status == "tagged"


def test_a_model_failure_is_reported_not_raised():
    def boom(ingredients, steps):
        raise RuntimeError("quota")
    result = plan_row(_row(), boom)
    assert (result.status, result.error) == ("failed", "quota")


def test_notes_when_the_old_tokens_were_not_the_raw_steps():
    rephrased = [{"step": 1, "text": "Drain {{ing_01|ricotta}} well."}]
    assert plan_row(_row(tokenized_directions=rephrased), _tag([])).text_changed


def test_summary_counts():
    results = [plan_row(_row(), _tag([])), plan_row(_row(body_rev=5), _tag([]))]
    statuses, dropped, demoted, changed = summarise(results)
    assert (statuses["tagged"], statuses["stale"], dropped, demoted, changed) == (1, 1, 0, 0, 0)
