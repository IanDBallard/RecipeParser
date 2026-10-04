"""
One-off: store every existing recipe title in house title case (the title-case titles
ruling, 2026-10-03). New imports are title-cased by REFINE; this brings the rows already
in the library into line, with the same ``title_case`` so the two cannot drift.

PostgREST cannot touch the ``recipes_own_body_rev`` trigger, and a plain UPDATE of
``title`` bumps ``body_rev`` and sends every recipe back to the regeneration worker. So
this script only reads and plans; it writes ONE SQL transaction that disables that
trigger, updates the changed titles, re-enables it and ends with a verification SELECT.
The SQL holds the owner's data, so write it to a scratch directory, never the repo.

    # print what would change; writes nothing
    python scripts/backfill_title_case.py

    # write the transaction, ending in ROLLBACK (a dry run against the database)
    python scripts/backfill_title_case.py --sql <scratch>/titles.sql

    # the same, ending in COMMIT
    python scripts/backfill_title_case.py --sql <scratch>/titles.sql --commit

Each UPDATE matches the title it was planned from, so a title edited between the read
and the run is left as its owner wrote it.

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipeparser.utils import title_case  # noqa: E402

PAGE = 500


def plan_titles(rows: List[Dict[str, Any]]) -> List[Tuple[str, str, str]]:
    """``(id, old title, new title)`` for every row whose title case differs."""
    plan = []
    for row in rows:
        old = row["title"]
        new = title_case(old)
        if new != old:
            plan.append((row["id"], old, new))
    return plan


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def render_sql(plan: List[Tuple[str, str, str]], *, commit: bool) -> str:
    """One transaction: the trigger off, the guarded UPDATE, the trigger on, a verification SELECT."""
    values = ",\n".join(
        f"    ({_literal(rid)}::uuid, {_literal(old)}, {_literal(new)})" for rid, old, new in plan
    )
    return f"""begin;

alter table public.recipes disable trigger recipes_own_body_rev;

with v(id, old_title, new_title) as (
  values
{values}
)
update public.recipes r
   set title = v.new_title
  from v
 where r.id = v.id
   and r.title = v.old_title;

alter table public.recipes enable trigger recipes_own_body_rev;

-- Verification: planned {len(plan)}; retitled should match it, and the trigger must be back on.
with v(id, new_title) as (
  values
{",".join(chr(10) + f"    ({_literal(rid)}::uuid, {_literal(new)})" for rid, _, new in plan)}
)
select (select count(*) from public.recipes r join v on r.id = v.id and r.title = v.new_title) as retitled,
       {len(plan)} as planned,
       (select tgenabled from pg_trigger
         where tgname = 'recipes_own_body_rev' and tgrelid = 'public.recipes'::regclass) as trigger_enabled;

{"commit" if commit else "rollback"};
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Store every recipe title in house title case.")
    ap.add_argument("--sql", type=Path, help="Write the transaction here (a scratch path, never the repo).")
    ap.add_argument("--commit", action="store_true", help="End the transaction in COMMIT, not ROLLBACK.")
    args = ap.parse_args()
    if args.commit and not args.sql:
        ap.error("--commit needs --sql")
    from supabase import create_client  # noqa: PLC0415
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = (sb.table("recipes").select("id,title")
                .order("id").range(offset, offset + PAGE - 1).execute().data or [])
        if not page:
            break
        rows.extend(page)
        offset += PAGE

    plan = plan_titles(rows)
    for _, old, new in plan:
        print(f"  {old!r} -> {new!r}")
    print(f"{len(plan)} of {len(rows)} title(s) would change")
    if args.sql and plan:
        args.sql.write_text(render_sql(plan, commit=args.commit), encoding="utf-8")
        print(f"wrote {args.sql} ending in {'COMMIT' if args.commit else 'ROLLBACK'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
