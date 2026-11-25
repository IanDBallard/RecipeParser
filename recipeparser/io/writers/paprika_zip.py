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
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from recipeparser.core.fat_tokens import strip_fat_tokens
from recipeparser.io.writers import RecipeWriter
from recipeparser.models import IngestResponse, RecipeExtraction
from recipeparser.utils import title_case

# Regex that matches a single Fat Token: {{ing_01|fallback text}}

log = logging.getLogger(__name__)


def _leading_minutes(text: str) -> int:
    """The first number in a time string ("20 min" → 20), or 0 when it has none."""
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else 0


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
            if prep and cook:
                t = _leading_minutes(prep) + _leading_minutes(cook)
                if t:
                    total = f"{t} mins"

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

@dataclass(frozen=True)
class MergeResult:
    """What ``merge_exports`` wrote, and what it left out.

    ``unreadable`` names each entry that could not be parsed ("<entry> in <archive>") and
    each input that was not a ZIP archive ("<archive> (not a ZIP archive)"), so the caller
    can tell the user rather than report a clean merge.
    """

    path: Path
    written: int
    duplicates: int
    unreadable: Tuple[str, ...]


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


def merge_exports(paths: List[Path], output_dir: Path) -> MergeResult:
    """
    Merge multiple .paprikarecipes archives into a single combined archive.

    Each archive is a ZIP of gzip-compressed JSON files (.paprikarecipe entries).
    Entries are deduplicated by normalised recipe name (lowercase, no accents,
    no punctuation); the first occurrence wins (preserves source order).

    Args:
        paths:      List of .paprikarecipes Path objects to merge.
        output_dir: Directory where the merged archive will be written.

    Returns:
        A ``MergeResult``: the merged ``merged_<timestamp>.paprikarecipes`` path, how many
        recipes it holds, how many duplicates were dropped, and every entry or archive
        that could not be read.

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
    unreadable: List[str] = []
    duplicates = 0

    for archive_path in paths:
        archive_path = Path(archive_path)
        log.info("merge_exports: reading %s", archive_path.name)
        try:
            zf = zipfile.ZipFile(archive_path, "r")
        except zipfile.BadZipFile as exc:
            log.warning("merge_exports: '%s' is not a valid ZIP — skipping. (%s)", archive_path.name, exc)
            unreadable.append(f"{archive_path.name} (not a ZIP archive)")
            continue
        with zf:
            for member in zf.namelist():
                try:
                    raw = zf.read(member)
                    data = json.loads(gzip.decompress(raw).decode("utf-8"))
                except (zipfile.BadZipFile, zlib.error, OSError, EOFError, ValueError) as exc:
                    # bad CRC / corrupt member, not gzip (BadGzipFile is an OSError), truncated,
                    # not UTF-8 or not JSON (UnicodeDecodeError and JSONDecodeError are ValueErrors)
                    log.warning(
                        "merge_exports: could not parse entry '%s' in '%s' — skipping. (%s)",
                        member, archive_path.name, exc,
                    )
                    unreadable.append(f"{member} in {archive_path.name}")
                    continue
                if not isinstance(data, dict):
                    log.warning(
                        "merge_exports: entry '%s' in '%s' is not a recipe object — skipping.",
                        member, archive_path.name,
                    )
                    unreadable.append(f"{member} in {archive_path.name}")
                    continue
                name = str(data.get("name") or "")

                key = _normalise_recipe_name(name)
                if key in seen_keys:
                    log.info(
                        "merge_exports: duplicate '%s' (key='%s') — skipping.", name, key
                    )
                    duplicates += 1
                    continue

                seen_keys.add(key)
                entries.append((member, raw))

    if not entries:
        raise ExportError("merge_exports: no recipes found after deduplication.")

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_filename = f"merged_{timestamp}.paprikarecipes"
    out_path = output_dir / out_filename

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf_out:
        for internal_name, raw_bytes in entries:
            zf_out.writestr(internal_name, raw_bytes)

    log.info(
        "merge_exports: wrote %d recipe(s) → %s (%d duplicate(s), %d unreadable)",
        len(entries), out_path, duplicates, len(unreadable),
    )
    return MergeResult(out_path, len(entries), duplicates, tuple(unreadable))


# ---------------------------------------------------------------------------
# PaprikaWriter — RecipeWriter port implementation
# ---------------------------------------------------------------------------

def _strip_fat_tokens(text: str) -> str:
    """Replace every Fat Token with its words: ``{{ing_01|flour|all}}`` → ``flour``."""
    return strip_fat_tokens(text)


def _ingest_to_paprika_dict(recipe: IngestResponse) -> Dict[str, Any]:
    """
    Convert an ``IngestResponse`` to a Paprika 3 JSON dict.

    - ``structured_ingredients`` → plain-text ``ingredients`` (one per line,
      using ``fallback_string`` so the output is human-readable).
    - ``tokenized_directions`` → plain-text ``directions`` (Fat Tokens stripped
      to their fallback strings).
    - ``categories`` flat list → Paprika ``categories`` list.
    - the recovered photo (``recipe.photo``) → ``photo`` + base64 ``photo_data``
      + ``photo_hash`` (see ``_photo_keys``). ``photo`` and ``photo_data`` are
      omitted when there is none: an empty ``photo`` key crashes Paprika for
      Windows with "Access is denied".
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
        "notes": recipe.notes or "",
        "description": recipe.description or "",
        "nutritional_info": recipe.nutritional_info or "",
        "difficulty": recipe.difficulty or "",
        # Paprika's rating is an integer; a plain Paprika archive has no
        # _cayenne_meta to hold the null, so an unrated recipe reads as 0 here and
        # is lost. That is the format's limit, not a defect: this writer's output
        # is for Paprika.
        "rating": recipe.rating or 0,
        "source": recipe.source or "",
        "source_url": recipe.source_url or "",
        "image_url": recipe.image_url or "",
        "categories": recipe.categories,
        "created": created,
        "hash": hashlib.sha256(recipe_uid.encode()).hexdigest(),
        "photo_hash": "",  # replaced by _photo_keys when there is a photo
        "photo_large": None,
        **_photo_keys(recipe),
    }


_PHOTO_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


def _photo_keys(recipe: IngestResponse) -> Dict[str, str]:
    """``photo``, ``photo_data`` and ``photo_hash`` for a recipe with a photo; nothing otherwise.

    Written as Paprika writes them in its own export:

    - ``photo`` is a fresh ``<UUID>.<ext>``, never the source's name. Paprika 4
      for Windows refuses an entry whose photo is named after the book's image
      ("Refusing Paprika file with an invalid photo p264.jpg", RecipeParser #149)
      and imports the same entry once the name is a UUID (measured 2026-11-17).
      Fresh on every export, so neither two recipes sharing one book image nor a
      book imported twice brings Paprika a photo it already has: Paprika 3
      crashed on one whose name and bytes were both in its library (2026-11-16).
    - ``photo_hash`` is the upper-case SHA-256 of the image bytes.
    """
    photo = recipe.photo
    if photo is None or not photo.data:
        return {}
    ext = _PHOTO_EXTENSIONS.get(photo.content_type, "jpg")
    return {
        "photo": f"{str(uuid.uuid4()).upper()}.{ext}",
        "photo_data": base64.b64encode(photo.data).decode("ascii"),
        "photo_hash": hashlib.sha256(photo.data).hexdigest().upper(),
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
