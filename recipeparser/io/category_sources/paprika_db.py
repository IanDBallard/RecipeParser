"""
Paprika SQLite-backed category source for CLI and GUI adapters.

Reads the user's existing Paprika category hierarchy from a local Paprika
SQLite database file (``paprika.db``).  Intended for the "import from Paprika"
workflow where the user wants to seed their Cayenne taxonomy from their
existing Paprika folder structure.

Paprika stores categories in a flat ``ZCATEGORY`` table with columns:
  Z_PK          — integer primary key
  ZNAME         — category name (text)
  ZPARENTCATEGORY — FK to parent Z_PK (NULL for top-level)

Because Paprika categories are a flat list (no axis concept), this source
maps each **top-level** Paprika category to its own axis, with its
sub-categories as the tags.  Top-level categories with no children are
treated as a single-tag axis (axis name == tag name).

If the database file is missing, unreadable, or has no categories, load_axes()
returns {} (no categorization — Zero-Tag Mandate).
"""
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from recipeparser.io.category_sources.base import CategorySource

log = logging.getLogger(__name__)


class PaprikaCategorySource(CategorySource):
    """
    Loads taxonomy axes from a Paprika SQLite database file.

    Args:
        db_path: Path to the Paprika ``paprika.db`` file.  If None or the
                 file does not exist, load_axes() returns {}.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._path = Path(db_path) if db_path else None

    # ------------------------------------------------------------------
    # CategorySource interface
    # ------------------------------------------------------------------

    def load_axes(self, user_id: str) -> Dict[str, List[str]]:
        """
        Derive multipolar axes from the Paprika category hierarchy.

        ``user_id`` is ignored (file-based source).

        Mapping strategy:
        - Top-level categories (ZPARENTCATEGORY IS NULL) → axis names
        - Their children → tags under that axis
        - Top-level categories with no children → axis with a single tag
          equal to the category name itself

        Returns {} if the file is missing, unreadable, or has no categories.
        """
        rows = self._read_categories()
        if not rows:
            return {}

        # Build id → name and id → parent_id maps
        id_to_name: Dict[int, str] = {}
        id_to_parent: Dict[int, Optional[int]] = {}
        for pk, name, parent_pk in rows:
            id_to_name[pk] = name
            id_to_parent[pk] = parent_pk

        # Separate top-level from children
        top_level_ids = [pk for pk, parent in id_to_parent.items() if parent is None]
        children_by_parent: Dict[int, List[str]] = {}
        for pk, parent in id_to_parent.items():
            if parent is not None:
                children_by_parent.setdefault(parent, []).append(id_to_name[pk])

        axes: Dict[str, List[str]] = {}
        for top_id in top_level_ids:
            axis_name = id_to_name[top_id]
            children = children_by_parent.get(top_id, [])
            if children:
                axes[axis_name] = sorted(children)
            else:
                # Leaf top-level category — treat as a single-tag axis
                axes[axis_name] = [axis_name]

        log.info(
            "PaprikaCategorySource: loaded %d axes from %s.",
            len(axes),
            self._path,
        )
        return axes

    def load_category_ids(self, user_id: str) -> Dict[str, str]:
        """
        Paprika DB source does not write to Supabase — returns empty dict.
        Junction table writes are not supported for file-based sources.
        """
        return {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_categories(self) -> List[tuple]:
        """
        Query the Paprika SQLite DB for all category rows.

        Returns a list of (Z_PK, ZNAME, ZPARENTCATEGORY) tuples, or []
        on any error.
        """
        if self._path is None:
            log.debug("PaprikaCategorySource: no db_path configured — returning empty axes.")
            return []

        if not self._path.exists():
            log.warning(
                "PaprikaCategorySource: database not found at %s — returning empty axes.",
                self._path,
            )
            return []

        try:
            con = sqlite3.connect(str(self._path))
            try:
                cur = con.execute(
                    "SELECT Z_PK, ZNAME, ZPARENTCATEGORY FROM ZCATEGORY"
                )
                rows = cur.fetchall()
            finally:
                con.close()
        except sqlite3.OperationalError as exc:
            # Table may not exist (not a Paprika DB, or different schema)
            log.warning(
                "PaprikaCategorySource: could not query ZCATEGORY in %s: %s",
                self._path,
                exc,
            )
            return []
        except Exception as exc:
            log.error(
                "PaprikaCategorySource: unexpected error reading %s: %s",
                self._path,
                exc,
            )
            return []

        # Filter out rows with null/empty names
        valid = [(pk, name, parent) for pk, name, parent in rows if name and name.strip()]
        log.debug(
            "PaprikaCategorySource: read %d valid category rows from %s.",
            len(valid),
            self._path,
        )
        return valid
