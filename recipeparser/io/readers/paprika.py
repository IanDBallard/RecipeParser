"""
paprika.py — Reader for .paprikarecipes archives.

A .paprikarecipes file is a ZIP archive where each entry is a gzip-compressed
JSON file (extension .paprikarecipe). Each JSON object represents one recipe
in Paprika 3 format.

Cayenne-flavored archives include a ``_cayenne_meta`` key in each entry
containing the full CayenneRecipe JSON plus the 1536-dim embedding, enabling
lossless round-trip restore without calling Gemini (Flow B — Instant Restore).

Legacy Paprika archives have no ``_cayenne_meta`` key and must be processed
through the full Cayenne pipeline (Flow A).

Usage::

    reader = PaprikaReader()
    entries = reader.read_entries("/path/to/export.paprikarecipes")
    for entry in entries:
        if "_cayenne_meta" in entry:
            # Flow B: Instant Restore
        else:
            # Flow A: Legacy Paprika → full pipeline
"""

import gzip
import json
import logging
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from recipeparser.core.models import Chunk, InputType
from recipeparser.io.readers import RecipeReader

log = logging.getLogger(__name__)


class PaprikaReader(RecipeReader):
    """
    Reads a .paprikarecipes ZIP archive and returns the decoded recipe entries.

    Each entry is a plain Python dict representing one Paprika recipe JSON
    object. The ``_cayenne_meta`` key, if present, contains the full
    CayenneRecipe JSON (including the 1536-dim embedding) as a nested dict.
    """

    def read(self, source: str) -> List[Chunk]:
        """
        Implement the RecipeReader ABC.

        Reads a .paprikarecipes archive and converts each entry into a Chunk:

        - Entries WITH ``_cayenne_meta`` → ``InputType.PAPRIKA_CAYENNE``
          - ``pre_parsed`` is populated from the meta dict (IngestResponse)
          - ``pre_parsed_embedding`` is extracted if present (enables $0 restore)
          - ``text`` is empty (not needed for the fast-path ASSEMBLE stage)
        - Entries WITHOUT ``_cayenne_meta`` → ``InputType.PAPRIKA_LEGACY``
          - ``text`` is built from name + ingredients + directions
          - Full pipeline (EXTRACT → REFINE → CATEGORIZE → EMBED → ASSEMBLE)

        Args:
            source: File-system path to the .paprikarecipes ZIP archive.

        Returns:
            A list of Chunk objects, one per recipe entry.
        """
        entries = self.read_entries(source)
        chunks: List[Chunk] = []

        for entry in entries:
            meta = entry.get("_cayenne_meta")

            if meta is not None:
                # Flow B — Cayenne Instant Restore
                # Lazy import to avoid circular dependency at module load time.
                from recipeparser.models import CayenneRecipe  # noqa: PLC0415

                # Extract the embedding separately before constructing the recipe
                # model. CayenneRecipe does not require an embedding field, so
                # this works whether or not the archive was exported with one.
                # IngestResponse *requires* embedding, so we use CayenneRecipe
                # here and carry the vector in pre_parsed_embedding instead.
                meta_copy = dict(meta)
                embedding: Optional[List[float]] = meta_copy.pop("embedding", None)

                try:
                    pre_parsed = CayenneRecipe(**meta_copy)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "PaprikaReader: could not deserialize _cayenne_meta for %r: %s",
                        entry.get("name"),
                        exc,
                    )
                    pre_parsed = None

                chunks.append(
                    Chunk(
                        text="",
                        input_type=InputType.PAPRIKA_CAYENNE,
                        pre_parsed=pre_parsed,
                        pre_parsed_embedding=embedding,
                        image_bytes=entry.get("photo_data"),
                    )
                )
            else:
                # Flow A — Legacy Paprika → full pipeline
                name = entry.get("name", "")
                ingredients = entry.get("ingredients", "")
                directions = entry.get("directions", "")
                text = f"{name}\n\nIngredients:\n{ingredients}\n\nDirections:\n{directions}"

                chunks.append(
                    Chunk(
                        text=text,
                        input_type=InputType.PAPRIKA_LEGACY,
                        image_bytes=entry.get("photo_data"),
                    )
                )

        log.info(
            "PaprikaReader.read: produced %d chunks from %s", len(chunks), source
        )
        return chunks


    def read_entries(self, path: str | Path) -> List[Dict[str, Any]]:
        """
        Parse a .paprikarecipes archive and return all recipe entries.

        Args:
            path: File-system path to the .paprikarecipes ZIP archive.

        Returns:
            A list of dicts, one per recipe. Each dict is the decoded JSON
            from the corresponding .paprikarecipe entry in the archive.

        Raises:
            ValueError: If the file is not a valid ZIP archive.
            RuntimeError: If a specific entry cannot be decoded (logged as
                          warning; the entry is skipped rather than aborting
                          the entire batch).
        """
        path = Path(path)
        if not zipfile.is_zipfile(path):
            raise ValueError(f"Not a valid ZIP archive: {path}")

        entries: List[Dict[str, Any]] = []

        with zipfile.ZipFile(path, "r") as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".paprikarecipe"):
                    log.debug("Skipping non-recipe entry: %s", name)
                    continue

                try:
                    compressed = zf.read(name)
                    raw_json = gzip.decompress(compressed)
                    entry: Dict[str, Any] = json.loads(raw_json)
                except gzip.BadGzipFile:
                    # Some exporters write uncompressed JSON directly
                    try:
                        entry = json.loads(compressed)
                    except json.JSONDecodeError as exc:
                        log.warning(
                            "Skipping entry %r — not valid gzip or JSON: %s", name, exc
                        )
                        continue
                except json.JSONDecodeError as exc:
                    log.warning("Skipping entry %r — JSON decode error: %s", name, exc)
                    continue
                except Exception as exc:
                    log.warning("Skipping entry %r — unexpected error: %s", name, exc)
                    continue

                # If _cayenne_meta is a JSON string, decode it to a dict now
                # so callers always receive a consistent nested-dict shape.
                meta = entry.get("_cayenne_meta")
                if isinstance(meta, str):
                    try:
                        entry["_cayenne_meta"] = json.loads(meta)
                    except json.JSONDecodeError as exc:
                        log.warning(
                            "Entry %r has _cayenne_meta but it is not valid JSON: %s",
                            name,
                            exc,
                        )
                        # Remove the malformed key so the entry falls through
                        # to Flow A (full pipeline) rather than failing silently.
                        del entry["_cayenne_meta"]

                entries.append(entry)
                log.debug(
                    "Parsed entry %r: name=%r cayenne=%s",
                    name,
                    entry.get("name"),
                    "_cayenne_meta" in entry,
                )

        log.info(
            "PaprikaReader: parsed %d entries from %s", len(entries), path.name
        )
        return entries

    def read_entries_with_images(self, path: str | Path) -> List[Dict[str, Any]]:
        """
        Enhanced version of read_entries that also returns binary image data.
        Returns a list of dicts, where each dict has:
          - 'recipe': The recipe JSON dict
          - 'image_bytes': Optional[bytes]
          - 'image_name': Optional[str]
        """
        path = Path(path)
        if not zipfile.is_zipfile(path):
            raise ValueError(f"Not a valid ZIP archive: {path}")

        results: List[Dict[str, Any]] = []

        with zipfile.ZipFile(path, "r") as zf:
            # Map of photo_hash -> binary data for fast lookup
            image_map = {}
            for name in zf.namelist():
                # Paprika stores images in the root or a subdirectory, usually matching the photo_hash
                if name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                    image_map[name] = zf.read(name)

            for name in zf.namelist():
                if not name.lower().endswith(".paprikarecipe"):
                    continue

                try:
                    compressed = zf.read(name)
                    raw_json = gzip.decompress(compressed)
                    entry = json.loads(raw_json)
                except Exception:
                    try:
                        entry = json.loads(compressed)
                    except Exception:
                        continue

                # Handle _cayenne_meta
                meta = entry.get("_cayenne_meta")
                if isinstance(meta, str):
                    try:
                        entry["_cayenne_meta"] = json.loads(meta)
                    except Exception:
                        pass

                # Extract image if present
                photo_name = entry.get('photo')
                image_bytes = None
                if photo_name and photo_name in image_map:
                    image_bytes = image_map[photo_name]
                
                results.append({
                    'recipe': entry,
                    'image_bytes': image_bytes,
                    'image_name': photo_name
                })

        return results
