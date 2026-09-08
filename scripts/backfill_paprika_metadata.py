"""
scripts/backfill_paprika_metadata.py — fill the six metadata columns on the
recipes that were imported before those columns existed.

Migration 012 added source, notes, rating, nutritional_info, description and
difficulty with no backfill, which is right for a schema change and leaves every
recipe imported before it carrying null in all six.  This reads the original
Paprika archive, matches its entries to rows by title, and PATCHes only the
fields that are null on the row and present in the archive.

    # dry run -- prints what it would do and writes nothing
    python scripts/backfill_paprika_metadata.py \\
        --archive "/path/to/Export.paprikarecipes" --user-id <uuid>

    # the real thing
    python scripts/backfill_paprika_metadata.py \\
        --archive "/path/to/Export.paprikarecipes" --user-id <uuid> --live

Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the environment.

This script never creates a recipe.  An archive entry with no matching row is
reported and left alone: recipes an import lost are a different repair, and
half-doing it here would be worse than not doing it.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

# Running this file directly puts scripts/ on sys.path rather than the repository
# root, so `recipeparser` would not import. Make the root importable either way,
# so the command in the docstring works from a plain checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recipeparser.core.models import SourceMeta  # noqa: E402
from recipeparser.io.readers.paprika import PaprikaReader  # noqa: E402

log = logging.getLogger("backfill")

#: The columns migration 012 added.  Also the keys Paprika uses for them, which
#: is why one tuple serves both the archive side and the row side.
FIELDS = ("source", "notes", "rating", "nutritional_info", "description", "difficulty")


@dataclass(frozen=True)
class RowUpdate:
    recipe_id: str
    title: str
    fields: Dict[str, Any]


@dataclass
class BackfillPlan:
    """What a run would do, before it does any of it."""

    updates: List[RowUpdate] = field(default_factory=list)
    ambiguous: List[str] = field(default_factory=list)
    unmatched: List[str] = field(default_factory=list)
    untouched: int = 0


def normalise_title(text: Any) -> str:
    """
    A title reduced to its letters and digits — the only key there is to match on.

    Paprika's ``uid`` and ``hash`` are deliberately not stored (design decision
    P8), so an archive entry and a row share no identifier at all.

    Punctuation is dropped rather than normalised because the ingestion pipeline
    rewrote it on the way in, and not consistently: the archive's curly quotes
    and apostrophes became straight ones, an en dash in "brown butter-braised"
    became a space, and the hyphen in "home-style" closed up.  A dash is a
    separator in one title and a joiner in the next, so no substitution rule
    serves both.  Comparing only the alphanumerics serves all of them, and 25 of
    the 82 entries that looked absent turned out to be this.

    Two titles that differ only in punctuation collide under this key, which the
    caller treats as ambiguous and skips -- the safe direction.
    """
    return "".join(ch for ch in str(text or "") if ch.isalnum()).casefold()


def plan_updates(
    entries: Sequence[Dict[str, Any]], rows: Sequence[Dict[str, Any]]
) -> BackfillPlan:
    """
    Decide what to write, writing nothing.

    Kept pure and separate from the HTTP so the rules below are testable without
    a database: they are the whole risk of this script.
    """
    plan = BackfillPlan()

    rows_by_title: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_title[normalise_title(row.get("title"))].append(row)

    entries_by_title: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        entries_by_title[normalise_title(entry.get("name"))].append(entry)

    for key, matched_entries in entries_by_title.items():
        matched_rows = [] if key == "" else rows_by_title.get(key, [])
        # Report the name as the archive wrote it; the key is for matching only.
        title = str(matched_entries[0].get("name") or "")

        if not matched_rows:
            # No row to fill.  This script does not create recipes; an entry that
            # never made it into the library is the separate repair's business.
            plan.unmatched.append(title)
            continue

        # Two recipes of one name, on either side, leave no way to tell which
        # source belongs to which.  A wrong source is worse than no source.
        if len(matched_rows) > 1 or len(matched_entries) > 1:
            plan.ambiguous.append(title)
            continue

        row = matched_rows[0]
        # SourceMeta rather than the raw dict, so the archive's "" and 0 are read
        # as absent by exactly the rule the ingestion path uses (design §5.1).
        meta = SourceMeta(**{name: matched_entries[0].get(name) for name in FIELDS})

        fields = {
            name: getattr(meta, name)
            for name in FIELDS
            if getattr(meta, name) is not None and row.get(name) is None
        }
        if fields:
            plan.updates.append(
                RowUpdate(recipe_id=row["id"], title=title, fields=fields)
            )
        else:
            plan.untouched += 1

    return plan


# ---------------------------------------------------------------------------
# The I/O shell
# ---------------------------------------------------------------------------

def _creds() -> Tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must both be set.")
    return url, key


def fetch_rows(url: str, key: str, user_id: str) -> List[Dict[str, Any]]:
    """Every recipe this user owns, with only the columns this script reasons about."""
    resp = httpx.get(
        f"{url}/rest/v1/recipes",
        params={"user_id": f"eq.{user_id}", "select": "id,title," + ",".join(FIELDS)},
        headers={"Authorization": f"Bearer {key}", "apikey": key},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()


def apply_update(url: str, key: str, update: RowUpdate) -> None:
    resp = httpx.patch(
        f"{url}/rest/v1/recipes",
        params={"id": f"eq.{update.recipe_id}"},
        headers={
            "Authorization": f"Bearer {key}",
            "apikey": key,
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        json=update.fields,
        timeout=30.0,
    )
    resp.raise_for_status()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill the six Paprika metadata columns.")
    parser.add_argument("--archive", required=True, help="Path to the .paprikarecipes export.")
    parser.add_argument("--user-id", required=True, help="The owner whose library to fill.")
    parser.add_argument("--live", action="store_true", help="Actually write. Omit for a dry run.")
    parser.add_argument("--verbose", action="store_true", help="List every row that would change.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    url, key = _creds()

    entries = PaprikaReader().read_entries(args.archive)
    rows = fetch_rows(url, key, args.user_id)
    plan = plan_updates(entries, rows)

    if args.verbose:
        for update in plan.updates:
            log.info("%s -> %s", update.title, ", ".join(sorted(update.fields)))
        for title in plan.ambiguous:
            log.warning("ambiguous, skipped: %s", title)

    written = 0
    failed = 0
    if args.live:
        for update in plan.updates:
            try:
                apply_update(url, key, update)
                written += 1
            except Exception as exc:  # noqa: BLE001
                # One bad row must not abandon the other 700. Report and carry on.
                failed += 1
                log.error("failed to update %r (%s): %s", update.title, update.recipe_id, exc)

    # Printed on a dry run and a live run alike, so the two are comparable.
    log.info(
        "%s: %d entries, %d rows, %d to fill, %d already complete, %d ambiguous, %d not in the library.",
        f"WROTE {written}" + (f", {failed} FAILED" if failed else "") if args.live else "DRY RUN",
        len(entries),
        len(rows),
        len(plan.updates),
        plan.untouched,
        len(plan.ambiguous),
        len(plan.unmatched),
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
