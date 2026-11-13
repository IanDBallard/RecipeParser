"""The agent's instructions and the request context (Cayenne AI search §7.6)."""

from __future__ import annotations

import uuid
from datetime import date

from recipeparser.adapters.ai_search.ai_db import Catalogue, ColumnInfo, ViewInfo
from recipeparser.adapters.ai_search.answer import Alternative, Group, OutOfScopeAnswer, RecipesAnswer, TextAnswer, Turn
from recipeparser.adapters.ai_search.prompt import (
    SYSTEM_PROMPT,
    request_text,
    schema_description,
    system_blocks,
)

CAT = Catalogue(
    views={
        "recipes": ViewInfo(
            "One row per recipe.",
            [ColumnInfo("id", "uuid", "The recipe id."), ColumnInfo("title", "text", "Its title.")],
        ),
        "categories": ViewInfo("One row per category.", [ColumnInfo("name", "text", "The category name.")]),
    },
    functions={"semantic_search": "Finds recipes by meaning.", "keyword_search": "Finds recipes by words."},
)


def test_system_prompt_has_spec_rules() -> None:
    lines = SYSTEM_PROMPT.split("\n")
    assert "  answer is worse than none." in lines
    assert "- Never mention AI, models, SQL, tools, databases or searching. The cook" in lines
    assert 'return kind "out_of_scope" and nothing else.' in SYSTEM_PROMPT
    assert "  come from a tool result in this conversation. A recipe you remember or" in lines
    assert "Answer format" in lines
    assert "never with\n  ai.semantic_search in SQL" in SYSTEM_PROMPT
    assert "Call ai.semantic_search" not in SYSTEM_PROMPT
    assert "{schema description" not in SYSTEM_PROMPT
    assert "The cook's library" not in SYSTEM_PROMPT
    assert "positional" in SYSTEM_PROMPT and "unnest" in SYSTEM_PROMPT


def test_schema_description_deterministic_and_complete() -> None:
    text = schema_description(CAT)
    assert text == schema_description(CAT)
    comments = (
        "One row per recipe.",
        "The recipe id.",
        "Its title.",
        "One row per category.",
        "The category name.",
        "Finds recipes by meaning.",
        "Finds recipes by words.",
    )
    for comment in comments:
        assert text.count(comment) == 1
    assert "ai.recipes: One row per recipe." in text
    assert "  title (text): Its title." in text
    order = [text.index(n) for n in ("ai.categories", "ai.recipes", "ai.keyword_search", "ai.semantic_search")]
    assert order == sorted(order)


def test_system_block_is_cached() -> None:
    blocks = system_blocks(CAT)
    assert len(blocks) == 1
    block = blocks[0]
    assert block["type"] == "text"
    assert block["cache_control"] == {"type": "ephemeral"}
    assert block["text"] == SYSTEM_PROMPT + "\n\nThe cook's library\n" + schema_description(CAT)


def test_request_text_layout() -> None:
    out = request_text(today=date(2026, 11, 11), measuring="metric, °C", filters=[], question="soups?", history=[])
    expected = "Today: Wednesday 11 November 2026\nMeasuring system: metric, °C\nQuestion: soups?"
    assert out == expected


def test_request_text_filters_one_per_line() -> None:
    out = request_text(
        today=date(2026, 11, 11), measuring="m", filters=["Course: Main", "Rated 4+"], question="q", history=[]
    )
    assert "Active filters:\n- Course: Main\n- Rated 4+\nQuestion: q" in out


def _recipes(*labels: str | None) -> RecipesAnswer:
    alts = [
        Alternative(label=label, note="n", groups=[Group(heading=None, recipe_ids=[uuid.uuid4()])]) for label in labels
    ]
    return RecipesAnswer(kind="recipes", alternatives=alts)


def _ask(history: list[Turn], filters: list[str] | None = None) -> str:
    return request_text(
        today=date(2026, 11, 11), measuring="m", filters=filters or [], question="q", history=history
    )


def test_request_text_history_lists_turns_in_order() -> None:
    t1 = Turn(question="dinner for six?", answer=OutOfScopeAnswer(kind="out_of_scope"))
    t2 = Turn(question="make it vegetarian", answer=TextAnswer(kind="text", text="ok", recipe_ids=[]))
    out = _ask([t1, t2])
    marks = [
        "Conversation so far (this is turn 3 of 6):",
        "Turn 1 question: dinner for six?",
        "Turn 1 answer: " + t1.answer.model_dump_json(),
        "Turn 2 question: make it vegetarian",
        "Turn 2 answer: " + t2.answer.model_dump_json(),
        "Question: q",
    ]
    positions = [out.index(m) for m in marks]
    assert positions == sorted(positions)
    assert out.endswith("Question: q")


def test_request_text_marks_shown_option() -> None:
    two = Turn(question="menu", answer=_recipes("Classic", "Vegetarian"), chosen=1)
    assert "Turn 1 option the cook was looking at: Vegetarian" in _ask([two])
    one = Turn(question="menu", answer=_recipes(None), chosen=0)
    assert "option the cook was looking at" not in _ask([one])


def test_request_text_option_without_label_is_numbered() -> None:
    turn = Turn(question="menu", answer=_recipes("Classic", None), chosen=1)
    assert "Turn 1 option the cook was looking at: Option 2" in _ask([turn])


def test_request_text_chosen_out_of_range_omits_the_line() -> None:
    turn = Turn(question="menu", answer=_recipes("A", "B"), chosen=3)
    assert "option the cook was looking at" not in _ask([turn])


def test_request_text_no_history_no_conversation_block() -> None:
    assert "Conversation so far" not in _ask([])


def test_request_text_omits_empty_filters() -> None:
    assert "Active filters" not in _ask([], filters=[])