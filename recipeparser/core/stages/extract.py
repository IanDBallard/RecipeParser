"""
recipeparser/core/stages/extract.py — EXTRACT stage.

Wraps gemini.extract_recipes() / gemini.extract_recipe_from_text() and holds the model to the
verbatim rule (verbatim-ingestion D1, D2): every number an ingredient line writes must be one the
source writes. The first attempt's clean recipes are kept. When some recipe breaks the rule the
chunk is extracted once more, and from that retry only a clean recipe of the same name as a failed
one is taken. Every failed recipe the retry does not recover is dropped and named, so no drop is
silent (D2). A recipe only the retry found is ignored.

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
import logging
from typing import Any, List, NamedTuple

from recipeparser.core.numbers import unmatched_numbers
from recipeparser.gemini import (
    extract_recipe_from_text,
    extract_recipes,
    needs_table_normalisation,
    normalise_baker_table,
)
from recipeparser.models import RecipeExtraction

log = logging.getLogger(__name__)


class Extraction(NamedTuple):
    """The recipes EXTRACT kept, and the titles of those it dropped as rewritten (D2)."""

    recipes: List[RecipeExtraction]
    rewritten: List[str]


def _run(chunk_text: str, client: Any, plain_text_mode: bool) -> List[RecipeExtraction]:
    result = extract_recipe_from_text(chunk_text, client) if plain_text_mode else extract_recipes(chunk_text, client)
    return result.recipes if result.recipes else []


def extract(
    chunk_text: str,
    client: Any,
    *,
    plain_text_mode: bool = False,
) -> Extraction:
    """
    Extract all recipes from a single text chunk, verbatim.

    Args:
        chunk_text:      The raw text to process.  Must be non-empty.
        client:          An initialised ``google.genai.Client`` instance.
        plain_text_mode: When True, uses the simpler ``extract_recipe_from_text``
                         prompt (suited for pasted/Paprika text rather than
                         EPUB/PDF book chunks).

    Returns:
        An ``Extraction``.  ``recipes`` is empty when the chunk contains no
        recognisable recipe — NOT an error.  ``rewritten`` names each recipe
        dropped because its ingredient lines write a number the source never does.

    Raises:
        ValueError: If ``chunk_text`` is empty or whitespace-only.
        ExtractionParseError: If Gemini's reply could not be parsed on any
            attempt.  Distinct from an empty chunk: the recipes existed and
            were lost, so the caller must count this rather than ignore it.
    """
    if not chunk_text or not chunk_text.strip():
        raise ValueError("extract(): chunk_text must be non-empty.")

    source_text = chunk_text
    # Baker's-percentage table pre-processing (book chunks only)
    if not plain_text_mode and needs_table_normalisation(chunk_text):
        log.info("extract(): baker's-percentage table detected — normalising.")
        chunk_text = normalise_baker_table(chunk_text, client)
        # The table prompt re-lays a table out and is told to add no value, so a number it wrote
        # counts as the writer's: the source is both texts.
        source_text = f"{source_text}\n{chunk_text}"

    def clean(recipe: RecipeExtraction) -> bool:
        return not unmatched_numbers(source_text, recipe.ingredients)

    first = _run(chunk_text, client, plain_text_mode)
    if all(clean(r) for r in first):
        log.info("extract(): kept %d recipe(s).", len(first))
        return Extraction(first, [])

    log.warning("extract(): ingredient lines did not match the source — extracting once more.")
    # Only a clean retry recipe of a failed recipe's exact name replaces it; each is used once.
    recovered = [r for r in _run(chunk_text, client, plain_text_mode) if clean(r)]

    kept: List[RecipeExtraction] = []
    rewritten: List[str] = []
    for recipe in first:
        if clean(recipe):
            kept.append(recipe)
            continue
        replacement = next((r for r in recovered if r.name == recipe.name), None)
        if replacement is not None:
            recovered.remove(replacement)
            kept.append(replacement)
            continue
        log.warning(
            "extract(): dropped %r — its ingredient lines write %s, which the source never does.",
            recipe.name, ", ".join(unmatched_numbers(source_text, recipe.ingredients)),
        )
        rewritten.append(recipe.name)
    log.info("extract(): kept %d recipe(s), dropped %d as rewritten.", len(kept), len(rewritten))
    return Extraction(kept, rewritten)
