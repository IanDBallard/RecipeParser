"""
recipeparser/core/clock.py — the one format RecipeParser writes a timestamp in.

Every ``updated_at`` (and any other ``timestamptz``) that RecipeParser writes
goes through ``utc_timestamp``: an aware UTC instant in ISO 8601, such as
``2026-09-30T19:05:28.123456+00:00``. Until Fix Roadmap F-068 there were
three formats: the deprecated ``datetime.utcnow()`` with a hand-added ``Z``,
a seconds-only ``strftime`` with a ``Z``, and this one.

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
from __future__ import annotations

import datetime
from typing import Optional


def utc_timestamp(now: Optional[datetime.datetime] = None) -> str:
    """Return ``now`` (default: the current time) as an aware UTC ISO 8601 string.

    A naive ``now`` is refused rather than guessed at: it has no zone, and
    ``astimezone`` would read it as the machine's local time.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    elif now.tzinfo is None:
        raise ValueError("utc_timestamp needs an aware datetime, not a naive one.")
    return now.astimezone(datetime.timezone.utc).isoformat()
