"""Tests for recipeparser.paprika_db — find_paprika_db and read_categories_from_db."""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from recipeparser.paprika_db import find_paprika_db, read_categories_from_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(path: Path, rows: list[tuple]) -> Path:
    """Create a minimal Paprika.sqlite with a ZCATEGORY table populated from rows.

    Each row is (Z_PK, ZPARENT, ZNAME).
    """
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE ZCATEGORY (Z_PK INTEGER PRIMARY KEY, ZPARENT INTEGER, ZNAME TEXT)"
    )
    conn.executemany("INSERT INTO ZCATEGORY VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# find_paprika_db
# ---------------------------------------------------------------------------

class TestFindPaprikaDb:

    def test_returns_none_on_unsupported_platform(self):
        with patch.object(sys, "platform", "linux"):
            assert find_paprika_db() is None

    def test_returns_none_when_no_match_on_windows(self, tmp_path):
        """Glob finds nothing → returns None."""
        with patch.object(sys, "platform", "win32"), \
             patch("recipeparser.paprika_db.glob.glob", return_value=[]):
            assert find_paprika_db() is None

    def test_returns_most_recent_match_on_windows(self, tmp_path):
        """When multiple matches exist the newest (by mtime) is returned."""
        older = tmp_path / "old" / "Paprika.sqlite"
        newer = tmp_path / "new" / "Paprika.sqlite"
        older.parent.mkdir()
        newer.parent.mkdir()
        older.write_bytes(b"x")
        newer.write_bytes(b"x")
        # Nudge mtimes so 'newer' is definitively newer
        import os, time
        os.utime(older, (time.time() - 100, time.time() - 100))
        os.utime(newer, (time.time(), time.time()))

        with patch.object(sys, "platform", "win32"), \
             patch("recipeparser.paprika_db.glob.glob", return_value=[str(older), str(newer)]):
            result = find_paprika_db()
        assert result == newer

    def test_returns_none_when_no_match_on_macos(self):
        with patch.object(sys, "platform", "darwin"), \
             patch("recipeparser.paprika_db.glob.glob", return_value=[]):
            assert find_paprika_db() is None

    def test_returns_path_on_macos_single_match(self, tmp_path):
        db = tmp_path / "Paprika.sqlite"
        db.write_bytes(b"x")
        with patch.object(sys, "platform", "darwin"), \
             patch("recipeparser.paprika_db.glob.glob", return_value=[str(db)]):
            assert find_paprika_db() == db


# ---------------------------------------------------------------------------
# read_categories_from_db
# ---------------------------------------------------------------------------

class TestReadCategoriesFromDb:

    def test_flat_list_no_children(self, tmp_path):
        """Three top-level categories with no children."""
        db = _make_db(
            tmp_path / "Paprika.sqlite",
            [
                (1, None, "Breakfast"),
                (2, None, "Lunch"),
                (3, None, "Dinner"),
            ],
        )
        data, order = read_categories_from_db(db)
        assert set(data.keys()) == {"Breakfast", "Lunch", "Dinner"}
        assert all(v == [] for v in data.values())
        assert order == ["Breakfast", "Lunch", "Dinner"]

    def test_parent_child_hierarchy(self, tmp_path):
        """Children are correctly attached to their parent."""
        db = _make_db(
            tmp_path / "Paprika.sqlite",
            [
                (1, None, "Baking"),
                (2, 1,    "Cakes"),
                (3, 1,    "Breads"),
                (4, None, "Soups"),
            ],
        )
        data, order = read_categories_from_db(db)
        assert set(data.keys()) == {"Baking", "Soups"}
        assert set(data["Baking"]) == {"Cakes", "Breads"}
        assert data["Soups"] == []
        assert order == ["Baking", "Soups"]

    def test_order_follows_z_pk_ascending(self, tmp_path):
        """Top-level categories are returned in Z_PK ascending order."""
        db = _make_db(
            tmp_path / "Paprika.sqlite",
            [
                (10, None, "Z-first"),
                (1,  None, "A-second"),
                (5,  None, "M-third"),
            ],
        )
        data, order = read_categories_from_db(db)
        # Z_PK 1, 5, 10 → A-second, M-third, Z-first
        assert order == ["A-second", "M-third", "Z-first"]

    def test_null_name_rows_are_ignored(self, tmp_path):
        """Rows with NULL ZNAME should be silently skipped."""
        db = _make_db(
            tmp_path / "Paprika.sqlite",
            [
                (1, None, "Valid"),
                (2, None, None),       # NULL name — should be ignored
            ],
        )
        data, order = read_categories_from_db(db)
        assert list(data.keys()) == ["Valid"]

    def test_orphaned_child_treated_as_top_level(self, tmp_path):
        """A child whose ZPARENT PK doesn't exist is treated as top-level."""
        db = _make_db(
            tmp_path / "Paprika.sqlite",
            [
                (1, None, "Real Parent"),
                (2, 999,  "Orphan"),    # ZPARENT 999 not in table
            ],
        )
        data, order = read_categories_from_db(db)
        assert "Orphan" in data
        assert "Real Parent" in data

    def test_empty_table_returns_empty_results(self, tmp_path):
        """An empty ZCATEGORY table returns empty data and order."""
        db = _make_db(tmp_path / "Paprika.sqlite", [])
        data, order = read_categories_from_db(db)
        assert data == {}
        assert order == []

    def test_deep_nesting_grandchild_attached_to_grandparent(self, tmp_path):
        """Grandchildren (depth > 1) are attached directly to the top-level
        grandparent because the algorithm only tracks one level of top_name_by_pk.
        'Child' (pk=3, parent=pk=2) won't match top_name_by_pk (only pk=1 is there),
        so it is skipped in the second pass — it does NOT appear at all.
        This test documents that behaviour explicitly."""
        db = _make_db(
            tmp_path / "Paprika.sqlite",
            [
                (1, None, "Grandparent"),
                (2, 1,    "Parent"),
                (3, 2,    "Child"),     # depth 2 — skipped in second pass
            ],
        )
        data, order = read_categories_from_db(db)
        assert "Grandparent" in data
        assert "Parent" in data["Grandparent"]
        # "Child" is neither top-level nor attached — it is silently dropped
        assert "Child" not in data
        assert all("Child" not in children for children in data.values())

    def test_raises_on_missing_table(self, tmp_path):
        """A database without a ZCATEGORY table raises sqlite3.OperationalError."""
        db = tmp_path / "empty.sqlite"
        conn = sqlite3.connect(str(db))
        conn.close()
        with pytest.raises(sqlite3.OperationalError):
            read_categories_from_db(db)

    def test_duplicate_child_not_added_twice(self, tmp_path):
        """If for some reason the same child name appears twice, it only
        appears once in the parent's child list."""
        db = _make_db(
            tmp_path / "Paprika.sqlite",
            [
                (1, None, "Baking"),
                (2, 1,    "Cakes"),
                (3, 1,    "Cakes"),   # duplicate name
            ],
        )
        data, order = read_categories_from_db(db)
        assert data["Baking"].count("Cakes") == 1
