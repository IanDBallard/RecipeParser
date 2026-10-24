"""
One-off: wrap the counts already-imported directions write, so they scale (Cayenne scaling text
audit 2026-10-24, F-491 and F-492), and demote the amounts REFINE put on words that name no one
ingredient (F-495).

No model is asked. Each recipe's stored ``tokenized_directions`` go through ``core.count_tokens.tag_counts``
(a count of an ingredient becomes an amount token, a yield count a ``yield:`` token) and then
``core.fat_tokens.check_mentions`` (the import's own checks, which now include the words that name no
one ingredient). Stripping the tokens gives every step back exactly as stored: no word changes.
Written: ``tokenized_directions`` only.

Skipped: a stale recipe (``derived_rev < body_rev`` -- the regeneration worker re-derives it and the
pass runs there), one with no directions, and one whose columns are not lists. A recipe the pass does
not change is ``unchanged`` and not written. The write is conditional on ``body_rev`` and
``derived_rev`` being what was read, so a cook's edit mid-run wins.

    # dry run against the live table: counts, and every proposed step to a CSV; writes nothing
    python scripts/backfill_count_tokens.py --all-users --review counts-review.csv

    # dry run against rows exported read-only (supabase db query ... -o json), no credentials needed
    python scripts/backfill_count_tokens.py --rows-json rows.json --review counts-review.csv

    # for real; --record keeps every rewritten row's old directions
    python scripts/backfill_count_tokens.py --all-users --live --record counts-before.csv

    # undo, from that record
    python scripts/backfill_count_tokens.py --restore counts-before.csv --live

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY except with --rows-json.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipeparser.core.count_tokens import tag_counts, yield_numbers  # noqa: E402
from recipeparser.core.fat_tokens import check_mentions, strip_fat_tokens  # noqa: E402
from recipeparser.models import StructuredIngredient, TokenizedDirection  # noqa: E402

COLUMNS = (
    "id,title,body_rev,derived_rev,base_servings,servings_min,servings_max,servings_note,"
    "structured_ingredients,tokenized_directions"
)
PAGE = 500
STATUSES = ("tagged", "unchanged", "stale", "empty", "malformed")


@dataclass
class RowResult:
    id: str
    title: str
    status: str
    before: List[Dict[str, Any]] = field(default_factory=list)
    after: List[Dict[str, Any]] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)
    kinds: Counter = field(default_factory=Counter)


def plan_row(row: Dict[str, Any]) -> RowResult:
    """What the pass would write for one stored row; never raises on a malformed row."""
    rid, title = str(row.get("id")), str(row.get("title") or "")
    body, derived = row.get("body_rev"), row.get("derived_rev")
    if isinstance(body, int) and (not isinstance(derived, int) or derived < body):
        return RowResult(rid, title, "stale")
    raw_dirs, raw_ings = row.get("tokenized_directions"), row.get("structured_ingredients")
    if not isinstance(raw_dirs, list) or not isinstance(raw_ings, list):
        return RowResult(rid, title, "empty" if raw_dirs in (None, []) else "malformed")
    if not raw_dirs:
        return RowResult(rid, title, "empty")
    try:
        directions = [TokenizedDirection(step=int(d["step"]), text=str(d["text"])) for d in raw_dirs]
        ingredients = [StructuredIngredient(**i) for i in raw_ings]
    except Exception:  # noqa: BLE001 — a row the models refuse is reported, never written
        return RowResult(rid, title, "malformed")
    base = row.get("base_servings")
    yields = yield_numbers(
        float(base) if base is not None else None,
        row.get("servings_min"),
        row.get("servings_max"),
        row.get("servings_note"),
    )
    tagged, lines = tag_counts(directions, ingredients, yields)
    checked, demotions = check_mentions(ingredients, tagged)
    if [d.text for d in checked] == [d.text for d in directions]:
        return RowResult(rid, title, "unchanged")
    # The words are the cook's: a pass that would change one is refused, whatever it found.
    assert [strip_fat_tokens(d.text) for d in checked] == [strip_fat_tokens(d.text) for d in directions], rid
    kinds: Counter = Counter()
    for line in lines:
        kinds["yield" if " yield " in line else "count"] += 1
    kinds["demoted"] += len(demotions)
    return RowResult(
        rid,
        title,
        "tagged",
        [d.model_dump() for d in directions],
        [d.model_dump() for d in checked],
        lines + [f"demoted: {d}" for d in demotions],
        kinds,
    )


def write_row(sb: Any, row: Dict[str, Any], r: RowResult) -> bool:
    """The conditional write: true when the row was still as read."""
    query = (
        sb.table("recipes").update({"tokenized_directions": r.after}).eq("id", r.id).eq("body_rev", row.get("body_rev"))
    )
    query = (
        query.eq("derived_rev", row.get("derived_rev"))
        if row.get("derived_rev") is not None
        else query.is_("derived_rev", "null")
    )
    return bool(query.execute().data)


def write_review(path: Path, results: List[RowResult]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "title", "step", "before", "after", "changes"])
        for r in results:
            if r.status != "tagged":
                continue
            first = True
            for b, a in zip(r.before, r.after):
                if b["text"] != a["text"]:
                    writer.writerow(
                        [r.id, r.title, a["step"], b["text"], a["text"], " | ".join(r.lines) if first else ""]
                    )
                    first = False


def _creds() -> Tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must both be set.")
    return url, key


def _read_rows(sb: Any, user_id: Optional[str], recipe_id: Optional[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        query = sb.table("recipes").select(COLUMNS)
        if user_id:
            query = query.eq("user_id", user_id)
        if recipe_id:
            query = query.eq("id", recipe_id)
        page = query.order("id").range(offset, offset + PAGE - 1).execute().data or []
        if not page:
            return rows
        rows.extend(page)
        offset += PAGE


def _restore(sb: Any, path: Path, live: bool) -> int:
    restored = 0
    with path.open(newline="", encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            if live:
                sb.table("recipes").update({"tokenized_directions": json.loads(rec["tokenized_directions"])}).eq(
                    "id", rec["id"]
                ).execute()
            restored += 1
    print(f"{'restored' if live else 'would restore'} {restored} recipe(s) from {path}")
    if not live:
        print("DRY RUN — nothing was written. Re-run with --live to apply.")
    return 0


def summary(results: List[RowResult]) -> str:
    statuses = Counter(r.status for r in results)
    kinds: Counter = Counter()
    for r in results:
        kinds.update(r.kinds)
    recipes = Counter()
    for r in results:
        for k in ("count", "yield", "demoted"):
            if r.kinds.get(k):
                recipes[k] += 1
    parts = [f"{s}: {statuses.get(s, 0)}" for s in STATUSES]
    return (
        f"{len(results)} recipe(s) — " + ", ".join(parts) + "\n"
        f"ingredient counts wrapped: {kinds['count']} in {recipes['count']} recipe(s); "
        f"yield counts wrapped: {kinds['yield']} in {recipes['yield']} recipe(s); "
        f"mentions demoted to none: {kinds['demoted']} in {recipes['demoted']} recipe(s)"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Wrap the counts stored directions write, so they scale.")
    who = ap.add_mutually_exclusive_group()
    who.add_argument("--user-id", help="The owner whose library to tag.")
    who.add_argument("--all-users", action="store_true", help="Every library: shared copies are rows of their own.")
    who.add_argument(
        "--rows-json", type=Path, help="Rows exported read-only (supabase db query -o json): a dry run only."
    )
    ap.add_argument("--recipe-id", help="This one recipe only.")
    ap.add_argument("--review", type=Path, help="CSV of every proposed step: id, title, step, before, after, changes.")
    ap.add_argument("--live", action="store_true", help="Actually write. Omit for a dry run.")
    ap.add_argument("--record", type=Path, help="CSV of each rewritten row's id, title and old tokenized_directions.")
    ap.add_argument("--restore", type=Path, help="Write this record's tokenized_directions back; nothing else.")
    ap.add_argument("--verbose", action="store_true", help="Print every change.")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
    if args.live and args.rows_json:
        raise SystemExit("--rows-json is a dry run: it has no table to write to.")
    if args.live and not args.restore and not args.record:
        raise SystemExit("--live needs --record: the record is the only way back.")

    sb = None
    if args.rows_json:
        data = json.loads(args.rows_json.read_text(encoding="utf-8"))
        rows = data["rows"] if isinstance(data, dict) else data
        if args.recipe_id:
            rows = [r for r in rows if r.get("id") == args.recipe_id]
    else:
        from supabase import create_client  # noqa: PLC0415

        sb = create_client(*_creds())
        if args.restore:
            return _restore(sb, args.restore, args.live)
        if not (args.user_id or args.all_users):
            raise SystemExit("Name --user-id or --all-users.")
        rows = _read_rows(sb, args.user_id, args.recipe_id)

    results = [plan_row(row) for row in rows]
    by_id = {r["id"]: r for r in rows}
    written = raced = 0
    record = writer = None
    if args.live:
        record = args.record.open("w", newline="", encoding="utf-8")
        writer = csv.writer(record)
        writer.writerow(["id", "title", "tokenized_directions"])
    for r in results:
        if r.status != "tagged":
            continue
        if args.verbose:
            print(f"\n  {r.title!r}  ({r.id})")
            for line in r.lines:
                print(f"    {line}")
        if args.live:
            assert writer is not None and record is not None and sb is not None
            writer.writerow([r.id, r.title, json.dumps(r.before)])
            record.flush()
            if write_row(sb, by_id[r.id], r):
                written += 1
            else:
                raced += 1
                print(f"  MOVED   {r.id}  {r.title!r}: edited during the run; left for the regeneration worker")
    if record:
        record.close()
    if args.review:
        write_review(args.review, results)
    print(summary(results))
    if args.live:
        print(f"written: {written}, moved during the run: {raced}")
    else:
        print("DRY RUN — nothing was written. Re-run with --live --record <file> to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
