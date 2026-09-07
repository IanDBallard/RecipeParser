"""Fixtures shared by the golden test families."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List

import pytest

from tests.goldens.golden_client import GoldenClient
from tests.goldens.paths import GEMINI_DIR

#: The taxonomy handed to every refine call in the golden suite.  Fixed here so
#: a recorded reply keys against a prompt that cannot drift with the user's
#: real categories.yaml.
FIXED_AXES: Dict[str, List[str]] = {
    "Cuisine": ["American", "British", "French", "Italian"],
    "Meal Type": ["Breakfast", "Dessert", "Dinner", "Bread"],
}


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
