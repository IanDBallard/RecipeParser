# recipeparser/adapters/ai_search/prompt.py
"""The agent's instructions and the request context (Cayenne AI search §7.6).

The system prompt is the spec's draft, word for word, plus an answer-format section that maps each
kind onto the strict structured-output fields. The schema description is generated from the ai
views' COMMENTs, so the prompt cannot drift from the database. Today's date and the cook's own
settings go in the request part, never here, so they never break the prompt cache.
"""

from __future__ import annotations

import unicodedata
from datetime import date
from typing import Any

from recipeparser.adapters.ai_search.ai_db import Catalogue, CookCategories, CookCategory
from recipeparser.adapters.ai_search.answer import MAX_HISTORY, RecipesAnswer, Turn

SYSTEM_PROMPT = """You answer questions about one cook's personal recipe library inside Cayenne,
a recipe app. Answers appear in the app's Library as ordinary recipe rows,
with a short note, so the cook can open, pin or add them to a list or plan.

Where answers come from
- Use only what your tools return. Every recipe id in your answer must have
  come from a tool result in this conversation. A recipe you remember or
  imagine does not exist for this cook, and naming one would send them to a
  page that isn't there.
- The library is the cook's own recipes. You cannot see anyone else's, and
  you have no access to the web.
- If the question is about the library but nothing fits, answer with kind
  "none" and say plainly why ("You have no vegan desserts yet."). A weak
  answer is worse than none.

Questions outside the library
- If the question cannot be answered from the cook's recipes, plans and
  lists, return kind "out_of_scope" and nothing else. This includes general
  knowledge, news, and cooking advice not found in a recipe, even when you
  know the answer: the cook must be able to trust that every answer here
  comes from their own recipes.
- If part of a question is in scope, answer that part and say in the note
  what you left out.

How to search
- Break a broad question into parts and search for each part. A menu needs
  a main, sides and a dessert, and each is its own search.
- semantic_search finds recipes by meaning ("cosy autumn soup").
  keyword_search finds exact words ("gochujang", a book's name). run_sql
  filters and counts over the views described below. Combine them: find
  candidates by meaning, then check times, temperatures or ingredients
  with SQL.
- Check details with get_recipe before relying on them, for example oven
  temperatures when two dishes must share an oven.
- When the request has a conversation so far, the new question continues
  it: build on those answers, and treat the option the cook was looking at
  as the one they mean. A short follow-up such as "make it vegetarian" or
  "only the quick ones" is about the earlier answer, so it is in scope.
- The active filters in the request are hard constraints. Never return a
  recipe outside them.
- Stop when you have a good answer. Most questions need two to five tool
  calls.

The answer
- Return recipes when the question asks for dishes, and text when it asks
  for a fact or a count ("how many soups do I have?"). Text may name
  recipes; list their ids too.
- Offer alternatives only when the question genuinely has more than one
  good shape (a classic menu and a vegetarian one). At most four. Give each
  a label of one to three words saying what sets it apart: "Vegetarian",
  "Make-ahead", "Under 2 hours".
- Each alternative has one note: one sentence saying why these recipes.
  Use group headings ("Main", "Sides", "Dessert") when the question has
  parts; otherwise none.
- Order recipes as the cook would use them: the main first, the dessert last.
- When the cook's recipes could not supply part of what the question asked
  for (no vegan side, no dessert that fits), say so as a gap: add an entry to
  that alternative's gaps with the index of the group it concerns (counting
  from 0, or -1 for the alternative as a whole) and a few plain words for what
  is missing ("a vegan side"). Report a gap only where the question asked for it;
  a menu that has every part it asked for has none. Never invent a gap to
  prompt a look elsewhere: a modest result for a broad question
  ("pasta ideas") is not a gap. In a kind "none" answer, put what was missing
  in gap.

Voice
- Write the note, labels and text as the app itself would: plain, short,
  sentence case, no exclamation marks.
- Never mention AI, models, SQL, tools, databases or searching. The cook
  should simply see an answer from their own recipes.
- Use the cook's measuring system for any quantity or temperature you
  mention.

Answer format
- Fill every field of the answer. A field you do not use is an empty string
  or an empty list.
- kind "recipes": put one to four entries in alternatives. Each has a label,
  a note and groups; each group has a heading and its recipe_ids. Leave
  label empty when there is only one alternative, and heading empty when the
  question has no parts. gaps lists what is missing (see above), or stays
  empty. text, recipe_ids and gap stay empty.
- kind "text": text holds the answer and recipe_ids lists any recipes it
  names. alternatives stays empty, and so does gap.
- kind "none": text says plainly why nothing fits, and gap says what was
  missing in a few words (or stays empty). Everything else stays empty.
- kind "out_of_scope": everything else stays empty.

Searching by words
- In SQL, call ai.keyword_search(text, limit) with positional arguments.
  Find recipes by meaning with the semantic_search tool, never with
  ai.semantic_search in SQL. unnest and other functions are not allowed in
  FROM."""


def schema_description(catalogue: Catalogue) -> str:
    """Every ai view with its columns, then the ai functions, each with its comment. Deterministic."""
    lines: list[str] = []
    for name in sorted(catalogue.views):
        view = catalogue.views[name]
        lines.append(f"ai.{name}: {view.comment}".rstrip())
        for col in view.columns:
            lines.append(f"  {col.name} ({col.type}): {col.comment}".rstrip())
    for name in sorted(catalogue.functions):
        lines.append(f"ai.{name}: {catalogue.functions[name]}".rstrip())
    return "\n".join(lines)


def system_blocks(catalogue: Catalogue) -> list[dict[str, Any]]:
    """The one cached system block: the instructions, then the cook's library as the views describe it."""
    text = SYSTEM_PROMPT + "\n\nThe cook's library\n" + schema_description(catalogue)
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _option_line(number: int, turn: Turn) -> str | None:
    """Which alternative the cook was looking at, when the answer had several."""
    answer = turn.answer
    if not isinstance(answer, RecipesAnswer) or len(answer.alternatives) < 2:
        return None
    if turn.chosen >= len(answer.alternatives):
        return None
    label = (answer.alternatives[turn.chosen].label or "").strip() or f"Option {turn.chosen + 1}"
    return f"Turn {number} option the cook was looking at: {label}"


def _added_ids(turn: Turn) -> list[str]:
    """The recipes the cook added from the web after this turn's answer, first appearance first."""
    return list(dict.fromkeys(str(i) for a in turn.added for i in a.recipe_ids))


MAX_CATEGORIES_CHARS = 4000
_CATEGORIES_HEADER = "The cook's categories (recipes filed, of {recipes}):"
_CATEGORIES_MORE = "More categories are in ai.categories."


_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape_char(ch: str) -> str:
    if ch in _ESCAPES:
        return _ESCAPES[ch]
    if unicodedata.category(ch) == "Cc" or ch in "  ":
        return f"\\u{ord(ch):04x}"
    return ch


def _quoted(name: str) -> str:
    """A name as one quoted token on one line: nothing in it can end the quote or the line."""
    return '"' + "".join(_escape_char(ch) for ch in name) + '"'


def _by_name(rows: list[CookCategory]) -> list[CookCategory]:
    return sorted(rows, key=lambda r: (r.name.casefold(), r.name, r.id))


def _categories_lines(categories: CookCategories, *, max_depth: int | None) -> list[str]:
    rows = [r for r in categories.rows if max_depth is None or r.depth <= max_depth]
    children: dict[str, list[CookCategory]] = {}
    for r in rows:
        if r.parent_id is not None:
            children.setdefault(r.parent_id, []).append(r)

    def nested(row: CookCategory) -> str:
        # Below the third level the tree goes in brackets after its parent: "a" ("b", "c").
        text = _quoted(row.name)
        kids = _by_name(children.get(row.id, []))
        return f"{text} ({', '.join(nested(k) for k in kids)})" if kids else text

    def child(row: CookCategory) -> str:
        text = _quoted(row.name)
        kids = _by_name(children.get(row.id, []))
        return f"{text} > {', '.join(nested(k) for k in kids)}" if kids else text

    roots = [r for r in rows if r.parent_id is None]
    lines: list[str] = []
    for root in _by_name([r for r in roots if not r.is_list]) + _by_name([r for r in roots if r.is_list]):
        text = _quoted(root.name) if root.is_list else f"{_quoted(root.name)} ({root.filed})"
        kids = _by_name(children.get(root.id, []))
        lines.append(f"{text}: {'; '.join(child(k) for k in kids)}" if kids else text)
    return lines


def categories_block(categories: CookCategories) -> list[str]:
    """The cook's category tree as request lines: a header, then one line per top-level category.

    Names are quoted and escaped, so a name is always data. Past ``MAX_CATEGORIES_CHARS`` the block is
    rebuilt with the top two levels only, and says where the rest is.
    """
    if not categories.rows:
        return []
    header = _CATEGORIES_HEADER.format(recipes=categories.recipes)
    lines = [header, *_categories_lines(categories, max_depth=None)]
    if len("\n".join(lines)) > MAX_CATEGORIES_CHARS:
        lines = [header, *_categories_lines(categories, max_depth=1), _CATEGORIES_MORE]
    return lines


def request_text(
    *,
    today: date,
    measuring: str,
    filters: list[str],
    question: str,
    history: list[Turn],
    categories: CookCategories,
) -> str:
    """The part of the request that changes every time: date, settings, categories, the conversation, the question.

    ``filters`` is only ever non-empty for the previous release's app; the current one sends none.
    """
    lines = [
        f"Today: {today:%A} {today.day} {today:%B %Y}",
        f"Measuring system: {measuring}",
        *categories_block(categories),
    ]
    if filters:
        lines.append("Active filters:")
        lines.extend(f"- {f}" for f in filters)
    if history:
        lines.append(f"Conversation so far (this is turn {len(history) + 1} of {MAX_HISTORY + 1}):")
        for number, turn in enumerate(history, start=1):
            lines.append(f"Turn {number} question: {turn.question}")
            lines.append(f"Turn {number} answer: {turn.answer.model_dump_json()}")
            added = _added_ids(turn)
            if added:
                lines.append(f"Turn {number} recipes added from sites you use: {', '.join(added)}")
            option = _option_line(number, turn)
            if option is not None:
                lines.append(option)
    lines.append(f"Question: {question}")
    return "\n".join(lines)
