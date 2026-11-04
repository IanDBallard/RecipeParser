"""
One-off: give existing recipes their ``yield_spec`` (servings, pieces or component, and the container a
pie or pastry makes), and settle each saved kitchen scale once (Cayenne yield kinds D77, D83, 9, 11.4;
docs/superpowers/specs/2026-11-03-yield-kinds-scaling-design.md).

Gemini is asked once per recipe through the yields-only prompt (``gemini.tag_yield``) with the stored
structured ingredients (headings included), steps (tin tokens unwrapped, fat tokens stripped), tins and
bake type; the answer is checked by ``core.yields.check_yield``. Written: ``yield_spec``, plus the
saved-scale settlement of 9 in the same row write:

- servings: the saved scale is unchanged;
- component with a chosen tin (``cook_pans``): the tin is kept; ``cook_servings`` and ``cook_scale_by``
  are cleared;
- component with a servings scale only: cleared (D83): ``cook_servings``, ``cook_pans``, ``cook_scale_by``;
- pieces with a servings scale, only with ``--settle-pieces`` (stage 2, once the client reads
  ``cook_yield.pieces``): ``cook_yield.pieces.count`` = max(1, round(count x cook_servings / base)), the
  size unchanged; ``cook_servings`` and ``cook_pans`` cleared. Without the flag a pieces row's scale is
  left for stage 2, and the review says so.

Skipped: a stale recipe (``derived_rev < body_rev``: REFINE re-derives it), one whose ``yield_spec`` the
cook edited (never touched, ``--force`` or not), one that already has a ``yield_spec`` (``--force`` asks
again), and one whose stored columns cannot be read (``malformed``). The write is conditional on
``body_rev`` and ``derived_rev`` being what was read, so a cook's edit mid-run wins.

Before the Cayenne migration is live, ``yield_spec`` and ``cook_yield`` do not exist: a dry run says so
and reads both as null; a live run stops before asking or writing anything.

    # dry run, scoped: every proposed spec and settlement to a CSV for the owner to read; writes nothing
    python scripts/backfill_yields.py --all-users --ids-file scope.txt --review yields-review.csv

    # for real; --record keeps every rewritten row's old yield_spec and cook_* columns
    python scripts/backfill_yields.py --all-users --ids-file scope.txt --live --record yields-before.csv

    # undo, from that record; a row whose current yield_spec the cook edited is skipped with a note
    python scripts/backfill_yields.py --all-users --restore yields-before.csv --live

Requires SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and GOOGLE_API_KEY.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recipeparser.core.fat_tokens import strip_fat_tokens  # noqa: E402
from recipeparser.core.yields import check_yield  # noqa: E402
from recipeparser.models import (  # noqa: E402
    RecipePan,
    StructuredIngredient,
    TokenizedDirection,
    YieldAnswer,
)


def _load_backfill_pans() -> Any:
    """The pans backfill's helpers (tin-token unwrap, ids file, credentials), loaded once."""
    if "backfill_pans" in sys.modules:
        return sys.modules["backfill_pans"]
    spec = importlib.util.spec_from_file_location("backfill_pans", ROOT / "scripts" / "backfill_pans.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["backfill_pans"] = module
    spec.loader.exec_module(module)
    return module


bp = _load_backfill_pans()

PAGE = 500
NEW_COLUMNS = ("yield_spec", "cook_yield")
COLUMNS = (
    "id,title,structured_ingredients,tokenized_directions,body_rev,derived_rev,pans,bake_type,yield_spec,"
    "cook_servings,cook_pans,cook_scale_by,cook_yield,base_servings,source_uom_system,source_uom_system_detected"
)
BASE_COLUMNS = ",".join(c for c in COLUMNS.split(",") if c not in NEW_COLUMNS)
RECORD_COLUMNS = ("yield_spec", "cook_servings", "cook_pans", "cook_scale_by", "cook_yield")
MISSING_NOTICE = "yield_spec/cook_yield are not on this database yet: dry run reads both as null"
PIECES_LEFT = "pieces scale left for stage 2"
NO_PIECE_COUNT = "saved scale left as it is: no piece count"

Asker = Callable[[List[str], List[StructuredIngredient], List[RecipePan], Optional[str]], YieldAnswer]
STATUSES = ("tagged", "none-found", "already-done", "cook-edited", "stale", "malformed", "failed")


@dataclass
class RowResult:
    id: str
    title: str
    status: str  # one of STATUSES
    spec: Optional[Dict[str, Any]] = None
    settled: Dict[str, Any] = field(default_factory=dict)  # the cook_* keys written beside yield_spec
    settled_text: str = ""  # the clearance or conversion in words; "" when nothing settles
    before: Dict[str, Any] = field(default_factory=dict)  # RECORD_COLUMNS as read
    lines: Dict[str, str] = field(default_factory=dict)  # ingredient id -> its words, for the review
    notes: List[str] = field(default_factory=list)
    error: str = ""


def _set(v: Any) -> bool:
    return v is not None and v != [] and v != {}


def _base(base_servings: Any) -> float:
    """The client reads a null or non-positive base as 1 (backfill_servings.py's precedent)."""
    try:
        b = float(base_servings)
    except (TypeError, ValueError):
        return 1.0
    return b if b > 0 else 1.0


def _round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def settle_scale(kind: str, spec: dict, cook_servings, cook_pans, cook_scale_by, cook_yield, base_servings) -> dict:
    """The cook_* values to write beside yield_spec (9). Returns only the keys that change."""
    target: Dict[str, Any] = {}
    if kind == "component":
        if _set(cook_pans):
            target = {"cook_servings": None, "cook_scale_by": None}  # the tin kept
        elif cook_servings is not None:
            target = {"cook_servings": None, "cook_pans": None, "cook_scale_by": None}  # D83: cleared
    elif kind == "pieces" and cook_servings is not None:
        pieces = (spec or {}).get("pieces") or {}
        count = pieces.get("count")
        if isinstance(count, (int, float)) and not isinstance(count, bool) and count >= 1:
            new_count = max(1, _round_half_up(count * float(cook_servings) / _base(base_servings)))
            merged = dict(cook_yield) if isinstance(cook_yield, dict) else {}
            merged["pieces"] = {"count": new_count, "weight_g": pieces.get("weight_g"),
                                "diameter_mm": pieces.get("diameter_mm")}
            target = {"cook_yield": merged, "cook_servings": None, "cook_pans": None}
    current = {"cook_servings": cook_servings, "cook_pans": cook_pans, "cook_scale_by": cook_scale_by,
               "cook_yield": cook_yield}
    return {k: v for k, v in target.items() if current[k] != v}


def _settled_text(kind: str, row: Dict[str, Any], settled: Dict[str, Any]) -> str:
    if not settled:
        return ""
    base = row.get("base_servings")
    if kind == "pieces":
        p = settled["cook_yield"]["pieces"]
        return f"converted: servings {row.get('cook_servings')} of {base} -> {p['count']} piece(s)"
    if not _set(row.get("cook_pans")):
        return f"cleared: servings {row.get('cook_servings')} of {base} (no tin)"
    return f"cleared: servings {row.get('cook_servings')} of {base}; tin kept"


def yield_inputs(row: Dict[str, Any]) -> Tuple[List[str], List[StructuredIngredient], List[RecipePan]]:
    """The yields-only prompt's inputs from a stored row, as probe_yield_kinds builds them. Raises on a
    column that cannot be read."""
    raw = row.get("tokenized_directions")
    if not isinstance(raw, list):
        raise ValueError("tokenized_directions is not a list")
    steps = [strip_fat_tokens(bp._unwrap_pan_tokens(TokenizedDirection.model_validate(d).text)) for d in raw]
    ings = row.get("structured_ingredients")
    if ings is None:
        ings = []
    if not isinstance(ings, list):
        raise ValueError("structured_ingredients is not a list")
    ingredients = [StructuredIngredient.model_validate(x) for x in ings]
    pans_raw = row.get("pans")
    if pans_raw is None:
        pans_raw = []
    if not isinstance(pans_raw, list):
        raise ValueError("pans is not a list")
    pans = [RecipePan.model_validate(p) for p in pans_raw]
    return steps, ingredients, pans


def plan_row(row: Dict[str, Any], ask: Asker, force: bool = False, settle_pieces: bool = False) -> RowResult:
    """What one recipe would be written as, writing nothing."""
    result = RowResult(id=row["id"], title=str(row.get("title") or ""), status="tagged",
                       before={c: row.get(c) for c in RECORD_COLUMNS})
    stored = row.get("yield_spec")
    if (row.get("derived_rev") or 0) < (row.get("body_rev") or 0):
        result.status = "stale"
        return result
    if stored is not None and not isinstance(stored, dict):
        result.status, result.error = "malformed", "yield_spec is not an object"
        return result
    if isinstance(stored, dict) and stored.get("edited") is True:
        result.status = "cook-edited"
        return result
    if stored is not None and not force:
        result.status = "already-done"
        return result
    try:
        steps, ingredients, pans = yield_inputs(row)
    except Exception as exc:  # noqa: BLE001
        result.status, result.error = "malformed", str(exc)
        return result
    result.lines = {i.id: i.fallback_string for i in ingredients}
    try:
        answer = ask(steps, ingredients, pans, row.get("bake_type"))
    except Exception as exc:  # noqa: BLE001
        result.status, result.error = "failed", str(exc)
        return result
    spec, warnings = check_yield(answer, ingredients, pans)
    result.notes = list(warnings)
    if spec is None:
        result.status = "none-found"
        return result
    result.spec = spec
    kind = spec["kind"]
    if kind == "pieces" and row.get("cook_servings") is not None:
        if not settle_pieces:
            result.notes.append(PIECES_LEFT)
        elif not (spec.get("pieces") or {}).get("count"):
            result.notes.append(NO_PIECE_COUNT)
    if kind != "pieces" or settle_pieces:
        result.settled = settle_scale(kind, spec, row.get("cook_servings"), row.get("cook_pans"),
                                      row.get("cook_scale_by"), row.get("cook_yield"), row.get("base_servings"))
    result.settled_text = _settled_text(kind, row, result.settled)
    if spec == stored and not result.settled:
        result.status = "none-found"  # nothing to write: no record line, no write
    return result


def write_row(sb: Any, row: Dict[str, Any], result: RowResult) -> bool:
    """Write yield_spec and the settlement only if the row is still the revision that was read."""
    response = (
        sb.table("recipes")
        .update({"yield_spec": result.spec, **result.settled})
        .eq("id", row["id"])
        .eq("body_rev", row.get("body_rev") or 0)
        .eq("derived_rev", row.get("derived_rev") or 0)
        .execute()
    )
    return bool(response.data)


def _lines_text(ids: Sequence[str], words: Dict[str, str]) -> str:
    return " | ".join(f"{i}: {words.get(i, '?')}" for i in ids)


def write_review(path: Path, results: Sequence[RowResult]) -> None:
    """The owner's file to read before any live run: one line per recipe."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "title", "status", "kind", "container", "area", "pieces", "component_lines",
                         "topping_lines", "settled", "notes"])
        for r in sorted(results, key=lambda r: r.id):
            notes = " | ".join(r.notes + ([r.error] if r.error else []))
            if r.spec is None:
                writer.writerow([r.id, r.title, r.status, "", "", "", "", "", "", "", notes])
                continue
            c = r.spec.get("container")
            shape = None if c is None else {k: v for k, v in c.items() if k not in ("component_lines",
                                                                                  "topping_lines")}
            writer.writerow([
                r.id, r.title, r.status, r.spec["kind"],
                "" if shape is None else json.dumps(shape),
                "" if r.spec.get("area") is None else json.dumps(r.spec["area"]),
                "" if r.spec.get("pieces") is None else json.dumps(r.spec["pieces"]),
                "" if c is None else _lines_text(c.get("component_lines") or [], r.lines),
                "" if c is None else _lines_text(c.get("topping_lines") or [], r.lines),
                r.settled_text, notes,
            ])


def _is_missing_column(exc: Exception) -> bool:
    text = str(exc)
    return any(c in text for c in NEW_COLUMNS) and ("does not exist" in text or "42703" in text)


def _read_rows(sb: Any, user_id: Optional[str], recipe_id: Optional[str], columns: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        query = sb.table("recipes").select(columns)
        if user_id:
            query = query.eq("user_id", user_id)
        if recipe_id:
            query = query.eq("id", recipe_id)
        page = query.order("id").range(offset, offset + PAGE - 1).execute().data or []
        if not page:
            return rows
        rows.extend(page)
        offset += PAGE


def read_rows(sb: Any, user_id: Optional[str], recipe_id: Optional[str], live: bool) -> List[Dict[str, Any]]:
    """Every row with COLUMNS. Before the migration (no yield_spec or cook_yield): a dry run reads both as
    null, with a notice; a live run stops."""
    try:
        return _read_rows(sb, user_id, recipe_id, COLUMNS)
    except Exception as exc:  # noqa: BLE001
        if not _is_missing_column(exc):
            raise
        if live:
            raise SystemExit("yield_spec/cook_yield are not on this database yet: apply the Cayenne "
                             f"migration before a live run ({exc})") from exc
        print(MISSING_NOTICE)
    rows = _read_rows(sb, user_id, recipe_id, BASE_COLUMNS)
    for r in rows:
        r.setdefault("yield_spec", None)
        r.setdefault("cook_yield", None)
    return rows


def _restore(sb: Any, path: Path, live: bool) -> int:
    restored = skipped = 0
    with path.open(newline="", encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            current = sb.table("recipes").select("yield_spec").eq("id", rec["id"]).execute().data or []
            now = current[0].get("yield_spec") if current else None
            if isinstance(now, dict) and now.get("edited") is True:
                skipped += 1
                print(f"  SKIPPED {rec['id']}  {rec.get('title', '')!r}: the yield was edited since the backfill")
                continue
            if live:
                old = {c: json.loads(rec[c]) for c in RECORD_COLUMNS}
                sb.table("recipes").update(old).eq("id", rec["id"]).execute()
            restored += 1
    print(f"{'restored' if live else 'would restore'} {restored} recipe(s) from {path}"
          f"{f', skipped {skipped} with an edited yield' if skipped else ''}")
    if not live:
        print("DRY RUN — nothing was written. Re-run with --live to apply.")
    return 0


def _is_assembly_without_lines(r: RowResult) -> bool:
    if r.spec is None or r.spec["kind"] == "component":
        return False
    c = r.spec.get("container")
    return c is not None and not c.get("component_lines")


def main() -> int:
    ap = argparse.ArgumentParser(description="Give existing recipes their yield_spec and settle each saved scale.")
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--user-id", help="The owner whose library to classify.")
    who.add_argument("--all-users", action="store_true", help="Every library: shared copies are rows of their own.")
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--recipe-id", help="Classify this one recipe only.")
    which.add_argument("--ids-file", type=Path,
                       help="Classify only the recipes listed in this file, one id per line (blank lines ignored).")
    ap.add_argument("--limit", type=int, help="Stop after this many recipes have been sent to Gemini.")
    ap.add_argument("--workers", type=int, default=4, help="Gemini calls in flight at once (default 4).")
    ap.add_argument("--review", type=Path, help="CSV of every proposal: id, title, status, kind, container, area, "
                                                "pieces, component_lines, topping_lines, settled, notes.")
    ap.add_argument("--live", action="store_true", help="Actually write. Omit for a dry run.")
    ap.add_argument("--record", type=Path, help="CSV of each rewritten row's id, title and old yield_spec, "
                                                "cook_servings, cook_pans, cook_scale_by and cook_yield (JSON).")
    ap.add_argument("--restore", type=Path, help="Write this record's five columns back; nothing else. A row "
                                                 "whose current yield_spec the cook edited is skipped.")
    ap.add_argument("--force", action="store_true",
                    help="Ask again about recipes that already have a yield_spec (a cook-edited one is never touched).")
    ap.add_argument("--settle-pieces", action="store_true",
                    help="convert a pieces recipe's saved servings scale to a piece count (stage 2, once the "
                         "client reads cook_yield.pieces)")
    ap.add_argument("--progress", type=int, default=50, help="Print a progress line every N recipes (0: none).")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if args.live and not args.restore and not args.record:
        raise SystemExit("--live needs --record: the record is the only way back.")

    from supabase import create_client  # noqa: PLC0415
    sb = create_client(*bp._creds())
    if args.restore:
        return _restore(sb, args.restore, args.live)

    rows = read_rows(sb, args.user_id, args.recipe_id, args.live)

    from recipeparser import client as gemini_client  # noqa: PLC0415
    from recipeparser.core.rate_limiter import GlobalRateLimiter  # noqa: PLC0415
    from recipeparser.gemini import tag_yield  # noqa: PLC0415
    if gemini_client is None:
        raise SystemExit("GOOGLE_API_KEY must be set.")
    limiter = GlobalRateLimiter()

    def ask(steps: List[str], ingredients: List[StructuredIngredient], pans: List[RecipePan],
            bake_type: Optional[str]) -> YieldAnswer:
        return tag_yield(steps, ingredients, pans, bake_type, gemini_client, limiter=limiter)

    if args.ids_file:
        wanted = bp.read_ids(args.ids_file)
        rows = [r for r in rows if r["id"] in wanted]
        missing = len(wanted) - len(rows)
        print(f"--ids-file: {len(wanted)} id(s) listed, {len(rows)} read"
              + (f", {missing} not found" if missing else ""))
    by_id = {r["id"]: r for r in rows}

    # Probe every row with a stub model first (cheap), so --limit counts the recipes that will call the
    # real one. A row that never asks is final after the probe.
    ready: List[RowResult] = []
    todo: List[Dict[str, Any]] = []
    sent = 0
    for row in rows:
        called: List[bool] = []

        def stub(*_a: Any, called: List[bool] = called) -> YieldAnswer:
            called.append(True)
            return YieldAnswer()

        probe = plan_row(row, stub, force=args.force, settle_pieces=args.settle_pieces)
        if not called:
            ready.append(probe)
        elif args.limit is None or sent < args.limit:
            todo.append(row)
            sent += 1

    writer = None
    record_fh = None
    if args.live:
        record_fh = args.record.open("w", newline="", encoding="utf-8")
        writer = csv.writer(record_fh)
        writer.writerow(["id", "title", *RECORD_COLUMNS])
        record_fh.flush()

    results: List[RowResult] = []
    counts = {"written": 0, "raced": 0, "failed_writes": 0, "done": 0}
    total = len(ready) + len(todo)
    started = time.monotonic()

    def finish(r: RowResult) -> None:
        """Report, record and write one recipe the moment it is planned."""
        results.append(r)
        counts["done"] += 1
        if r.status == "failed":
            print(f"  FAILED  {r.id}  {r.title!r}: {r.error}")
        elif r.status == "tagged" and args.live:
            assert writer is not None and record_fh is not None
            # The record line is written, and flushed, before the row: the record is the way back.
            writer.writerow([r.id, r.title, *(json.dumps(r.before[c]) for c in RECORD_COLUMNS)])
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
        futures = [pool.submit(plan_row, r, ask, args.force, args.settle_pieces) for r in todo]
        for future in as_completed(futures):
            finish(future.result())
    if record_fh:
        record_fh.close()
    if args.review:
        write_review(args.review, results)

    statuses = Counter(r.status for r in results)
    kinds = Counter(r.spec["kind"] for r in results if r.status == "tagged" and r.spec)
    print(f"\n{len(rows)} recipes read")
    for status in STATUSES:
        print(f"  {status:15s} {statuses.get(status, 0):5d}")
    print("by kind (tagged):")
    for kind in ("servings", "pieces", "component"):
        print(f"  {kind:15s} {kinds.get(kind, 0):5d}")
    print(f"assemblies with no component lines: {sum(_is_assembly_without_lines(r) for r in results)}")
    settled = sorted((r for r in results if r.status == "tagged" and r.settled_text), key=lambda r: r.id)
    print(f"saved scales settled: {len(settled)}")
    for r in settled:
        print(f"  {r.id}  {r.title!r}: {r.settled_text}")
    left = sum(1 for r in results if PIECES_LEFT in r.notes)
    if left:
        print(f"pieces scales left for stage 2: {left} (--settle-pieces converts them)")
    if args.limit is not None and sent >= args.limit:
        print(f"  stopped at --limit {args.limit}; the rest were not sent to the model")
    if args.review:
        print(f"review: {args.review}")
    if args.live:
        print(f"wrote {counts['written']} recipe(s), {counts['raced']} moved during the run, "
              f"{counts['failed_writes']} failed; record: {args.record}")
    else:
        print(f"would write {statuses.get('tagged', 0)} recipe(s)")
        print("DRY RUN — nothing was written. Re-run with --live --record <file> to apply.")
    return 1 if counts["failed_writes"] or statuses.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
