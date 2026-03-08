"""
Phase 3d — Recategorize an existing .paprikarecipes archive.

Loads every recipe from the archive, re-runs the Gemini categorisation
against the current categories.yaml, and writes a new archive alongside
the original with a ``_recategorized`` suffix.

Usage (CLI)::

    recipeparser --recategorize path/to/cookbook.paprikarecipes

Usage (Python)::

    from recipeparser.recategorize import recategorize
    out = recategorize(Path("cookbook.paprikarecipes"), client)
"""
from __future__ import annotations

import gzip
import json
import logging
import zipfile
from pathlib import Path
from typing import Optional

from recipeparser.categories import load_category_tree, build_paprika_categories, categorise_recipe
from recipeparser.exceptions import RecategorizationError
from recipeparser.models import RecipeExtraction

log = logging.getLogger(__name__)


def recategorize(paprika_path: Path, client, output_dir: Optional[Path] = None) -> Path:
    """
    Re-run category assignment on every recipe in a .paprikarecipes archive.

    Reads the archive, calls ``categorise_recipe()`` for each entry using the
    current ``categories.yaml``, and writes a new archive:
    ``<stem>_recategorized.paprikarecipes`` in ``output_dir`` (defaults to the
    same directory as the source file).

    Args:
        paprika_path: Path to the source ``.paprikarecipes`` file.
        client:       Authenticated ``google.genai.Client`` instance.
        output_dir:   Directory for the output file.  Defaults to the parent
                      directory of ``paprika_path``.

    Returns:
        Path to the newly written ``_recategorized.paprikarecipes`` file.

    Raises:
        RecategorizationError: If the archive cannot be read, is empty, or
                               writing fails.
    """
    paprika_path = Path(paprika_path)
    if not paprika_path.exists():
        raise RecategorizationError(f"File not found: {paprika_path}")
    if not zipfile.is_zipfile(paprika_path):
        raise RecategorizationError(f"Not a valid .paprikarecipes archive: {paprika_path}")

    # Resolve output path
    dest_dir = Path(output_dir) if output_dir else paprika_path.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_path = dest_dir / f"{paprika_path.stem}_recategorized.paprikarecipes"

    # Load category taxonomy once
    category_tree = load_category_tree()
    paprika_cats = build_paprika_categories(category_tree)
    log.info(
        "recategorize: loaded %d categories from taxonomy.", len(paprika_cats)
    )

    entries: list[tuple[str, bytes]] = []  # (member_name, updated_gzipped_json)

    with zipfile.ZipFile(paprika_path, "r") as zf_in:
        members = zf_in.namelist()
        if not members:
            raise RecategorizationError(
                f"Archive contains no entries: {paprika_path}"
            )

        log.info(
            "recategorize: processing %d recipe(s) from '%s'…",
            len(members), paprika_path.name,
        )

        for member in members:
            raw = zf_in.read(member)
            try:
                data = json.loads(gzip.decompress(raw).decode("utf-8"))
            except Exception as exc:
                log.warning(
                    "recategorize: could not parse '%s' — skipping. (%s)", member, exc
                )
                continue

            # Build a minimal RecipeExtraction-like object for categorise_recipe
            recipe = RecipeExtraction(
                name=data.get("name", ""),
                ingredients=_split_lines(data.get("ingredients", "")),
                directions=_split_lines(data.get("directions", "")),
                notes=data.get("notes") or None,
                categories=data.get("categories", []),
            )

            old_cats = recipe.categories
            try:
                new_cats = categorise_recipe(recipe, category_tree, paprika_cats, client)
            except Exception as exc:
                log.warning(
                    "recategorize: categorisation failed for '%s': %s — keeping original.",
                    recipe.name, exc,
                )
                new_cats = old_cats

            log.info(
                "  %-40s  %s → %s",
                recipe.name[:40],
                old_cats,
                new_cats,
            )

            data["categories"] = new_cats
            updated_json = json.dumps(data, ensure_ascii=False)
            updated_gz = gzip.compress(updated_json.encode("utf-8"))
            entries.append((member, updated_gz))

    if not entries:
        raise RecategorizationError(
            f"No parseable recipes found in '{paprika_path.name}'."
        )

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf_out:
        for member_name, gz_bytes in entries:
            zf_out.writestr(member_name, gz_bytes)

    log.info(
        "recategorize: wrote %d recipe(s) → %s", len(entries), out_path
    )
    return out_path


def _split_lines(value) -> list[str]:
    """Coerce a string or list to a list of non-empty lines."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [ln for ln in value.splitlines() if ln.strip()]
    return []
