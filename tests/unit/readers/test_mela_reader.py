"""MelaReader (Mela import spec §5.1): entries, field mapping and text cleaning."""
from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path

import pytest

from recipeparser.core.citation import classify_source, web_citation
from recipeparser.core.models import InputType
from recipeparser.exceptions import MelaExtractionError
from recipeparser.io.readers.mela import MelaReader, clean_text
from tests.unit.readers.mela_builder import TINY_JPEG, TINY_PNG, entry, make_archive, make_single


def _read_one(tmp_path, **fields):
    chunks = MelaReader().read(make_archive(tmp_path, {"Pie.melarecipe": entry(**fields)}))
    assert len(chunks) == 1
    return chunks[0]


# ── Opening ──────────────────────────────────────────────────────────────────

def test_an_archive_yields_one_mela_chunk_per_recipe_in_archive_order(tmp_path):
    path = make_archive(tmp_path, {
        "B.melarecipe": entry(title="Bread"),
        "A.melarecipe": entry(title="Apple Pie"),
    })
    chunks = MelaReader().read(path)
    assert [c.label for c in chunks] == ["Bread", "Apple Pie"]
    assert {c.input_type for c in chunks} == {InputType.MELA}
    assert all(c.skip_reason is None and c.pre_parsed is None for c in chunks)


def test_members_that_are_not_recipes_are_ignored(tmp_path):
    """Review Focus 1: macOS zips carry __MACOSX/._* resource forks beside the recipes."""
    path = make_archive(tmp_path, {
        "Pie.melarecipe": entry(),
        "__MACOSX/._Pie.melarecipe": b"\x00\x05\x16\x07binary-resource-fork",
        "notes.txt": b"hello",
    })
    chunks = MelaReader().read(path)
    assert [c.label for c in chunks] == ["Pie"]


def test_a_recipe_in_a_folder_inside_the_archive_is_read(tmp_path):
    chunks = MelaReader().read(make_archive(tmp_path, {"Recipes/Pie.melarecipe": entry()}))
    assert [c.label for c in chunks] == ["Pie"]


def test_the_extension_is_matched_without_regard_to_case(tmp_path):
    path = make_archive(tmp_path, {"PIE.MELARECIPE": entry()}, name="LIBRARY.MELARECIPES")
    assert [c.label for c in MelaReader().read(path)] == ["Pie"]


def test_a_single_melarecipe_is_a_library_of_one(tmp_path):
    chunks = MelaReader().read(make_single(tmp_path, entry(title="Soup")))
    assert [c.label for c in chunks] == ["Soup"]


def test_a_byte_order_mark_does_not_stop_a_recipe_being_read(tmp_path):
    """Review Focus 2."""
    raw = b"\xef\xbb\xbf" + json.dumps(entry(title="Soup")).encode("utf-8")
    assert [c.label for c in MelaReader().read(make_single(tmp_path, raw))] == ["Soup"]


# ── Chunk text ───────────────────────────────────────────────────────────────

def test_the_text_is_title_facts_ingredients_directions(tmp_path):
    chunk = _read_one(tmp_path, title="Pie", ingredients="1 egg\n2 cups flour", instructions="Mix.\nBake.",
                      **{"yield": "Serves 4", "totalTime": "65min"})
    assert chunk.text == (
        "Pie\n\nServings: Serves 4\nTotal time: 65min\n\n"
        "Ingredients:\n1 egg\n2 cups flour\n\nDirections:\nMix.\nBake."
    )


def test_no_yield_and_no_total_time_means_no_fact_lines(tmp_path):
    chunk = _read_one(tmp_path, title="Pie", ingredients="1 egg", instructions="Bake.")
    assert chunk.text == "Pie\n\nIngredients:\n1 egg\n\nDirections:\nBake."


def test_a_missing_title_is_labelled_untitled_but_still_imported(tmp_path):
    chunk = _read_one(tmp_path, title="")
    assert chunk.label == "Untitled recipe"
    assert chunk.skip_reason is None


# ── Cleaning ─────────────────────────────────────────────────────────────────

def test_heading_lines_lose_their_hashes():
    cleaned = clean_text("# For the crust\n1 cup flour\n## Filling\n2 eggs")
    assert cleaned == "For the crust\n1 cup flour\nFilling\n2 eggs"


def test_a_hash_without_a_space_is_not_a_heading():
    assert clean_text("#1 grade eggs\nUse a 9\" tin #2") == "#1 grade eggs\nUse a 9\" tin #2"


def test_a_link_to_another_mela_recipe_keeps_only_its_label():
    assert clean_text("Serve with [Garlic Bread](mela://recipe/ABC-123).") == "Serve with Garlic Bread."


def test_a_web_link_keeps_its_url_in_brackets():
    assert clean_text("See [the video](https://example.com/v).") == "See the video (https://example.com/v)."


def test_a_link_url_with_parentheses_survives():
    assert clean_text("[Wiki](https://en.wikipedia.org/wiki/Pie_(food))") == "Wiki (https://en.wikipedia.org/wiki/Pie_(food))"


def test_anything_that_is_not_a_string_cleans_to_empty():
    assert clean_text(None) == "" and clean_text(42) == "" and clean_text(["a"]) == ""


def test_ingredients_and_directions_are_cleaned_in_the_chunk(tmp_path):
    chunk = _read_one(tmp_path, ingredients="# Crust\n1 cup flour", instructions="Make [Crust](mela://x).")
    assert "Ingredients:\nCrust\n1 cup flour" in chunk.text
    assert "Directions:\nMake Crust." in chunk.text


# ── Meta and source ──────────────────────────────────────────────────────────

def test_times_description_notes_and_nutrition_go_to_source_meta(tmp_path):
    chunk = _read_one(tmp_path, prepTime="15 min", cookTime="1 hr", text="A [rich](mela://x) pie.",
                      notes="# Tip\nChill it.", nutrition="520 kcal")
    m = chunk.meta
    assert (m.prep_time, m.cook_time) == ("15 min", "1 hr")
    assert m.description == "A rich pie."
    assert m.notes == "Tip\nChill it."
    assert m.nutritional_info == "520 kcal"


def test_empty_meta_fields_are_absent(tmp_path):
    chunk = _read_one(tmp_path, notes="", prepTime="  ")
    assert chunk.meta.notes is None and chunk.meta.prep_time is None


def test_a_web_link_is_the_source_url_and_cited_as_the_page(tmp_path):
    chunk = _read_one(tmp_path, link="https://www.bonappetit.com/recipe/banana-bread")
    assert chunk.source_url == "https://www.bonappetit.com/recipe/banana-bread"
    assert chunk.meta.source is None
    assert chunk.citation == web_citation("https://www.bonappetit.com/recipe/banana-bread")


def test_a_plain_text_link_is_the_stated_source(tmp_path):
    chunk = _read_one(tmp_path, link="Salt Fat Acid Heat, p. 112")
    assert chunk.source_url is None
    assert chunk.meta.source == "Salt Fat Acid Heat, p. 112"
    assert chunk.citation == classify_source("Salt Fat Acid Heat, p. 112").citation


def test_a_mela_scheme_link_is_not_a_source_url(tmp_path):
    """Review Focus 4."""
    chunk = _read_one(tmp_path, link="mela://recipe/ABC")
    assert chunk.source_url is None


def test_no_link_is_the_uncited_citation(tmp_path):
    chunk = _read_one(tmp_path)
    assert chunk.citation == classify_source(None).citation


# ── Picture ──────────────────────────────────────────────────────────────────

def test_the_first_image_is_the_chunks_picture(tmp_path):
    chunk = _read_one(tmp_path, images=[base64.b64encode(TINY_JPEG).decode(), base64.b64encode(TINY_PNG).decode()])
    assert chunk.image_bytes == TINY_JPEG
    assert chunk.image_content_type == "image/jpeg"


def test_a_png_picture_is_typed_by_its_bytes(tmp_path):
    chunk = _read_one(tmp_path, images=[base64.b64encode(TINY_PNG).decode()])
    assert chunk.image_content_type == "image/png"


def test_a_wrapped_base64_picture_still_decodes(tmp_path):
    """Review Focus 3."""
    b64 = base64.b64encode(TINY_JPEG).decode()
    wrapped = "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))
    assert _read_one(tmp_path, images=[wrapped]).image_bytes == TINY_JPEG


def test_a_picture_that_will_not_decode_is_dropped_and_the_recipe_kept(tmp_path):
    chunk = _read_one(tmp_path, images=["not base64 !!!"])
    assert chunk.image_bytes is None
    assert chunk.skip_reason is None


def test_no_images_means_no_picture(tmp_path):
    assert _read_one(tmp_path, images=[]).image_bytes is None
    assert _read_one(tmp_path, images="oops").image_bytes is None


# ── Favourite and dropped fields ─────────────────────────────────────────────

def test_a_favourite_rides_restore_fields(tmp_path):
    chunk = _read_one(tmp_path, favorite=True)
    assert chunk.restore is not None and chunk.restore.is_favourite is True


def test_not_a_favourite_carries_no_restore_fields(tmp_path):
    assert _read_one(tmp_path, favorite=False).restore is None
    assert _read_one(tmp_path).restore is None


def test_a_favorite_that_is_not_a_real_boolean_is_not_a_favourite(tmp_path):
    """Review Focus 4: only JSON true counts."""
    assert _read_one(tmp_path, favorite="true").restore is None
    assert _read_one(tmp_path, favorite=1).restore is None


def test_categories_and_want_to_cook_never_reach_the_chunk(tmp_path):
    chunk = _read_one(tmp_path, categories=["Bread", "BA's Best"], wantToCook=True)
    assert "Bread" not in chunk.text and "BA's Best" not in chunk.text


def test_categories_of_the_wrong_type_do_not_crash(tmp_path):
    assert _read_one(tmp_path, categories="Bread").skip_reason is None


# ── Damaged archives ─────────────────────────────────────────────────────────

def _two_member_zip(tmp_path):
    """A zip of 'Good' and 'Bad' recipes (deflated); returns the path."""
    path = tmp_path / "Library.melarecipes"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Good.melarecipe", json.dumps(entry(title="Good", ingredients="flour " * 200)))
        zf.writestr("Bad.melarecipe", json.dumps(entry(title="Bad", ingredients="sugar " * 200)))
    return path


def _assert_bad_member_skipped(path):
    chunks = MelaReader().read(str(path))
    assert [c.label for c in chunks] == ["Good", "Bad"]
    assert chunks[0].skip_reason is None
    assert chunks[1].skip_reason == "Bad: could not be read"


def test_a_member_with_corrupt_deflate_data_is_skipped_and_the_library_kept(tmp_path):
    path = _two_member_zip(tmp_path)
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo("Bad.melarecipe")
        start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra) + 2
    raw = bytearray(path.read_bytes())
    raw[start:start + 12] = b"\xff" * 12
    path.write_bytes(bytes(raw))
    _assert_bad_member_skipped(path)


def test_an_encrypted_member_is_skipped_and_the_library_kept(tmp_path):
    path = _two_member_zip(tmp_path)
    with zipfile.ZipFile(path) as zf:
        offset = zf.getinfo("Bad.melarecipe").header_offset
    raw = bytearray(path.read_bytes())
    raw[offset + 6] |= 0x1  # the encryption bit of the local header's flags
    central = raw.find(b"PK\x01\x02", raw.find(b"PK\x01\x02") + 1)  # the second (Bad) central entry
    raw[central + 8] |= 0x1  # and of the central directory's
    path.write_bytes(bytes(raw))
    _assert_bad_member_skipped(path)


# ── Link rule: only mela:// and web links are touched ────────────────────────

def test_a_bracket_pair_that_is_not_a_mela_or_web_link_stays_verbatim():
    assert clean_text("Add [optional](see note) salt") == "Add [optional](see note) salt"
    assert clean_text("Mix [1/2](tsp)") == "Mix [1/2](tsp)"
    assert clean_text("[a](ftp://x)") == "[a](ftp://x)"


def test_the_description_gets_link_cleaning_but_no_heading_rule(tmp_path):
    chunk = _read_one(tmp_path, text="# Not a heading\nA [rich](mela://x) pie.")
    assert chunk.meta.description == "# Not a heading\nA rich pie."


# ── A damaged central directory ──────────────────────────────────────────────

def test_a_central_directory_that_will_not_open_is_not_a_mela_export(tmp_path):
    path = make_archive(tmp_path, {"A.melarecipe": entry(title="A"), "B.melarecipe": entry(title="B")})
    raw = bytearray(Path(path).read_bytes())
    second = raw.find(b"PK\x01\x02", raw.find(b"PK\x01\x02") + 1)
    raw[second:second + 4] = b"\x00\x00\x00\x00"  # EOCD intact, so is_zipfile still accepts it
    Path(path).write_bytes(bytes(raw))
    assert zipfile.is_zipfile(path)
    with pytest.raises(MelaExtractionError, match="not a ZIP archive"):
        MelaReader().read(path)


# ── Final review: notes-only entries, numeric facts, picture budget ──────────

def test_a_notes_only_entry_sends_its_notes_as_the_directions(tmp_path):
    notes = "2 lb beef\n1 onion\nBrown it."
    chunk = _read_one(tmp_path, title="Gran's Stew", ingredients="", instructions="", notes=notes)
    assert chunk.skip_reason is None
    assert chunk.text.endswith("Directions:\n2 lb beef\n1 onion\nBrown it.")
    assert chunk.meta.notes == "2 lb beef\n1 onion\nBrown it."


def test_a_title_only_entry_is_skipped_with_a_line(tmp_path):
    path = make_archive(tmp_path, {
        "Good.melarecipe": entry(title="Good"),
        "Bare.melarecipe": {"title": "Bare", "link": "https://example.com/bare"},
    })
    chunks = MelaReader().read(path)
    assert chunks[0].skip_reason is None
    assert chunks[1].skip_reason == "Bare: has no recipe text"


def test_a_numeric_yield_and_times_are_kept(tmp_path):
    chunk = _read_one(tmp_path, **{"yield": 8, "totalTime": 12.5, "prepTime": 10, "cookTime": True})
    assert "Servings: 8" in chunk.text
    assert "Total time: 12.5" in chunk.text
    assert chunk.meta.prep_time == "10"
    assert not chunk.meta.cook_time


def test_an_oversized_picture_is_dropped_and_the_recipe_kept(tmp_path, monkeypatch):
    monkeypatch.setattr("recipeparser.io.readers.mela._MAX_PICTURE_BYTES", len(TINY_JPEG) - 1)
    chunk = _read_one(tmp_path, images=[base64.b64encode(TINY_JPEG).decode()])
    assert chunk.image_bytes is None
    assert chunk.skip_reason is None


def test_pictures_past_the_read_budget_are_dropped_and_the_recipes_kept(tmp_path, monkeypatch):
    monkeypatch.setattr("recipeparser.io.readers.mela._MAX_KEPT_PICTURE_BYTES", len(TINY_JPEG) + 1)
    pic = [base64.b64encode(TINY_JPEG).decode()]
    path = make_archive(tmp_path, {
        "A.melarecipe": entry(title="A", images=pic),
        "B.melarecipe": entry(title="B", images=pic),
        "C.melarecipe": entry(title="C", images=pic),
    })
    chunks = MelaReader().read(path)
    assert [c.skip_reason for c in chunks] == [None, None, None]
    assert [c.image_bytes for c in chunks] == [TINY_JPEG, None, None]
