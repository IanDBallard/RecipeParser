"""
The Cayenne side of the Paprika export contract, as the rows Cayenne stores.

Transcribed (asserted fields only) from Cayenne's
cayenne-web/tests/fixtures/paprika/contractRows.ts (plan A). The archive
tests/fixtures/cayenne-export-contract.paprikarecipes is the export of these
three rows; a restore of it must give them back. JSON columns are written here
as the parsed values the row carries, not as JSON text.
"""


def _ing(id, kind, line_index, name, amount, unit, fallback, state=None):
    return {
        "id": id, "kind": kind, "amount": amount, "unit": unit, "name": name,
        "fallback_string": fallback, "converted_amount": None, "converted_unit": None,
        "is_ai_converted": False, "line_index": line_index, "state": state,
    }


def _pan(id, raw, **over):
    return {
        "id": id, "raw": raw, "count": 1, "shape": "round", "family": "round-cake",
        "dims_mm": {"d": 200}, "capacity_ml": None, "catalog_hint": None, "edited": False, **over,
    }


_NO_SPANS = {
    "prep_min_minutes": None, "prep_max_minutes": None, "prep_note": None,
    "cook_min_minutes": None, "cook_max_minutes": None, "cook_note": None,
    "servings_min": None, "servings_max": None, "servings_note": None,
    "servings_source": None,
}

LEMON = {
    **_NO_SPANS,
    "title": "Lemon Drizzle Cake",
    "base_servings": 8,
    "cook_min_minutes": 45, "cook_max_minutes": 45,
    "rating": None, "is_favourite": False,
    "source_uom_system": None, "source_uom_system_detected": None,
    "image_source": None,
    "pans": [
        _pan("pan_01", "a 20 cm tin", edited=True),
        _pan("pan_02", "a 23 cm tin", dims_mm={"d": 230}, removed=True, edited=True),
    ],
    "ingredient_lines": ["CAKE", "200 g plain flour", "50 ml lemon juice", "SYRUP", "100 g caster sugar"],
    "direction_steps": [
        "Heat the oven to 180C and line a 20 cm tin.",
        "Mix the flour and the lemon juice.",
        "Bake, then pour over the syrup.",
    ],
    "structured_ingredients": [
        _ing("ing_01", "heading", 0, "CAKE", None, None, "CAKE"),
        _ing("ing_02", "ingredient", 1, "plain flour", 200, "g", "200 g plain flour", "solid"),
        _ing("ing_03", "ingredient", 2, "lemon juice", 50, "ml", "50 ml lemon juice", "liquid"),
        _ing("ing_04", "heading", 3, "SYRUP", None, None, "SYRUP"),
        _ing("ing_05", "ingredient", 4, "caster sugar", 100, "g", "100 g caster sugar"),
    ],
    "tokenized_directions": [
        {"step": 1, "text": "Heat the oven to {{temp:180C,oven|180C}} and line {{pan_01|a 20 cm tin}}."},
        {"step": 2, "text": "Mix {{ing_02|the flour}} and {{ing_03|the lemon juice}}."},
        {"step": 3, "text": "Bake, then pour over {{ing_05|the syrup}}."},
    ],
    "body_rev": 0, "derived_rev": 0,
    "embedding_length": 1536,
}

XMAS = {
    **_NO_SPANS,
    "title": "Christmas Cake",
    "base_servings": 8,
    "servings_min": 10, "servings_max": 12, "servings_note": "as a starter",
    "prep_min_minutes": 30, "prep_max_minutes": 45, "prep_note": "overnight",
    "cook_min_minutes": 180, "cook_max_minutes": 180,
    "rating": 4, "is_favourite": True,
    "source_uom_system": "UK", "source_uom_system_detected": "UK",
    "image_source": "generated",
    "pans": [],
    "ingredient_lines": ["500 g mixed fruit", "250 g butter"],
    "direction_steps": ["Soak the fruit overnight.", "Bake slowly."],
    "structured_ingredients": [
        _ing("ing_01", "ingredient", 0, "mixed fruit", 500, "g", "500 g mixed fruit"),
        _ing("ing_02", "ingredient", 1, "butter", 250, "g", "250 g butter"),
    ],
    "tokenized_directions": [
        {"step": 1, "text": "Soak {{ing_01|the fruit}} overnight."},
        {"step": 2, "text": "Cream {{ing_02|the butter}}, then bake slowly."},
    ],
    "body_rev": 0, "derived_rev": 0,
    "embedding_length": 1536,
}

# The stale row: Cayenne has no raw lines for it (null), so a restore derives them from
# the structured data. body_rev 1 / derived_rev 0 is what makes the regen worker re-derive it.
SCONES = {
    **_NO_SPANS,
    "title": "grandma's best scones",
    "base_servings": 6,
    "rating": None, "is_favourite": False,
    "source_uom_system": None, "source_uom_system_detected": None,
    "image_source": None,
    "pans": [],
    "ingredient_lines": ["225 g self-raising flour"],
    "direction_steps": ["Rub the flour with butter."],
    "structured_ingredients": [_ing("ing_01", "ingredient", 0, "self-raising flour", 225, "g", "225 g self-raising flour")],
    "tokenized_directions": [{"step": 1, "text": "Rub {{ing_01|the flour}} with butter."}],
    "body_rev": 1, "derived_rev": 0,
    "embedding_length": 1536,
}

CONTRACT_ROWS = {r["title"]: r for r in (LEMON, XMAS, SCONES)}
