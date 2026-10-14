"""
One-off: give every existing recipe its tins, bake type, baking step and internal ("core") temperatures
(Cayenne pan-aware scaling D22; RecipeParser pan contract).

Gemini is asked once per recipe (``gemini.tag_pans``) to name the tins the directions bake in, the
bake type, the step that bakes, and which temperatures are internal doneness figures. The answer is
checked (core.pans) and spliced into the stored ``tokenized_directions`` around the words that are
there: no word changes. Written: ``pans``, ``bake_type``, ``bake_step`` and ``tokenized_directions``.

Nothing found: a recipe whose answer changes nothing (no tins, no bake type, nothing to place) gets the
status ``none-found``: it is not written and has no record line. R7 has no "looked, found nothing" marker
on the row, so a rerun asks Gemini about these non-baking recipes again; ``--limit`` is for trial runs,
not for batching.

Skipped: a stale recipe (``derived_rev < body_rev`` -- the regeneration worker re-derives it with
REFINE, which writes all of this itself), one that is already done (a ``bake_type``, any ``pans`` or a
tin token in its directions; ``--force`` looks again, unwrapping its tin tokens first), one with no
directions, and one whose column is not a list. A recipe with a tin the cook edited is never touched,
``--force`` or not. The write is conditional on ``body_rev`` and ``derived_rev`` being what was read, so
a cook's edit mid-run wins.

    # dry run: every proposed tin and core flag to a CSV for the owner to read; writes nothing
    python scripts/backfill_pans.py --all-users --review pans-review.csv

    # for real; --record keeps every rewritten row's old pans, bake type, step and directions
    python scripts/backfill_pans.py --all-users --live --record pans-before.csv

    # undo, from that record; a row whose current tins include an edited one is skipped with a note,
    # so a restore never undoes a cook's later tin edit. The record also holds rows whose write was
    # refused (moved during the run): restoring those writes back what is already there.
    python scripts/backfill_pans.py --all-users --restore pans-before.csv --live

Run order: after the temperature backfill (its tokens are what core flags are set on).
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

from recipeparser.core.fat_tokens import (  # noqa: E402
    TEMP_PREFIX,
    TOKEN_RE,
    is_pan_id,
    is_temp_id,
    parse_temp_value,
    strip_fat_tokens,
)
from recipeparser.core.pans import (  # noqa: E402
    check_bake,
    check_pan_tokens,
    heading_steps,
    mark_core,
    normalise_pans,
    place_pan_mentions,
)
from recipeparser.models import PanAnswer, TokenizedDirection  # noqa: E402

PAGE = 500
COLUMNS = (
    "id,title,ingredient_lines,structured_ingredients,tokenized_directions,body_rev,derived_rev,"
    "pans,bake_type,bake_step,source_uom_system,source_uom_system_detected"
)

Asker = Callable[[List[str], List[str], Optional[str]], PanAnswer]


@dataclass
class RowResult:
    id: str
    title: str
    # tagged | none-found | already-done | cook-edited | stale | empty | malformed | failed
    status: str
    directions: List[TokenizedDirection] = field(default_factory=list)
    pans: List[Dict[str, Any]] = field(default_factory=list)
    bake_type: Optional[str] = None
    bake_step: Optional[int] = None  # 0-based
    before: Any = None  # the stored tokenized_directions
    before_pans: Any = None
    before_bake_type: Any = None
    before_bake_step: Any = None
    notes: List[str] = field(default_factory=list)  # every warning, drop and core note
    error: str = ""


def _unwrap_pan_tokens(text: str) -> str:
    return TOKEN_RE.sub(lambda m: m.group(2) if is_pan_id(m.group(1)) else m.group(0), text)


def _ingredients(row: Dict[str, Any]) -> List[str]:
    lines = row.get("ingredient_lines")
    if isinstance(lines, list) and lines:
        return [str(x) for x in lines]
    out: List[str] = []
    for ing in row.get("structured_ingredients") or []:
        if isinstance(ing, dict) and ing.get("fallback_string"):
            out.append(str(ing["fallback_string"]))
    return out


def _group_by_step(items: Sequence[Any], size: int, what: str) -> Tuple[Dict[int, List[Any]], List[str]]:
    """Group model items by their 1-based ``step`` as 0-based keys. A step outside the recipe is left
    out and reported."""
    grouped: Dict[int, List[Any]] = {}
    ignored: List[str] = []
    for item in items:
        i = item.step - 1
        if 0 <= i < size:
            grouped.setdefault(i, []).append(item)
        else:
            ignored.append(f"{what} ignored: step {item.step} is not a step of this recipe ({item.quote!r})")
    return grouped, ignored


def plan_row(row: Dict[str, Any], ask: Asker, force: bool = False) -> RowResult:
    """What one recipe would be written as, writing nothing."""
    result = RowResult(
        id=row["id"], title=str(row.get("title") or ""), status="tagged",
        before=row.get("tokenized_directions"), before_pans=row.get("pans"),
        before_bake_type=row.get("bake_type"), before_bake_step=row.get("bake_step"),
    )
    raw = row.get("tokenized_directions")
    if (row.get("derived_rev") or 0) < (row.get("body_rev") or 0):
        result.status = "stale"
        return result
    if not isinstance(raw, list):
        result.status = "malformed"
        return result
    try:
        old = [TokenizedDirection.model_validate(d) for d in raw]
    except Exception as exc:  # noqa: BLE001
        result.status, result.error = "malformed", str(exc)
        return result
    stored_pans = row.get("pans") or []
    if any(isinstance(p, dict) and p.get("edited") for p in stored_pans):
        result.status = "cook-edited"
        return result
    has_token = any(is_pan_id(m.group(1)) for d in old for m in TOKEN_RE.finditer(d.text))
    if not force and (row.get("bake_type") is not None or stored_pans or has_token):
        result.status = "already-done"
        return result
    if not old:
        result.status = "empty"
        return result
    texts = [_unwrap_pan_tokens(d.text) for d in old]
    plain = [strip_fat_tokens(t) for t in texts]
    try:
        answer = ask(plain, _ingredients(row), row.get("source_uom_system") or row.get("source_uom_system_detected"))
    except Exception as exc:  # noqa: BLE001
        result.status, result.error = "failed", str(exc)
        return result

    pans, notes = normalise_pans(answer.pans)
    ids = {p.id for p in pans}
    mentions, ignored = _group_by_step([m for m in answer.mentions if m.pan_id in ids], len(texts), "tin mention")
    cores, ignored_core = _group_by_step(answer.core, len(texts), "core temperature")
    notes += ignored + ignored_core
    for i, text in enumerate(texts):
        step = old[i].step
        if i in mentions:
            text, dropped = place_pan_mentions(text, [(m.quote, m.context, m.pan_id) for m in mentions[i]])
            notes += [f"step {step}: {line}" for line in dropped]
        if i in cores:
            text, core_notes = mark_core(text, [c.quote for c in cores[i]])
            notes += [f"step {step}: {line}" for line in core_notes]
        texts[i] = text
    directions = [TokenizedDirection(step=d.step, text=t) for d, t in zip(old, texts)]
    directions, w = check_pan_tokens(directions, ids, "\n".join(plain))
    notes += w
    bake_type, bake_step = check_bake(answer.bake_type, answer.bake_step, directions, heading_steps(directions))
    result.directions = directions
    result.pans = [p.model_dump() for p in pans]
    result.bake_type, result.bake_step = bake_type, bake_step
    result.notes = notes
    if (result.pans == stored_pans and bake_type == row.get("bake_type") and bake_step == row.get("bake_step")
            and [d.text for d in directions] == [d.text for d in old]):
        result.status = "none-found"  # nothing to write: no record line, no write
    return result


def write_row(sb: Any, row: Dict[str, Any], result: RowResult) -> bool:
    """Write the four columns only if the row is still the revision that was read. False when it moved."""
    response = (
        sb.table("recipes")
        .update({
            "pans": result.pans,
            "bake_type": result.bake_type,
            "bake_step": result.bake_step,
            "tokenized_directions": [d.model_dump() for d in result.directions],
        })
        .eq("id", row["id"])
        .eq("body_rev", row.get("body_rev") or 0)
        .eq("derived_rev", row.get("derived_rev") or 0)
        .execute()
    )
    return bool(response.data)


def _step_tokens(text: str) -> Tuple[List[str], List[str]]:
    """The tin words and the core temperature words in one step's text."""
    pan: List[str] = []
    core: List[str] = []
    for m in TOKEN_RE.finditer(text):
        if is_pan_id(m.group(1)):
            pan.append(f"{m.group(1)}: {m.group(2)}")
        elif is_temp_id(m.group(1)):
            parsed = parse_temp_value(m.group(1)[len(TEMP_PREFIX):])
            if parsed is not None and parsed.core:
                core.append(m.group(2))
    return pan, core


def write_review(path: Path, results: Sequence[RowResult]) -> None:
    """The owner's file to read before any live run. A tagged recipe has one line per step that has a
    tin or core token (one blank-step line if none), with the step's new text; every other recipe has
    one line saying why it was skipped. ``bake_step_1based`` counts like ``step``. ``notes`` holds the
    drops and warnings (or the error) and is on the recipe's first line."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "title", "status", "pans", "bake_type", "bake_step_1based", "step", "step_text",
                         "pan_tokens", "core_tokens", "notes"])
        for r in sorted(results, key=lambda r: r.id):
            if r.status != "tagged":
                writer.writerow([r.id, r.title, r.status, "", "", "", "", "", "", "", r.error])
                continue
            head: List[Any] = [r.id, r.title, r.status, json.dumps(r.pans), r.bake_type or "",
                               "" if r.bake_step is None else r.bake_step + 1]
            lines: List[List[Any]] = []
            for d in r.directions:
                pan, core = _step_tokens(d.text)
                if pan or core:
                    lines.append([d.step, d.text, " | ".join(pan), " | ".join(core)])
            for n, tail in enumerate(lines or [["", "", "", ""]]):
                writer.writerow(head + tail + [" | ".join(r.notes) if n == 0 else ""])


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
    restored = skipped = 0
    with path.open(newline="", encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            current = sb.table("recipes").select("pans").eq("id", rec["id"]).execute().data or []
            now = (current[0].get("pans") if current else None) or []
            if any(isinstance(p, dict) and p.get("edited") for p in now):
                skipped += 1
                print(f"  SKIPPED {rec['id']}  {rec.get('title', '')!r}: a tin was edited since the backfill")
                continue
            if live:
                sb.table("recipes").update({
                    "pans": json.loads(rec["pans"]),
                    "bake_type": json.loads(rec["bake_type"]),
                    "bake_step": json.loads(rec["bake_step"]),
                    "tokenized_directions": json.loads(rec["tokenized_directions"]),
                }).eq("id", rec["id"]).execute()
            restored += 1
    print(f"{'restored' if live else 'would restore'} {restored} recipe(s) from {path}"
          f"{f', skipped {skipped} with an edited tin' if skipped else ''}")
    if not live:
        print("DRY RUN — nothing was written. Re-run with --live to apply.")
    return 0


STATUSES = ("tagged", "none-found", "already-done", "cook-edited", "stale", "empty", "malformed", "failed")


def main() -> int:
    ap = argparse.ArgumentParser(description="Give existing recipes their tins, bake type and step and core flags.")
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--user-id", help="The owner whose library to tag.")
    who.add_argument("--all-users", action="store_true", help="Every library: shared copies are rows of their own.")
    ap.add_argument("--recipe-id", help="Tag this one recipe only.")
    ap.add_argument("--limit", type=int, help="Stop after this many recipes have been sent to Gemini.")
    ap.add_argument("--workers", type=int, default=4, help="Gemini calls in flight at once (default 4).")
    ap.add_argument("--review", type=Path, help="CSV of every proposal: id, title, pans, bake_type, bake_step, step, "
                                                "pan_tokens, core_tokens, notes.")
    ap.add_argument("--live", action="store_true", help="Actually write. Omit for a dry run.")
    ap.add_argument("--record", type=Path, help="CSV of each rewritten row's id, title and old pans, bake_type, "
                                                "bake_step and tokenized_directions. It also holds rows whose "
                                                "write was refused (moved during the run).")
    ap.add_argument("--restore", type=Path, help="Write this record's four columns back; nothing else. A row "
                                                 "whose current tins include an edited one is skipped.")
    ap.add_argument("--force", action="store_true",
                    help="Look again at recipes that already have a bake type, tins or a tin token "
                         "(a cook-edited tin is still never touched).")
    ap.add_argument("--verbose", action="store_true", help="Print every tagged recipe's new steps.")
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
    from recipeparser.gemini import tag_pans  # noqa: PLC0415
    if gemini_client is None:
        raise SystemExit("GOOGLE_API_KEY must be set.")
    limiter = GlobalRateLimiter()

    def ask(steps: List[str], ingredients: List[str], system: Optional[str]) -> PanAnswer:
        return tag_pans(steps, ingredients, system, gemini_client, limiter=limiter)

    rows = _read_rows(sb, args.user_id, args.recipe_id)
    by_id = {r["id"]: r for r in rows}

    # Probe every row with a stub model first (cheap), so --limit counts the recipes that will call
    # the real one. A row that never asks is final after the probe.
    ready: List[RowResult] = []
    todo: List[Dict[str, Any]] = []
    sent = 0
    for row in rows:
        called: List[bool] = []
        def stub(s: List[str], i: List[str], sy: Optional[str], called: List[bool] = called) -> PanAnswer:
            called.append(True)
            return PanAnswer()

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
        writer.writerow(["id", "title", "pans", "bake_type", "bake_step", "tokenized_directions"])
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
        elif r.status == "tagged":
            if args.verbose:
                print(f"\n  {r.title!r}  ({r.id})  bake: {r.bake_type} @ {r.bake_step}  tins: {len(r.pans)}")
                for d in r.directions:
                    print(f"    {d.step}. {d.text}")
                for line in r.notes:
                    print(f"    note: {line}")
            if args.live:
                assert writer is not None and record_fh is not None
                # The record line is written, and flushed, before the row: the record is the way back.
                writer.writerow([r.id, r.title, json.dumps(r.before_pans if r.before_pans is not None else []),
                                 json.dumps(r.before_bake_type), json.dumps(r.before_bake_step),
                                 json.dumps(r.before)])
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
    tins = sum(len(r.pans) for r in results)
    notes = sum(len(r.notes) for r in results)
    print(f"\n{len(rows)} recipes read")
    for status in STATUSES:
        print(f"  {status:15s} {statuses.get(status, 0):5d}")
    print(f"  tins found:                                       {tins}")
    print(f"  notes (dropped mentions, core not marked, ...):   {notes}")
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
