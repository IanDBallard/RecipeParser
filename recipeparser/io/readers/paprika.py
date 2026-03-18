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
from typing import Any, Dict, List

log = logging.getLogger(__name__)


class PaprikaReader:
    """
    Reads a .paprikarecipes ZIP archive and returns the decoded recipe entries.

    Each entry is a plain Python dict representing one Paprika recipe JSON
    object. The ``_cayenne_meta`` key, if present, contains the full
    CayenneRecipe JSON (including the 1536-dim embedding) as a nested dict.
    """

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
