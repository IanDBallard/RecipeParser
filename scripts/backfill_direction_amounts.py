"""
One-off: tag every direction mention in a library with the amount it uses (Cayenne's direction
amounts design, docs/superpowers/specs/2026-10-02-direction-amounts-design.md; RecipeParser 9.5.0).

Recipes imported before 9.5.0 carry legacy tokens, ``{{ing_02|flour}}``, which say nothing about
how much of the ingredient a step uses, so the kitchen can only show the whole amount or nothing.
This asks Gemini, once per recipe, which ingredient each mention in the steps refers to and how
much it uses, and rewrites ``tokenized_directions`` alone. The ingredient list, its ids, amounts and
conversions, the embedding, the categories and the cook's edits are not touched. The steps are the
text the kitchen shows today — the current tokens' words, in the current numbering — not the raw
``direction_steps``: those carry the source's OCR noise ("1 In a wok", "30 40 minutes") that the
import cleaned. The model answers with quotes, and ``core.fat_tokens.splice_mentions`` places each
token where its words are, so no word of a recipe changes. A quote it cannot place loses its chip,
nothing else.

Skipped: a recipe whose tokens already carry a use (``--force`` re-tags it), a stale recipe
(``derived_rev < body_rev`` — the regeneration worker re-derives it with 9.5.0's REFINE anyway),
one with no directions or no ingredients, and one whose columns are not lists. The write is conditional
on ``body_rev`` and ``derived_rev`` being what was read, so a cook's edit mid-run wins.

    # --all-users in place of --user-id re-tags every library (a shared recipe is a row of its own)

    # dry run of a sample -- calls Gemini, prints each recipe's new steps, writes nothing
    python scripts/backfill_direction_amounts.py --user-id <uuid> --limit 20 --verbose

    # one recipe
    python scripts/backfill_direction_amounts.py --user-id <uuid> --recipe-id <uuid> --verbose

    # for real; --record keeps every rewritten row's old tokenized_directions
    python scripts/backfill_direction_amounts.py --user-id <uuid> --live --record before.csv

    # undo, from that record
    python scripts/backfill_direction_amounts.py --user-id <uuid> --restore before.csv --live

A dry run makes the Gemini calls too (that is the only way to see the result): one call per recipe.
Requires SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and GOOGLE_API_KEY.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipeparser.core.fat_tokens import check_mentions, is_tagged, splice_mentions, strip_fat_tokens  # noqa: E402
from recipeparser.models import DirectionMention, StructuredIngredient, TokenizedDirection  # noqa: E402

PAGE = 500
COLUMNS = "id,title,structured_ingredients,tokenized_directions,body_rev,derived_rev"

Tagger = Callable[[List[StructuredIngredient], List[str]], List[DirectionMention]]


@dataclass
class RowResult:
    id: str
    title: str
    status: str  # tagged | already-tagged | stale | empty | malformed | failed
    directions: List[TokenizedDirection] = field(default_factory=list)
    before: Any = None
    dropped: List[str] = field(default_factory=list)
    demoted: List[str] = field(default_factory=list)
    error: str = ""


def _list(value: Any) -> Optional[List[Any]]:
    """A jsonb column as a list; None for anything else, a double-encoded string included."""
    return value if isinstance(value, list) else None


def plan_row(row: Dict[str, Any], tag: Tagger, force: bool = False) -> RowResult:
    """What one recipe would be written as, writing nothing. Pure but for ``tag``."""
    result = RowResult(id=row["id"], title=str(row.get("title") or ""), status="tagged",
                       before=row.get("tokenized_directions"))
    ingredients_raw = _list(row.get("structured_ingredients"))
    tokenized_raw = _list(row.get("tokenized_directions"))
    if ingredients_raw is None or tokenized_raw is None:
        result.status = "malformed"
        return result
    if (row.get("derived_rev") or 0) < (row.get("body_rev") or 0):
        result.status = "stale"
        return result
    try:
        ingredients = [StructuredIngredient.model_validate(i) for i in ingredients_raw]
        old = [TokenizedDirection.model_validate(d) for d in tokenized_raw]
    except Exception as exc:  # noqa: BLE001
        result.status, result.error = "malformed", str(exc)
        return result
    if is_tagged(old) and not force:
        result.status = "already-tagged"
        return result
    # What the kitchen shows today, in its numbering: the text the chips are placed in.
    steps = [strip_fat_tokens(d.text) for d in old]
    if not steps or not ingredients:
        result.status = "empty"
        return result
    try:
        mentions = tag(ingredients, steps)
    except Exception as exc:  # noqa: BLE001
        result.status, result.error = "failed", str(exc)
        return result
    spliced, result.dropped = splice_mentions(steps, mentions, [i.id for i in ingredients], [d.step for d in old])
    result.directions, result.demoted = check_mentions(ingredients, spliced)
    return result


def write_row(sb: Any, row: Dict[str, Any], directions: Sequence[TokenizedDirection]) -> bool:
    """Write the directions only if the row is still the revision that was read. False when it moved."""
    response = (
        sb.table("recipes")
        .update({"tokenized_directions": [d.model_dump() for d in directions]})
        .eq("id", row["id"])
        .eq("body_rev", row.get("body_rev") or 0)
        .eq("derived_rev", row.get("derived_rev") or 0)
        .execute()
    )
    return bool(response.data)


def summarise(results: Sequence[RowResult]) -> Tuple["Counter[str]", int, int]:
    statuses = Counter(r.status for r in results)
    dropped = sum(len(r.dropped) for r in results)
    corrected = sum(len(r.demoted) for r in results)
    return statuses, dropped, corrected


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
                sb.table("recipes").update({"tokenized_directions": json.loads(rec["tokenized_directions"])}) \
                    .eq("id", rec["id"]).execute()
            restored += 1
    print(f"{'restored' if live else 'would restore'} {restored} recipe(s) from {path}")
    if not live:
        print("DRY RUN — nothing was written. Re-run with --live to apply.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Tag every direction mention with the amount it uses.")
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--user-id", help="The owner whose library to re-tag.")
    who.add_argument("--all-users", action="store_true", help="Every library: shared copies are rows of their own.")
    ap.add_argument("--recipe-id", help="Re-tag this one recipe only.")
    ap.add_argument("--limit", type=int, help="Stop after this many recipes have been sent to Gemini.")
    ap.add_argument("--workers", type=int, default=4, help="Gemini calls in flight at once (default 4).")
    ap.add_argument("--live", action="store_true", help="Actually write. Omit for a dry run.")
    ap.add_argument("--record", type=Path, help="CSV of each rewritten row's id, title and old tokenized_directions.")
    ap.add_argument("--restore", type=Path, help="Write this record's tokenized_directions back; nothing else.")
    ap.add_argument("--force", action="store_true", help="Re-tag recipes whose tokens already carry a use.")
    ap.add_argument("--verbose", action="store_true", help="Print every re-tagged recipe's new steps.")
    ap.add_argument("--progress", type=int, default=50, help="Print a progress line every N recipes (0: none).")
    args = ap.parse_args()
    # Redirected to a file, stdout is block-buffered and a long run's log stays empty; flush per line.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if args.live and not args.restore and not args.record:
        raise SystemExit("--live needs --record: the record is the only way back.")

    from supabase import create_client  # noqa: PLC0415
    sb = create_client(*_creds())
    if args.restore:
        return _restore(sb, args.restore, args.live)

    from recipeparser import client as gemini_client  # noqa: PLC0415
    from recipeparser.gemini import tag_direction_mentions  # noqa: PLC0415
    if gemini_client is None:
        raise SystemExit("GOOGLE_API_KEY must be set.")

    rows = _read_rows(sb, args.user_id, args.recipe_id)
    by_id = {r["id"]: r for r in rows}

    sent = 0

    def tag(ingredients: List[StructuredIngredient], steps: List[str]) -> List[DirectionMention]:
        return tag_direction_mentions(ingredients, steps, gemini_client)

    # Decide which rows go to Gemini first, so --limit counts calls, not rows read.
    todo, results = [], []
    for row in rows:
        probe = plan_row(row, lambda *_: [], force=args.force)
        if probe.status != "tagged":
            results.append(probe)
        elif args.limit is None or sent < args.limit:
            todo.append(row)
            sent += 1
    writer = None
    record_fh = None
    if args.live:
        record_fh = args.record.open("w", newline="", encoding="utf-8")
        writer = csv.writer(record_fh)
        writer.writerow(["id", "title", "tokenized_directions"])
        record_fh.flush()

    counts = {"written": 0, "raced": 0, "failed_writes": 0, "done": 0}
    started = time.monotonic()

    def finish(r: RowResult) -> None:
        """Report, record and write one recipe the moment its call returns (F-220)."""
        results.append(r)
        counts["done"] += 1
        if r.status == "failed":
            print(f"  FAILED  {r.id}  {r.title!r}: {r.error}")
        elif r.status == "tagged":
            if args.verbose:
                print(f"\n  {r.title!r}  ({r.id})")
                for d in r.directions:
                    print(f"    {d.step}. {d.text}")
                for line in r.dropped:
                    print(f"    dropped: {line}")
                for line in r.demoted:
                    print(f"    corrected: {line}")
            if args.live:
                assert writer is not None and record_fh is not None
                # The record line is written, and flushed, before the row: the record is the way back.
                writer.writerow([r.id, r.title, json.dumps(r.before)])
                record_fh.flush()
                try:
                    if write_row(sb, by_id[r.id], r.directions):
                        counts["written"] += 1
                    else:
                        counts["raced"] += 1
                        print(f"  MOVED   {r.id}  {r.title!r}: edited during the run; left for the regeneration worker")
                except Exception as exc:  # noqa: BLE001
                    counts["failed_writes"] += 1
                    print(f"  FAILED to write {r.id} {r.title!r}: {exc}")
        if args.progress and (counts["done"] % args.progress == 0 or counts["done"] == len(todo)):
            minutes = (time.monotonic() - started) / 60
            print(f"progress: {counts['done']}/{len(todo)} sent to Gemini, {counts['written']} written, "
                  f"{minutes:.1f} min")

    # Each recipe is handled as its call returns, so a crash loses only the calls in flight, and the
    # log shows the run moving (F-220: the first live run sat silent for most of an hour).
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(plan_row, r, tag, args.force) for r in todo]
        for future in as_completed(futures):
            finish(future.result())
    if record_fh:
        record_fh.close()
    written, raced, failed_writes = counts["written"], counts["raced"], counts["failed_writes"]

    statuses, dropped, corrected = summarise(results)
    print(f"\n{len(rows)} recipes read")
    for status in ("tagged", "already-tagged", "stale", "empty", "malformed", "failed"):
        print(f"  {status:15s} {statuses.get(status, 0):5d}")
    print(f"  mentions dropped (not placed, or unknown id):     {dropped}")
    print(f"  mentions corrected by the checks:                 {corrected}")
    if args.limit is not None and sent >= args.limit:
        print(f"  stopped at --limit {args.limit}; the rest were not sent")
    if args.live:
        print(f"wrote {written} recipe(s), {raced} moved during the run, {failed_writes} failed; record: {args.record}")
    else:
        print(f"would write {statuses.get('tagged', 0)} recipe(s)")
        print("DRY RUN — nothing was written. Re-run with --live --record <file> to apply.")
    return 1 if failed_writes or statuses.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
