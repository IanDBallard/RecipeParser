"""
One-off: derive the four citation columns from the free-text `source` on every
recipe already in the library (design 2026-09-11, "The backfill, per measured
value"). Seven ordered rules, first match wins; `recipeparser.core.citation`
holds them, so the backfill and the ingestion path cannot disagree.

Mirrors the CLI shape of `scripts/backfill_durations.py`: a dry run is the
default and writing is an explicit opt-in.

    # dry run -- prints the per-rule counts and writes nothing
    python scripts/backfill_sources.py --user-id <uuid>

    # for real
    python scripts/backfill_sources.py --user-id <uuid> --live

    # list every row and what it would get
    python scripts/backfill_sources.py --user-id <uuid> --verbose

Rows whose `source_kind` is already set are left alone (a cook's Set source
must survive a re-run); `--force` reclassifies them too. Rule 6 (`Gemini`)
also clears `source`: the model named itself, and that is not a source.

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipeparser.core.citation import classify_source  # noqa: E402

PAGE = 500
RULE_NAMES = {
    1: "no source",
    2: "book: Title — Author",
    3: "unknown book (Auto-Import)",
    4: "site domain or URL",
    5: "domain then author",
    6: "Gemini (cleared)",
    7: "person / other text",
}

RowPlan = Tuple[str, Dict[str, Any], int]


def _spelling(source: str) -> str:
    """The site as the row wrote it, without scheme or path, case kept: the title candidates for rule 4."""
    v = source.strip()
    if v.lower().startswith(("http://", "https://")):
        v = v.split("://", 1)[1]
    return v.split("/", 1)[0]


def plan_backfill(rows: List[Dict[str, Any]], force: bool = False) -> List[RowPlan]:
    """Decide what to write, writing nothing. Pure, so the rules are testable without a database."""
    classified = []
    spellings: Dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        if row.get("source_kind") is not None and not force:
            continue
        result = classify_source(row.get("source"))
        classified.append((row, result))
        if result.rule == 4:
            spellings[result.citation.key][_spelling(row["source"])] += 1

    plan: List[RowPlan] = []
    for row, result in classified:
        cit = result.citation
        cols: Dict[str, Any] = {
            "source_kind": cit.kind,
            "source_key": cit.key,
            "source_title": cit.title,
            "source_author": cit.author,
        }
        if result.rule == 4:
            # "the host as written on the majority of its rows"
            cols["source_title"] = spellings[cit.key].most_common(1)[0][0]
        if result.rule == 6:
            cols["source"] = None
        plan.append((row["id"], cols, result.rule))
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill the four citation columns from `source`.")
    ap.add_argument("--user-id", required=True, help="The owner whose library to repair.")
    ap.add_argument("--live", action="store_true", help="Actually write. Omit for a dry run.")
    ap.add_argument("--force", action="store_true", help="Reclassify rows whose source_kind is already set.")
    ap.add_argument("--verbose", action="store_true", help="List every row that would change.")
    args = ap.parse_args()
    from supabase import create_client  # noqa: PLC0415
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    # Read everything first: rule 4's majority spelling needs the whole library.
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = (sb.table("recipes").select("id,title,source,source_kind").eq("user_id", args.user_id)
                .order("id").range(offset, offset + PAGE - 1).execute().data or [])
        if not page:
            break
        rows.extend(page)
        offset += PAGE
    titles = {r["id"]: r.get("title") for r in rows}

    plan = plan_backfill(rows, force=args.force)
    counts = Counter(rule for _, _, rule in plan)
    updated, failed = 0, 0
    for rid, cols, rule in plan:
        if args.verbose:
            print(f"  rule {rule}  {rid}  {titles[rid]!r}  {cols}")
        if args.live:
            try:
                sb.table("recipes").update(cols).eq("id", rid).execute()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  FAILED to update {rid} {titles[rid]!r}: {exc}")
                continue
        updated += 1

    # Printed on a dry run and a live run alike, so the two are comparable.
    print(f"{len(rows)} recipes read, {len(rows) - len(plan)} already classified and skipped")
    for rule in sorted(RULE_NAMES):
        print(f"  rule {rule}  {counts.get(rule, 0):5d}  {RULE_NAMES[rule]}")
    print(f"{'updated' if args.live else 'would update'} {updated} recipe(s)"
          + (f", {failed} failed" if failed else ""))
    if not args.live:
        print("DRY RUN — nothing was written. Re-run with --live to apply.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
