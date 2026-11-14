"""The answer shape (Cayenne AI search §4.5) and the id checks that clean it."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from recipeparser.adapters.ai_search.answer import (
    MAX_GAPS,
    MAX_MISSING_CHARS,
    NONE_LEFT,
    RAW_ANSWER_SCHEMA,
    Added,
    Alternative,
    Answer,
    Gap,
    Group,
    NoneAnswer,
    OutOfScopeAnswer,
    Previous,
    RawAnswer,
    RecipesAnswer,
    TextAnswer,
    Turn,
    answer_ids,
    finalize,
    raw_ids,
)

A, B, C, D = (str(uuid.uuid4()) for _ in range(4))
ADAPTER: TypeAdapter[Answer] = TypeAdapter(Answer)


def raw(**kw: Any) -> RawAnswer:
    base: dict[str, Any] = {"kind": "recipes", "text": "", "recipe_ids": [], "alternatives": [], "gap": ""}
    base.update(kw)
    return RawAnswer.model_validate(base)


def alt(
    *groups: tuple[str, list[str]], label: str = "", note: str = "Why these.", gaps: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "label": label,
        "note": note,
        "groups": [{"heading": h, "recipe_ids": ids} for h, ids in groups],
        "gaps": gaps or [],
    }


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


def test_finalize_blank_text_becomes_none() -> None:
    out = finalize(raw(kind="text", text="  \n ", recipe_ids=[A]), permitted={A})
    assert out == NoneAnswer(kind="none", text=NONE_LEFT)


def test_raw_ids_canonical_lowercase_and_valid_only() -> None:
    r = raw(recipe_ids=[A.upper(), "junk"], alternatives=[alt(("", [A, B.upper()]))])
    assert raw_ids(r) == [A, B]


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


def test_turn_bounds() -> None:
    ans = {"kind": "out_of_scope"}
    assert Turn.model_validate({"question": "q", "answer": ans}).chosen == 0
    assert Turn.model_validate({"question": "q", "answer": ans, "chosen": 3}).chosen == 3
    for bad in (
        {"question": "", "answer": ans},
        {"question": "x" * 1001, "answer": ans},
        {"question": "q", "answer": ans, "chosen": -1},
        {"question": "q", "answer": ans, "chosen": 4},
        {"question": "q", "answer": {"kind": "bogus"}},
    ):
        with pytest.raises(ValidationError):
            Turn.model_validate(bad)


# -- limits: a previous answer cannot be made big enough to bill many times over ----------------


@pytest.mark.parametrize(
    "answer",
    [
        {"kind": "text", "text": "x" * 2001, "recipe_ids": []},
        {"kind": "none", "text": "x" * 2001},
        {"kind": "text", "text": "ok", "recipe_ids": [str(uuid.uuid4()) for _ in range(101)]},
        {"kind": "recipes", "alternatives": [{"note": "x" * 301, "groups": [{"heading": None, "recipe_ids": []}]}]},
        {"kind": "recipes", "alternatives": [{"label": "x" * 301, "note": "n", "groups": []}]},
        {"kind": "recipes", "alternatives": [{"note": "n", "groups": [{"heading": "x" * 301, "recipe_ids": []}]}]},
        {
            "kind": "recipes",
            "alternatives": [{"note": "n", "groups": [{"heading": None, "recipe_ids": []}] * 11}],
        },
        {
            "kind": "recipes",
            "alternatives": [
                {"note": "n", "groups": [{"heading": None, "recipe_ids": [str(uuid.uuid4()) for _ in range(101)]}]}
            ],
        },
        {"kind": "recipes", "alternatives": [{"note": "n", "groups": []}] * 5},
    ],
)
def test_answer_limits_refuse_oversize(answer: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(answer)


def test_answer_limits_accept_the_maximum() -> None:
    ids = [str(uuid.uuid4()) for _ in range(100)]
    ADAPTER.validate_python({"kind": "text", "text": "x" * 2000, "recipe_ids": ids})
    group = {"heading": "h" * 300, "recipe_ids": ids}
    alt_ = {"label": "l" * 300, "note": "n" * 300, "groups": [group] * 10}
    ADAPTER.validate_python({"kind": "recipes", "alternatives": [alt_] * 4})


def test_finalize_output_always_validates_within_limits() -> None:
    """The agent's own answer is cut to the limits, never refused on its way out."""
    many = [str(uuid.uuid4()) for _ in range(150)]
    permitted = set(many)
    big_alt = {
        "label": "L" * 500,
        "note": "N" * 500,
        "groups": [{"heading": "H" * 500, "recipe_ids": many} for _ in range(15)],
        "gaps": [{"group": n, "missing": "M" * 500} for n in range(-1, 8)],
    }
    out = finalize(raw(alternatives=[big_alt] * 6), permitted=permitted)
    assert isinstance(out, RecipesAnswer)
    ADAPTER.validate_python(ADAPTER.dump_python(out, mode="json"))
    first = out.alternatives[0]
    assert len(out.alternatives) == 4
    assert len(first.groups) == 10
    assert len(first.groups[0].recipe_ids) == 100
    assert first.label is not None and len(first.label) == 300 and first.label.endswith("…")
    assert len(first.gaps) == 4 and all(len(g.missing) == 120 for g in first.gaps)
    assert len(first.note) == 300 and first.groups[0].heading is not None and len(first.groups[0].heading) == 300

    text = finalize(raw(kind="text", text="T" * 5000, recipe_ids=many), permitted=permitted)
    assert isinstance(text, TextAnswer)
    assert len(text.text) == 2000 and text.text.endswith("…") and len(text.recipe_ids) == 100
    ADAPTER.validate_python(ADAPTER.dump_python(text, mode="json"))

    none = finalize(raw(kind="none", text="Z" * 5000), permitted=set())
    assert isinstance(none, NoneAnswer) and len(none.text) == 2000
    gapped = finalize(raw(kind="none", text="x", gap="G" * 500), permitted=set())
    assert isinstance(gapped, NoneAnswer) and len(gapped.gap) == 120
    ADAPTER.validate_python(ADAPTER.dump_python(gapped, mode="json"))


def gap(group: int, missing: str) -> dict[str, Any]:
    return {"group": group, "missing": missing}


def test_finalize_keeps_gaps_and_remaps_groups() -> None:
    # raw groups: 0 Main (kept), 1 Sides (unowned, dropped), 2 Dessert (kept)
    r = raw(
        alternatives=[
            alt(
                ("Main", [A]),
                ("Sides", [B]),
                ("Dessert", [C]),
                gaps=[gap(2, "a lighter dessert"), gap(1, "a side"), gap(0, "a bigger main")],
            )
        ]
    )
    out = finalize(r, permitted={A, C})
    assert isinstance(out, RecipesAnswer)
    assert [(g.group, g.missing) for g in out.alternatives[0].gaps] == [
        (1, "a lighter dessert"),
        (-1, "a side"),
        (0, "a bigger main"),
    ]


def test_finalize_drops_out_of_range_gap() -> None:
    gaps = [gap(7, "wine"), gap(3, "past the end"), gap(-2, "negative"), gap(0, "kept")]
    out = finalize(raw(alternatives=[alt(("Main", [A]), ("Dessert", [C]), gaps=gaps)]), permitted={A, C})
    assert isinstance(out, RecipesAnswer)
    assert [(g.group, g.missing) for g in out.alternatives[0].gaps] == [(0, "kept")]


def test_finalize_keeps_whole_answer_gap_group() -> None:
    out = finalize(raw(alternatives=[alt(("", [A]), gaps=[gap(-1, "a vegan option")])]), permitted={A})
    assert isinstance(out, RecipesAnswer)
    assert out.alternatives[0].gaps == [Gap(group=-1, missing="a vegan option")]


def test_finalize_caps_gaps() -> None:
    gaps = [gap(0, f"thing {n}") for n in range(6)]
    out = finalize(raw(alternatives=[alt(("", [A]), gaps=gaps)]), permitted={A})
    assert isinstance(out, RecipesAnswer)
    assert len(out.alternatives[0].gaps) == MAX_GAPS == 4


def test_finalize_trims_missing() -> None:
    gaps = [gap(0, "  a side  "), gap(0, "x" * 300)]
    out = finalize(raw(alternatives=[alt(("", [A]), gaps=gaps)]), permitted={A})
    assert isinstance(out, RecipesAnswer)
    kept = out.alternatives[0].gaps
    assert kept[0].missing == "a side"
    assert len(kept[1].missing) == MAX_MISSING_CHARS == 120


def test_finalize_drops_blank_gap() -> None:
    out = finalize(raw(alternatives=[alt(("", [A]), gaps=[gap(0, "   "), gap(0, "")])]), permitted={A})
    assert isinstance(out, RecipesAnswer)
    assert out.alternatives[0].gaps == []


def test_finalize_none_gap() -> None:
    out = finalize(raw(kind="none", text="Nothing fits.", gap=" a vegan side "), permitted=set())
    assert isinstance(out, NoneAnswer)
    assert out.gap == "a vegan side"
    assert ADAPTER.dump_python(out, mode="json")["gap"] == "a vegan side"
    blank = finalize(raw(kind="none", text="Nothing fits.", gap="  "), permitted=set())
    assert "gap" not in ADAPTER.dump_python(blank, mode="json")


def test_answer_dump_omits_empty_gaps() -> None:
    out = finalize(raw(alternatives=[alt(("Main", [A]))]), permitted={A})
    dumped = ADAPTER.dump_python(out, mode="json")
    assert "gaps" not in dumped["alternatives"][0]
    assert "label" not in dumped["alternatives"][0]
    assert "gap" not in ADAPTER.dump_python(NoneAnswer(kind="none", text="x"), mode="json")


def test_raw_schema_requires_gap_fields() -> None:
    props = RAW_ANSWER_SCHEMA["properties"]
    assert "gap" in props and "gap" in RAW_ANSWER_SCHEMA["required"]
    assert "gaps" in props["alternatives"]["items"]["properties"]


def test_gap_wire_limits() -> None:
    with pytest.raises(ValidationError):
        Gap(group=0, missing="")
    with pytest.raises(ValidationError):
        Gap(group=0, missing="x" * 121)
    with pytest.raises(ValidationError):
        Alternative(label=None, note="n", groups=[], gaps=[Gap(group=0, missing="m")] * 5)


def _turn(**kw: Any) -> dict[str, Any]:
    return {"question": "q", "answer": {"kind": "out_of_scope"}, **kw}


def test_turn_accepts_added() -> None:
    added = {"alternative": 1, "group": -1, "heading": None, "recipe_ids": [A]}
    turn = Turn.model_validate(_turn(added=[added]))
    assert turn.added == [Added(alternative=1, group=-1, heading=None, recipe_ids=[uuid.UUID(A)])]
    assert Turn.model_validate(_turn()).added == []


def test_turn_rejects_nine_added() -> None:
    added = {"alternative": 0, "group": 0, "heading": "Main", "recipe_ids": [A]}
    assert len(Turn.model_validate(_turn(added=[added] * 8)).added) == 8
    with pytest.raises(ValidationError):
        Turn.model_validate(_turn(added=[added] * 9))


@pytest.mark.parametrize(
    "bad",
    [
        {"alternative": 4, "group": 0, "heading": None, "recipe_ids": []},
        {"alternative": 0, "group": -2, "heading": None, "recipe_ids": []},
        {"alternative": 0, "group": 0, "heading": "h" * 301, "recipe_ids": []},
    ],
)
def test_added_limits(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Added.model_validate(bad)
