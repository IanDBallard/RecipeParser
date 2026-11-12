# recipeparser/adapters/ai_search/tools.py
"""The agent's four tools and the record of every recipe id they hand back (Cayenne AI search §7.4).

``ToolBox.run`` never raises: a refused statement, a failed query, a bad input or an unknown tool
comes back as an error outcome the agent can read and recover from. Every UUID that a tool result
carries is remembered in ``seen_ids`` (canonical lowercase); the endpoint later lets only ids that
were seen *and* are the cook's own reach the answer (A2).
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from recipeparser.adapters.ai_search.ai_db import AiReaderDb, Catalogue, QueryFailed, QueryResult
from recipeparser.adapters.ai_search.answer import Answer, answer_ids
from recipeparser.adapters.ai_search.sql_gate import StatementRefused, check_statement

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 300
MAX_ROWS = 200
MAX_SEARCH_LIMIT = 50
PROGRESS_TEXT_CHARS = 40

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "run_sql",
        "description": (
            "Run one read-only SQL SELECT over the cook's own recipe views (the ai schema) and get back the rows. "
            "Use it to filter, count, sort and compare: by category, rating, time, source, ingredient and so on. "
            "Write one statement only. To search by words inside SQL call ai.keyword_search(text, limit) with "
            "positional arguments; to find recipes by meaning use the semantic_search tool instead, never "
            "ai.semantic_search in SQL. At most 200 rows come back and long text is cut short."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The SELECT statement."}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "semantic_search",
        "description": (
            "Find the cook's recipes by meaning: describe what is wanted in a few words (a mood, a dish, a "
            "season) and get the closest recipes with their ids, titles and a closeness score. Use this for "
            "fuzzy ideas where exact words may not appear in the recipe."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to look for, in plain words."},
                "limit": {"type": "integer", "description": "How many recipes to return, 1 to 50."},
            },
            "required": ["text", "limit"],
            "additionalProperties": False,
        },
    },
    {
        "name": "keyword_search",
        "description": (
            "Find the cook's recipes whose text contains the given words: returns ids, titles and a rank. "
            "Use this when a specific word, name or ingredient matters more than the general idea."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The words to look for."},
                "limit": {"type": "integer", "description": "How many recipes to return, 1 to 50."},
            },
            "required": ["text", "limit"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_recipe",
        "description": (
            "Read one of the cook's recipes in full: its details, its ingredients in order and its directions "
            "in order. Use it to check a recipe before recommending it."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "The recipe's id, as returned by another tool."}},
            "required": ["id"],
            "additionalProperties": False,
        },
        "cache_control": {"type": "ephemeral"},
    },
]

_RECIPE_SQL = "select * from ai.recipes where id = %s"
_INGREDIENTS_SQL = "select * from ai.recipe_ingredients where recipe_id = %s order by position"
_STEPS_SQL = "select * from ai.recipe_steps where recipe_id = %s order by position"


@dataclass
class ToolOutcome:
    content: str
    is_error: bool
    progress: str | None = None


class _BadInput(Exception):
    pass


def _canonical_uuid(value: Any) -> str | None:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str):
        try:
            return str(uuid.UUID(value))
        except ValueError:
            return None
    return None


def _cut(text: str) -> str:
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS] + "…"


def _jsonable(value: Any, seen: set[str]) -> Any:
    """The value as JSON-safe data; every UUID (instance or whole-string) is added to ``seen``."""
    found = _canonical_uuid(value)
    if found is not None:
        seen.add(found)
        return found
    if isinstance(value, str):
        return _cut(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v, seen) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(v, seen) for v in value]
    return _cut(str(value))


def _rows(result: QueryResult, seen: set[str]) -> list[dict[str, Any]]:
    return [{c: _jsonable(v, seen) for c, v in zip(result.columns, row, strict=False)} for row in result.rows]


def _vector_text(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _require_text(tool_input: Mapping[str, Any], key: str) -> str:
    value = tool_input.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _BadInput(f"{key} must be a non-empty string.")
    return value


def _require_limit(tool_input: Mapping[str, Any]) -> int:
    value = tool_input.get("limit")
    if isinstance(value, bool) or not isinstance(value, int):
        raise _BadInput("limit must be an integer.")
    return max(1, min(MAX_SEARCH_LIMIT, value))


class ToolBox:
    def __init__(
        self,
        db: AiReaderDb,
        claims: Mapping[str, Any],
        embed: Callable[[str], list[float]],
        catalogue: Catalogue,
    ) -> None:
        self._db = db
        self._claims = claims
        self._embed = embed
        self._catalogue = catalogue
        self.seen_ids: set[str] = set()

    # -- provenance ---------------------------------------------------------------------------

    def admit_previous(self, answer: Answer) -> None:
        """Let the ids of the previous turn's answer back in, but only the ones the cook owns (D2)."""
        ids = answer_ids(answer)
        if ids:
            self.seen_ids |= self._db.own_recipe_ids(self._claims, ids)

    def permitted(self, candidate_ids: Iterable[str]) -> set[str]:
        """Candidates a tool returned (or the verified previous answer held) that the cook owns."""
        candidates: set[str] = set()
        for value in candidate_ids:
            canonical = _canonical_uuid(value)
            if canonical is not None:
                candidates.add(canonical)
        return self._db.own_recipe_ids(self._claims, candidates & self.seen_ids)

    # -- dispatch -----------------------------------------------------------------------------

    def run(self, name: str, tool_input: Mapping[str, Any]) -> ToolOutcome:
        handlers: dict[str, Callable[[Mapping[str, Any]], ToolOutcome]] = {
            "run_sql": self._run_sql,
            "semantic_search": self._semantic_search,
            "keyword_search": self._keyword_search,
            "get_recipe": self._get_recipe,
        }
        handler = handlers.get(name)
        if handler is None:
            return ToolOutcome(f"There is no tool called {name}.", True, None)
        try:
            return handler(tool_input)
        except _BadInput as e:
            return ToolOutcome(str(e), True, None)
        except QueryFailed as e:
            return ToolOutcome(e.message, True, None)
        except Exception as e:  # a tool must never raise into the agent loop
            logger.warning("tool %s failed for user %s: %s", name, self._user(), type(e).__name__)
            return ToolOutcome("That didn't work. Try something else.", True, None)

    def _user(self) -> str:
        return str(self._claims.get("sub", ""))

    # -- tools --------------------------------------------------------------------------------

    def _run_sql(self, tool_input: Mapping[str, Any]) -> ToolOutcome:
        query = _require_text(tool_input, "query")
        try:
            check_statement(query, self._catalogue.relations, self._catalogue.columns)
        except StatementRefused as e:
            logger.warning("run_sql refused for user %s: %s | %s", self._user(), e.reason, query)
            return ToolOutcome(e.reason, True, None)
        result = self._db.run(self._claims, query, max_rows=MAX_ROWS)
        logger.info(
            "tool=run_sql user=%s duration_ms=%.0f rows=%d sql=%s",
            self._user(),
            result.duration_ms,
            len(result.rows),
            query,
        )
        payload: dict[str, Any] = {"columns": result.columns, "rows": _rows(result, self.seen_ids)}
        if result.truncated:
            payload["note"] = f"Showing the first {MAX_ROWS} rows."
        return ToolOutcome(json.dumps(payload), False, "Checking the details")

    def _search(self, sql: str, params: list[Any], tool: str, text: str) -> ToolOutcome:
        result = self._db.run(self._claims, sql, params, max_rows=MAX_SEARCH_LIMIT)
        logger.info(
            "tool=%s user=%s duration_ms=%.0f rows=%d", tool, self._user(), result.duration_ms, len(result.rows)
        )
        rows = _rows(result, self.seen_ids)
        progress = "Looking for " + text[:PROGRESS_TEXT_CHARS]
        return ToolOutcome(json.dumps({"columns": result.columns, "rows": rows}), False, progress)

    def _semantic_search(self, tool_input: Mapping[str, Any]) -> ToolOutcome:
        text = _require_text(tool_input, "text")
        limit = _require_limit(tool_input)
        vector = _vector_text(self._embed(text))
        return self._search(
            "select id, title, score from ai.semantic_search(%s, %s)", [vector, limit], "semantic_search", text
        )

    def _keyword_search(self, tool_input: Mapping[str, Any]) -> ToolOutcome:
        text = _require_text(tool_input, "text")
        limit = _require_limit(tool_input)
        return self._search(
            "select id, title, rank from ai.keyword_search(%s, %s)", [text, limit], "keyword_search", text
        )

    def _get_recipe(self, tool_input: Mapping[str, Any]) -> ToolOutcome:
        recipe_id = _canonical_uuid(tool_input.get("id"))
        if recipe_id is None:
            raise _BadInput("id must be a recipe id, as returned by another tool.")
        head = self._db.run(self._claims, _RECIPE_SQL, [recipe_id], max_rows=1)
        if not head.rows:
            return ToolOutcome("No recipe with that id.", True, "Checking the details")
        ingredients = self._db.run(self._claims, _INGREDIENTS_SQL, [recipe_id], max_rows=MAX_ROWS)
        steps = self._db.run(self._claims, _STEPS_SQL, [recipe_id], max_rows=MAX_ROWS)
        logger.info("tool=get_recipe user=%s", self._user())
        scratch: set[str] = set()
        recipe = _rows(head, scratch)[0]
        recipe["ingredients"] = _rows(ingredients, scratch)
        recipe["steps"] = _rows(steps, scratch)
        self.seen_ids.add(recipe_id)
        title = recipe.get("title")
        progress = f"Checking {title}" if isinstance(title, str) and title else "Checking the details"
        return ToolOutcome(json.dumps(recipe), False, progress)
