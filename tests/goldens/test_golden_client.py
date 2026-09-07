"""Unit tests for the GoldenClient keying rules (spec §5.2)."""
from __future__ import annotations

import pytest

from recipeparser import gemini, toc
from recipeparser.models import RecipeExtraction
from tests.goldens import golden_client as gc
from tests.goldens.conftest import FIXED_AXES


def _sent_prompt(monkeypatch, call) -> object:
    """Run *call* against a client that captures contents and returns nothing useful."""
    seen = {}

    class _Models:
        def generate_content(self, *, model, contents, config):
            seen["contents"] = contents
            raise RuntimeError("stop here — we only wanted the prompt")

    class _Client:
        models = _Models()

    monkeypatch.setattr(gemini.time, "sleep", lambda *_: None)
    try:
        call(_Client())
    except Exception:
        pass
    return seen["contents"]


class TestSniffStage:
    def test_book_extract_prompt_sniffs_as_extract(self, monkeypatch):
        contents = _sent_prompt(
            monkeypatch, lambda c: gemini.extract_recipes("BAKER'S % table inside", c)
        )
        assert gc.sniff_stage(contents) == "extract"

    def test_plain_text_extract_prompt_sniffs_as_extract(self, monkeypatch):
        contents = _sent_prompt(
            monkeypatch, lambda c: gemini.extract_recipe_from_text("a recipe", c)
        )
        assert gc.sniff_stage(contents) == "extract"

    def test_refine_prompt_sniffs_as_refine(self, monkeypatch):
        raw = RecipeExtraction(name="X", ingredients=["1 cup flour"], directions=["Mix."])
        contents = _sent_prompt(
            monkeypatch,
            lambda c: gemini.refine_recipe_for_cayenne(raw, c, user_axes=FIXED_AXES),
        )
        assert gc.sniff_stage(contents) == "refine"

    def test_table_prompt_sniffs_as_table(self, monkeypatch):
        contents = _sent_prompt(
            monkeypatch, lambda c: gemini.normalise_baker_table("Flour 500g 100%", c)
        )
        assert gc.sniff_stage(contents) == "table"

    def test_toc_parse_prompt_sniffs_as_toc_parse(self, monkeypatch):
        contents = _sent_prompt(
            monkeypatch, lambda c: toc._parse_toc_from_text_fallback(["Contents", "Soups 3"], c)
        )
        assert gc.sniff_stage(contents) == "toc-parse"

    def test_toc_classify_prompt_sniffs_as_toc_classify(self, monkeypatch):
        contents = _sent_prompt(
            monkeypatch,
            lambda c: toc._classify_toc_recipe_indices([("Baker's % Bread", 3)], c),
        )
        assert gc.sniff_stage(contents) == "toc-classify"

    def test_connectivity_prompt_sniffs_as_connectivity(self):
        assert gc.sniff_stage("Reply with the single word OK.") == "connectivity"

    def test_vision_prompt_sniffs_as_vision(self):
        contents = [object(), "You are an OCR assistant. The image is a page from a recipe document."]
        assert gc.sniff_stage(contents) == "vision"

    def test_an_unrecognised_prompt_fails_loudly(self):
        with pytest.raises(gc.UnknownPromptError):
            gc.sniff_stage("Write me a poem about soup.")


class TestPromptBody:
    def test_extract_body_is_the_chunk_text_only(self, monkeypatch):
        contents = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c))
        assert gc.prompt_body(contents, "extract").strip() == "MY CHUNK"

    def test_the_units_mode_does_not_change_the_body(self, monkeypatch):
        book = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c, units="book"))
        metric = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c, units="metric"))
        assert gc.prompt_body(book, "extract") == gc.prompt_body(metric, "extract")

    def test_table_body_is_the_chunk_text_only(self, monkeypatch):
        contents = _sent_prompt(monkeypatch, lambda c: gemini.normalise_baker_table("MY TABLE", c))
        assert gc.prompt_body(contents, "table").strip() == "MY TABLE"

    def test_refine_body_is_the_raw_recipe_repr(self, monkeypatch):
        raw = RecipeExtraction(name="X", ingredients=["1 cup flour"], directions=["Mix."])
        contents = _sent_prompt(
            monkeypatch, lambda c: gemini.refine_recipe_for_cayenne(raw, c, user_axes=FIXED_AXES)
        )
        assert gc.prompt_body(contents, "refine").strip() == str(raw)

    def test_stages_without_a_marker_use_the_whole_prompt(self):
        assert gc.prompt_body("Reply with the single word OK.", "connectivity") == (
            "Reply with the single word OK."
        )


class TestKeys:
    def test_body_sha8_is_eight_stable_hex_digits(self):
        first = gc.body_sha8("some chunk")
        assert first == gc.body_sha8("some chunk")
        assert len(first) == 8 and all(ch in "0123456789abcdef" for ch in first)

    def test_different_bodies_key_differently(self):
        assert gc.body_sha8("chunk a") != gc.body_sha8("chunk b")

    def test_record_key_pairs_the_directory_with_a_zero_padded_filename(self):
        directory, filename = gc.record_key("extract", "chunk", 0)
        assert directory == gc.body_sha8("chunk")
        assert filename == "extract-00.json"

    def test_the_parse_retry_lands_on_the_next_ordinal(self):
        assert gc.record_key("extract", "chunk", 1)[1] == "extract-01.json"
