"""Tests for recipeparser.__main__ — CLI argument parsing and path resolution."""
import sqlite3
import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from recipeparser.__main__ import _resolve_epub, _cmd_sync_categories


class TestResolveEpub:

    def test_direct_epub_file_returned(self, tmp_path):
        epub = tmp_path / "cookbook.epub"
        epub.write_bytes(b"PK")
        assert _resolve_epub(str(epub)) == str(epub)

    def test_calibre_folder_with_one_epub_resolved(self, tmp_path):
        epub = tmp_path / "My Cookbook - Author.epub"
        epub.write_bytes(b"PK")
        result = _resolve_epub(str(tmp_path))
        assert result == str(epub)

    def test_calibre_folder_with_multiple_epubs_exits(self, tmp_path):
        (tmp_path / "book_a.epub").write_bytes(b"PK")
        (tmp_path / "book_b.epub").write_bytes(b"PK")
        with pytest.raises(SystemExit):
            _resolve_epub(str(tmp_path))

    def test_folder_with_no_epub_exits(self, tmp_path):
        (tmp_path / "readme.txt").write_text("hello")
        with pytest.raises(SystemExit):
            _resolve_epub(str(tmp_path))

    def test_non_epub_file_exits(self, tmp_path):
        f = tmp_path / "cookbook.pdf"
        f.write_bytes(b"%PDF")
        with pytest.raises(SystemExit):
            _resolve_epub(str(f))

    def test_nonexistent_path_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            _resolve_epub(str(tmp_path / "ghost.epub"))


# ---------------------------------------------------------------------------
# --sync-categories CLI flag
# ---------------------------------------------------------------------------

def _make_db_with_categories(path: Path) -> Path:
    """Create a minimal Paprika.sqlite with a couple of categories."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE ZCATEGORY (Z_PK INTEGER PRIMARY KEY, ZPARENT INTEGER, ZNAME TEXT)"
    )
    conn.executemany(
        "INSERT INTO ZCATEGORY VALUES (?, ?, ?)",
        [
            (1, None, "Breakfast"),
            (2, 1,    "Pancakes"),
            (3, None, "Dinner"),
        ],
    )
    conn.commit()
    conn.close()
    return path


class TestSyncCategoriesCommand:

    def test_writes_yaml_to_categories_file(self, tmp_path, monkeypatch):
        """Happy path: DB found, YAML written, success message printed."""
        db = _make_db_with_categories(tmp_path / "Paprika.sqlite")
        dest = tmp_path / "categories.yaml"

        with patch("recipeparser.__main__.find_paprika_db", return_value=db), \
             patch("recipeparser.__main__._CATEGORIES_FILE", dest):
            _cmd_sync_categories()

        assert dest.exists()
        content = yaml.safe_load(dest.read_text(encoding="utf-8"))
        assert "categories" in content
        cats = content["categories"]
        assert "Breakfast" in cats
        assert "Pancakes" in cats["Breakfast"]
        assert "Dinner" in cats

    def test_exits_when_db_not_found(self, capsys):
        """When find_paprika_db returns None the command should sys.exit(1)."""
        with patch("recipeparser.__main__.find_paprika_db", return_value=None):
            with pytest.raises(SystemExit) as exc_info:
                _cmd_sync_categories()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Paprika" in captured.err

    def test_exits_when_db_empty(self, tmp_path, capsys):
        """When the DB has no categories the command should sys.exit(1)."""
        db = tmp_path / "Paprika.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE ZCATEGORY (Z_PK INTEGER PRIMARY KEY, ZPARENT INTEGER, ZNAME TEXT)"
        )
        conn.commit()
        conn.close()

        dest = tmp_path / "categories.yaml"
        with patch("recipeparser.__main__.find_paprika_db", return_value=db), \
             patch("recipeparser.__main__._CATEGORIES_FILE", dest):
            with pytest.raises(SystemExit) as exc_info:
                _cmd_sync_categories()
        assert exc_info.value.code == 1

    def test_prints_summary_counts(self, tmp_path, capsys):
        """The success output should mention category and subcategory counts."""
        db = _make_db_with_categories(tmp_path / "Paprika.sqlite")
        dest = tmp_path / "categories.yaml"

        with patch("recipeparser.__main__.find_paprika_db", return_value=db), \
             patch("recipeparser.__main__._CATEGORIES_FILE", dest):
            _cmd_sync_categories()

        captured = capsys.readouterr()
        # Should mention "2" top-level categories (Breakfast, Dinner)
        assert "2" in captured.out
        # Should mention the destination file
        assert str(dest) in captured.out

    def test_cli_flag_triggers_sync_and_exits_cleanly(self, tmp_path):
        """Integration: passing --sync-categories via sys.argv calls _cmd_sync_categories."""
        db = _make_db_with_categories(tmp_path / "Paprika.sqlite")
        dest = tmp_path / "categories.yaml"

        with patch("recipeparser.__main__.find_paprika_db", return_value=db), \
             patch("recipeparser.__main__._CATEGORIES_FILE", dest), \
             patch("sys.argv", ["recipeparser", "--sync-categories"]):
            from recipeparser.__main__ import main
            main()  # should not raise

        assert dest.exists()

    def test_epub_arg_still_required_without_flag(self, capsys):
        """Without --sync-categories the epub positional arg is still required."""
        with patch("sys.argv", ["recipeparser"]):
            from recipeparser.__main__ import main
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code != 0
