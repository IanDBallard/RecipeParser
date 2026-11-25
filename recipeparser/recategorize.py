"""
Phase 3d — Recategorize an existing .paprikarecipes archive.

Loads every recipe from the archive, re-runs the Gemini categorisation
against the current categories.yaml, and writes a new archive alongside
the original with a ``_recategorized`` suffix.

Usage (CLI)::

    recipeparser --recategorize path/to/cookbook.paprikarecipes

Usage (Python)::

    from recipeparser.recategorize import recategorize
    result = recategorize(Path("cookbook.paprikarecipes"), client)
    result.path, result.kept_original, result.copied_unparsed
"""
from __future__ import annotations

import gzip
import json
import logging
import zipfile
from pathlib import Path
from typing import NamedTuple, Optional

from recipeparser.categories import load_category_tree, build_paprika_categories, categorise_recipe
from recipeparser.exceptions import DailyQuotaExhaustedError, JobCancelledError, RecategorizationError
from recipeparser.models import RecipeExtraction

log = logging.getLogger(__name__)


class RecategorizeResult(NamedTuple):
    """What a recategorise run wrote. Every member of the source is in the output."""
    path: Path
    #: Recipes given the categories Gemini chose this run.
    recategorized: int
    #: Recipes whose categorisation call failed, written with their own categories.
    kept_original: int
    #: Members that could not be parsed as a recipe, copied through byte for byte.
    copied_unparsed: int


def recategorize(paprika_path: Path, client, output_dir: Optional[Path] = None) -> RecategorizeResult:
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
        A ``RecategorizeResult``: the path of the newly written
        ``_recategorized.paprikarecipes`` file and the counts of recipes
        recategorised, kept with their own categories (the call failed), and
        members copied through unparsed. The output holds every source member.

    Raises:
        RecategorizationError: If the archive cannot be read, is empty, holds
                               no parseable recipe, or writing fails.
        ConfigurationError:    If categories.yaml exists but is malformed.
        DailyQuotaExhaustedError, JobCancelledError: the run stops; nothing is
                               written.
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
    recategorized = kept_original = copied_unparsed = 0

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
                if not isinstance(data, dict):
                    raise ValueError(f"expected a recipe object, got {type(data).__name__}")
                # Build a minimal RecipeExtraction-like object for categorise_recipe
                recipe = RecipeExtraction(
                    name=data.get("name", ""),
                    ingredients=_split_lines(data.get("ingredients", "")),
                    directions=_split_lines(data.get("directions", "")),
                    notes=data.get("notes") or None,
                    categories=data.get("categories", []),
                )
            except (OSError, EOFError, ValueError) as exc:
                # gzip raises OSError/EOFError; JSON, UTF-8 and pydantic raise ValueError.
                # The member is copied through unchanged so the output keeps every
                # recipe the source had, and the count is reported at the end.
                log.warning(
                    "recategorize: could not parse '%s' — copied through unchanged. (%s)", member, exc
                )
                entries.append((member, raw))
                copied_unparsed += 1
                continue

            old_cats = recipe.categories
            try:
                new_cats = categorise_recipe(recipe, category_tree, paprika_cats, client)
                recategorized += 1
            except (DailyQuotaExhaustedError, JobCancelledError):
                raise  # every later recipe would fail the same way: stop the run
            # Any other failure of the call keeps the recipe's own categories; counted
            # and reported to the caller, never silently given the fallback.
            except Exception as exc:  # fail-loud.bare-except-ok: counted in kept_original and reported
                log.warning(
                    "recategorize: categorisation failed for '%s': %s — keeping original.",
                    recipe.name, exc,
                )
                new_cats = old_cats
                kept_original += 1

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

    if not recategorized and not kept_original:
        raise RecategorizationError(
            f"No parseable recipes found in '{paprika_path.name}'."
        )

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf_out:
        for member_name, gz_bytes in entries:
            zf_out.writestr(member_name, gz_bytes)

    log.info(
        "recategorize: wrote %d member(s) → %s (%d recategorised, %d kept their own "
        "categories, %d copied through unparsed)",
        len(entries), out_path, recategorized, kept_original, copied_unparsed,
    )
    return RecategorizeResult(out_path, recategorized, kept_original, copied_unparsed)


def _split_lines(value) -> list[str]:
    """Coerce a string or list to a list of non-empty lines."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [ln for ln in value.splitlines() if ln.strip()]
    return []
