"""Tests for recipeparser.recategorize — Phase 3d."""
import gzip
import json
import zipfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from recipeparser.recategorize import recategorize, _split_lines
from recipeparser.exceptions import RecategorizationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_paprika_archive(path: Path, recipes: list[dict]) -> Path:
    """
    Write a minimal .paprikarecipes archive (ZIP of gzip-compressed JSON entries).
    Each dict in ``recipes`` becomes one member named ``<name>.paprikarecipe``.
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for recipe in recipes:
            name = recipe.get("name", "recipe")
            member = f"{name}.paprikarecipe"
            gz = gzip.compress(json.dumps(recipe, ensure_ascii=False).encode("utf-8"))
            zf.writestr(member, gz)
    return path


def _make_client(new_categories: list[str] | None = None):
    """Return a mock client whose categorise_recipe call returns new_categories."""
    client = MagicMock()
    # categorise_recipe is called inside recategorize; we patch it at the module level
    return client


# ---------------------------------------------------------------------------
# _split_lines helper
# ---------------------------------------------------------------------------

class TestSplitLines:

    def test_string_splits_on_newlines(self):
        result = _split_lines("line1\nline2\nline3")
        assert result == ["line1", "line2", "line3"]

    def test_list_returned_as_is(self):
        result = _split_lines(["a", "b", "c"])
        assert result == ["a", "b", "c"]

    def test_empty_string_returns_empty_list(self):
        assert _split_lines("") == []

    def test_blank_lines_filtered_out(self):
        result = _split_lines("line1\n\n  \nline2")
        assert result == ["line1", "line2"]

    def test_none_returns_empty_list(self):
        assert _split_lines(None) == []

    def test_integer_returns_empty_list(self):
        assert _split_lines(42) == []


# ---------------------------------------------------------------------------
# recategorize() — error cases
# ---------------------------------------------------------------------------

class TestRecategorizeErrors:

    def test_raises_when_file_not_found(self, tmp_path):
        client = MagicMock()
        with pytest.raises(RecategorizationError, match="not found"):
            recategorize(tmp_path / "ghost.paprikarecipes", client)

    def test_raises_when_not_a_zip(self, tmp_path):
        bad = tmp_path / "bad.paprikarecipes"
        bad.write_bytes(b"NOT A ZIP FILE")
        client = MagicMock()
        with pytest.raises(RecategorizationError, match="valid"):
            recategorize(bad, client)

    def test_raises_when_archive_is_empty(self, tmp_path):
        empty = tmp_path / "empty.paprikarecipes"
        with zipfile.ZipFile(empty, "w") as _:
            pass  # empty archive
        client = MagicMock()
        with pytest.raises(RecategorizationError, match="no entries"):
            recategorize(empty, client)

    def test_raises_when_all_entries_unparseable(self, tmp_path):
        bad = tmp_path / "bad.paprikarecipes"
        with zipfile.ZipFile(bad, "w") as zf:
            # Not gzip-compressed JSON — just raw bytes
            zf.writestr("recipe.paprikarecipe", b"NOT GZIP DATA")
        client = MagicMock()
        with pytest.raises(RecategorizationError, match="No parseable"):
            recategorize(bad, client)


# ---------------------------------------------------------------------------
# recategorize() — happy path
# ---------------------------------------------------------------------------

class TestRecategorizeHappyPath:

    def test_output_file_created_with_recategorized_suffix(self, tmp_path):
        archive = _make_paprika_archive(
            tmp_path / "cookbook.paprikarecipes",
            [{"name": "Pasta", "ingredients": "flour\nwater", "directions": "mix"}],
        )
        client = MagicMock()
        with patch("recipeparser.recategorize.categorise_recipe", return_value=["Italian"]):
            out = recategorize(archive, client)
        assert out.name == "cookbook_recategorized.paprikarecipes"
        assert out.exists()

    def test_output_written_to_custom_output_dir(self, tmp_path):
        archive = _make_paprika_archive(
            tmp_path / "cookbook.paprikarecipes",
            [{"name": "Pasta", "ingredients": "flour", "directions": "mix"}],
        )
        out_dir = tmp_path / "exports"
        client = MagicMock()
        with patch("recipeparser.recategorize.categorise_recipe", return_value=["Italian"]):
            out = recategorize(archive, client, output_dir=out_dir)
        assert out.parent == out_dir
        assert out_dir.is_dir()

    def test_output_is_valid_zip(self, tmp_path):
        archive = _make_paprika_archive(
            tmp_path / "cookbook.paprikarecipes",
            [{"name": "Pasta", "ingredients": "flour", "directions": "mix"}],
        )
        client = MagicMock()
        with patch("recipeparser.recategorize.categorise_recipe", return_value=["Italian"]):
            out = recategorize(archive, client)
        assert zipfile.is_zipfile(out)

    def test_categories_updated_in_output(self, tmp_path):
        archive = _make_paprika_archive(
            tmp_path / "cookbook.paprikarecipes",
            [{"name": "Pasta", "categories": ["Old Category"],
              "ingredients": "flour", "directions": "mix"}],
        )
        client = MagicMock()
        with patch("recipeparser.recategorize.categorise_recipe", return_value=["Italian"]):
            out = recategorize(archive, client)

        # Read back and verify
        with zipfile.ZipFile(out, "r") as zf:
            members = zf.namelist()
            assert len(members) == 1
            raw = zf.read(members[0])
            data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["categories"] == ["Italian"]

    def test_all_recipes_processed(self, tmp_path):
        archive = _make_paprika_archive(
            tmp_path / "cookbook.paprikarecipes",
            [
                {"name": "Pasta", "ingredients": "flour", "directions": "mix"},
                {"name": "Salad", "ingredients": "lettuce", "directions": "toss"},
                {"name": "Soup",  "ingredients": "broth",  "directions": "simmer"},
            ],
        )
        client = MagicMock()
        with patch("recipeparser.recategorize.categorise_recipe", return_value=["Misc"]):
            out = recategorize(archive, client)

        with zipfile.ZipFile(out, "r") as zf:
            assert len(zf.namelist()) == 3

    def test_other_fields_preserved(self, tmp_path):
        """Non-category fields (notes, source_url, etc.) must survive round-trip."""
        archive = _make_paprika_archive(
            tmp_path / "cookbook.paprikarecipes",
            [{"name": "Pasta", "ingredients": "flour", "directions": "mix",
              "notes": "Grandma's recipe", "source_url": "http://example.com"}],
        )
        client = MagicMock()
        with patch("recipeparser.recategorize.categorise_recipe", return_value=["Italian"]):
            out = recategorize(archive, client)

        with zipfile.ZipFile(out, "r") as zf:
            raw = zf.read(zf.namelist()[0])
            data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["notes"] == "Grandma's recipe"
        assert data["source_url"] == "http://example.com"

    def test_unparseable_entry_skipped_gracefully(self, tmp_path):
        """A corrupt entry is skipped; valid entries are still processed."""
        good = {"name": "Pasta", "ingredients": "flour", "directions": "mix"}
        archive = tmp_path / "cookbook.paprikarecipes"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            # Good entry
            zf.writestr(
                "Pasta.paprikarecipe",
                gzip.compress(json.dumps(good).encode("utf-8")),
            )
            # Bad entry (not gzip)
            zf.writestr("Bad.paprikarecipe", b"NOT GZIP")

        client = MagicMock()
        with patch("recipeparser.recategorize.categorise_recipe", return_value=["Italian"]):
            out = recategorize(archive, client)

        with zipfile.ZipFile(out, "r") as zf:
            # Only the good entry should appear in the output
            assert len(zf.namelist()) == 1

    def test_categorisation_failure_keeps_original_categories(self, tmp_path):
        """When categorise_recipe raises, the original categories are preserved."""
        archive = _make_paprika_archive(
            tmp_path / "cookbook.paprikarecipes",
            [{"name": "Pasta", "categories": ["Original"],
              "ingredients": "flour", "directions": "mix"}],
        )
        client = MagicMock()
        with patch(
            "recipeparser.recategorize.categorise_recipe",
            side_effect=RuntimeError("API down"),
        ):
            out = recategorize(archive, client)

        with zipfile.ZipFile(out, "r") as zf:
            raw = zf.read(zf.namelist()[0])
            data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["categories"] == ["Original"]

    def test_default_output_dir_is_same_as_source(self, tmp_path):
        archive = _make_paprika_archive(
            tmp_path / "cookbook.paprikarecipes",
            [{"name": "Pasta", "ingredients": "flour", "directions": "mix"}],
        )
        client = MagicMock()
        with patch("recipeparser.recategorize.categorise_recipe", return_value=["Italian"]):
            out = recategorize(archive, client)
        assert out.parent == tmp_path

    def test_output_dir_created_if_missing(self, tmp_path):
        archive = _make_paprika_archive(
            tmp_path / "cookbook.paprikarecipes",
            [{"name": "Pasta", "ingredients": "flour", "directions": "mix"}],
        )
        new_dir = tmp_path / "new" / "nested" / "dir"
        client = MagicMock()
        with patch("recipeparser.recategorize.categorise_recipe", return_value=["Italian"]):
            recategorize(archive, client, output_dir=new_dir)
        assert new_dir.is_dir()
