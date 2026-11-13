# recipeparser/adapters/ai_search/answer.py
"""The answer's shape (Cayenne AI search §4.5) and the checks that clean it before it leaves.

``RawAnswer`` is what the agent returns (every field present, unused ones empty, because the
structured-output schema is strict). ``finalize`` turns it into an ``Answer``: only recipes the cook
owns survive (A2), empty parts disappear, and everything is cut to the ``Answer`` limits.

The limits bound what a device can send back as ``history[].answer``: that answer is re-sent to the
agent on every call of the refinement, so an unbounded one would be billed many times over.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

NONE_LEFT = "None of your recipes fit that."
MAX_ALTERNATIVES = 4
MAX_TEXT_CHARS = 2000  # an answer's text
MAX_SHORT_CHARS = 300  # a note, label or heading
MAX_IDS = 100  # recipe ids in one list
MAX_GROUPS = 10  # groups in one alternative

_Text = Annotated[str, Field(max_length=MAX_TEXT_CHARS)]
_Short = Annotated[str, Field(max_length=MAX_SHORT_CHARS)]
_Ids = Annotated[list[uuid.UUID], Field(max_length=MAX_IDS)]

_KINDS = ["recipes", "text", "none", "out_of_scope"]


class Group(BaseModel):
    heading: _Short | None
    recipe_ids: _Ids


class Alternative(BaseModel):
    label: _Short | None = None
    note: _Short
    groups: list[Group] = Field(max_length=MAX_GROUPS)

    @model_serializer(mode="wrap")
    def _omit_empty_label(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if data.get("label") is None:
            data.pop("label", None)
        return data


class RecipesAnswer(BaseModel):
    kind: Literal["recipes"]
    alternatives: list[Alternative] = Field(min_length=1, max_length=MAX_ALTERNATIVES)


class TextAnswer(BaseModel):
    kind: Literal["text"]
    text: _Text
    recipe_ids: _Ids


class NoneAnswer(BaseModel):
    kind: Literal["none"]
    text: _Text


class OutOfScopeAnswer(BaseModel):
    kind: Literal["out_of_scope"]


Answer = Annotated[
    RecipesAnswer | TextAnswer | NoneAnswer | OutOfScopeAnswer,
    Field(discriminator="kind"),
]


MAX_HISTORY = 5  # earlier turns of one conversation (the sixth question is the sixth turn)


class Turn(BaseModel):
    """One earlier turn of the conversation: the device holds it and sends it back (nothing is stored here).

    ``chosen`` is the alternative the cook was looking at when they asked the next question.
    """

    question: str = Field(min_length=1, max_length=1000)
    answer: Answer
    chosen: int = Field(default=0, ge=0, le=MAX_ALTERNATIVES - 1)


class Previous(BaseModel):
    """The turn being refined, as the previous release's app sends it. Kept for one release; see ``Turn``.

    ``round`` is accepted but ignored (transitional).
    """

    question: str = Field(min_length=1, max_length=1000)
    answer: Answer
    round: int = Field(ge=1, le=5)


class RawGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    heading: str
    recipe_ids: list[str]


class RawAlternative(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    note: str
    groups: list[RawGroup]


class RawAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["recipes", "text", "none", "out_of_scope"]
    text: str
    recipe_ids: list[str]
    alternatives: list[RawAlternative]


RAW_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": _KINDS},
        "text": {"type": "string"},
        "recipe_ids": {"type": "array", "items": {"type": "string"}},
        "alternatives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "note": {"type": "string"},
                    "groups": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "heading": {"type": "string"},
                                "recipe_ids": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["heading", "recipe_ids"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["label", "note", "groups"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["kind", "text", "recipe_ids", "alternatives"],
    "additionalProperties": False,
}


def _unique(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(ids))


def answer_ids(answer: Answer) -> list[str]:
    """Every recipe id the answer names, first appearance first."""
    if isinstance(answer, RecipesAnswer):
        return _unique([str(i) for a in answer.alternatives for g in a.groups for i in g.recipe_ids])
    if isinstance(answer, TextAnswer):
        return _unique([str(i) for i in answer.recipe_ids])
    return []


def raw_ids(raw: RawAnswer) -> list[str]:
    """Every id the raw answer names: the candidates to check for ownership."""
    named = list(raw.recipe_ids)
    named.extend(i for a in raw.alternatives for g in a.groups for i in g.recipe_ids)
    ids: list[str] = []
    for value in named:
        try:
            ids.append(str(uuid.UUID(value)))
        except ValueError:
            continue
    return _unique(ids)


def _clip(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters, ending in an ellipsis when it was cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _owned(ids: list[str], allowed: set[str]) -> list[uuid.UUID]:
    """The ids in ``allowed``, in order, repeats dropped, at most ``MAX_IDS``; non-uuids dropped."""
    kept: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for value in ids:
        try:
            rid = uuid.UUID(value)
        except ValueError:
            continue
        if str(rid) in allowed and rid not in seen:
            seen.add(rid)
            kept.append(rid)
            if len(kept) == MAX_IDS:
                break
    return kept


def finalize(raw: RawAnswer, *, permitted: set[str]) -> Answer:
    """The raw answer with every id outside ``permitted`` removed and empty parts dropped."""
    allowed = {p.lower() for p in permitted}
    if raw.kind == "out_of_scope":
        return OutOfScopeAnswer(kind="out_of_scope")
    if raw.kind == "none":
        return NoneAnswer(kind="none", text=_clip(raw.text.strip(), MAX_TEXT_CHARS) or NONE_LEFT)
    if raw.kind == "text":
        if not raw.text.strip():
            return NoneAnswer(kind="none", text=NONE_LEFT)
        return TextAnswer(kind="text", text=_clip(raw.text, MAX_TEXT_CHARS), recipe_ids=_owned(raw.recipe_ids, allowed))
    alternatives: list[Alternative] = []
    for a in raw.alternatives:
        groups = [
            Group(heading=_clip(g.heading.strip(), MAX_SHORT_CHARS) or None, recipe_ids=ids)
            for g in a.groups
            if (ids := _owned(g.recipe_ids, allowed))
        ][:MAX_GROUPS]
        if groups:
            label = _clip(a.label.strip(), MAX_SHORT_CHARS) or None
            alternatives.append(Alternative(label=label, note=_clip(a.note, MAX_SHORT_CHARS), groups=groups))
    alternatives = alternatives[:MAX_ALTERNATIVES]
    if not alternatives:
        return NoneAnswer(kind="none", text=NONE_LEFT)
    if len(alternatives) == 1:
        alternatives[0].label = None
    return RecipesAnswer(kind="recipes", alternatives=alternatives)
