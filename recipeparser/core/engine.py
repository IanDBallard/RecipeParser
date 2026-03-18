"""
core/engine.py — Pure orchestration engine for Project Cayenne.

ARCHITECTURAL INVARIANT:
  This module is pure — it performs no file I/O, no writer calls, and no
  adapter-layer operations.  It only orchestrates calls between the Gemini
  provider (via the injected client) and the models layer.

  Allowed imports: recipeparser.models, recipeparser.gemini, recipeparser.utils
  Forbidden imports: recipeparser.export, recipeparser.supabase_writer,
                     recipeparser.io.*, recipeparser.adapters.*
"""
import logging
from typing import Dict, List, Optional

from recipeparser.models import RecipeExtraction

log = logging.getLogger(__name__)


def deduplicate_recipes(recipes: List[RecipeExtraction]) -> List[RecipeExtraction]:
    """
    Remove duplicate recipes based on a normalised version of the name.
    Keeps the first occurrence (preserves chapter order).
    """
    seen: set = set()
    unique: List[RecipeExtraction] = []

    for recipe in recipes:
        key = recipe.name.lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(recipe)
        else:
            log.info("Duplicate recipe skipped: '%s'", recipe.name)

    return unique


def run_cayenne_pipeline(
    source_text: str,
    client,
    uom_system: str = "US",
    measure_preference: str = "Volume",
    source_url: Optional[str] = None,
    image_url: Optional[str] = None,
    user_axes: Optional[Dict[str, List[str]]] = None,
) -> "IngestResponse":
    """
    Stateless high-fidelity pipeline for Project Cayenne:
    1. Extract raw recipe(s) from text.
    2. Refine the first recipe found into Cayenne format (Fat Tokens + UOM +
       multipolar categorization when ``user_axes`` is provided).
    3. Generate 1536-dim vector embedding of 'title + ingredient names'.

    ``image_url`` is an optional Supabase Storage public URL for the recipe's
    hero image.  The caller is responsible for uploading the image and
    constructing the URL before calling this function.  When None, the recipe
    row will have no photo.

    ``user_axes`` is the caller-supplied multipolar taxonomy dict
    (axis_name → [tag, ...]).  When provided, the refinement pass instructs
    Gemini to categorize the recipe against those axes.  When None or empty,
    no categories are assigned (Zero-Tag Mandate).

    Returns an IngestResponse (defined in models.py).
    Raises RuntimeError or ValueError on pipeline failure.
    """
    from recipeparser.models import CayenneRecipe, IngestResponse
    from recipeparser.gemini import (
        extract_recipe_from_text,
        refine_recipe_for_cayenne,
        get_embeddings,
    )
    from recipeparser.utils import title_case

    recipe_list = extract_recipe_from_text(source_text, client)
    if not recipe_list or not recipe_list.recipes:
        raise ValueError("No recipes found in source text.")

    raw_recipe = recipe_list.recipes[0]

    refined = refine_recipe_for_cayenne(
        raw_recipe,
        client,
        uom_system=uom_system,
        measure_preference=measure_preference,
        user_axes=user_axes or {},
    )
    if not refined:
        raise RuntimeError("Refinement pass failed.")

    # Vectorize for semantic search: title + ingredient names
    ing_names = ", ".join([i.name for i in refined.structured_ingredients])
    embedding_input = f"{refined.title}\n\n{ing_names}"
    embedding = get_embeddings(embedding_input, client)

    # Flatten grid_categories → flat categories list for Paprika compatibility.
    # e.g. {"Cuisine": ["Italian"], "Protein": ["Chicken"]} → ["Italian", "Chicken"]
    grid = refined.grid_categories if isinstance(refined.grid_categories, dict) else {}
    flat_categories: List[str] = [
        tag
        for tags in grid.values()
        for tag in (tags if isinstance(tags, list) else [])
    ]

    cayenne_recipe = CayenneRecipe(
        title=title_case(refined.title),
        prep_time=raw_recipe.prep_time,
        cook_time=raw_recipe.cook_time,
        base_servings=refined.base_servings or 4,
        source_url=source_url,
        image_url=image_url,
        categories=flat_categories,
        grid_categories=grid,
        structured_ingredients=refined.structured_ingredients,
        tokenized_directions=refined.tokenized_directions,
    )

    return IngestResponse(**cayenne_recipe.model_dump(), embedding=embedding)
