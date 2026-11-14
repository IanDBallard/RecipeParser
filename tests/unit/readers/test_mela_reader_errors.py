"""MelaReader refusals and skipped entries (Mela import spec §6)."""
from __future__ import annotations

import pytest

import recipeparser.io.readers.mela as mela
from recipeparser.exceptions import MelaExtractionError, UnreadableInputError
from recipeparser.io.readers.mela import MelaReader
from tests.unit.readers.mela_builder import entry, make_archive, make_single


def test_an_archive_that_is_not_a_zip_is_refused_before_any_model_call(tmp_path):
    path = tmp_path / "Recipes.melarecipes"
    path.write_bytes(b"this is not a zip")
    with pytest.raises(MelaExtractionError, match=r"^is not a Mela export: not a ZIP archive\.$"):
        MelaReader().read(str(path))


def test_the_refusal_is_an_unreadable_input_error_so_the_api_names_the_file():
    assert issubclass(MelaExtractionError, UnreadableInputError)


def test_an_archive_with_no_recipe_members_is_refused(tmp_path):
    path = make_archive(tmp_path, {"readme.txt": b"hi"})
    with pytest.raises(MelaExtractionError, match=r"^contains no recipes\.$"):
        MelaReader().read(path)


def test_an_archive_whose_every_entry_is_skipped_is_refused(tmp_path):
    path = make_archive(
        tmp_path,
        {"A.melarecipe": b"{broken", "B.melarecipe": entry(title="", ingredients="", instructions="")},
    )
    with pytest.raises(MelaExtractionError, match=r"^contains no recipes\.$"):
        MelaReader().read(path)


def test_a_broken_entry_is_named_and_the_rest_imported(tmp_path):
    path = make_archive(tmp_path, {"Good.melarecipe": entry(title="Good"), "Old Soup.melarecipe": b"{not json"})
    chunks = MelaReader().read(path)
    assert [c.label for c in chunks if c.skip_reason is None] == ["Good"]
    assert [c.skip_reason for c in chunks if c.skip_reason] == ["Old Soup: could not be read"]


def test_an_entry_that_is_json_but_not_an_object_is_named(tmp_path):
    path = make_archive(tmp_path, {"Good.melarecipe": entry(), "List.melarecipe": b"[1, 2]"})
    assert [c.skip_reason for c in MelaReader().read(path) if c.skip_reason] == ["List: could not be read"]


def test_an_entry_over_the_cap_is_named_without_being_read_whole(tmp_path, monkeypatch):
    monkeypatch.setattr(mela, "_MAX_ENTRY_BYTES", 200)
    path = make_archive(tmp_path, {"Good.melarecipe": entry(), "Huge.melarecipe": entry(notes="x" * 500)})
    assert [c.skip_reason for c in MelaReader().read(path) if c.skip_reason] == ["Huge: larger than 50 MB"]


def test_an_entry_with_no_recipe_text_is_named(tmp_path):
    path = make_archive(
        tmp_path,
        {"Good.melarecipe": entry(), "Blank.melarecipe": entry(title="", ingredients="", instructions="")},
    )
    assert [c.skip_reason for c in MelaReader().read(path) if c.skip_reason] == ["Blank: has no recipe text"]


def test_a_skipped_chunk_runs_no_stage(tmp_path):
    from recipeparser.core.pipeline import chunk_stages
    path = make_archive(tmp_path, {"Good.melarecipe": entry(), "Bad.melarecipe": b"{"})
    skipped = [c for c in MelaReader().read(path) if c.skip_reason]
    assert chunk_stages(skipped[0]) == []


def test_a_single_recipe_that_is_not_json_is_refused(tmp_path):
    with pytest.raises(MelaExtractionError, match=r"^is not a Mela recipe: not readable JSON\.$"):
        MelaReader().read(make_single(tmp_path, b"\x00\x01binary"))


def test_a_single_recipe_over_the_cap_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(mela, "_MAX_ENTRY_BYTES", 200)
    with pytest.raises(MelaExtractionError, match=r"^is not a Mela recipe: larger than 50 MB\.$"):
        MelaReader().read(make_single(tmp_path, entry(notes="x" * 500)))


def test_a_single_recipe_with_no_text_is_refused_as_no_recipes(tmp_path):
    with pytest.raises(MelaExtractionError, match=r"^contains no recipes\.$"):
        MelaReader().read(make_single(tmp_path, entry(title="", ingredients="", instructions="")))


def test_no_refusal_names_the_temp_path(tmp_path):
    path = tmp_path / "secret-temp-name.melarecipes"
    path.write_bytes(b"nope")
    with pytest.raises(MelaExtractionError) as excinfo:
        MelaReader().read(str(path))
    assert "secret-temp-name" not in str(excinfo.value)
