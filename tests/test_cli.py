"""Tests for recipeparser.__main__ — CLI argument parsing and path resolution."""
import sys
import pytest
from pathlib import Path
from unittest.mock import patch

from recipeparser.__main__ import _resolve_epub


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
