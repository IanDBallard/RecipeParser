"""
Locate and read category data from the live Paprika 3 SQLite database.

Only the ZCATEGORY table is accessed, opened strictly read-only so it is safe
to call while Paprika is running (assuming no active sync is in progress).
"""
from __future__ import annotations

import glob
import sqlite3
import sys
from pathlib import Path
from typing import Optional


def find_paprika_db() -> Optional[Path]:
    """Search the OS-specific app-data location for Paprika.sqlite.

    Returns the path to the most-recently-modified match, or None if not found.
    Supports Windows and macOS; returns None on other platforms.
    """
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Local" / "Packages"
        pattern = str(base / "HindsightLabsLLC.PaprikaRecipeManager_*" / "LocalState" / "Paprika.sqlite")
        matches = glob.glob(pattern)

    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Containers"
        pattern = str(base / "com.hindsightlabs.paprika.mac*" / "Data" / "Library" / "**" / "Paprika.sqlite")
        matches = glob.glob(pattern, recursive=True)

    else:
        return None

    if not matches:
        return None

    matches.sort(key=lambda p: Path(p).stat().st_mtime, reverse=True)
    return Path(matches[0])


def read_categories_from_db(
    db_path: Path,
) -> tuple[dict[str, list[str]], list[str]]:
    """Read the ZCATEGORY table and return the category hierarchy.

    Returns a tuple of:
      data   — dict mapping each parent name to an ordered list of child names.
               Top-level categories (no parent) map to an empty list.
      order  — list of parent names in Z_PK ascending order (insertion order).

    This maps directly onto CategoryEditorFrame._data and ._order so the GUI
    can load the result without any further transformation.

    Raises sqlite3.Error if the database cannot be opened or queried.
    """
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT Z_PK, ZPARENT, ZNAME FROM ZCATEGORY ORDER BY Z_PK ASC"
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    # Build a lookup of pk -> (name, parent_pk)
    nodes: dict[int, tuple[str, Optional[int]]] = {
        pk: (name, parent_pk)
        for pk, parent_pk, name in rows
        if name  # guard against NULL names
    }

    # First pass: identify top-level entries (ZPARENT IS NULL or points to
    # a pk that doesn't exist in the result set — handles orphaned rows)
    valid_pks = set(nodes.keys())
    data: dict[str, list[str]] = {}
    order: list[str] = []

    for pk, (name, parent_pk) in nodes.items():
        if parent_pk is None or parent_pk not in valid_pks:
            data[name] = []
            order.append(name)

    # Second pass: attach children to their parents
    top_name_by_pk = {
        pk: name
        for pk, (name, parent_pk) in nodes.items()
        if parent_pk is None or parent_pk not in valid_pks
    }

    for pk, (name, parent_pk) in nodes.items():
        if parent_pk is not None and parent_pk in top_name_by_pk:
            parent_name = top_name_by_pk[parent_pk]
            if name not in data[parent_name]:
                data[parent_name].append(name)

    return data, order
