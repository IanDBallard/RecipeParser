"""Fixtures shared by the golden test families."""
from __future__ import annotations

from typing import Dict, List

import pytest

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
