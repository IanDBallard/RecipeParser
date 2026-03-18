"""Paprika 3 export bundler — assembles the .paprikarecipes ZIP archive."""
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
from typing import List

from recipeparser.models import RecipeExtraction
from recipeparser.utils import title_case

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

    seen_keys: set = set()
    entries: list = []  # list of (internal_filename, raw_bytes) tuples

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
