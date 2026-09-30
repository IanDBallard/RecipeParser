"""Fix Roadmap F-068: RecipeParser writes a timestamp in one format."""
from __future__ import annotations

import datetime
import re
from pathlib import Path

import pytest

from recipeparser.core.clock import utc_timestamp

_PACKAGE = Path(__file__).resolve().parents[2] / "recipeparser"


def test_the_stamp_is_an_aware_utc_iso_instant():
    stamp = utc_timestamp()
    parsed = datetime.datetime.fromisoformat(stamp)
    assert parsed.utcoffset() == datetime.timedelta(0)
    assert stamp.endswith("+00:00")


def test_a_given_instant_is_converted_to_utc():
    ten_am_in_toronto = datetime.datetime(
        2026, 9, 30, 10, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-4))
    )
    assert utc_timestamp(ten_am_in_toronto) == "2026-09-30T14:00:00+00:00"


def test_a_naive_datetime_is_refused():
    with pytest.raises(ValueError, match="aware"):
        utc_timestamp(datetime.datetime(2026, 9, 30, 10, 0))


def test_no_module_writes_a_timestamp_its_own_way():
    """The three formats F-068 found must not creep back in."""
    offenders = []
    for path in _PACKAGE.rglob("*.py"):
        if path.name == "clock.py":
            continue  # its docstring names the formats it replaced
        text = path.read_text(encoding="utf-8")
        for pattern in (r"\butcnow\(", r"strftime\(\"%Y-%m-%dT", r"isoformat\(\)\s*\+\s*\"Z\""):
            if re.search(pattern, text):
                offenders.append(f"{path.relative_to(_PACKAGE)}: {pattern}")
    assert offenders == [], "use recipeparser.core.clock.utc_timestamp: " + ", ".join(offenders)
