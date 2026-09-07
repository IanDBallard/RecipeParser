"""Meta tests for the golden harness itself — no Gemini, no corpus content."""
from __future__ import annotations

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


def _readme_fixture_names() -> set[str]:
    """Filenames in the first column of the README's provenance table."""
    import re

    text = (paths.CORPUS_DIR / "README.md").read_text(encoding="utf-8")
    names: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        first = line.split("|")[1].strip()
        m = re.fullmatch(r"`([^`]+)`", first)
        if m:
            names.add(m.group(1))
    return names


def test_every_corpus_file_has_a_readme_entry():
    on_disk = {p.name for p in paths.CORPUS_DIR.iterdir() if p.name != "README.md"}
    assert on_disk == _readme_fixture_names()


def test_the_readme_documents_exactly_the_declared_fixtures():
    assert _readme_fixture_names() == set(paths.CORPUS_FIXTURES)


def test_the_corpus_stays_under_two_megabytes():
    total = sum(p.stat().st_size for p in paths.CORPUS_DIR.iterdir())
    assert total < 2 * 1024 * 1024, f"corpus is {total} bytes"
