"""The answer shape (Cayenne AI search §4.5) and the id checks that clean it."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from recipeparser.adapters.ai_search.answer import (
    NONE_LEFT,
    RAW_ANSWER_SCHEMA,
    Alternative,
    Answer,
    Group,
    NoneAnswer,
    OutOfScopeAnswer,
    Previous,
    RawAnswer,
    RecipesAnswer,
    TextAnswer,
    answer_ids,
    finalize,
    raw_ids,
)

A, B, C, D = (str(uuid.uuid4()) for _ in range(4))
ADAPTER: TypeAdapter[Answer] = TypeAdapter(Answer)


def raw(**kw: Any) -> RawAnswer:
    base: dict[str, Any] = {"kind": "recipes", "text": "", "recipe_ids": [], "alternatives": []}
    base.update(kw)
    return RawAnswer.model_validate(base)


def alt(*groups: tuple[str, list[str]], label: str = "", note: str = "Why these.") -> dict[str, Any]:
    return {"label": label, "note": note, "groups": [{"heading": h, "recipe_ids": ids} for h, ids in groups]}


def test_raw_schema_strict() -> None:
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(RAW_ANSWER_SCHEMA)
    assert RAW_ANSWER_SCHEMA["type"] == "object"
    assert RAW_ANSWER_SCHEMA["properties"]["kind"]["enum"] == ["recipes", "text", "none", "out_of_scope"]


def test_finalize_drops_unowned() -> None:
    r = raw(alternatives=[alt(("Main", [A, B, A]), ("Dessert", [C]), ("Sides", [D]))])
    out = finalize(r, permitted={A, D})
    assert isinstance(out, RecipesAnswer)
    groups = out.alternatives[0].groups
    assert [g.recipe_ids for g in groups] == [[uuid.UUID(A)], [uuid.UUID(D)]]
    assert [g.heading for g in groups] == ["Main", "Sides"]


def test_finalize_drops_empty_alternative_and_bad_ids() -> None:
    r = raw(alternatives=[alt(("", [B]), label="X"), alt(("", [A, "not-a-uuid"]), label="Y")])
    out = finalize(r, permitted={A})
    assert isinstance(out, RecipesAnswer)
    assert len(out.alternatives) == 1
    assert out.alternatives[0].label is None
    assert out.alternatives[0].groups[0].heading is None


def test_finalize_single_alternative_has_no_label() -> None:
    out = finalize(raw(alternatives=[alt(("", [A]), label="Classic")]), permitted={A})
    assert isinstance(out, RecipesAnswer)
    assert out.alternatives[0].label is None


def test_finalize_two_alternatives_keep_labels() -> None:
    out = finalize(raw(alternatives=[alt(("", [A]), label="One"), alt(("", [B]), label="Two")]), permitted={A, B})
    assert isinstance(out, RecipesAnswer)
    assert [a.label for a in out.alternatives] == ["One", "Two"]


def test_finalize_caps_four_alternatives() -> None:
    ids = [str(uuid.uuid4()) for _ in range(6)]
    r = raw(alternatives=[alt(("", [i]), label=f"L{n}") for n, i in enumerate(ids)])
    out = finalize(r, permitted=set(ids))
    assert isinstance(out, RecipesAnswer)
    assert [a.label for a in out.alternatives] == ["L0", "L1", "L2", "L3"]


def test_finalize_all_dropped_becomes_none() -> None:
    out = finalize(raw(alternatives=[alt(("", [B]))]), permitted={A})
    assert out == NoneAnswer(kind="none", text="None of your recipes fit that.")
    assert NONE_LEFT == "None of your recipes fit that."


def test_finalize_text_filters_ids() -> None:
    out = finalize(raw(kind="text", text="You have 2 soups.", recipe_ids=[A, B, A]), permitted={A})
    assert out == TextAnswer(kind="text", text="You have 2 soups.", recipe_ids=[uuid.UUID(A)])


def test_finalize_none_and_out_of_scope() -> None:
    assert finalize(raw(kind="none", text="You have no vegan desserts yet."), permitted=set()) == NoneAnswer(
        kind="none", text="You have no vegan desserts yet."
    )
    assert finalize(raw(kind="none", text=""), permitted=set()) == NoneAnswer(kind="none", text=NONE_LEFT)
    assert finalize(raw(kind="out_of_scope", text="ignored"), permitted=set()) == OutOfScopeAnswer(kind="out_of_scope")


def test_finalize_matches_ids_case_insensitively() -> None:
    out = finalize(raw(kind="text", text="x", recipe_ids=[A.upper()]), permitted={A})
    assert isinstance(out, TextAnswer)
    assert out.recipe_ids == [uuid.UUID(A)]


def test_ids_listing() -> None:
    r = raw(recipe_ids=[A], alternatives=[alt(("", [B, A]), ("", [C]))])
    assert raw_ids(r) == [A, B, C]
    ans = RecipesAnswer(
        kind="recipes",
        alternatives=[Alternative(label=None, note="n", groups=[Group(heading=None, recipe_ids=[uuid.UUID(B)])])],
    )
    assert answer_ids(ans) == [B]
    assert answer_ids(TextAnswer(kind="text", text="t", recipe_ids=[uuid.UUID(A)])) == [A]
    assert answer_ids(NoneAnswer(kind="none", text="t")) == []
    assert answer_ids(OutOfScopeAnswer(kind="out_of_scope")) == []


def test_answer_round_trips_spec_shape() -> None:
    rid = uuid.UUID(A)
    recipes = RecipesAnswer(
        kind="recipes",
        alternatives=[
            Alternative(label="Vegetarian", note="n", groups=[Group(heading="Main", recipe_ids=[rid])]),
            Alternative(label=None, note="m", groups=[Group(heading=None, recipe_ids=[rid])]),
        ],
    )
    cases: list[tuple[Any, set[str]]] = [
        (recipes, {"kind", "alternatives"}),
        (TextAnswer(kind="text", text="t", recipe_ids=[rid]), {"kind", "text", "recipe_ids"}),
        (NoneAnswer(kind="none", text="t"), {"kind", "text"}),
        (OutOfScopeAnswer(kind="out_of_scope"), {"kind"}),
    ]
    for answer, keys in cases:
        data = ADAPTER.dump_python(answer, mode="json")
        assert set(data) == keys
        assert ADAPTER.validate_python(data) == answer
    first, second = ADAPTER.dump_python(recipes, mode="json")["alternatives"]
    assert set(first) == {"label", "note", "groups"}
    assert "label" not in second  # omitted when there is one alternative
    assert set(first["groups"][0]) == {"heading", "recipe_ids"}
    assert second["groups"][0]["heading"] is None


def test_recipes_answer_needs_one_to_four_alternatives() -> None:
    with pytest.raises(ValidationError):
        RecipesAnswer(kind="recipes", alternatives=[])
    one = Alternative(label=None, note="n", groups=[])
    with pytest.raises(ValidationError):
        RecipesAnswer(kind="recipes", alternatives=[one] * 5)


def test_previous_bounds() -> None:
    ans = {"kind": "out_of_scope"}
    assert Previous.model_validate({"question": "q", "answer": ans, "round": 1}).round == 1
    for bad in (
        {"question": "", "answer": ans, "round": 1},
        {"question": "x" * 1001, "answer": ans, "round": 1},
        {"question": "q", "answer": ans, "round": 0},
        {"question": "q", "answer": ans, "round": 6},
        {"question": "q", "answer": {"kind": "bogus"}, "round": 1},
    ):
        with pytest.raises(ValidationError):
            Previous.model_validate(bad)
