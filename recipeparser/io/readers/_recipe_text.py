"""
_recipe_text.py — the chunk text and citation a library reader builds for one recipe.

PaprikaReader (legacy entries) and MelaReader both hand the extractor the same
layout, so the two importers cannot drift apart: the title, any facts the source
states ("Servings: 4"), the ingredients, then the directions.
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

from recipeparser.core.citation import Citation, classify_source, web_citation


def recipe_text(
    title: str,
    ingredients: str,
    directions: str,
    facts: Sequence[Tuple[str, str]] = (),
) -> str:
    """The text the extractor reads: title, the non-empty facts one per line, ingredients, directions."""
    lines = [f"{label}: {value}" for label, value in facts if value]
    block = "\n".join(lines) + "\n\n" if lines else ""
    return f"{title}\n\n{block}Ingredients:\n{ingredients}\n\nDirections:\n{directions}"


def source_citation(source: Optional[str], url: Optional[str]) -> Citation:
    """A stated source first, else the web page the recipe came from, else no citation."""
    if source:
        return classify_source(source).citation
    if url and url.lower().startswith("http"):
        return web_citation(url)
    return classify_source(None).citation
