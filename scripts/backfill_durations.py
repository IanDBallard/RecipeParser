"""
One-off: parse prep_time / cook_time text into the structured duration columns
(spec 7.2).  Servings text was never stored, so servings_min/max are seeded
from base_servings.  `base_servings` itself is never changed — it is numeric and
user-owned, and it is written back exactly as it was read.  Rows whose text
landed entirely in a note are printed for a manual look.

Mirrors the CLI shape of `scripts/backfill_paprika_metadata.py` (on master): a dry
run is the default and writing is an explicit opt-in, so a mistyped invocation
costs nothing.

    # dry run -- prints what it would do and writes nothing
    python scripts/backfill_durations.py

    # for real
    python scripts/backfill_durations.py --live

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipeparser.core.durations import duration_columns  # noqa: E402

PAGE = 500


def _servings_text(base: Any) -> Optional[str]:
    """Servings text for the parser from a numeric base_servings, without truncating."""
    if base is None:
        return None
    value = float(base)
    return str(int(value)) if value.is_integer() else str(value)


def plan_backfill(rows: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any], bool]]:
    plan = []
    for row in rows:
        base = row.get("base_servings")
        # recipes.base_servings is numeric, so int(base) turns 4.5 into 4 — and
        # duration_columns puts its result into cols["base_servings"], which the
        # UPDATE writes. A --live run would silently rewrite every fractional
        # base_servings in the library. Derive the servings text from the true
        # value and hand back the original untouched: this backfill seeds the
        # structured servings columns and never changes base_servings.
        servings_text = _servings_text(base)
        cols = duration_columns(row.get("prep_time"), row.get("cook_time"), servings_text, None)
        cols["base_servings"] = base
        note_only = any(
            cols[f"{k}_note"] and cols[f"{k}_min_minutes"] is None and row.get(f"{k}_time")
            for k in ("prep", "cook")
        )
        plan.append((row["id"], cols, note_only))
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill the structured duration and servings columns.")
    ap.add_argument("--live", action="store_true", help="Actually write. Omit for a dry run.")
    ap.add_argument("--verbose", action="store_true", help="List every row that would change.")
    args = ap.parse_args()
    from supabase import create_client  # noqa: PLC0415
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    offset, updated, failed, flagged = 0, 0, 0, []
    while True:
        rows = (sb.table("recipes").select("id,title,prep_time,cook_time,base_servings")
                .order("id").range(offset, offset + PAGE - 1).execute().data or [])
        if not rows:
            break
        titles = {r["id"]: r.get("title") for r in rows}
        for rid, cols, note_only in plan_backfill(rows):
            if note_only:
                flagged.append((rid, titles[rid], cols.get("prep_note"), cols.get("cook_note")))
            if args.verbose:
                print(f"  {rid}  {titles[rid]!r}  {cols}")
            if args.live:
                try:
                    sb.table("recipes").update(cols).eq("id", rid).execute()
                except Exception as exc:  # noqa: BLE001
                    # One bad row must not abandon the rest of the library. Report and carry on.
                    failed += 1
                    print(f"  FAILED to update {rid} {titles[rid]!r}: {exc}")
                    continue
            updated += 1
        offset += PAGE

    # Printed on a dry run and a live run alike, so the two are comparable.
    print(
        f"{'updated' if args.live else 'would update'} {updated} recipe(s)"
        + (f", {failed} failed" if failed else "")
    )
    if not args.live:
        print("DRY RUN — nothing was written. Re-run with --live to apply.")
    if flagged:
        print(f"{len(flagged)} row(s) with note-only durations — eyeball these:")
        for rid, title, p, c in flagged:
            print(f"  {rid}  {title!r}  prep_note={p!r}  cook_note={c!r}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
