"""Filesystem layout of the golden suite — the one place that knows it."""
from __future__ import annotations

from pathlib import Path

GOLDENS_DIR: Path = Path(__file__).resolve().parent
CORPUS_DIR: Path = GOLDENS_DIR / "corpus"
READERS_DIR: Path = GOLDENS_DIR / "readers"
GEMINI_DIR: Path = GOLDENS_DIR / "gemini"
E2E_DIR: Path = GOLDENS_DIR / "e2e"

#: Every corpus file, in the order the spec's table lists them.  A fixture id
#: is its filename: it is what the reader is handed and what names its golden.
CORPUS_FIXTURES: tuple[str, ...] = (
    "gutenberg-multi.epub",
    "dual-units.epub",
    "phases-bakers.epub",
    "text-pages.pdf",
    "scanned.pdf",
    "saved-page.html",
    "legacy-photo.paprikarecipes",
)


def corpus_path(name: str) -> Path:
    """Absolute path to one corpus file."""
    return CORPUS_DIR / name


def golden_path(kind: str, name: str) -> Path:
    """Absolute path to an expected-output file, e.g. golden_path("readers", "scanned.pdf")."""
    return GOLDENS_DIR / kind / f"{name}.json"
