"""The agent's instructions and the request context (Cayenne AI search §7.6)."""

from __future__ import annotations

from datetime import date

from recipeparser.adapters.ai_search.ai_db import Catalogue, ColumnInfo, ViewInfo
from recipeparser.adapters.ai_search.answer import OutOfScopeAnswer, Previous
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
    for text in ("out_of_scope", "Never mention AI", "Every recipe id", "Active filters", "Answer format"):
        assert text in SYSTEM_PROMPT or text.lower() in SYSTEM_PROMPT.lower()
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
    out = request_text(today=date(2026, 11, 11), measuring="metric, °C", filters=[], question="soups?", previous=None)
    expected = "Today: Wednesday 11 November 2026\nMeasuring system: metric, °C\nActive filters: none\nQuestion: soups?"
    assert out == expected


def test_request_text_filters_one_per_line() -> None:
    out = request_text(
        today=date(2026, 11, 11), measuring="m", filters=["Course: Main", "Rated 4+"], question="q", previous=None
    )
    assert "Active filters:\n- Course: Main\n- Rated 4+\nQuestion: q" in out


def test_request_text_refinement() -> None:
    prev = Previous(question="a menu", answer=OutOfScopeAnswer(kind="out_of_scope"), round=2)
    out = request_text(today=date(2026, 11, 11), measuring="m", filters=[], question="swap the stuffing", previous=prev)
    assert out.index("Refining the previous answer (round 2 of 5).") < out.index("Question: swap the stuffing")
    assert "Previous question: a menu" in out
    assert 'Previous answer: {"kind":"out_of_scope"}' in out
