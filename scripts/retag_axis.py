"""
scripts/retag_axis.py -- re-tag one axis of a library under the tagging rules,
replacing what is there (Cayenne Fix Roadmap F-205).

Until 9.6.0 both tagging prompts asked only for tags that "describe" or "apply
to" a recipe, and the model answered by what appeared anywhere in it: potato
gnocchi was tagged Egg for the eggs in its dough, minestrone Chicken for its
stock. The prompts now carry TAGGING_RULES (recipeparser/gemini.py). A bulk
recategorise only ever adds, so it cannot take the old tags off; this can.

Two steps, so what is written is exactly what was read:

    # 1. ask the model, write every change to a plan, write nothing to the database
    python scripts/retag_axis.py --user-id <uuid> --axis Protein --plan protein.csv

    # 2. read protein.csv, then write exactly what it says (no model call)
    python scripts/retag_axis.py --user-id <uuid> --apply protein.csv

Step 1 reads SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and GOOGLE_API_KEY, and
makes one model call per 10 recipes (about 160 for a 1,600-recipe library).
Step 2 needs only the Supabase pair.

Every recipe the user owns is put to the model against the axis's tags, exactly
as "Apply to existing recipes" on that axis would, and the answer replaces the
recipe's links within the axis: a link the answer does not repeat is dropped, a
tag it adds is added, links on other axes are never touched. A link to the axis
row itself is in scope and is dropped, since an axis is no longer offered as a
tag. A recipe whose batch fails twice keeps every link it had.

recipe_categories records no provenance, so a tag the cook chose is replaced
like any other; the plan names every drop, so those few can be seen and kept
by deleting their lines before step 2. The plan is also the undo: its drop
lines are the links to put back.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

# Running this file directly puts scripts/ on sys.path rather than the repository
# root, so `recipeparser` would not import. Make the root importable either way.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recipeparser.adapters.recat_worker import resolve_new_axes  # noqa: E402
from recipeparser.core.stages.categorize import chunked, filter_batch_result  # noqa: E402
from recipeparser.core.taxonomy import descendants_of, parents_from_rows  # noqa: E402

log = logging.getLogger("retag-axis")

PAGE = 500
BATCH_SIZE = 10
PLAN_COLUMNS = ["action", "user_id", "recipe_id", "title", "tag", "category_id"]


@dataclass(frozen=True)
class Offer:
    """What one axis offers the model, and which links it owns."""
    axes: Dict[str, List[str]]
    tag_ids: Dict[str, str]
    parents: Dict[str, str]
    scope: Set[str]  # every category id in the axis, the axis row included
    names: Dict[str, str]  # category id -> name, for every id in scope


@dataclass(frozen=True)
class Change:
    action: str  # "drop" or "add"
    recipe_id: str
    title: str
    tag: str
    category_id: str


@dataclass
class Plan:
    changes: List[Change] = field(default_factory=list)
    kept: int = 0
    failed: List[Tuple[str, str]] = field(default_factory=list)  # (title, reason): links left as they were


def axis_offer(rows: Sequence[Mapping[str, Any]], axis_name: str) -> Offer:
    """The offer for the top-level category named ``axis_name``. Raises when there is not exactly one."""
    roots = [r for r in rows if not r.get("parent_id") and (r.get("name") or "").strip() == axis_name]
    if len(roots) != 1:
        raise ValueError(f"Expected one top-level category named {axis_name!r}, found {len(roots)}.")
    scope = descendants_of(roots[0]["id"], rows)
    axes, tag_ids = resolve_new_axes([dict(r) for r in rows], scope)
    names = {r["id"]: (r.get("name") or "").strip() for r in rows if r.get("id") in set(scope)}
    return Offer(axes=axes, tag_ids=tag_ids, parents=parents_from_rows(rows), scope=set(scope), names=names)


def plan_retag(
    recipes: Sequence[Mapping[str, Any]],
    links: Iterable[Mapping[str, Any]],
    offer: Offer,
    categorize: Callable[[List[Mapping[str, Any]]], Dict[str, List[str]]],
    batch_size: int = BATCH_SIZE,
) -> Plan:
    """Every link to drop and add so each recipe's tags on the axis are the model's answer. No writes."""
    existing: Dict[str, Set[str]] = {}
    for link in links:
        if link["category_id"] in offer.scope:
            existing.setdefault(link["recipe_id"], set()).add(link["category_id"])

    plan = Plan()
    for batch in chunked(list(recipes), batch_size):
        hits: Optional[Dict[str, List[str]]] = None
        reason = ""
        for _attempt in (1, 2):
            try:
                raw = categorize(batch)
                hits = filter_batch_result(raw, set(offer.tag_ids), offer.axes, offer.parents)
                break
            except Exception as exc:  # noqa: BLE001 -- one bad batch must not stop the run
                reason = str(exc)[:300]
        if hits is None:
            plan.failed.extend((r.get("title") or r["id"], reason) for r in batch)
            continue
        for recipe in batch:
            rid, title = recipe["id"], recipe.get("title") or recipe["id"]
            old = existing.get(rid, set())
            new = {offer.tag_ids[t] for t in hits.get(rid, [])}
            plan.kept += len(old & new)
            for cid in sorted(old - new):
                plan.changes.append(Change("drop", rid, title, offer.names.get(cid, cid), cid))
            for cid in sorted(new - old):
                plan.changes.append(Change("add", rid, title, offer.names.get(cid, cid), cid))
    return plan


def write_plan(path: str, user_id: str, plan: Plan) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(PLAN_COLUMNS)
        for c in plan.changes:
            writer.writerow([c.action, user_id, c.recipe_id, c.title, c.tag, c.category_id])


def read_plan(path: str, user_id: str) -> List[Change]:
    """The plan's changes. Refuses a plan made for another user or holding an unknown action."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    changes: List[Change] = []
    for n, row in enumerate(rows, start=2):
        if row["user_id"] != user_id:
            raise ValueError(f"Line {n} of {path} is for user {row['user_id']}, not {user_id}.")
        if row["action"] not in ("drop", "add"):
            raise ValueError(f"Line {n} of {path} has unknown action {row['action']!r}.")
        changes.append(Change(row["action"], row["recipe_id"], row["title"], row["tag"], row["category_id"]))
    return changes


def apply_plan(sb: Any, user_id: str, changes: Sequence[Change]) -> Tuple[int, int, int]:
    """Adds first, then drops, so a run cut short leaves a recipe over-tagged, never untagged.

    Returns (added, dropped, failed). An add that already exists is skipped by the
    unique pair, and a drop already gone deletes nothing: re-running a plan is safe.
    """
    added = dropped = failed = 0
    for c in [c for c in changes if c.action == "add"] + [c for c in changes if c.action == "drop"]:
        try:
            if c.action == "add":
                sb.table("recipe_categories").upsert(
                    [{"id": str(uuid.uuid4()), "recipe_id": c.recipe_id, "category_id": c.category_id,
                      "user_id": user_id}],
                    on_conflict="recipe_id,category_id", ignore_duplicates=True,
                ).execute()
                added += 1
            else:
                (sb.table("recipe_categories").delete()
                 .eq("recipe_id", c.recipe_id).eq("category_id", c.category_id).execute())
                dropped += 1
        except Exception as exc:  # noqa: BLE001 -- report it and carry on with the rest
            failed += 1
            log.error("could not %s %s on %r: %s", c.action, c.tag, c.title, exc)
    return added, dropped, failed


# ---------------------------------------------------------------------------
# The I/O shell
# ---------------------------------------------------------------------------

def _paged(query: Callable[[], Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = query().order("id").range(offset, offset + PAGE - 1).execute().data or []
        out.extend(page)
        if len(page) < PAGE:
            return out
        offset += PAGE


def _supabase() -> Any:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must both be set.")
    from supabase import create_client  # noqa: PLC0415
    return create_client(url, key)


def _gemini() -> Any:
    key = os.environ.get("GOOGLE_API_KEY", "")
    if not key:
        raise SystemExit("GOOGLE_API_KEY must be set to plan: the plan asks the model.")
    from google import genai  # noqa: PLC0415
    return genai.Client(api_key=key)


def _plan(sb: Any, user_id: str, axis: str, path: str) -> int:
    from recipeparser.gemini import categorize_batch  # noqa: PLC0415

    client = _gemini()
    rows = _paged(lambda: sb.table("categories").select("id,name,parent_id").eq("user_id", user_id))
    try:
        offer = axis_offer(rows, axis)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    recipes = _paged(lambda: sb.table("recipes").select("id,title,ingredient_lines,direction_steps")
                     .eq("user_id", user_id))
    links = _paged(lambda: sb.table("recipe_categories").select("id,recipe_id,category_id")
                   .in_("category_id", sorted(offer.scope)))
    log.info("%s: %d tags, %d recipes, %d links on the axis. One model call per %d recipes.",
             axis, len(offer.tag_ids), len(recipes), len(links), BATCH_SIZE)

    plan = plan_retag(
        recipes, links, offer,
        lambda batch: categorize_batch([dict(r) for r in batch], offer.axes, client, parents=offer.parents),
    )
    write_plan(path, user_id, plan)
    for c in plan.changes:
        log.info("%s %s %s", c.title, "-" if c.action == "drop" else "+", c.tag)
    for title, reason in plan.failed:
        log.warning("%s: left as it is, the model could not be asked (%s).", title, reason)
    drops = sum(c.action == "drop" for c in plan.changes)
    log.info("PLAN %s: %d to drop, %d to add, %d kept, %d recipes left as they are. Nothing was written.",
             path, drops, len(plan.changes) - drops, plan.kept, len(plan.failed))
    return 0


def _apply(sb: Any, user_id: str, path: str) -> int:
    try:
        changes = read_plan(path, user_id)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    added, dropped, failed = apply_plan(sb, user_id, changes)
    log.info("APPLIED %s: %d added, %d dropped%s.", path, added, dropped, f", {failed} FAILED" if failed else "")
    return 1 if failed else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Re-tag one axis under the tagging rules (Cayenne F-205).")
    parser.add_argument("--user-id", required=True, help="The owner whose library to re-tag.")
    parser.add_argument("--axis", help="With --plan: the top-level category to re-tag, by name.")
    step = parser.add_mutually_exclusive_group(required=True)
    step.add_argument("--plan", help="Ask the model and write every change to this CSV. Writes no data.")
    step.add_argument("--apply", help="Write the changes in this CSV, made by --plan. Asks no model.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.plan and not args.axis:
        raise SystemExit("--plan needs --axis.")
    sb = _supabase()
    return _plan(sb, args.user_id, args.axis, args.plan) if args.plan else _apply(sb, args.user_id, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
