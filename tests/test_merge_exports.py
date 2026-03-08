"""Tests for recipeparser.export.merge_exports — Phase 3a multi-file merge."""
import gzip
import json
import zipfile
from pathlib import Path

import pytest

from recipeparser.export import merge_exports, _normalise_recipe_name
from recipeparser.exceptions import ExportError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_archive(path: Path, recipes: list[dict]) -> Path:
    """Write a minimal .paprikarecipes archive containing the given recipe dicts."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for recipe in recipes:
            name = recipe.get("name", "Untitled")
            safe = "".join(c for c in name if c.isalnum() or c in " -_").strip() or "Untitled"
            gz = gzip.compress(json.dumps(recipe, ensure_ascii=False).encode("utf-8"))
            zf.writestr(f"{safe}.paprikarecipe", gz)
    return path


def _read_names(archive: Path) -> list[str]:
    """Return all recipe names from a .paprikarecipes archive."""
    names = []
    with zipfile.ZipFile(archive, "r") as zf:
        for member in zf.namelist():
            data = json.loads(gzip.decompress(zf.read(member)).decode("utf-8"))
            names.append(data["name"])
    return names


# ---------------------------------------------------------------------------
# _normalise_recipe_name
# ---------------------------------------------------------------------------

class TestNormaliseRecipeName:

    def test_lowercase(self):
        assert _normalise_recipe_name("Chocolate Cake") == "chocolate cake"

    def test_strips_accents(self):
        assert _normalise_recipe_name("Crème Brûlée") == "creme brulee"

    def test_removes_punctuation(self):
        assert _normalise_recipe_name("Mac & Cheese!") == "mac cheese"

    def test_collapses_whitespace(self):
        assert _normalise_recipe_name("  Banana   Bread  ") == "banana bread"

    def test_empty_string(self):
        assert _normalise_recipe_name("") == ""

    def test_identical_after_normalise(self):
        assert _normalise_recipe_name("pasta") == _normalise_recipe_name("PASTA")


# ---------------------------------------------------------------------------
# merge_exports — basic behaviour
# ---------------------------------------------------------------------------

class TestMergeExports:

    def test_raises_on_empty_paths(self, tmp_path):
        with pytest.raises(ValueError, match="at least one"):
            merge_exports([], tmp_path)

    def test_single_archive_round_trips(self, tmp_path):
        src = _make_archive(
            tmp_path / "a.paprikarecipes",
            [{"name": "Pasta", "ingredients": "", "directions": ""}],
        )
        out = merge_exports([src], tmp_path)
        assert out.exists()
        assert out.suffix == ".paprikarecipes"
        assert "merged_" in out.name
        names = _read_names(out)
        assert names == ["Pasta"]

    def test_two_archives_merged(self, tmp_path):
        a = _make_archive(
            tmp_path / "a.paprikarecipes",
            [{"name": "Pasta"}, {"name": "Salad"}],
        )
        b = _make_archive(
            tmp_path / "b.paprikarecipes",
            [{"name": "Soup"}, {"name": "Bread"}],
        )
        out = merge_exports([a, b], tmp_path)
        names = _read_names(out)
        assert set(names) == {"Pasta", "Salad", "Soup", "Bread"}
        assert len(names) == 4

    def test_exact_duplicate_removed(self, tmp_path):
        a = _make_archive(tmp_path / "a.paprikarecipes", [{"name": "Pasta"}])
        b = _make_archive(tmp_path / "b.paprikarecipes", [{"name": "Pasta"}])
        out = merge_exports([a, b], tmp_path)
        names = _read_names(out)
        assert names.count("Pasta") == 1

    def test_case_insensitive_dedup(self, tmp_path):
        a = _make_archive(tmp_path / "a.paprikarecipes", [{"name": "Chocolate Cake"}])
        b = _make_archive(tmp_path / "b.paprikarecipes", [{"name": "chocolate cake"}])
        out = merge_exports([a, b], tmp_path)
        names = _read_names(out)
        assert len(names) == 1

    def test_accent_insensitive_dedup(self, tmp_path):
        a = _make_archive(tmp_path / "a.paprikarecipes", [{"name": "Crème Brûlée"}])
        b = _make_archive(tmp_path / "b.paprikarecipes", [{"name": "Creme Brulee"}])
        out = merge_exports([a, b], tmp_path)
        names = _read_names(out)
        assert len(names) == 1

    def test_first_occurrence_wins(self, tmp_path):
        """When a duplicate is found, the entry from the first archive is kept."""
        a = _make_archive(
            tmp_path / "a.paprikarecipes",
            [{"name": "Pasta", "source": "Book A"}],
        )
        b = _make_archive(
            tmp_path / "b.paprikarecipes",
            [{"name": "Pasta", "source": "Book B"}],
        )
        out = merge_exports([a, b], tmp_path)
        with zipfile.ZipFile(out, "r") as zf:
            data = json.loads(gzip.decompress(zf.read(zf.namelist()[0])).decode("utf-8"))
        assert data.get("source") == "Book A"

    def test_output_is_valid_zip(self, tmp_path):
        a = _make_archive(tmp_path / "a.paprikarecipes", [{"name": "Soup"}])
        out = merge_exports([a], tmp_path)
        assert zipfile.is_zipfile(out)

    def test_output_dir_created_if_missing(self, tmp_path):
        a = _make_archive(tmp_path / "a.paprikarecipes", [{"name": "Soup"}])
        new_dir = tmp_path / "subdir" / "output"
        out = merge_exports([a], new_dir)
        assert out.exists()

    def test_bad_zip_skipped_gracefully(self, tmp_path):
        """A corrupt archive is skipped; valid archives still contribute."""
        bad = tmp_path / "bad.paprikarecipes"
        bad.write_bytes(b"not a zip file")
        good = _make_archive(tmp_path / "good.paprikarecipes", [{"name": "Soup"}])
        out = merge_exports([bad, good], tmp_path)
        names = _read_names(out)
        assert "Soup" in names

    def test_all_bad_zips_raises_export_error(self, tmp_path):
        bad = tmp_path / "bad.paprikarecipes"
        bad.write_bytes(b"not a zip file")
        with pytest.raises(ExportError):
            merge_exports([bad], tmp_path)

    def test_output_filename_has_timestamp(self, tmp_path):
        a = _make_archive(tmp_path / "a.paprikarecipes", [{"name": "Soup"}])
        out = merge_exports([a], tmp_path)
        # filename: merged_YYYYMMDD_HHMMSS.paprikarecipes
        import re
        assert re.match(r"merged_\d{8}_\d{6}\.paprikarecipes", out.name)

    def test_three_archives_all_unique(self, tmp_path):
        archives = []
        for i, name in enumerate(["Apple Pie", "Beef Stew", "Carrot Cake"]):
            p = _make_archive(tmp_path / f"{i}.paprikarecipes", [{"name": name}])
            archives.append(p)
        out = merge_exports(archives, tmp_path)
        names = _read_names(out)
        assert set(names) == {"Apple Pie", "Beef Stew", "Carrot Cake"}
