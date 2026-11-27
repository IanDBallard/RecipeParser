"""
One-off (Cayenne F-888): bring recipes stored before RecipeParser 9.48.1/9.49.2 in line with the parser
fixes new imports have. The planners call the shipped rules, so the backfill and ingestion cannot
disagree. Design: Cayenne docs/superpowers/specs/2026-11-27-f888-stored-recipe-backfill-design.md.

Classes, run one at a time:
    sources      a source title that is a web address becomes the page's host (F-878)
    sizes        a size in brackets becomes the line's second measure (F-858, as fixed by F-892)
    totals       a cook time taken whole from a stated total loses the prep (F-877)
    fetch-times  a web recipe with no cook time gets its page's JSON-LD times (F-877); this fetches
                 each page (no model, one a second) and fills only what is empty

This script only reads and plans. With --sql it writes ONE transaction: a guarded UPDATE per row
(each column written must still hold the value it was planned from, so a row a cook changed in
between is left alone and a second run plans nothing), a verification SELECT, and ROLLBACK -- or
COMMIT with --commit. None of the columns written is a body column, so nothing regenerates or
re-embeds and no trigger is touched. The SQL holds owner data: write it to a scratch path, never
the repo. Run it with ``supabase db query --linked``. To commit what was reviewed, run the same
file with its last line changed from ``rollback;`` to ``commit;``, rather than re-planning.

    # counts and one line per change; writes nothing
    python scripts/backfill_f888.py sources

    # the transaction ending in ROLLBACK, plus the before-image
    python scripts/backfill_f888.py sources --sql <scratch>/sources.sql --record <records>/f888-sources.json

    # undo: the reverse transaction, guarded by the values the backfill wrote
    python scripts/backfill_f888.py restore <records>/f888-sources.json --sql <scratch>/undo.sql

Reads with SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the environment, or from --env-file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipeparser.core.citation import _site_name, host_of  # noqa: E402
from recipeparser.core.durations import duration_columns, parse_duration  # noqa: E402
from recipeparser.core.stages.assemble import _cook_from_total  # noqa: E402
from recipeparser.core.stages.refine import fill_bracketed_size  # noqa: E402
from recipeparser.exceptions import SiteRefusedError, UrlFetchError  # noqa: E402
from recipeparser.io.readers.url import fetch_page_html, page_meta_from_html  # noqa: E402
from recipeparser.models import StructuredIngredient  # noqa: E402

PAGE = 500
COLUMN_TYPES: Dict[str, str] = {
    "structured_ingredients": "jsonb",
    "source_title": "text",
    "prep_time": "text",
    "cook_time": "text",
    "prep_note": "text",
    "cook_note": "text",
    "prep_min_minutes": "integer",
    "prep_max_minutes": "integer",
    "cook_min_minutes": "integer",
    "cook_max_minutes": "integer",
}


@dataclass(frozen=True)
class RowChange:
    """One recipe's planned write: ``old`` holds the values read, ``new`` the values to write (same keys)."""

    recipe_id: str
    old: Dict[str, Any]
    new: Dict[str, Any]
    note: str


# ── planners ───────────────────────────────────────────────────────────────────

def plan_sources(rows: List[Dict[str, Any]]) -> List[RowChange]:
    """F-878: a title with a scheme or a path is not a name; the page's own title is its host."""
    plan = []
    for row in rows:
        title = row.get("source_title")
        if not title or not title.strip() or _site_name(title) is not None:
            continue
        stripped = title.strip()
        if "://" not in stripped and "/" not in stripped:
            continue  # a bare host is web_citation's designed title, not a fault
        host = host_of(row.get("source_url") or stripped)
        if host and host != title:
            plan.append(RowChange(row["id"], {"source_title": title}, {"source_title": host},
                                  f"{title!r} -> {host!r}"))
    return plan


_SIZE_KEYS = ("converted_amount", "converted_unit", "is_ai_converted")


def _size_for(item: Any, index: int) -> Optional[Dict[str, Any]]:
    """The three size keys fill_bracketed_size gives a stored item, or None when it gives none."""
    if not isinstance(item, dict) or item.get("kind") == "heading" or not item.get("fallback_string"):
        return None
    try:
        ing = StructuredIngredient(
            id=str(item.get("id") or f"ing_{index}"), kind="ingredient", amount=item.get("amount"),
            unit=item.get("unit"), name=str(item.get("name") or ""), fallback_string=item["fallback_string"],
            converted_amount=item.get("converted_amount"), converted_unit=item.get("converted_unit"),
            is_ai_converted=bool(item.get("is_ai_converted")),
        )
    except (TypeError, ValueError):
        return None  # a stored item the model refuses is left as it is
    if not fill_bracketed_size(ing):
        return None
    return {k: getattr(ing, k) for k in _SIZE_KEYS}


def plan_sizes(rows: List[Dict[str, Any]]) -> List[RowChange]:
    """F-858/F-892: each line the shipped rule would give a second measure gets it; nothing else changes."""
    plan = []
    for row in rows:
        items = row.get("structured_ingredients")
        if not isinstance(items, list):
            continue
        new_items, notes = [], []
        for i, item in enumerate(items):
            size = _size_for(item, i)
            if size is None:
                new_items.append(item)
                continue
            new_items.append({**item, **size})
            notes.append(f"{item['fallback_string']!r} -> {size['converted_amount']:g} {size['converted_unit']}")
        if notes:
            plan.append(RowChange(row["id"], {"structured_ingredients": items},
                                  {"structured_ingredients": new_items}, "; ".join(notes)))
    return plan


def _single(lo: Any, hi: Any, note: Any) -> bool:
    return lo is not None and lo == hi and note is None


def plan_totals(rows: List[Dict[str, Any]], skip_ids: AbstractSet[str] = frozenset()) -> List[RowChange]:
    """
    F-877: with no cook text, the cook minutes came from a stated total; the cook is the total less the
    prep (_cook_from_total). Only an imported, unedited single prep under a single longer cook. A
    repaired row still looks like this, so ids already repaired (an earlier record's) are skipped.
    """
    plan = []
    for row in rows:
        if row["id"] in skip_ids or row.get("cook_time") is not None or not row.get("prep_time"):
            continue
        prep, cook = row.get("prep_min_minutes"), row.get("cook_min_minutes")
        if not (_single(prep, row.get("prep_max_minutes"), row.get("prep_note"))
                and _single(cook, row.get("cook_max_minutes"), row.get("cook_note"))):
            continue
        stated = parse_duration(row["prep_time"])
        if (stated.min, stated.max) != (prep, prep):
            continue  # the prep was edited since import
        new = parse_duration(_cook_from_total(row["prep_time"], f"{cook} min")).min
        if new is None or new == cook:
            continue
        plan.append(RowChange(row["id"], {"cook_min_minutes": cook, "cook_max_minutes": cook},
                              {"cook_min_minutes": new, "cook_max_minutes": new},
                              f"prep {prep} + cook {cook} -> cook {new}"))
    return plan



_PREP = ("prep_time", "prep_min_minutes", "prep_max_minutes", "prep_note")
_COOK = ("cook_time", "cook_min_minutes", "cook_max_minutes", "cook_note")


def _fetch_candidate(row: Dict[str, Any]) -> bool:
    return (row.get("source_kind", "web") == "web" and bool(row.get("source_url"))
            and all(row.get(k) is None for k in _COOK))


def plan_fetch_times(rows: List[Dict[str, Any]], fetch: Callable[[str], str], *, pause_s: float = 1.0,
                     sleep: Callable[[float], Any] = time.sleep) -> tuple[List[RowChange], Counter]:
    """
    F-877: a web recipe with no cook at all gets the times its page's JSON-LD states, as a fresh import
    now does: cook = cookTime, else the total less the prep (_cook_from_total). The cook_time text is
    written only when the page states a cook time, and prep only when the row has none. Never
    overwrites. The Counter is keyed (source_key, outcome): filled, refused, fetch-failed, no-times.
    """
    plan: List[RowChange] = []
    report: Counter = Counter()
    first = True
    for row in rows:
        if not _fetch_candidate(row):
            continue
        if not first:
            sleep(pause_s)
        first = False
        host = row.get("source_key") or host_of(row["source_url"])
        try:
            meta = page_meta_from_html(fetch(row["source_url"]))
        except SiteRefusedError:
            report[(host, "refused")] += 1
            continue
        except UrlFetchError:
            report[(host, "fetch-failed")] += 1
            continue
        has_prep = any(row.get(k) is not None for k in _PREP)
        prep_text = row.get("prep_time") if has_prep else meta.prep_time
        cook_span = meta.cook_time or _cook_from_total(prep_text, meta.total_time)
        cols = duration_columns(prep_text, cook_span, None, None)
        new: Dict[str, Any] = {}
        if cols["cook_min_minutes"] is not None or cols["cook_note"] is not None:
            new.update({k: cols[k] for k in _COOK[1:]})
            if meta.cook_time:
                new["cook_time"] = meta.cook_time
        if new and not has_prep and meta.prep_time:
            new.update({"prep_time": meta.prep_time, **{k: cols[k] for k in _PREP[1:]}})
        if not new:
            report[(host, "no-times")] += 1
            continue
        report[(host, "filled")] += 1
        plan.append(RowChange(row["id"], {k: row.get(k) for k in new}, new,
                              f"{row['source_url']}: " + ", ".join(f"{k}={v}" for k, v in new.items())))
    return plan, report


# ── SQL ────────────────────────────────────────────────────────────────────────

def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def sql_literal(value: Any, kind: str) -> str:
    """A Postgres literal (standard_conforming_strings on, so backslashes stay as written)."""
    if value is None:
        return "null"
    if kind == "jsonb":
        return _quote(json.dumps(value, ensure_ascii=False)) + "::jsonb"
    if kind == "integer":
        return str(int(value))
    return _quote(str(value))


def _assignments(values: Dict[str, Any]) -> str:
    return ", ".join(f"{col} = {sql_literal(v, COLUMN_TYPES[col])}" for col, v in values.items())


def _matches(values: Dict[str, Any], prefix: str = "") -> str:
    return " and ".join(f"{prefix}{col} is not distinct from {sql_literal(v, COLUMN_TYPES[col])}"
                        for col, v in values.items())


def render_sql(changes: List[RowChange], *, label: str, commit: bool) -> str:
    """One transaction: a guarded UPDATE per change, a verification SELECT, then COMMIT or ROLLBACK."""
    updates = "\n".join(
        f"update public.recipes set {_assignments(c.new)}\n"
        f" where id = {_quote(c.recipe_id)}::uuid and {_matches(c.old)};"
        for c in changes
    )
    done = "\n    or ".join(f"(r.id = {_quote(c.recipe_id)}::uuid and {_matches(c.new, 'r.')})"
                            for c in changes) or "false"
    return f"""begin;

{updates}

-- Verification ({label}): changed should equal planned; a shortfall is rows edited since the plan.
select (select count(*) from public.recipes r
         where {done}) as changed,
       {len(changes)} as planned;

{"commit" if commit else "rollback"};
"""


# ── record and restore ─────────────────────────────────────────────────────────

def invert(changes: List[RowChange]) -> List[RowChange]:
    """The restore: each change with old and new swapped, so it is guarded by what the backfill wrote."""
    return [RowChange(c.recipe_id, c.new, c.old, c.note) for c in changes]


def write_record(changes: List[RowChange], path: Path) -> None:
    data = [{"recipe_id": c.recipe_id, "old": c.old, "new": c.new, "note": c.note} for c in changes]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def read_record(path: Path) -> List[RowChange]:
    return [RowChange(d["recipe_id"], d["old"], d["new"], d["note"])
            for d in json.loads(path.read_text(encoding="utf-8"))]


def record_ids(paths: List[Path]) -> set[str]:
    """The recipe ids earlier records hold (--skip-record)."""
    return {c.recipe_id for p in paths for c in read_record(p)}


# ── reading ────────────────────────────────────────────────────────────────────

def _client(env_file: Optional[Path]):
    from supabase import create_client  # noqa: PLC0415
    env = dict(os.environ)
    if env_file is not None:
        from dotenv import dotenv_values  # noqa: PLC0415
        env.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    return create_client(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])


def _read(sb, columns: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = (sb.table("recipes").select(columns)
                .order("id").range(offset, offset + PAGE - 1).execute().data or [])
        if not page:
            return rows
        rows.extend(page)
        offset += PAGE


_TIMES = ("id,prep_time,cook_time,prep_min_minutes,prep_max_minutes,prep_note,"
          "cook_min_minutes,cook_max_minutes,cook_note")
CLASSES: Dict[str, tuple[str, Callable[..., List[RowChange]]]] = {
    "sources": ("id,source_title,source_url", plan_sources),
    "sizes": ("id,structured_ingredients", plan_sizes),
    "totals": (_TIMES, plan_totals),
    "fetch-times": ("id,source_url,source_key,source_kind," + _TIMES[3:], plan_fetch_times),
}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Cayenne F-888: stored recipes get the parser fixes.")
    ap.add_argument("what", choices=[*CLASSES, "restore"])
    ap.add_argument("record_in", nargs="?", type=Path, help="restore: the record to undo.")
    ap.add_argument("--sql", type=Path, help="Write the transaction here (a scratch path, never the repo).")
    ap.add_argument("--commit", action="store_true", help="End the transaction in COMMIT, not ROLLBACK.")
    ap.add_argument("--record", type=Path, help="Write the before-image here (needed with --commit).")
    ap.add_argument("--env-file", type=Path, help="Read SUPABASE_URL and the service key from this file.")
    ap.add_argument("--skip-record", type=Path, action="append", default=[],
                    help="totals: skip the recipes this earlier record holds (repeatable).")
    args = ap.parse_args(argv)
    if args.commit and not args.sql:
        ap.error("--commit needs --sql")
    if args.record and args.record.exists():
        ap.error(f"{args.record} exists: a before-image is never overwritten")
    if args.what == "restore":
        if args.record_in is None or not args.sql:
            ap.error("restore needs a record and --sql")
        changes = invert(read_record(args.record_in))
    else:
        if args.commit and not args.record:
            ap.error("--commit needs --record: the record is the only way back")
        columns, planner = CLASSES[args.what]
        rows = _read(_client(args.env_file), columns)
        if args.what == "totals":
            changes = planner(rows, skip_ids=record_ids(args.skip_record))
        elif args.what == "fetch-times":
            changes, report = planner(rows, lambda url: fetch_page_html(url, 20.0))
            for (host, outcome), n in sorted(report.items()):
                print(f"  {host:32} {outcome:13} {n}")
        else:
            changes = planner(rows)
    for c in changes:
        print(f"  {c.recipe_id}  {c.note}")
    print(f"{len(changes)} recipe(s) would change ({args.what})")
    if args.record and args.what != "restore":
        write_record(changes, args.record)
        print(f"wrote the before-image {args.record}")
    if args.sql and changes:
        args.sql.write_text(render_sql(changes, label=args.what, commit=args.commit), encoding="utf-8")
        print(f"wrote {args.sql} ending in {'COMMIT' if args.commit else 'ROLLBACK'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
