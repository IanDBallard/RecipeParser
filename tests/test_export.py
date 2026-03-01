"""Tests for recipeparser.export — Paprika archive bundling."""
import base64
import gzip
import json
import zipfile

import pytest

from tests.conftest import make_recipe
from recipeparser.models import RecipeExtraction
from recipeparser.export import create_paprika_export


class TestCreatePaprikaExport:

    def test_empty_recipe_list_returns_false(self, tmp_path):
        result = create_paprika_export([], str(tmp_path), str(tmp_path), "out.paprikarecipes")
        assert result is False
        assert not (tmp_path / "out.paprikarecipes").exists()

    def test_export_file_created(self, tmp_path):
        recipes = [make_recipe("Brownies")]
        result = create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        assert result is True
        assert (tmp_path / "out.paprikarecipes").exists()

    def test_archive_is_valid_zip(self, tmp_path):
        recipes = [make_recipe("Waffles")]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        assert zipfile.is_zipfile(tmp_path / "out.paprikarecipes")

    def test_archive_contains_one_entry_per_recipe(self, tmp_path):
        recipes = [make_recipe("Waffles"), make_recipe("Pancakes"), make_recipe("French Toast")]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            assert len(zf.namelist()) == 3

    def test_inner_file_is_valid_gzipped_json(self, tmp_path):
        recipes = [make_recipe("Quiche")]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["name"] == "Quiche"
        assert "ingredients" in data
        assert "directions" in data

    def test_required_paprika_keys_present(self, tmp_path):
        """Core keys are always present; photo/photo_data only appear when an image exists."""
        required_keys = {
            "uid", "name", "directions", "ingredients", "prep_time", "cook_time",
            "total_time", "servings", "notes", "source", "source_url", "categories",
            "rating", "created", "hash", "description", "nutritional_info",
            "difficulty", "image_url",
        }
        recipes = [make_recipe("Tart")]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert required_keys.issubset(data.keys())
        assert "photo" not in data
        assert "photo_data" not in data

    def test_uid_is_uppercase_uuid(self, tmp_path):
        import re
        UUID_RE = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")
        recipes = [make_recipe("Scones")]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert UUID_RE.match(data["uid"]), f"UID format unexpected: {data['uid']}"

    def test_photo_embedded_when_image_exists(self, tmp_path):
        img_dir = tmp_path / "images"
        img_dir.mkdir()
        fake_png = bytes([137, 80, 78, 71, 13, 10, 26, 10])
        (img_dir / "cake.jpg").write_bytes(fake_png)

        recipes = [make_recipe("Cake", photo="cake.jpg")]
        create_paprika_export(recipes, str(tmp_path), str(img_dir), "out.paprikarecipes")

        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))

        assert data["photo"] == "cake.jpg"
        assert data["photo_data"] == base64.b64encode(fake_png).decode("utf-8")

    def test_missing_image_does_not_crash(self, tmp_path):
        """Recipe whose image file is missing must still export cleanly, with no photo keys."""
        recipes = [make_recipe("Mystery Pie", photo="ghost.jpg")]
        result = create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        assert result is True
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert "photo" not in data
        assert "photo_data" not in data

    def test_recipe_name_with_special_chars_sanitised(self, tmp_path):
        """Special characters in recipe names must not produce invalid ZIP entry names."""
        recipes = [make_recipe("Crème Brûlée & Friends! <test>")]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            entry_name = zf.namelist()[0]
        for bad_char in ["<", ">", "&", "/"]:
            assert bad_char not in entry_name

    def test_untitled_fallback_for_empty_name(self, tmp_path):
        """A recipe whose name is all special characters gets the Untitled_Recipe fallback."""
        recipes = [make_recipe("!!!???###")]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            assert zf.namelist()[0] == "Untitled_Recipe.paprikarecipe"

    def test_multiple_recipes_have_unique_uids(self, tmp_path):
        recipes = [make_recipe(f"Recipe {i}") for i in range(5)]
        create_paprika_export(recipes, str(tmp_path), str(tmp_path), "out.paprikarecipes")
        uids = []
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            for name in zf.namelist():
                data = json.loads(gzip.decompress(zf.read(name)).decode("utf-8"))
                uids.append(data["uid"])
        assert len(set(uids)) == 5

    def test_categories_written_into_paprika_export(self, tmp_path):
        recipe = RecipeExtraction(
            name="Chicken Curry",
            ingredients=["flour", "water"],
            directions=["Mix and bake."],
            categories=["Chicken Dishes", "Indian"],
        )
        create_paprika_export([recipe], str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["categories"] == ["Chicken Dishes", "Indian"]

    def test_default_categories_fallback_in_export(self, tmp_path):
        recipe = RecipeExtraction(
            name="Plain Recipe",
            ingredients=["flour", "water"],
            directions=["Mix and bake."],
        )
        create_paprika_export([recipe], str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["categories"] == ["EPUB Imports"]

    def test_book_source_written_to_export(self, tmp_path):
        recipe = RecipeExtraction(
            name="Risotto",
            ingredients=["flour", "water"],
            directions=["Mix and bake."],
        )
        create_paprika_export(
            [recipe], str(tmp_path), str(tmp_path), "out.paprikarecipes",
            book_source="Italian Food — Elizabeth David",
        )
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["source"] == "Italian Food — Elizabeth David"

    def test_total_time_derived_from_prep_and_cook(self, tmp_path):
        recipe = RecipeExtraction(
            name="Quick Pasta",
            ingredients=["pasta", "sauce"],
            directions=["Boil pasta.", "Add sauce."],
            prep_time="10 mins",
            cook_time="20 mins",
        )
        create_paprika_export([recipe], str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["total_time"] == "30 mins"

    def test_ingredients_as_string_coerced_to_list(self, tmp_path):
        """If Gemini returns ingredients as a plain string, split on newlines."""
        recipe = RecipeExtraction(
            name="String Ingredients Test",
            ingredients="1 cup flour\n2 eggs\n1/2 tsp salt",
            directions=["Mix.", "Bake."],
        )
        assert recipe.ingredients == ["1 cup flour", "2 eggs", "1/2 tsp salt"]

    def test_directions_as_string_coerced_to_list(self, tmp_path):
        """If Gemini returns directions as a plain string, split on newlines."""
        recipe = RecipeExtraction(
            name="String Directions Test",
            ingredients=["1 cup flour"],
            directions="Mix flour with water.\nKnead for 10 minutes.\nBake at 200C.",
        )
        assert recipe.directions == [
            "Mix flour with water.",
            "Knead for 10 minutes.",
            "Bake at 200C.",
        ]

    def test_total_time_empty_when_times_missing(self, tmp_path):
        recipe = RecipeExtraction(
            name="Mystery Stew",
            ingredients=["flour", "water"],
            directions=["Mix and bake."],
        )
        create_paprika_export([recipe], str(tmp_path), str(tmp_path), "out.paprikarecipes")
        with zipfile.ZipFile(tmp_path / "out.paprikarecipes") as zf:
            raw = zf.read(zf.namelist()[0])
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
        assert data["total_time"] == ""
