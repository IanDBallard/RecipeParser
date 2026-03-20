"""
cayenne_zip.py — CayenneZipWriter: writes a Cayenne-flavored .paprikarecipes archive.

A Cayenne archive is a standard Paprika 3 ZIP (gzip-compressed JSON entries)
with one extra key per entry: ``_cayenne_meta``.

``_cayenne_meta`` format::

    {
        # All fields from CayenneRecipe.model_dump()
        "title": "...",
        "structured_ingredients": [...],
        "tokenized_directions": [...],   # Fat Tokens PRESERVED (not stripped)
        ...
        # Plus the 1536-dim embedding vector
        "embedding": [0.001, -0.002, ...]
    }

When ``PaprikaReader`` encounters ``_cayenne_meta`` it routes the entry to
Flow B (Instant Restore): the CayenneRecipe is reconstructed directly from
the meta dict and the embedding is reused — zero Gemini API calls, zero cost.

TID rule: this module MUST NOT import from ``recipeparser.adapters``.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Union

from recipeparser.io.writers import RecipeWriter
from recipeparser.models import CayenneRecipe, IngestResponse

log = logging.getLogger(__name__)


def _build_cayenne_meta(recipe: IngestResponse) -> Dict[str, Any]:
    """
    Build the ``_cayenne_meta`` dict for a single recipe.

    The dict is ``CayenneRecipe.model_dump()`` merged with
    ``{"embedding": recipe.embedding}``.  Fat Tokens in
    ``tokenized_directions[].text`` are preserved verbatim so that
    Flow B can reconstruct the full structured recipe without Gemini.

    Args:
        recipe: A fully-processed ``IngestResponse`` from the pipeline.

    Returns:
        A plain Python dict suitable for JSON serialisation.
    """
    # CayenneRecipe is the parent of IngestResponse; model_dump() on the
    # IngestResponse includes the embedding field, so we must exclude it
    # and re-add it explicitly to keep the format stable.
    cayenne_fields = CayenneRecipe.model_fields.keys()
    meta: Dict[str, Any] = {k: getattr(recipe, k) for k in cayenne_fields}

    # model_dump() gives us nested Pydantic objects as dicts automatically;
    # we need to do the same for our manual extraction.
    meta["structured_ingredients"] = [
        ing.model_dump() for ing in recipe.structured_ingredients
    ]
    meta["tokenized_directions"] = [
        step.model_dump() for step in recipe.tokenized_directions
    ]

    # Attach the embedding vector
    meta["embedding"] = recipe.embedding

    return meta


class CayenneZipWriter(RecipeWriter):
    """
    Writes a batch of ``IngestResponse`` objects to a Cayenne-flavored
    ``.paprikarecipes`` ZIP archive.

    Each entry is a standard Paprika 3 gzip-compressed JSON file with an
    additional ``_cayenne_meta`` key containing the full ``CayenneRecipe``
    JSON plus the 1536-dim embedding vector.

    When this archive is imported back into Cayenne, ``PaprikaReader``
    detects ``_cayenne_meta`` and routes to Flow B (Instant Restore):
    the recipe is reconstructed directly from the meta dict — zero Gemini
    API calls, zero cost.

    The plain-text ``ingredients`` and ``directions`` fields are also
    written (Fat Tokens stripped to fallback strings) so the archive
    remains compatible with stock Paprika 3.

    Args:
        output_path: Destination file path for the ``.paprikarecipes`` archive.
                     The parent directory must already exist.

    Example::

        writer = CayenneZipWriter(output_path="/tmp/cayenne_export.paprikarecipes")
        writer.write(pipeline_results)
    """

    def __init__(self, output_path: Union[str, Path]) -> None:
        self._output_path = Path(output_path)

    def write(self, recipes: List[IngestResponse], **kwargs: object) -> None:
        """
        Serialise each recipe to Paprika 3 format + ``_cayenne_meta`` and
        bundle into a ZIP archive.

        Args:
            recipes: All successfully processed ``IngestResponse`` objects.
            **kwargs: Accepted but ignored (satisfies the ABC contract).

        Raises:
            RuntimeError: If the archive cannot be written.
        """
        if not recipes:
            log.warning("CayenneZipWriter.write: no recipes — skipping.")
            return

        # Import the Fat Token stripper from the sibling module to avoid
        # duplicating the regex.  This is an intra-package import (io → io),
        # which is permitted by the TID rule.
        from recipeparser.io.writers.paprika_zip import _strip_fat_tokens  # noqa: PLC0415

        try:
            with zipfile.ZipFile(self._output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for recipe in recipes:
                    recipe_uid = str(uuid.uuid4()).upper()

                    # Plain-text fields (Paprika-compatible)
                    ingredients_lines = [
                        ing.fallback_string for ing in recipe.structured_ingredients
                    ]
                    directions_lines = [
                        _strip_fat_tokens(step.text)
                        for step in sorted(
                            recipe.tokenized_directions, key=lambda s: s.step
                        )
                    ]
                    servings_str = (
                        str(recipe.base_servings)
                        if recipe.base_servings is not None
                        else ""
                    )

                    paprika_dict: Dict[str, Any] = {
                        "uid": recipe_uid,
                        "name": recipe.title,
                        "directions": "\n".join(directions_lines),
                        "ingredients": "\n".join(ingredients_lines),
                        "prep_time": recipe.prep_time or "",
                        "cook_time": recipe.cook_time or "",
                        "total_time": "",
                        "servings": servings_str,
                        "notes": "",
                        "description": "",
                        "nutritional_info": "",
                        "difficulty": "",
                        "rating": 0,
                        "source": "",
                        "source_url": recipe.source_url or "",
                        "image_url": recipe.image_url or "",
                        "categories": recipe.categories,
                        "hash": hashlib.sha256(recipe_uid.encode()).hexdigest(),
                        "photo_hash": "",
                        "photo_large": None,
                        # Cayenne-specific key — enables Flow B (Instant Restore)
                        "_cayenne_meta": _build_cayenne_meta(recipe),
                    }

                    json_str = json.dumps(paprika_dict, ensure_ascii=False)
                    gzipped = gzip.compress(json_str.encode("utf-8"))

                    safe_title = (
                        "".join(
                            c for c in recipe.title if c.isalnum() or c in " -_"
                        ).strip()
                        or "Untitled_Recipe"
                    )
                    zf.writestr(f"{safe_title}.paprikarecipe", gzipped)

        except OSError as exc:
            raise RuntimeError(
                f"CayenneZipWriter: could not write archive {self._output_path}: {exc}"
            ) from exc

        log.info(
            "CayenneZipWriter: wrote %d recipe(s) → %s",
            len(recipes),
            self._output_path,
        )
