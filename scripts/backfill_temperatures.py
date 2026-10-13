"""
One-off: wrap every temperature in a library's directions in a temperature token (Cayenne's oven
temperatures design, docs/superpowers/specs/2026-10-12-oven-temperatures-design.md; RecipeParser 9.11.0).

The clear forms ("350°F", "180°C/160°C fan/gas mark 4", "gas 4") are tagged by rule
(core.temperatures); Gemini is asked, once per recipe and only about the steps the rule could not
settle, for bare figures and described ovens ("bake at 180", "a moderate oven"), with the recipe's
measuring system (the cook's, else the detected one) as its guide. Only ``tokenized_directions`` is
written, and no word changes: tokens are spliced around the text that is there.

Skipped: a stale recipe (``derived_rev < body_rev`` -- the regeneration worker re-derives it with
9.11.0's REFINE, which writes the tokens itself), one that already has a temperature token
(``--force`` looks again), one with no directions, and one whose column is not a list. The write is
conditional on ``body_rev`` and ``derived_rev`` being what was read, so a cook's edit mid-run wins.

    # dry run: every proposed token to a CSV for the owner to read; writes nothing
    python scripts/backfill_temperatures.py --all-users --review temperatures-review.csv

    # rule only, no Gemini calls
    python scripts/backfill_temperatures.py --all-users --no-model --review temperatures-review.csv

    # for real; --record keeps every rewritten row's old tokenized_directions
    python scripts/backfill_temperatures.py --all-users --live --record temperatures-before.csv

    # undo, from that record
    python scripts/backfill_temperatures.py --all-users --restore temperatures-before.csv --live

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY; GOOGLE_API_KEY unless --no-model.
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

from recipeparser.core.fat_tokens import TOKEN_RE, is_temp_id  # noqa: E402
from recipeparser.core.temperatures import (  # noqa: E402
    find_temperatures,
    needs_model,
    place_temperature_mentions,
    repair_tokens,
    splice,
)
from recipeparser.models import TemperatureMention, TokenizedDirection  # noqa: E402

PAGE = 500
COLUMNS = "id,title,tokenized_directions,body_rev,derived_rev,source_uom_system,source_uom_system_detected"

Asker = Callable[[List[str], Optional[str]], List[TemperatureMention]]


@dataclass
class RowResult:
    id: str
    title: str
    status: str  # tagged | already-tagged | none-found | stale | empty | malformed | failed
    directions: List[TokenizedDirection] = field(default_factory=list)
    before: Any = None
    proposals: List[Tuple[int, str, str, str]] = field(default_factory=list)  # step, words, value, by
    dropped: List[str] = field(default_factory=list)
    error: str = ""


def plan_repair(row: Dict[str, Any]) -> RowResult:
    """``--repair``: the temperature tokens a recipe already has, mended (core.temperatures.repair_tokens:
    a bare figure takes in its "degrees", one end of a range is unwrapped). No model, no new token."""
    result = RowResult(id=row["id"], title=str(row.get("title") or ""), status="tagged",
                       before=row.get("tokenized_directions"))
    raw = row.get("tokenized_directions")
    if not isinstance(raw, list):
        result.status = "malformed"
        return result
    if (row.get("derived_rev") or 0) < (row.get("body_rev") or 0):
        result.status = "stale"
        return result
    try:
        old = [TokenizedDirection.model_validate(d) for d in raw]
    except Exception as exc:  # noqa: BLE001
        result.status, result.error = "malformed", str(exc)
        return result
    if not old:
        result.status = "empty"
        return result
    texts = []
    for d in old:
        text, changes = repair_tokens(d.text)
        texts.append(text)
        result.proposals += [(d.step, change, "", "repair") for change in changes]
    result.directions = [TokenizedDirection(step=d.step, text=t) for d, t in zip(old, texts)]
    if all(d.text == t for d, t in zip(old, texts)):
        result.status = "none-found"
    return result


def _new_tokens(before: str, after: str) -> List[Tuple[str, str]]:
    """The temperature tokens in ``after`` that ``before`` does not have, as (words, value)."""
    old = {m.group(0) for m in TOKEN_RE.finditer(before)}
    return [(m.group(2), m.group(1)[len("temp:"):]) for m in TOKEN_RE.finditer(after)
            if is_temp_id(m.group(1)) and m.group(0) not in old]


def plan_row(row: Dict[str, Any], ask: Optional[Asker], force: bool = False) -> RowResult:
    """What one recipe would be written as, writing nothing."""
    result = RowResult(id=row["id"], title=str(row.get("title") or ""), status="tagged",
                       before=row.get("tokenized_directions"))
    raw = row.get("tokenized_directions")
    if not isinstance(raw, list):
        result.status = "malformed"
        return result
    if (row.get("derived_rev") or 0) < (row.get("body_rev") or 0):
        result.status = "stale"
        return result
    try:
        old = [TokenizedDirection.model_validate(d) for d in raw]
    except Exception as exc:  # noqa: BLE001
        result.status, result.error = "malformed", str(exc)
        return result
    if not old:
        result.status = "empty"
        return result
    if not force and any(is_temp_id(m.group(1)) for d in old for m in TOKEN_RE.finditer(d.text)):
        result.status = "already-tagged"
        return result
    texts = [splice(d.text, find_temperatures(d.text)) for d in old]
    for d, text in zip(old, texts):
        result.proposals += [(d.step, w, v, "rule") for w, v in _new_tokens(d.text, text)]
    unsettled = [i for i, text in enumerate(texts) if needs_model(text)]
    if unsettled and ask is not None:
        system = row.get("source_uom_system") or row.get("source_uom_system_detected")
        try:
            mentions = ask([TOKEN_RE.sub(r"\2", texts[i]) for i in unsettled], system)
        except Exception as exc:  # noqa: BLE001
            result.status, result.error = "failed", str(exc)
            return result
        for n, i in enumerate(unsettled, start=1):
            mine = [(m.quote, m.context, m.value) for m in mentions if m.step == n]
            if not mine:
                continue
            before = texts[i]
            texts[i], dropped = place_temperature_mentions(texts[i], mine)
            result.dropped += [f"step {old[i].step}: {line}" for line in dropped]
            result.proposals += [(old[i].step, w, v, "model") for w, v in _new_tokens(before, texts[i])]
    result.directions = [TokenizedDirection(step=d.step, text=t) for d, t in zip(old, texts)]
    if all(d.text == t for d, t in zip(old, texts)):
        result.status = "none-found"
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


def write_review(path: Path, results: Sequence[RowResult]) -> None:
    """One line per proposed token: the owner's file to read before any live run."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "title", "step", "words", "value", "by"])
        for r in sorted(results, key=lambda r: r.id):
            for step, words, value, by in r.proposals:
                writer.writerow([r.id, r.title, step, words, value, by])


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

STATUSES = ("tagged", "already-tagged", "none-found", "stale", "empty", "malformed", "failed")


def main() -> int:
    ap = argparse.ArgumentParser(description="Wrap every temperature in the directions in a temperature token.")
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--user-id", help="The owner whose library to tag.")
    who.add_argument("--all-users", action="store_true", help="Every library: shared copies are rows of their own.")
    ap.add_argument("--recipe-id", help="Tag this one recipe only.")
    ap.add_argument("--limit", type=int, help="Stop after this many recipes have been sent to Gemini.")
    ap.add_argument("--workers", type=int, default=4, help="Gemini calls in flight at once (default 4).")
    ap.add_argument("--no-model", action="store_true", help="Rule only: no Gemini calls, no GOOGLE_API_KEY.")
    ap.add_argument("--review", type=Path, help="CSV of every proposed token: id, title, step, words, value, by.")
    ap.add_argument("--live", action="store_true", help="Actually write. Omit for a dry run.")
    ap.add_argument("--record", type=Path, help="CSV of each rewritten row's id, title and old tokenized_directions.")
    ap.add_argument("--restore", type=Path, help="Write this record's tokenized_directions back; nothing else.")
    ap.add_argument("--force", action="store_true", help="Look again at recipes that already have a temperature token.")
    ap.add_argument("--repair", action="store_true",
                    help="Only mend existing temperature tokens: take in trailing unit words, unwrap range ends. No model.")
    ap.add_argument("--verbose", action="store_true", help="Print every tagged recipe's new steps.")
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

    ask: Optional[Asker] = None
    if not args.no_model and not args.repair:
        from recipeparser import client as gemini_client  # noqa: PLC0415
        from recipeparser.gemini import tag_temperatures  # noqa: PLC0415
        if gemini_client is None:
            raise SystemExit("GOOGLE_API_KEY must be set (or pass --no-model).")

        def ask(steps: List[str], system: Optional[str]) -> List[TemperatureMention]:  # noqa: F811
            return tag_temperatures(steps, system, gemini_client)

    rows = _read_rows(sb, args.user_id, args.recipe_id)
    by_id = {r["id"]: r for r in rows}

    # Probe every row with a stub model first (cheap), so --limit counts the recipes that will call
    # the real one. A row the rule settles is final after the probe; the others are planned again
    # with the model, up to --limit of them.
    ready: List[RowResult] = []  # planned without the model
    todo: List[Dict[str, Any]] = []  # rows the model must read
    sent = 0
    for row in rows if not args.repair else []:
        called: List[bool] = []
        probe = plan_row(row, (lambda s, sy: called.append(True) or []) if ask is not None else None, force=args.force)
        if not called:
            ready.append(probe)
        elif args.limit is None or sent < args.limit:
            todo.append(row)
            sent += 1
        # else: over the limit, left unplanned and unwritten.
    if args.repair:
        ready = [plan_repair(row) for row in rows]

    writer = None
    record_fh = None
    if args.live:
        record_fh = args.record.open("w", newline="", encoding="utf-8")
        writer = csv.writer(record_fh)
        writer.writerow(["id", "title", "tokenized_directions"])
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
                print(f"\n  {r.title!r}  ({r.id})")
                for d in r.directions:
                    print(f"    {d.step}. {d.text}")
                for line in r.dropped:
                    print(f"    dropped: {line}")
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
    by_rule = sum(1 for r in results for p in r.proposals if p[3] == "rule")
    by_model = sum(1 for r in results for p in r.proposals if p[3] == "model")
    dropped = sum(len(r.dropped) for r in results)
    print(f"\n{len(rows)} recipes read")
    for status in STATUSES:
        print(f"  {status:15s} {statuses.get(status, 0):5d}")
    print(f"  proposals by rule:                                {by_rule}")
    print(f"  proposals by model:                               {by_model}")
    if args.repair:
        print(f"  tokens mended:                                    {sum(len(r.proposals) for r in results)}")
    print(f"  mentions dropped (not placed):                    {dropped}")
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
