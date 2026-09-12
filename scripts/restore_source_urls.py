"""
scripts/restore_source_urls.py — put the Paprika archive's per-entry clip URL
back onto the rows the bulk import dropped it from.

Measured 2026-09-11: 788 recipes, `source_url` set on 5, while about 115 rows
carry a site domain in `source` that came from the Paprika clipper. The archive
still has each entry's `source_url`. This matches entries to rows by title,
with the rule `backfill_paprika_metadata.py` established (alphanumerics only),
and PATCHes `source_url` only where the row has none and the entry has an
http(s) one.

    # dry run -- prints what it would do and writes nothing
    python scripts/restore_source_urls.py --archive "/path/to/Export.paprikarecipes" --user-id <uuid>

    # the real thing
    python scripts/restore_source_urls.py --archive "/path/to/Export.paprikarecipes" --user-id <uuid> --live

Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the environment. Never
creates a recipe; an entry with no matching row is reported and left alone.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipeparser.io.readers.paprika import PaprikaReader  # noqa: E402
from scripts.backfill_paprika_metadata import normalise_title  # noqa: E402

PAGE = 500


@dataclass
class RestorePlan:
    updates: List[Tuple[str, str, str]] = field(default_factory=list)  # (recipe_id, title, url)
    ambiguous: List[str] = field(default_factory=list)
    unmatched: List[str] = field(default_factory=list)
    untouched: int = 0


def _url(value: Any) -> str:
    v = str(value or "").strip()
    return v if v.lower().startswith(("http://", "https://")) else ""


def plan_restore(entries: Sequence[Dict[str, Any]], rows: Sequence[Dict[str, Any]]) -> RestorePlan:
    """Decide what to write, writing nothing."""
    plan = RestorePlan()
    rows_by_title: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_title[normalise_title(row.get("title"))].append(row)
    entries_by_title: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        entries_by_title[normalise_title(entry.get("name"))].append(entry)

    for key, matched_entries in entries_by_title.items():
        matched_rows = [] if key == "" else rows_by_title.get(key, [])
        title = str(matched_entries[0].get("name") or "")
        if not matched_rows:
            plan.unmatched.append(title)
            continue
        if len(matched_rows) > 1 or len(matched_entries) > 1:
            plan.ambiguous.append(title)
            continue
        row, url = matched_rows[0], _url(matched_entries[0].get("source_url"))
        if url and not str(row.get("source_url") or "").strip():
            plan.updates.append((row["id"], title, url))
        else:
            plan.untouched += 1
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description="Restore source_url from a Paprika archive.")
    ap.add_argument("--archive", required=True, help="The .paprikarecipes export.")
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--live", action="store_true", help="Actually write. Omit for a dry run.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    from supabase import create_client  # noqa: PLC0415
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    entries = PaprikaReader().read_entries(args.archive)
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = (sb.table("recipes").select("id,title,source_url").eq("user_id", args.user_id)
                .order("id").range(offset, offset + PAGE - 1).execute().data or [])
        if not page:
            break
        rows.extend(page)
        offset += PAGE

    plan = plan_restore(entries, rows)
    updated, failed = 0, 0
    for rid, title, url in plan.updates:
        if args.verbose:
            print(f"  {rid}  {title!r}  <- {url}")
        if args.live:
            try:
                sb.table("recipes").update({"source_url": url}).eq("id", rid).execute()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  FAILED to update {rid} {title!r}: {exc}")
                continue
        updated += 1

    print(f"{len(entries)} archive entries, {len(rows)} rows; {plan.untouched} untouched, "
          f"{len(plan.ambiguous)} ambiguous, {len(plan.unmatched)} unmatched")
    print(f"{'updated' if args.live else 'would update'} {updated} recipe(s)"
          + (f", {failed} failed" if failed else ""))
    if not args.live:
        print("DRY RUN — nothing was written. Re-run with --live to apply.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
