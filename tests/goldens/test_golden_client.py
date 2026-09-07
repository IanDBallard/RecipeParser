"""Unit tests for the GoldenClient keying rules (spec §5.2)."""
from __future__ import annotations

import hashlib
import json
import warnings

import pytest

from recipeparser import gemini, toc
from recipeparser.models import RecipeExtraction
from tests.goldens import golden_client as gc
from tests.goldens.conftest import FIXED_AXES
from tests.goldens.golden_client import GoldenClient, MissingRecordingError


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

    def test_connectivity_prompt_sniffs_as_connectivity(self, monkeypatch):
        contents = _sent_prompt(monkeypatch, lambda c: gemini.verify_connectivity(c))
        assert gc.sniff_stage(contents) == "connectivity"

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


def _prompt_sha256(prompt) -> str:
    """The full prompt_sha256 GoldenClient._generate compares against on replay.

    Matches ``hashlib.sha256(text_part(contents)...)`` in golden_client.py exactly
    (the whole prompt, not the body substring ``body_sha8`` hashes), so recordings
    written with this value replay without triggering the drift warning.
    """
    return hashlib.sha256(gc.text_part(prompt).encode("utf-8")).hexdigest()


def _write_recording(root, fixture, stage, body, ordinal, response_text, sha=None):
    directory, filename = gc.record_key(stage, body, ordinal)
    target = root / fixture / directory
    target.mkdir(parents=True, exist_ok=True)
    target.joinpath(filename).write_text(
        json.dumps(
            {
                "stage": stage,
                "ordinal": ordinal,
                "model": "gemini-2.5-flash",
                "config": {"temperature": 0.1},
                "prompt_sha256": sha or "0" * 64,
                "response_text": response_text,
            }
        ),
        encoding="utf-8",
    )


class TestReplay:
    def test_it_serves_the_recorded_reply(self, tmp_path, monkeypatch):
        prompt = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c))
        body = gc.prompt_body(prompt, "extract")
        sha = _prompt_sha256(prompt)
        _write_recording(tmp_path, "f.epub", "extract", body, 0, '{"recipes": []}', sha=sha)

        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        response = client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt, config={}
        )
        assert response.text == '{"recipes": []}'

    def test_a_second_call_for_one_body_serves_the_next_ordinal(self, tmp_path, monkeypatch):
        prompt = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c))
        body = gc.prompt_body(prompt, "extract")
        sha = _prompt_sha256(prompt)
        _write_recording(tmp_path, "f.epub", "extract", body, 0, "truncated {", sha=sha)
        _write_recording(tmp_path, "f.epub", "extract", body, 1, '{"recipes": []}', sha=sha)

        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        first = client.models.generate_content(model="m", contents=prompt, config={})
        second = client.models.generate_content(model="m", contents=prompt, config={})
        assert first.text == "truncated {"
        assert second.text == '{"recipes": []}'

    def test_a_missing_recording_names_the_path_it_wanted(self, tmp_path, monkeypatch):
        prompt = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c))
        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        with pytest.raises(MissingRecordingError) as excinfo:
            client.models.generate_content(model="m", contents=prompt, config={})
        assert "extract-00.json" in str(excinfo.value)

    def test_a_prompt_hash_mismatch_warns_but_still_serves(self, tmp_path, monkeypatch):
        prompt = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c))
        body = gc.prompt_body(prompt, "extract")
        _write_recording(tmp_path, "f.epub", "extract", body, 0, "ok", sha="f" * 64)

        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            response = client.models.generate_content(model="m", contents=prompt, config={})
        assert response.text == "ok"
        assert any("prompt_sha256" in str(w.message) for w in caught)

    def test_the_parse_retry_replays_end_to_end(self, tmp_path, monkeypatch):
        prompt = _sent_prompt(monkeypatch, lambda c: gemini.extract_recipes("MY CHUNK", c))
        body = gc.prompt_body(prompt, "extract")
        sha = _prompt_sha256(prompt)
        _write_recording(tmp_path, "f.epub", "extract", body, 0, "{ truncated", sha=sha)
        _write_recording(
            tmp_path, "f.epub", "extract", body, 1,
            '{"recipes": [{"name": "Scones", "ingredients": ["1 cup flour"], "directions": ["Bake."]}]}',
            sha=sha,
        )
        monkeypatch.setattr(gemini.time, "sleep", lambda *_: None)

        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        result = gemini.extract_recipes("MY CHUNK", client)
        assert [r.name for r in result.recipes] == ["Scones"]


class TestEmbeddings:
    def test_it_returns_a_deterministic_1536_vector(self, tmp_path):
        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        first = client.models.embed_content(model="m", contents="soup", config=None)
        second = client.models.embed_content(model="m", contents="soup", config=None)
        assert len(first.embeddings[0].values) == 1536
        assert first.embeddings[0].values == second.embeddings[0].values

    def test_different_text_gives_a_different_vector(self, tmp_path):
        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        soup = client.models.embed_content(model="m", contents="soup", config=None)
        cake = client.models.embed_content(model="m", contents="cake", config=None)
        assert soup.embeddings[0].values != cake.embeddings[0].values

    def test_it_never_touches_the_recordings(self, tmp_path):
        client = GoldenClient(fixture_id="f.epub", root=tmp_path)
        client.models.embed_content(model="m", contents="soup", config=None)
        assert not (tmp_path / "f.epub").exists()


class TestRecordModeGuard:
    def test_the_dummy_key_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key-for-tests")
        with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
            GoldenClient(fixture_id="f.epub", root=tmp_path, record=True)

    def test_an_absent_key_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
            GoldenClient(fixture_id="f.epub", root=tmp_path, record=True)


def test_the_fixture_hands_back_a_replay_client(golden_client, tmp_path):
    client = golden_client("dual-units.epub")
    assert client.fixture_id == "dual-units.epub"
    assert client.record is False
