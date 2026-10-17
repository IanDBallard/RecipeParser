"""
One-off: give every existing recipe its servings_source, and correct a base_servings that was a guess
(Cayenne scale-sheet redesign; spec D44-D51).

Each recipe is classified from its stored columns: the yield text is rebuilt from ``servings_min``,
``servings_max`` and ``servings_note``; the directions are ``direction_steps``, as ingest reads them, else
the text of ``tokenized_directions`` with its tokens stripped. The
deterministic rule runs first (``core.servings.servings_for_recipe``: the recipe's words, then its tins).
Gemini (``gemini.estimate_servings``) is asked only when both give nothing. Written: ``base_servings``,
``servings_source`` and, rescaled by new/old, ``cook_servings`` (a cook's saved scale keeps its meaning; a
null old base is read as 1, as the client reads it). A tin that does not validate is skipped, not the row;
a ``pans`` or directions column that is not a list (double-encoded jsonb) reads as empty.

Statuses: stale (``derived_rev < body_rev``; the regeneration worker re-derives it), stated (the row
already says ``stated``: never touched, ``--force`` or not), unchanged, set (rule or tin), model (set by
the model), unplaced (no base at all; nothing written), error. The write is conditional on ``id``,
``body_rev`` and ``derived_rev`` being what was read, so a cook's edit mid-run wins.

    # dry run: every proposal to a CSV for the owner to read (rows that move more than 25% say "check")
    python scripts/backfill_servings.py --all-users --review servings-review.csv

    # for real; --record keeps every written row's old base_servings, servings_source, cook_servings
    python scripts/backfill_servings.py --all-users --live --record servings-before.csv

    # undo, from that record
    python scripts/backfill_servings.py --all-users --restore servings-before.csv --live

``--force`` re-asks rows that already have a source (a ``stated`` row is still never touched).
Requires SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and GOOGLE_API_KEY.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError  # noqa: E402

from recipeparser.core.durations import Span  # noqa: E402
from recipeparser.core.fat_tokens import strip_fat_tokens  # noqa: E402
from recipeparser.core.regen import raw_body_column  # noqa: E402
from recipeparser.core.servings import SERVING_WORDS_RE, servings_for_recipe, text_from_span  # noqa: E402
from recipeparser.models import RecipePan  # noqa: E402

PAGE = 500
COLUMNS = (
    "id,title,ingredient_lines,direction_steps,tokenized_directions,servings_min,servings_max,servings_note,"
    "base_servings,servings_source,pans,bake_type,cook_servings,body_rev,derived_rev"
)
CHECK_PCT = 25

Asker = Callable[[str, List[str], List[str]], Optional[int]]


@dataclass
class RowResult:
    id: str
    title: str
    # stale | stated | unchanged | set | model | unplaced | error
    status: str
    old_base: Optional[int] = None
    new_base: Optional[int] = None
    source: Optional[str] = None
    new_cook: Optional[float] = None
    yield_text: str = ""
    used_model: bool = False
    notes: List[str] = field(default_factory=list)
    old_source: Optional[str] = None
    old_cook: Optional[float] = None
    # The old row read as stated: a bare number equal to its base, or a serving word in the note.
    looked_stated: bool = False


def _directions(row: Dict[str, Any]) -> List[str]:
    """The steps ingest classifies: the raw ``direction_steps``, else the tokenized text, tokens stripped."""
    try:
        steps = raw_body_column(row, "direction_steps")
    except TypeError:  # double-encoded jsonb: read as empty, as the tokenized column is
        steps = []
    if steps:
        return steps
    raw = row.get("tokenized_directions")
    if not isinstance(raw, list):
        return []
    return [strip_fat_tokens(str(d["text"])) for d in raw if isinstance(d, dict) and d.get("text")]


def _pans(row: Dict[str, Any]) -> List[RecipePan]:
    """Each tin validated on its own: a bad one is skipped, never the row."""
    raw = row.get("pans")
    if not isinstance(raw, list):
        return []
    pans: List[RecipePan] = []
    for p in raw:
        try:
            pans.append(RecipePan.model_validate(p))
        except ValidationError:
            continue
    return pans


def _looked_stated(row: Dict[str, Any]) -> bool:
    note = row.get("servings_note")
    if note and SERVING_WORDS_RE.search(str(note).lower()):
        return True
    return not note and row.get("servings_min") is not None and row.get("servings_min") == row.get("base_servings")


def _ingredients(row: Dict[str, Any]) -> List[str]:
    lines = row.get("ingredient_lines")
    return [str(x) for x in lines] if isinstance(lines, list) else []


def plan_row(row: Dict[str, Any], ask: Asker, force: bool = False) -> RowResult:
    """What one recipe would be written as, writing nothing."""
    old_base = row.get("base_servings")
    result = RowResult(
        id=row["id"], title=str(row.get("title") or ""), status="set", old_base=old_base,
        old_source=row.get("servings_source"), old_cook=row.get("cook_servings"),
        new_cook=row.get("cook_servings"), looked_stated=_looked_stated(row),
    )
    if (row.get("derived_rev") or 0) < (row.get("body_rev") or 0):
        result.status = "stale"
        return result
    if row.get("servings_source") == "stated":
        result.status = "stated"
        return result
    if not force and row.get("servings_source") is not None:
        # Already sourced (counted, tin or estimated): skipped, so a re-run costs nothing.
        result.status = "unchanged"
        result.new_base, result.source = old_base, row.get("servings_source")
        return result
    try:
        pans = _pans(row)
        result.yield_text = text_from_span(
            Span(row.get("servings_min"), row.get("servings_max"), row.get("servings_note"))
        )
        directions = _directions(row)
        found = servings_for_recipe(result.yield_text, directions, pans, row.get("bake_type"), None)
        if found.base is None:
            result.used_model = True
            estimate = ask(result.title, _ingredients(row), directions)
            found = servings_for_recipe(result.yield_text, directions, pans, row.get("bake_type"), estimate)
    except Exception as exc:  # noqa: BLE001
        result.status = "error"
        result.notes.append(str(exc))
        return result
    if found.base is None:
        result.status = "unplaced"
        return result
    result.new_base, result.source = found.base, found.source
    if found.base == old_base and found.source == row.get("servings_source"):
        result.status = "unchanged"
        return result
    cook = row.get("cook_servings")
    if cook is not None:
        # The client reads a null (or unusable) base as 1, so the saved scale was against 1.
        was = old_base if old_base and old_base > 0 else 1
        result.new_cook = round(cook * found.base / was, 4)
    result.status = "model" if found.source == "estimated" else "set"
    return result


def write_row(sb: Any, row: Dict[str, Any], result: RowResult) -> bool:
    """Write only if the row is still the revision that was read. False when it moved."""
    payload: Dict[str, Any] = {"base_servings": result.new_base, "servings_source": result.source}
    if result.new_cook != row.get("cook_servings"):
        payload["cook_servings"] = result.new_cook
    response = (
        sb.table("recipes")
        .update(payload)
        .eq("id", row["id"])
        .eq("body_rev", row.get("body_rev") or 0)
        .eq("derived_rev", row.get("derived_rev") or 0)
        .execute()
    )
    return bool(response.data)


def _moved_pct(r: RowResult) -> Optional[int]:
    if r.status in ("stale", "stated", "unplaced", "error") or not r.old_base or r.new_base is None:
        return None
    return round(abs(r.new_base - r.old_base) / r.old_base * 100)


def write_review(path: Path, results: Sequence[RowResult]) -> None:
    """The owner's file to read before any live run, one line per recipe."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "title", "status", "old_base", "new_base", "source", "yield", "new_cook",
                         "used_model", "moved_pct", "flag"])
        for r in sorted(results, key=lambda r: r.id):
            pct = _moved_pct(r)
            writer.writerow([
                r.id, r.title, r.status, _cell(r.old_base), _cell(r.new_base), r.source or "", r.yield_text,
                _cell(r.new_cook), "yes" if r.used_model else "", "" if pct is None else pct,
                "check" if _check(r, pct) else "",
            ])


def _check(r: RowResult, pct: Optional[int]) -> bool:
    """Moves more than CHECK_PCT, or read as stated and now falls to the tins or the model (same base or not)."""
    if pct is not None and pct > CHECK_PCT:
        return True
    return r.status in ("set", "model") and r.looked_stated and r.source in ("tin", "estimated")


def _cell(value: Any) -> Any:
    return "" if value is None else value


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
                sb.table("recipes").update({
                    "base_servings": int(rec["base_servings"]) if rec["base_servings"] != "" else None,
                    "servings_source": rec["servings_source"] or None,
                    "cook_servings": float(rec["cook_servings"]) if rec["cook_servings"] != "" else None,
                }).eq("id", rec["id"]).execute()
            restored += 1
    print(f"{'restored' if live else 'would restore'} {restored} recipe(s) from {path}")
    if not live:
        print("DRY RUN — nothing was written. Re-run with --live to apply.")
    return 0


STATUSES = ("stale", "stated", "unchanged", "set", "model", "unplaced", "error")


def main() -> int:
    ap = argparse.ArgumentParser(description="Give existing recipes their servings_source and a sound base_servings.")
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--user-id", help="The owner whose library to process.")
    who.add_argument("--all-users", action="store_true", help="Every library: shared copies are rows of their own.")
    ap.add_argument("--recipe-id", help="Process this one recipe only.")
    ap.add_argument("--limit", type=int, help="Stop after this many recipes have been sent to Gemini.")
    ap.add_argument("--workers", type=int, default=4, help="Gemini calls in flight at once (default 4).")
    ap.add_argument("--review", type=Path, help="CSV of every proposal.")
    ap.add_argument("--live", action="store_true", help="Actually write. Omit for a dry run.")
    ap.add_argument("--record", type=Path, help="CSV of each written row's id and old base_servings, "
                                                "servings_source and cook_servings (also refused writes).")
    ap.add_argument("--restore", type=Path, help="Write this record's three columns back; nothing else.")
    ap.add_argument("--force", action="store_true", help="Re-ask rows that already have a source "
                                                         "(a stated row is still never touched).")
    ap.add_argument("--verbose", action="store_true", help="Print every written recipe's change.")
    ap.add_argument("--progress", type=int, default=50, help="Print a progress line every N recipes (0: none).")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if args.live and not args.restore and not args.record:
        raise SystemExit("--live needs --record: the record is the only way back.")

    from supabase import create_client  # noqa: PLC0415
    sb = create_client(*_creds())
    if args.restore:
        return _restore(sb, args.restore, args.live)

    from recipeparser import client as gemini_client  # noqa: PLC0415
    from recipeparser.core.rate_limiter import GlobalRateLimiter  # noqa: PLC0415
    from recipeparser.gemini import estimate_servings  # noqa: PLC0415
    if gemini_client is None:
        raise SystemExit("GOOGLE_API_KEY must be set.")
    limiter = GlobalRateLimiter()

    def ask(title: str, ingredients: List[str], directions: List[str]) -> Optional[int]:
        return estimate_servings(gemini_client, title, ingredients, directions, limiter=limiter)

    rows = _read_rows(sb, args.user_id, args.recipe_id)
    by_id = {r["id"]: r for r in rows}

    # Probe every row with a stub model first (cheap), so --limit counts the recipes that will call
    # the real one. A row that never asks is final after the probe.
    ready: List[RowResult] = []
    todo: List[Dict[str, Any]] = []
    sent = 0
    for row in rows:
        called: List[bool] = []

        def stub(t: str, i: List[str], d: List[str], called: List[bool] = called) -> Optional[int]:
            called.append(True)
            return None

        probe = plan_row(row, stub, force=args.force)
        if not called:
            ready.append(probe)
        elif args.limit is None or sent < args.limit:
            todo.append(row)
            sent += 1
        # else: over the limit, left unplanned and unwritten.

    writer = None
    record_fh = None
    if args.live:
        record_fh = args.record.open("w", newline="", encoding="utf-8")
        writer = csv.writer(record_fh)
        writer.writerow(["id", "base_servings", "servings_source", "cook_servings"])
        record_fh.flush()

    results: List[RowResult] = []
    counts = {"written": 0, "raced": 0, "failed_writes": 0, "done": 0}
    total = len(ready) + len(todo)
    started = time.monotonic()

    def finish(r: RowResult) -> None:
        """Report, record and write one recipe the moment it is planned."""
        results.append(r)
        counts["done"] += 1
        if r.status == "error":
            print(f"  ERROR   {r.id}  {r.title!r}: {' | '.join(r.notes)}")
        elif r.status in ("set", "model"):
            if args.verbose:
                print(f"  {r.title!r} ({r.id}): {r.old_base} -> {r.new_base} [{r.source}]  yield: {r.yield_text!r}")
            if args.live:
                assert writer is not None and record_fh is not None
                # The record line is written, and flushed, before the row: the record is the way back.
                writer.writerow([r.id, _cell(r.old_base), r.old_source or "", _cell(r.old_cook)])
                record_fh.flush()
                try:
                    if write_row(sb, by_id[r.id], r):
                        counts["written"] += 1
                    else:
                        counts["raced"] += 1
                        print(f"  MOVED   {r.id}  {r.title!r}: edited during the run; left for the regeneration worker")
                except Exception as exc:  # noqa: BLE001
                    counts["failed_writes"] += 1
                    print(f"  FAILED to write {r.id} {r.title!r}: {exc}")
        if args.progress and (counts["done"] % args.progress == 0 or counts["done"] == total):
            minutes = (time.monotonic() - started) / 60
            print(f"progress: {counts['done']}/{total} planned, {counts['written']} written, {minutes:.1f} min")

    for r in ready:
        finish(r)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(plan_row, r, ask, args.force) for r in todo]
        for future in as_completed(futures):
            finish(future.result())
    if record_fh:
        record_fh.close()
    if args.review:
        write_review(args.review, results)

    statuses = Counter(r.status for r in results)
    print(f"\n{len(rows)} recipes read")
    for status in STATUSES:
        print(f"  {status:10s} {statuses.get(status, 0):5d}")
    print(f"  used the model: {sum(1 for r in results if r.used_model)}")
    if args.limit is not None and sent >= args.limit:
        print(f"  stopped at --limit {args.limit}; the rest were not sent to the model")
    if args.review:
        print(f"review: {args.review}")
    if args.live:
        print(f"wrote {counts['written']} recipe(s), {counts['raced']} moved during the run, "
              f"{counts['failed_writes']} failed; record: {args.record}")
    else:
        print(f"would write {statuses.get('set', 0) + statuses.get('model', 0)} recipe(s)")
        print("DRY RUN — nothing was written. Re-run with --live --record <file> to apply.")
    return 1 if counts["failed_writes"] or statuses.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
