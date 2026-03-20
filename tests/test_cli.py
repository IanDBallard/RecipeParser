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

    def test_non_epub_pdf_file_exits(self, tmp_path):
        f = tmp_path / "cookbook.txt"
        f.write_text("hello")
        with pytest.raises(SystemExit):
            _resolve_epub(str(f))

    def test_direct_pdf_file_returned(self, tmp_path):
        pdf = tmp_path / "cookbook.pdf"
        pdf.write_bytes(b"%PDF")
        assert _resolve_epub(str(pdf)) == str(pdf)

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


# ---------------------------------------------------------------------------
# --concurrency and --rpm CLI args
# ---------------------------------------------------------------------------

class TestConcurrencyRpmArgs:

    def test_concurrency_1_accepted(self, tmp_path):
        epub = tmp_path / "cookbook.epub"
        epub.write_bytes(b"PK")
        with patch("recipeparser.__main__.run_cli_pipeline") as mock_process, \
             patch("recipeparser.__main__._resolve_epub", return_value=str(epub)), \
             patch("google.genai.Client"), \
             patch("os.environ.get", return_value="dummy-key"), \
             patch("sys.argv", ["recipeparser", str(epub), "--concurrency", "1"]):
            from recipeparser.__main__ import main
            main()
        mock_process.assert_called_once()
        assert mock_process.call_args.kwargs["concurrency"] == 1

    def test_concurrency_10_accepted(self, tmp_path):
        epub = tmp_path / "cookbook.epub"
        epub.write_bytes(b"PK")
        with patch("recipeparser.__main__.run_cli_pipeline") as mock_process, \
             patch("recipeparser.__main__._resolve_epub", return_value=str(epub)), \
             patch("google.genai.Client"), \
             patch("os.environ.get", return_value="dummy-key"), \
             patch("sys.argv", ["recipeparser", str(epub), "--concurrency", "10"]):
            from recipeparser.__main__ import main
            main()
        assert mock_process.call_args.kwargs["concurrency"] == 10

    def test_concurrency_0_rejected(self, tmp_path, capsys):
        epub = tmp_path / "cookbook.epub"
        epub.write_bytes(b"PK")
        with patch("recipeparser.__main__._resolve_epub", return_value=str(epub)), \
             patch("sys.argv", ["recipeparser", str(epub), "--concurrency", "0"]):
            from recipeparser.__main__ import main
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "concurrency" in err.lower() or "1" in err

    def test_concurrency_11_rejected(self, tmp_path, capsys):
        epub = tmp_path / "cookbook.epub"
        epub.write_bytes(b"PK")
        with patch("recipeparser.__main__._resolve_epub", return_value=str(epub)), \
             patch("sys.argv", ["recipeparser", str(epub), "--concurrency", "11"]):
            from recipeparser.__main__ import main
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "concurrency" in err.lower() or "10" in err

    def test_rpm_passed_to_process_epub(self, tmp_path):
        epub = tmp_path / "cookbook.epub"
        epub.write_bytes(b"PK")
        with patch("recipeparser.__main__.run_cli_pipeline") as mock_process, \
             patch("recipeparser.__main__._resolve_epub", return_value=str(epub)), \
             patch("google.genai.Client"), \
             patch("os.environ.get", return_value="dummy-key"), \
             patch("sys.argv", ["recipeparser", str(epub), "--rpm", "15"]):
            from recipeparser.__main__ import main
            main()
        mock_process.assert_called_once()
        assert mock_process.call_args.kwargs["rpm"] == 15

    def test_concurrency_and_rpm_both_passed(self, tmp_path):
        epub = tmp_path / "cookbook.epub"
        epub.write_bytes(b"PK")
        with patch("recipeparser.__main__.run_cli_pipeline") as mock_process, \
             patch("recipeparser.__main__._resolve_epub", return_value=str(epub)), \
             patch("google.genai.Client"), \
             patch("os.environ.get", return_value="dummy-key"), \
             patch("sys.argv", ["recipeparser", str(epub), "--concurrency", "5", "--rpm", "30"]):
            from recipeparser.__main__ import main
            main()
        assert mock_process.call_args.kwargs["concurrency"] == 5
        assert mock_process.call_args.kwargs["rpm"] == 30


# ---------------------------------------------------------------------------
# --merge CLI flag (Phase 3a)
# ---------------------------------------------------------------------------

def _make_minimal_paprika(path) -> "Path":
    """Write a minimal valid .paprikarecipes archive for merge tests."""
    import gzip, json, zipfile
    from pathlib import Path as _Path
    path = _Path(path)
    recipe = {"name": "Test Recipe", "ingredients": "flour", "directions": "mix"}
    gz = gzip.compress(json.dumps(recipe).encode("utf-8"))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Test Recipe.paprikarecipe", gz)
    return path


class TestMergeCommand:

    def test_merge_two_archives_calls_merge_exports(self, tmp_path):
        a = _make_minimal_paprika(tmp_path / "a.paprikarecipes")
        b = _make_minimal_paprika(tmp_path / "b.paprikarecipes")
        merged = tmp_path / "merged_20260101_120000.paprikarecipes"

        with patch("recipeparser.__main__._cmd_merge") as mock_merge, \
             patch("sys.argv", ["recipeparser", "--merge", str(a), str(b),
                                "--output", str(tmp_path)]):
            from recipeparser.__main__ import main
            main()
        mock_merge.assert_called_once_with([str(a), str(b)], str(tmp_path))

    def test_merge_missing_file_exits_nonzero(self, tmp_path, capsys):
        a = _make_minimal_paprika(tmp_path / "a.paprikarecipes")
        ghost = tmp_path / "ghost.paprikarecipes"

        with patch("sys.argv", ["recipeparser", "--merge", str(a), str(ghost),
                                "--output", str(tmp_path)]):
            from recipeparser.__main__ import main
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code != 0
        assert "not found" in capsys.readouterr().err

    def test_merge_single_archive_succeeds(self, tmp_path):
        a = _make_minimal_paprika(tmp_path / "a.paprikarecipes")

        with patch("recipeparser.__main__._cmd_merge") as mock_merge, \
             patch("sys.argv", ["recipeparser", "--merge", str(a),
                                "--output", str(tmp_path)]):
            from recipeparser.__main__ import main
            main()
        mock_merge.assert_called_once()

    def test_merge_prints_output_path(self, tmp_path, capsys):
        a = _make_minimal_paprika(tmp_path / "a.paprikarecipes")
        b = _make_minimal_paprika(tmp_path / "b.paprikarecipes")

        with patch("recipeparser.export.merge_exports") as mock_merge_fn:
            expected_out = tmp_path / "merged_20260101_120000.paprikarecipes"
            mock_merge_fn.return_value = expected_out
            with patch("sys.argv", ["recipeparser", "--merge", str(a), str(b),
                                    "--output", str(tmp_path)]):
                from recipeparser.__main__ import main
                main()

        captured = capsys.readouterr()
        assert str(expected_out) in captured.out


# ---------------------------------------------------------------------------
# --recategorize CLI flag (Phase 3d)
# ---------------------------------------------------------------------------

class TestRecategorizeCommand:

    def test_recategorize_calls_cmd_recategorize(self, tmp_path):
        archive = _make_minimal_paprika(tmp_path / "cookbook.paprikarecipes")

        with patch("recipeparser.__main__._cmd_recategorize") as mock_recategorize, \
             patch("sys.argv", ["recipeparser", "--recategorize", str(archive),
                                "--output", str(tmp_path)]):
            from recipeparser.__main__ import main
            main()
        mock_recategorize.assert_called_once_with(str(archive), str(tmp_path))

    def test_recategorize_exits_when_no_api_key(self, tmp_path, capsys, monkeypatch):
        archive = _make_minimal_paprika(tmp_path / "cookbook.paprikarecipes")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        with patch("recipeparser.__main__.get_env_file") as mock_env_file:
            # Return a path that doesn't exist so no key is loaded from .env
            mock_env_file.return_value = tmp_path / ".env_missing"
            with patch("sys.argv", ["recipeparser", "--recategorize", str(archive),
                                    "--output", str(tmp_path)]):
                from recipeparser.__main__ import main
                with pytest.raises(SystemExit) as exc_info:
                    main()
        assert exc_info.value.code != 0
        assert "GOOGLE_API_KEY" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --folder CLI flag (Phase 3a)
# ---------------------------------------------------------------------------

class TestFolderCommand:

    def test_folder_processes_all_epubs(self, tmp_path):
        (tmp_path / "a.epub").write_bytes(b"PK")
        (tmp_path / "b.epub").write_bytes(b"PK")

        import os
        os.environ["GOOGLE_API_KEY"] = "dummy-key"
        with patch("recipeparser.__main__.run_cli_pipeline", return_value=str(tmp_path / "out.paprikarecipes")) as mock_process, \
             patch("google.genai.Client"), \
             patch("sys.argv", ["recipeparser", "--folder", str(tmp_path),
                                "--output", str(tmp_path)]):
            from recipeparser.__main__ import main
            main()

        assert mock_process.call_count == 2

    def test_folder_nonexistent_dir_exits(self, tmp_path, capsys):
        ghost_dir = tmp_path / "ghost_folder"
        with patch("sys.argv", ["recipeparser", "--folder", str(ghost_dir),
                                "--output", str(tmp_path)]):
            from recipeparser.__main__ import main
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code != 0
        assert "not a directory" in capsys.readouterr().err.lower()

    def test_folder_empty_dir_exits(self, tmp_path, capsys):
        empty = tmp_path / "empty"
        empty.mkdir()
        with patch("sys.argv", ["recipeparser", "--folder", str(empty),
                                "--output", str(tmp_path)]):
            from recipeparser.__main__ import main
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "No .epub" in err or "No" in err

    def test_folder_includes_pdfs(self, tmp_path):
        (tmp_path / "a.epub").write_bytes(b"PK")
        (tmp_path / "b.pdf").write_bytes(b"%PDF")

        import os
        os.environ["GOOGLE_API_KEY"] = "dummy-key"
        with patch("recipeparser.__main__.run_cli_pipeline", return_value=str(tmp_path / "out.paprikarecipes")) as mock_process, \
             patch("google.genai.Client"), \
             patch("sys.argv", ["recipeparser", "--folder", str(tmp_path),
                                "--output", str(tmp_path)]):
            from recipeparser.__main__ import main
            main()

        assert mock_process.call_count == 2

    def test_folder_partial_failure_exits_nonzero(self, tmp_path, capsys):
        """If one book fails, the command exits non-zero after processing all."""
        (tmp_path / "good.epub").write_bytes(b"PK")
        (tmp_path / "bad.epub").write_bytes(b"PK")

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if "bad" in args[0]:
                raise RuntimeError("bad book")
            return str(tmp_path / "out.paprikarecipes")

        import os
        os.environ["GOOGLE_API_KEY"] = "dummy-key"
        with patch("recipeparser.__main__.run_cli_pipeline", side_effect=side_effect), \
             patch("google.genai.Client"), \
             patch("sys.argv", ["recipeparser", "--folder", str(tmp_path),
                                "--output", str(tmp_path)]):
            from recipeparser.__main__ import main
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code != 0
        # Both books were attempted
        assert call_count == 2
