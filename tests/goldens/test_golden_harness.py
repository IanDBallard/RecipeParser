"""Meta tests for the golden harness itself — no Gemini, no corpus content."""
from __future__ import annotations

from pathlib import Path

from tests.goldens import paths


def test_flags_are_registered_and_default_off(request):
    assert request.config.getoption("--record-gemini") is False
    assert request.config.getoption("--update-goldens") is False


def test_fixture_flags_mirror_the_options(record_gemini, update_goldens):
    assert record_gemini is False
    assert update_goldens is False


def test_paths_point_inside_the_goldens_package():
    assert paths.GOLDENS_DIR.name == "goldens"
    assert paths.CORPUS_DIR == paths.GOLDENS_DIR / "corpus"
    assert paths.READERS_DIR == paths.GOLDENS_DIR / "readers"
    assert paths.GEMINI_DIR == paths.GOLDENS_DIR / "gemini"
    assert paths.E2E_DIR == paths.GOLDENS_DIR / "e2e"


def test_corpus_path_joins_onto_the_corpus_dir():
    assert paths.corpus_path("dual-units.epub") == paths.CORPUS_DIR / "dual-units.epub"


def test_seven_fixtures_are_declared():
    assert len(paths.CORPUS_FIXTURES) == 7
    assert len(set(paths.CORPUS_FIXTURES)) == 7


def test_fixed_axes_are_the_two_axes_the_spec_names():
    from tests.goldens.conftest import FIXED_AXES

    assert sorted(FIXED_AXES) == ["Cuisine", "Meal Type"]
    assert all(isinstance(v, list) and v for v in FIXED_AXES.values())
