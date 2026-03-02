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

    On Windows two install variants are checked:
      1. Desktop installer  — %LOCALAPPDATA%\\Paprika Recipe Manager 3\\Database\\
      2. Microsoft Store    — %LOCALAPPDATA%\\Packages\\HindsightLabsLLC.*\\LocalState\\
    """
    if sys.platform == "win32":
        local = Path.home() / "AppData" / "Local"

        patterns = [
            # Desktop / EXE installer (most common)
            str(local / "Paprika Recipe Manager 3" / "Database" / "Paprika.sqlite"),
            # Microsoft Store / UWP package
            str(local / "Packages" / "HindsightLabsLLC.PaprikaRecipeManager_*" / "LocalState" / "Paprika.sqlite"),
        ]
        matches = []
        for pattern in patterns:
            matches.extend(glob.glob(pattern))

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


def _detect_schema(conn: sqlite3.Connection) -> str:
    """Return 'modern' for the desktop EXE schema or 'cordata' for the old
    CoreData/UWP schema, based on which tables are present."""
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "recipe_categories" in tables:
        return "modern"
    if "ZCATEGORY" in tables:
        return "coredata"
    raise sqlite3.OperationalError(
        "Cannot find a recognised category table (recipe_categories or ZCATEGORY). "
        "Is this a Paprika 3 database?"
    )


def _read_modern(conn: sqlite3.Connection) -> tuple[dict[str, list[str]], list[str]]:
    """Read the desktop-installer schema: recipe_categories table.

    Columns used: uid (TEXT), name (TEXT), order_flag (INTEGER), parent_uid (TEXT).
    """
    rows = conn.execute(
        "SELECT uid, name, order_flag, parent_uid FROM recipe_categories "
        "WHERE status != 'deleted' OR status IS NULL "
        "ORDER BY order_flag ASC"
    ).fetchall()

    # uid -> (name, parent_uid)
    nodes: dict[str, tuple[str, Optional[str]]] = {
        uid: (name, parent_uid)
        for uid, name, order_flag, parent_uid in rows
        if name
    }

    valid_uids = set(nodes.keys())
    data: dict[str, list[str]] = {}
    order: list[str] = []

    for uid, (name, parent_uid) in nodes.items():
        if not parent_uid or parent_uid not in valid_uids:
            data[name] = []
            order.append(name)

    top_name_by_uid = {
        uid: name
        for uid, (name, parent_uid) in nodes.items()
        if not parent_uid or parent_uid not in valid_uids
    }

    for uid, (name, parent_uid) in nodes.items():
        if parent_uid and parent_uid in top_name_by_uid:
            parent_name = top_name_by_uid[parent_uid]
            if name not in data[parent_name]:
                data[parent_name].append(name)

    return data, order


def _read_coredata(conn: sqlite3.Connection) -> tuple[dict[str, list[str]], list[str]]:
    """Read the CoreData/UWP schema: ZCATEGORY table.

    Columns used: Z_PK (INTEGER), ZPARENT (INTEGER), ZNAME (TEXT).
    """
    rows = conn.execute(
        "SELECT Z_PK, ZPARENT, ZNAME FROM ZCATEGORY ORDER BY Z_PK ASC"
    ).fetchall()

    nodes: dict[int, tuple[str, Optional[int]]] = {
        pk: (name, parent_pk)
        for pk, parent_pk, name in rows
        if name
    }

    valid_pks = set(nodes.keys())
    data: dict[str, list[str]] = {}
    order: list[str] = []

    for pk, (name, parent_pk) in nodes.items():
        if parent_pk is None or parent_pk not in valid_pks:
            data[name] = []
            order.append(name)

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


def read_categories_from_db(
    db_path: Path,
) -> tuple[dict[str, list[str]], list[str]]:
    """Read the Paprika category hierarchy from a SQLite database.

    Supports both the modern desktop-installer schema (recipe_categories table)
    and the older CoreData/UWP schema (ZCATEGORY table), auto-detecting which
    is present.

    Returns a tuple of:
      data   — dict mapping each parent name to an ordered list of child names.
               Top-level categories (no parent) map to an empty list.
      order  — list of parent names in display order.

    Raises sqlite3.Error if the database cannot be opened or queried.
    """
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        schema = _detect_schema(conn)
        if schema == "modern":
            return _read_modern(conn)
        else:
            return _read_coredata(conn)
    finally:
        conn.close()
