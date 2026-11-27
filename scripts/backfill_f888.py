"""
One-off (Cayenne F-888): bring recipes stored before RecipeParser 9.48.1/9.49.2 in line with the parser
fixes new imports have. The planners call the shipped rules, so the backfill and ingestion cannot
disagree. Design: Cayenne docs/superpowers/specs/2026-11-27-f888-stored-recipe-backfill-design.md.

Classes, run one at a time:
    sources      a source title that is a web address becomes the page's host (F-878)

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipeparser.core.citation import _site_name, host_of  # noqa: E402

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


CLASSES: Dict[str, tuple[str, Callable[[List[Dict[str, Any]]], List[RowChange]]]] = {
    "sources": ("id,source_title,source_url", plan_sources),
}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Cayenne F-888: stored recipes get the parser fixes.")
    ap.add_argument("what", choices=[*CLASSES, "restore"])
    ap.add_argument("record_in", nargs="?", type=Path, help="restore: the record to undo.")
    ap.add_argument("--sql", type=Path, help="Write the transaction here (a scratch path, never the repo).")
    ap.add_argument("--commit", action="store_true", help="End the transaction in COMMIT, not ROLLBACK.")
    ap.add_argument("--record", type=Path, help="Write the before-image here (needed with --commit).")
    ap.add_argument("--env-file", type=Path, help="Read SUPABASE_URL and the service key from this file.")
    args = ap.parse_args(argv)
    if args.commit and not args.sql:
        ap.error("--commit needs --sql")
    if args.what == "restore":
        if args.record_in is None or not args.sql:
            ap.error("restore needs a record and --sql")
        changes = invert(read_record(args.record_in))
    else:
        if args.commit and not args.record:
            ap.error("--commit needs --record: the record is the only way back")
        columns, planner = CLASSES[args.what]
        changes = planner(_read(_client(args.env_file), columns))
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
