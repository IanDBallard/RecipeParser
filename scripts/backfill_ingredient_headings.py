"""
One-off: give every structured ingredient in a library its `kind` (Cayenne's ingredient headings
design, docs/superpowers/specs/2026-10-12-ingredient-headings-design.md, D8-D10; RecipeParser 9.10.0).

Rows imported before 9.10.0 carry no `kind`, and their section headings are either missing (REFINE
left them out) or ordinary entries a cook could tick and the shopping list could gather. This marks
every entry `ingredient`, then proposes, by the rules in recipeparser.core.headings: a null-amount
entry shaped like a heading becomes one; a raw line no entry claims, shaped like a heading, becomes
a new heading entry with the next free id. A short title-case line ("Lemon Sauce") is proposed
unticked: the owner decides. An entry a direction step names (a Fat Token) is a food and is never
proposed. No Gemini call; no existing id changes.

    # dry run: reads every library, writes the review file, changes nothing
    python scripts/backfill_ingredient_headings.py --review headings-review.md

    # after the owner has ticked the file: writes only what it ticks
    python scripts/backfill_ingredient_headings.py --apply headings-review.md --live

    # check: every entry of every row carries a kind
    python scripts/backfill_ingredient_headings.py --verify

Only `structured_ingredients` is written, and only if `body_rev` and `derived_rev` are still what
was read, so a cook's edit mid-run wins. Every library is read (a shared recipe is a row of its own)
and stale rows are included (the kitchen parses them too). Requires SUPABASE_URL and
SUPABASE_SERVICE_ROLE_KEY.
"""
from __future__ import annotations

import argparse
import copy
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipeparser.core.fat_tokens import TOKEN_RE  # noqa: E402
from recipeparser.core.headings import heading_name, heading_shape  # noqa: E402

PAGE = 500
COLUMNS = "id,title,ingredient_lines,structured_ingredients,tokenized_directions,body_rev,derived_rev"
_ID = re.compile(r"^ing_(\d+)$")
_TICK = re.compile(r"^- \[(?P<tick>[ xX])\] `(?P<key>[^`]+)`")
_RECIPE = re.compile(r"^### .* <!-- id:(?P<id>[^ ]+) -->$")


@dataclass
class Proposal:
    key: str
    text: str
    change: Literal["mark", "insert"]
    sure: bool
    line_index: Optional[int] = None


@dataclass
class RowPlan:
    id: str
    title: str
    status: str  # ok | malformed
    proposals: List[Proposal] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    entries: List[Dict[str, Any]] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def _used_ids(directions: Any) -> Set[str]:
    """Ids of every ingredient a direction step names; a missing or malformed column names none."""
    if not isinstance(directions, list):
        return set()
    used: Set[str] = set()
    for step in directions:
        text = step.get("text") if isinstance(step, dict) else None
        if isinstance(text, str):
            used.update(m.group(1) for m in TOKEN_RE.finditer(text))
    return used


def _position(entries: List[Dict[str, Any]], lines: List[str], line: int) -> Optional[int]:
    """Where a new heading for raw line `line` goes: before the first entry whose line comes after it."""
    for at, entry in enumerate(entries):
        idx = entry.get("line_index")
        if not isinstance(idx, int):
            later = [i for i in range(line + 1, len(lines)) if _norm(lines[i]) == _norm(entry.get("fallback_string", ""))]
            idx = later[0] if later else None
        if idx is not None and idx > line:
            return at
    claimed_before = any(isinstance(e.get("line_index"), int) and e["line_index"] < line for e in entries)
    return len(entries) if claimed_before else None


def plan_row(row: Dict[str, Any]) -> RowPlan:
    entries, lines = row.get("structured_ingredients"), row.get("ingredient_lines")
    plan = RowPlan(id=row["id"], title=row.get("title") or "", status="ok")
    if (not isinstance(entries, list) or not isinstance(lines if lines is not None else [], list)
            or any(not isinstance(e, dict) or not str(e.get("id") or "").strip() for e in entries)):
        plan.status = "malformed"
        return plan
    lines = [str(l) for l in (lines or [])]
    plan.lines = lines
    plan.entries = [dict(e, kind=e.get("kind") or "ingredient") for e in entries]
    if any("kind" in e for e in entries):
        return plan  # written by 9.10.0 or already backfilled: REFINE decided
    used = _used_ids(row.get("tokenized_directions"))
    for e in plan.entries:
        if e.get("amount") is not None or e.get("id") in used:
            continue
        shape = heading_shape(str(e.get("fallback_string", "")))
        if shape:
            plan.proposals.append(Proposal(key=e["id"], text=e["fallback_string"], change="mark", sure=shape == "heading"))
    claimed = {e["line_index"] for e in plan.entries if isinstance(e.get("line_index"), int)}
    texts = {_norm(e.get("fallback_string", "")) for e in plan.entries}
    marked = {_norm(heading_name(p.text)) for p in plan.proposals}  # a mark already covers this heading
    for i, line in enumerate(lines):
        if i in claimed or _norm(line) in texts or _norm(heading_name(line)) in marked:
            continue
        shape = heading_shape(line)
        if shape and _position(plan.entries, lines, i) is not None:
            plan.proposals.append(Proposal(key=f"line:{i}", text=line, change="insert", sure=shape == "heading", line_index=i))
        elif shape:
            plan.notes.append(f"heading-shaped line {i} cannot be placed: {line}")
        else:
            plan.notes.append(f"unclaimed line {i}: {line}")
    return plan


def _heading(entry: Dict[str, Any]) -> Dict[str, Any]:
    return dict(entry, kind="heading", amount=None, unit=None, converted_amount=None, converted_unit=None,
                is_ai_converted=False, state=None, name=heading_name(str(entry.get("fallback_string", ""))))


def apply_decisions(plan: RowPlan, accepted: Set[str]) -> List[Dict[str, Any]]:
    marks = {p.key for p in plan.proposals if p.change == "mark" and p.key in accepted}
    entries = [(_heading(e) if e["id"] in marks else copy.deepcopy(e)) for e in plan.entries]
    numbers = [int(m.group(1)) for e in entries if (m := _ID.match(str(e.get("id", ""))))]
    next_number = max(numbers, default=0) + 1
    inserts = [p for p in plan.proposals if p.change == "insert" and p.key in accepted]
    for p in sorted(inserts, key=lambda p: p.line_index, reverse=True):
        at = _position(entries, plan.lines, p.line_index)
        if at is None:
            continue
        entries.insert(at, _heading({"id": "", "fallback_string": p.text, "line_index": p.line_index}))
    for e in entries:
        if e["id"] == "":
            e["id"] = f"ing_{next_number:02d}"
            next_number += 1
    return entries


def _one_line(text: Any) -> str:
    """Collapse every whitespace run (newlines included) so no text can start a line of its own in the review."""
    return " ".join(str(text).split())


def render_review(plans: List[RowPlan], shopping: Dict[str, List[str]]) -> str:
    out = ["# Ingredient headings backfill - review", "",
           "Ticked lines are applied; untick to refuse, tick an unticked one to accept. Notes are for information.", ""]
    for plan in plans:
        if not plan.proposals and not plan.notes and not shopping.get(plan.id):
            continue
        out.append(f"### {_one_line(plan.title)} <!-- id:{plan.id} -->")
        for p in plan.proposals:
            verb = "mark as heading" if p.change == "mark" else f"add heading at line {p.line_index}"
            out.append(f"- [{'x' if p.sure else ' '}] `{p.key}` {verb}: {_one_line(p.text)}")
        for n in plan.notes:
            out.append(f"  - note: {_one_line(n)}")
        for item in shopping.get(plan.id, []):
            out.append(f"  - shopping item to delete in the app: {_one_line(item)}")
        out.append("")
    return "\n".join(out)


def parse_review(text: str) -> Dict[str, Set[str]]:
    accepted: Dict[str, Set[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        if m := _RECIPE.match(line):
            current = m.group("id")
            accepted.setdefault(current, set())
        elif (m := _TICK.match(line)) and current and m.group("tick") in "xX":
            accepted[current].add(m.group("key"))
    return {k: v for k, v in accepted.items() if v}


def write_row(sb: Any, row: Dict[str, Any], entries: List[Dict[str, Any]]) -> bool:
    """Write the entries only if the row is still the revision that was read. False when it moved."""
    response = (
        sb.table("recipes")
        .update({"structured_ingredients": entries})
        .eq("id", row["id"])
        .eq("body_rev", row.get("body_rev") or 0)
        .eq("derived_rev", row.get("derived_rev") or 0)
        .execute()
    )
    return bool(response.data)


def verify(rows: List[Dict[str, Any]]) -> List[str]:
    return [r["id"] for r in rows
            if not isinstance(r.get("structured_ingredients"), list)
            or any(not isinstance(e, dict) or e.get("kind") not in ("ingredient", "heading") for e in r["structured_ingredients"])]


def _creds() -> Tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must both be set.")
    return url, key


def _read_rows(sb: Any, columns: str = COLUMNS) -> List[Dict[str, Any]]:
    """Every row of every library, stale or not (Review Focus 4)."""
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = sb.table("recipes").select(columns).order("id").range(offset, offset + PAGE - 1).execute().data or []
        if not page:
            return rows
        rows.extend(page)
        offset += PAGE


def _shopping_matches(sb: Any, plans: List[RowPlan]) -> Dict[str, List[str]]:
    """Shopping items gathered from an entry this run proposes as a heading, for the owner to delete."""
    marked = {(p.id, q.key) for p in plans for q in p.proposals if q.change == "mark"}
    out: Dict[str, List[str]] = {}
    contributions = sb.table("shopping_contributions").select("recipe_id,ingredient_id,item_id").execute().data or []
    for c in contributions:
        if (c.get("recipe_id"), c.get("ingredient_id")) in marked:
            out.setdefault(c["recipe_id"], []).append(f"item {c['item_id']} (from {c['ingredient_id']})")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Give every structured ingredient its kind (ingredient headings).")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--review", type=Path, help="Dry run: write the review file here; change nothing.")
    mode.add_argument("--apply", type=Path, help="Apply the ticks in this reviewed file.")
    mode.add_argument("--verify", action="store_true", help="List rows with an entry lacking kind.")
    ap.add_argument("--live", action="store_true", help="With --apply: actually write. Omit for a dry run.")
    args = ap.parse_args()

    from supabase import create_client
    sb = create_client(*_creds())

    if args.verify:
        bad = verify(_read_rows(sb, "id,structured_ingredients"))
        print(f"{len(bad)} row(s) with an entry lacking kind" + (f": {', '.join(bad[:20])}" if bad else ""))
        return 1 if bad else 0

    rows = _read_rows(sb)
    plans = [plan_row(r) for r in rows]
    malformed = [p.id for p in plans if p.status == "malformed"]
    if args.review:
        args.review.write_text(render_review(plans, _shopping_matches(sb, plans)), encoding="utf-8")
        print(f"{len(rows)} rows; {sum(len(p.proposals) for p in plans)} proposals; {len(malformed)} malformed; review at {args.review}")
        return 0

    accepted = parse_review(args.apply.read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in rows}
    written = moved = unchanged = 0
    for plan in plans:
        if plan.status != "ok":
            continue
        entries = apply_decisions(plan, accepted.get(plan.id, set()))
        if entries == by_id[plan.id]["structured_ingredients"]:
            unchanged += 1
            continue
        if not args.live:
            written += 1
            continue
        if write_row(sb, by_id[plan.id], entries):
            written += 1
        else:
            moved += 1
            print(f"moved since read, not written: {plan.id} {plan.title}")
    print(f"{'wrote' if args.live else 'would write'} {written}; unchanged {unchanged}; moved {moved}; malformed {len(malformed)}")
    if not args.live:
        print("DRY RUN — nothing was written. Re-run with --live to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
