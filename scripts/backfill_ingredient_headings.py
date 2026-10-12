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
    if not isinstance(entries, list) or not isinstance(lines if lines is not None else [], list):
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
    for i, line in enumerate(lines):
        if i in claimed or _norm(line) in texts:
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
    entries = [(_heading(e) if e["id"] in accepted else copy.deepcopy(e)) for e in plan.entries]
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


def render_review(plans: List[RowPlan], shopping: Dict[str, List[str]]) -> str:
    out = ["# Ingredient headings backfill - review", "",
           "Ticked lines are applied; untick to refuse, tick an unticked one to accept. Notes are for information.", ""]
    for plan in plans:
        if not plan.proposals and not plan.notes and not shopping.get(plan.id):
            continue
        out.append(f"### {plan.title} <!-- id:{plan.id} -->")
        for p in plan.proposals:
            verb = "mark as heading" if p.change == "mark" else f"add heading at line {p.line_index}"
            out.append(f"- [{'x' if p.sure else ' '}] `{p.key}` {verb}: {p.text}")
        for n in plan.notes:
            out.append(f"  - note: {n}")
        for item in shopping.get(plan.id, []):
            out.append(f"  - shopping item to delete in the app: {item}")
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
