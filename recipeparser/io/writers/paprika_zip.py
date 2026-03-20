"""Paprika 3 export bundler — assembles the .paprikarecipes ZIP archive.

Also exposes ``PaprikaWriter`` — a ``RecipeWriter`` port implementation that
converts ``IngestResponse`` objects to Paprika 3 format and writes them to a
``.paprikarecipes`` ZIP archive.

Fat Tokens in ``tokenized_directions[].text`` are stripped to their fallback
strings before writing, so the output is plain-text compatible with Paprika.
"""
import base64
import datetime
import gzip
import hashlib
import json
import logging
import os
import re
import unicodedata
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from recipeparser.io.writers import RecipeWriter
from recipeparser.models import IngestResponse, RecipeExtraction
from recipeparser.utils import title_case

# Regex that matches a single Fat Token: {{ing_01|fallback text}}
_FAT_TOKEN_RE = re.compile(r"\{\{[^|]+\|([^}]+)\}\}")

log = logging.getLogger(__name__)


def create_paprika_export(
    recipes: List[RecipeExtraction],
    output_dir: str,
    image_dir: str,
    export_filename: str,
    book_source: str = "EPUB Auto-Import",
) -> bool:
    """
    Bundle recipes into a .paprikarecipes archive (ZIP of gzipped JSON files).

    Returns True on success, False if nothing was written.
    ``book_source`` should be "Title — Author" derived from EPUB metadata.

    Photo keys are omitted entirely when no image is present — an empty ``photo``
    key causes Paprika for Windows to crash with "Access is denied".
    """
    if not recipes:
        log.warning("No recipes to export — skipping bundle creation.")
        return False

    export_path = os.path.join(output_dir, export_filename)
    log.info("Bundling %d recipe(s) into %s ...", len(recipes), export_filename)

    with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as zip_archive:
        for recipe in recipes:
            recipe_uid = str(uuid.uuid4()).upper()
            photo_data = ""
            photo_name = ""

            if recipe.photo_filename:
                img_path = os.path.join(image_dir, recipe.photo_filename)
                if os.path.exists(img_path):
                    with open(img_path, "rb") as img_file:
                        photo_data = base64.b64encode(img_file.read()).decode("utf-8")
                    photo_name = recipe.photo_filename
                else:
                    log.warning(
                        "Image '%s' referenced by recipe '%s' not found — skipping photo.",
                        recipe.photo_filename,
                        recipe.name,
                    )

            created = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            prep = recipe.prep_time or ""
            cook = recipe.cook_time or ""

            total = ""
            try:
                if prep and cook:
                    def _mins(s: str) -> int:
                        m = re.search(r"(\d+)", s)
                        return int(m.group(1)) if m else 0

                    t = _mins(prep) + _mins(cook)
                    if t:
                        total = f"{t} mins"
            except Exception:
                pass

            paprika_dict = {
                "uid": recipe_uid,
                "name": title_case(recipe.name),
                "directions": "\n".join(recipe.directions),
                "ingredients": "\n".join(recipe.ingredients),
                "prep_time": prep,
                "cook_time": cook,
                "total_time": total,
                "servings": recipe.servings or "",
                "notes": recipe.notes or "",
                "description": "",
                "nutritional_info": "",
                "difficulty": "",
                "rating": 0,
                "source": title_case(book_source),
                "source_url": "",
                "image_url": "",
                "categories": recipe.categories,
                "created": created,
                "hash": hashlib.sha256(recipe_uid.encode()).hexdigest(),
                "photo_hash": "",
                "photo_large": None,
            }

            if photo_name and photo_data:
                paprika_dict["photo"] = photo_name
                paprika_dict["photo_data"] = photo_data

            json_str = json.dumps(paprika_dict, ensure_ascii=False)
            gzipped_content = gzip.compress(json_str.encode("utf-8"))

            safe_title = "".join(
                c for c in recipe.name if c.isalnum() or c in " -_"
            ).strip()
            if not safe_title:
                safe_title = "Untitled_Recipe"
            internal_filename = f"{safe_title}.paprikarecipe"

            zip_archive.writestr(internal_filename, gzipped_content)

    log.info("Export created: %s", export_path)
    return True


# ---------------------------------------------------------------------------
# Phase 3a — Multi-file merge
# ---------------------------------------------------------------------------

def _normalise_recipe_name(name: str) -> str:
    """
    Normalise a recipe name for deduplication:
    lowercase, strip accents, remove punctuation, collapse whitespace.
    """
    # Decompose accented characters (e.g. é → e + combining accent)
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Remove punctuation, keep alphanumeric and spaces
    cleaned = re.sub(r"[^\w\s]", "", ascii_only, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def merge_exports(paths: List[Path], output_dir: Path) -> Path:
    """
    Merge multiple .paprikarecipes archives into a single combined archive.

    Each archive is a ZIP of gzip-compressed JSON files (.paprikarecipe entries).
    Entries are deduplicated by normalised recipe name (lowercase, no accents,
    no punctuation); the first occurrence wins (preserves source order).

    Args:
        paths:      List of .paprikarecipes Path objects to merge.
        output_dir: Directory where the merged archive will be written.

    Returns:
        Path to the merged ``merged_<timestamp>.paprikarecipes`` file.

    Raises:
        ValueError:  If ``paths`` is empty.
        ExportError: If no entries survive deduplication or writing fails.
    """
    from recipeparser.exceptions import ExportError

    if not paths:
        raise ValueError("merge_exports() requires at least one input path.")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    seen_keys: Set[str] = set()
    entries: List[Tuple[str, bytes]] = []  # list of (internal_filename, raw_bytes) tuples

    for archive_path in paths:
        archive_path = Path(archive_path)
        log.info("merge_exports: reading %s", archive_path.name)
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                for member in zf.namelist():
                    raw = zf.read(member)
                    try:
                        data = json.loads(gzip.decompress(raw).decode("utf-8"))
                        name = data.get("name", "")
                    except Exception:
                        log.warning(
                            "merge_exports: could not parse entry '%s' in '%s' — skipping.",
                            member, archive_path.name,
                        )
                        continue

                    key = _normalise_recipe_name(name)
                    if key in seen_keys:
                        log.info(
                            "merge_exports: duplicate '%s' (key='%s') — skipping.", name, key
                        )
                        continue

                    seen_keys.add(key)
                    entries.append((member, raw))
        except zipfile.BadZipFile as exc:
            log.warning("merge_exports: '%s' is not a valid ZIP — skipping. (%s)", archive_path.name, exc)

    if not entries:
        raise ExportError("merge_exports: no recipes found after deduplication.")

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_filename = f"merged_{timestamp}.paprikarecipes"
    out_path = output_dir / out_filename

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf_out:
        for internal_name, raw_bytes in entries:
            zf_out.writestr(internal_name, raw_bytes)

    log.info(
        "merge_exports: wrote %d recipe(s) → %s", len(entries), out_path
    )
    return out_path


# ---------------------------------------------------------------------------
# PaprikaWriter — RecipeWriter port implementation
# ---------------------------------------------------------------------------

def _strip_fat_tokens(text: str) -> str:
    """Replace every Fat Token with its fallback string.

    ``{{ing_01|1.5 cups flour}}`` → ``1.5 cups flour``
    """
    return _FAT_TOKEN_RE.sub(r"\1", text)


def _ingest_to_paprika_dict(recipe: IngestResponse) -> Dict[str, Any]:
    """
    Convert an ``IngestResponse`` to a Paprika 3 JSON dict.

    - ``structured_ingredients`` → plain-text ``ingredients`` (one per line,
      using ``fallback_string`` so the output is human-readable).
    - ``tokenized_directions`` → plain-text ``directions`` (Fat Tokens stripped
      to their fallback strings).
    - ``categories`` flat list → Paprika ``categories`` list.
    """
    recipe_uid = str(uuid.uuid4()).upper()
    created = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    ingredients_lines = [
        ing.fallback_string for ing in recipe.structured_ingredients
    ]
    directions_lines = [
        _strip_fat_tokens(step.text)
        for step in sorted(recipe.tokenized_directions, key=lambda s: s.step)
    ]

    servings_str = str(recipe.base_servings) if recipe.base_servings is not None else ""

    return {
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
        "created": created,
        "hash": hashlib.sha256(recipe_uid.encode()).hexdigest(),
        "photo_hash": "",
        "photo_large": None,
    }


class PaprikaWriter(RecipeWriter):
    """
    Writes a batch of ``IngestResponse`` objects to a ``.paprikarecipes`` ZIP.

    Fat Tokens in ``tokenized_directions[].text`` are stripped to their
    fallback strings so the output is plain-text compatible with Paprika 3.
    No ``_cayenne_meta`` key is embedded — use ``CayenneZipWriter`` for that.

    Args:
        output_path: Destination file path for the ``.paprikarecipes`` archive.
                     The parent directory must already exist.

    Example::

        writer = PaprikaWriter(output_path="/tmp/export.paprikarecipes")
        writer.write(pipeline_results)
    """

    def __init__(self, output_path: Union[str, Path]) -> None:
        self._output_path = Path(output_path)

    def write(self, recipes: List[IngestResponse], **kwargs: object) -> None:
        """
        Serialise each recipe to Paprika 3 format and bundle into a ZIP.

        Args:
            recipes: All successfully processed ``IngestResponse`` objects.
            **kwargs: Accepted but ignored (satisfies the ABC contract).

        Raises:
            RuntimeError: If the archive cannot be written.
        """
        if not recipes:
            log.warning("PaprikaWriter.write: no recipes — skipping.")
            return

        try:
            with zipfile.ZipFile(self._output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for recipe in recipes:
                    paprika_dict = _ingest_to_paprika_dict(recipe)
                    json_str = json.dumps(paprika_dict, ensure_ascii=False)
                    gzipped = gzip.compress(json_str.encode("utf-8"))

                    safe_title = "".join(
                        c for c in recipe.title if c.isalnum() or c in " -_"
                    ).strip() or "Untitled_Recipe"
                    zf.writestr(f"{safe_title}.paprikarecipe", gzipped)
        except OSError as exc:
            raise RuntimeError(
                f"PaprikaWriter: could not write archive {self._output_path}: {exc}"
            ) from exc

        log.info(
            "PaprikaWriter: wrote %d recipe(s) → %s",
            len(recipes),
            self._output_path,
        )
