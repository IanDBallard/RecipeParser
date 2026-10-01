"""Fixtures shared by the golden test families."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List
from unittest.mock import patch

import pytest

from recipeparser.core.models import Chunk
from recipeparser.io.readers.pdf import PdfReader
from tests.goldens.golden_client import GoldenClient
from tests.goldens.paths import GEMINI_DIR

#: The taxonomy handed to every refine call in the golden suite.  Fixed here so
#: a recorded reply keys against a prompt that cannot drift with the user's
#: real categories.yaml.
FIXED_AXES: Dict[str, List[str]] = {
    "Cuisine": ["American", "British", "French", "Italian"],
    "Meal Type": ["Breakfast", "Dessert", "Dinner", "Bread"],
}


def read_pdf_by_page(path: str) -> List[Chunk]:
    """``PdfReader().read`` held to one chunk per page, the path a PDF too long for one chunk takes.

    ``text-pages.pdf`` (four pages, about 10,000 characters) now fits in one chunk, so the reader
    sends it whole; its recorded extract and refine replies are keyed to its page chunks. The
    Gemini-backed families keep it on the page path, which long PDFs still take, until a run with
    a real key records the whole-document replies (``--record-gemini``). The reader golden shows
    what the reader returns for it today.
    """
    with patch("recipeparser.io.readers.pdf.MAX_CHUNK_CHARS", 0):
        return PdfReader().read(path)


@pytest.fixture
def record_gemini(request) -> bool:
    """True when the run was started with --record-gemini."""
    return bool(request.config.getoption("--record-gemini"))


@pytest.fixture
def update_goldens(request) -> bool:
    """True when the run was started with --update-goldens."""
    return bool(request.config.getoption("--update-goldens"))


@pytest.fixture
def golden_client(request) -> Callable[[str], GoldenClient]:
    """Factory: golden_client("dual-units.epub") -> GoldenClient for that fixture."""
    record = bool(request.config.getoption("--record-gemini"))

    def _make(fixture_id: str, root: Path = GEMINI_DIR) -> GoldenClient:
        return GoldenClient(fixture_id=fixture_id, root=root, record=record)

    return _make
