# recipeparser/adapters/ai_search/answer.py
"""The answer's shape (Cayenne AI search §4.5) and the checks that clean it before it leaves.

``RawAnswer`` is what the agent returns (every field present, unused ones empty, because the
structured-output schema is strict). ``finalize`` turns it into an ``Answer``: only recipes the cook
owns survive (A2), and empty parts disappear.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

NONE_LEFT = "None of your recipes fit that."
MAX_ALTERNATIVES = 4

_KINDS = ["recipes", "text", "none", "out_of_scope"]


class Group(BaseModel):
    heading: str | None
    recipe_ids: list[uuid.UUID]


class Alternative(BaseModel):
    label: str | None = None
    note: str
    groups: list[Group]

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
    text: str
    recipe_ids: list[uuid.UUID]


class NoneAnswer(BaseModel):
    kind: Literal["none"]
    text: str


class OutOfScopeAnswer(BaseModel):
    kind: Literal["out_of_scope"]


Answer = Annotated[
    RecipesAnswer | TextAnswer | NoneAnswer | OutOfScopeAnswer,
    Field(discriminator="kind"),
]


class Previous(BaseModel):
    """The turn being refined: the device holds it and sends it back (nothing is stored here)."""

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


def _owned(ids: list[str], allowed: set[str]) -> list[uuid.UUID]:
    """The ids in ``allowed``, in order, repeats dropped; anything that is not a uuid is dropped."""
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
    return kept


def finalize(raw: RawAnswer, *, permitted: set[str]) -> Answer:
    """The raw answer with every id outside ``permitted`` removed and empty parts dropped."""
    allowed = {p.lower() for p in permitted}
    if raw.kind == "out_of_scope":
        return OutOfScopeAnswer(kind="out_of_scope")
    if raw.kind == "none":
        return NoneAnswer(kind="none", text=raw.text.strip() or NONE_LEFT)
    if raw.kind == "text":
        if not raw.text.strip():
            return NoneAnswer(kind="none", text=NONE_LEFT)
        return TextAnswer(kind="text", text=raw.text, recipe_ids=_owned(raw.recipe_ids, allowed))
    alternatives: list[Alternative] = []
    for a in raw.alternatives:
        groups = [
            Group(heading=g.heading.strip() or None, recipe_ids=ids)
            for g in a.groups
            if (ids := _owned(g.recipe_ids, allowed))
        ]
        if groups:
            alternatives.append(Alternative(label=a.label.strip() or None, note=a.note, groups=groups))
    alternatives = alternatives[:MAX_ALTERNATIVES]
    if not alternatives:
        return NoneAnswer(kind="none", text=NONE_LEFT)
    if len(alternatives) == 1:
        alternatives[0].label = None
    return RecipesAnswer(kind="recipes", alternatives=alternatives)
