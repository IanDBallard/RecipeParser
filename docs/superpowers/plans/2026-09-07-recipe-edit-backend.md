# Recipe Edit Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Merged:** PR #26 (https://github.com/IanDBallard/RecipeParser/pull/26), merged 2026-09-09T15:50:45Z (`7639cb7`), onto `master`. Plan commit `d2f15df`, first code commit `48034de`, last commit `2bb5445`. Its requirement on Cayenne migrations 013 (`recipe_edit_columns`) and 014 (`regen_rpcs`) is satisfied — both are in Cayenne's `supabase/migrations/`, and `REGEN_WORKER_ENABLED` is live.

**Status: executed and landed, by subagent-driven development with a task review per task and a whole-branch review at the end. The checkboxes below were never ticked; that is bookkeeping, not an unfinished plan.** Do not read status from this file. Four later pull requests extend this work and are part of its real state: #27 (the regen RPCs implemented and applied), #32 (`/health` publishes whether the workers actually started), #33 (the unparseable-duration note tidies its whitespace) and #34 (the API refuses to start with the flag set and no service client). What the execution decided against this plan's text:

- **The plan was overridden once, deliberately** (`e1e36ce`). It mandated that `_validate_line_index` raise on a bad index. That `ValueError` reaches the per-chunk error boundary, which discards the whole recipe — but `line_index` is `Optional[int]` by design and spec 4.3 defines the client-side fallback for a missing or wrong one. A duplicate index is a plausible model slip on a long sectioned ingredient list, so a cosmetic fault in an optional field was destroying a whole recipe at ingest, on every ingest, before any edit feature was switched on. It now clears only the offending entries and keeps the recipe. The spec says the stage validates the field; it never says a failed validation destroys the recipe.
- **The migration requirement was stated backwards, in three places** (`5d07702`). CHANGELOG, ARCHITECTURE section 13 and the README all scoped migration 013 to `REGEN_WORKER_ENABLED` and said "the workers no-op without them". True of the workers, false of the writer: `SupabaseWriter` puts `ingredient_lines`, `direction_steps`, `body_rev`, `derived_rev`, `amount_overrides` and the nine duration columns into every INSERT, unconditionally and behind no flag. Against a pre-013 schema PostgREST returns PGRST204 and every ingest fails, for every user, on every path — while a reader of the old wording concludes the flag being off makes deploying safe. 013 is a deploy blocker; only 014 is a worker prerequisite. **The Requires block in Task 12 below still carries the original wording and is wrong on this point.**
- **The two regen RPCs arrived as names with no semantics** (`636245a`). Spec 5.3 and 5.6 define claiming and failure as concrete SQL; this plan replaced that with `claim_stale_recipes(p_limit)` and `regen_failed(p_id, p_msg)` and kept only the signatures. Nothing in the branch said that claiming must set `claimed_at`, apply the 20-second quiet window, honour the 5-minute lease, or exclude rows that had already failed three times on the current `body_rev` — all load-bearing, and whoever wrote Cayenne migration 014 had two names and nothing else. `docs/sql/regen-rpcs.md` now carries the reference SQL, the named-argument calling convention, grants, and the `moddatetime` caveat.
- **A `try/except ImportError` around the `RecatWorker` import was scaffolding that outlived its reason** (`84b93b7`). It would have swallowed an ImportError anywhere inside `recat_worker.py`, `gemini`, or `core.stages.categorize`, silently degrading the process to regen-only while logging a cheerful "Background workers started: ['RegenWorker']".
- Seven further defects were found and fixed inside the branch rather than after it: bulk recategorise paging from the nil UUID instead of an empty string (`88692cc`), a non-list raw body column being iterated as a string (`be824ee`), the duration backfill truncating a fractional `base_servings` (`09b5d13`) and not guarding per-row writes against a mid-run failure (`706f8f8`), `RecatWorker` counting junction rows instead of distinct recipes (`9dce81a`), `categorize_batch` failures not reaching the worker (`bfee493`), and the regen failure path escaping `_process` (`2bb5445`).
- Follow-on note: #34's refusal to start with the flag set and no service client is correct in production but collided with the test suite, where `live_writes_blocked()` always withholds the client — fixed in PR #38 by having the suite set its own `REGEN_WORKER_ENABLED`.

**Goal:** Make RecipeParser the sole writer of derived recipe data: carry raw text through ingestion, regenerate derived columns for edited recipes via a polling worker, run additive bulk recategorisation on taxonomy additions, and parse durations and servings into structured columns.

**Architecture:** Pure logic lives in `recipeparser/core` (no I/O imports, enforced by ruff TID): a duration/servings parser, a regen planner, and a categorise batch planner. Adapters in `recipeparser/adapters` own Supabase and Gemini calls: a `RegenWorker` and a `RecatWorker` with a `run_once()` each, driven by one asyncio loop started from a FastAPI lifespan hook. The ingestion writer gains the raw and structured columns so new recipes arrive complete.

**Tech Stack:** Python 3.9, FastAPI, Pydantic v2, `google-genai` (`client.models.generate_content` via `_call_with_retry`), `supabase-py` 2.x, `httpx`, pytest, syrupy snapshots.

**Spec:** `docs/superpowers/specs/2026-09-07-recipe-edit-philosophy-design.md`

## Global Constraints

- Python 3.9 syntax only: `Optional[X]`, `List[X]`, `Dict[K, V]` from `typing`; no `X | Y`, no `match`.
- `recipeparser/core/**` must not import from `recipeparser.io` or `recipeparser.adapters` (ruff TID rule; run `ruff check recipeparser` before every commit).
- Tests never reach a real Supabase project: `recipeparser.config.live_writes_blocked()` is true under pytest unless `ALLOW_LIVE_WRITES_IN_TESTS=1`. Tests that exercise the writer set that env var **and** patch `httpx.post`.
- All Gemini calls go through `recipeparser.gemini._call_with_retry(client, model=, contents=, config=, what=)`. Never hardcode a model name: pass `model=GEMINI_MODEL`, imported from `recipeparser.config` (default `gemini-3.1-flash-lite`, overridable by the `GEMINI_MODEL` env var). `_call_with_retry` applies `_finalize_config` (HTTP timeout + thinking budget) itself, and logs usage metadata under the `what=` label — give every new call site a specific one, or its tokens land in the log as an anonymous "Gemini call". Embeddings via `recipeparser.gemini.get_embeddings`.
- Every prompt is a `build_*_prompt(...)` function, never an f-string inside the call site. `tests/goldens/test_prompts_snapshot.py` snapshots each builder and asserts the call site sends exactly what the builder renders; a new call needs both a builder and a snapshot there.
- Changing a prompt or a Gemini-facing schema is a snapshot change, not just a code change. Re-approve with `pytest tests/goldens tests/snapshots --snapshot-update -q`, review the resulting `.ambr` diff, and stage `tests/goldens` alongside `tests/snapshots`. Recorded replies are keyed by prompt *body*, so a changed rule warns (`prompt_sha256 mismatch`) but still serves — see the scoped `filterwarnings` precedent in `tests/goldens/test_stages_golden.py:63-79`.
- **Gate — verify before starting Task 6.** Database prerequisites live in the Cayenne repo (plan `2026-09-07-recipe-edit-schema.md`): migrations 013 and 014 applied. As of the rebase onto `master` (2026-09-08) neither the migrations nor that plan exist in the sibling `cayenne/` checkout, which holds only `SpecificationDocumentation/`. Tasks 1-5 and 8-12 are unblocked; Tasks 6, 7 and 10 are unit-testable against fakes but cannot be integration-verified until the schema lands. Confirm the RPCs exist before treating those three as done.
- Baseline on `master` at rebase time: `pytest -q` -> 759 passed, 27 snapshots, ~10s. Every task's "full suite green" check is measured against that. The RPC signatures this plan relies on: `claim_stale_recipes(p_limit integer)` returning `id, user_id, title, ingredient_lines, direction_steps, body_rev`; `regen_failed(p_id uuid, p_msg text)`.
- New env var: `REGEN_WORKER_ENABLED` (truthy values `1`, `true`, `yes`, `on`). Absent means the worker never starts.
- Run the full suite with `pytest -q` before each commit; it must stay green.
- Commit messages: conventional prefix (`feat:`, `test:`, `docs:`), imperative mood.

---

### Task 1: `line_index` on structured ingredients

**Files:**
- Modify: `recipeparser/models.py:98-108` (`StructuredIngredient`)
- Modify: `recipeparser/gemini.py:655-699` (`build_refine_prompt`, the `1. STRUCTURED INGREDIENTS:` block)
- Modify: `recipeparser/core/stages/refine.py`
- Create: `tests/unit/stages/test_refine_line_index.py`
- Modify: `tests/snapshots/__snapshots__/*` and `tests/goldens/__snapshots__/*` (regenerated)

**Interfaces:**
- Produces: `StructuredIngredient.line_index: Optional[int]` (0-based index into the raw ingredient list; `None` allowed).
- Produces: `refine()` raises `ValueError` when any `line_index` is out of range for `raw.ingredients` or repeated.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/stages/test_refine_line_index.py
"""refine() validates line_index against the raw ingredient list (spec 4.3)."""
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.models import (
    CayenneRefinement,
    RecipeExtraction,
    StructuredIngredient,
    TokenizedDirection,
)


def _raw(n_lines: int) -> RecipeExtraction:
    return RecipeExtraction(
        name="Cake",
        ingredients=[f"{i} cups thing {i}" for i in range(n_lines)],
        directions=["Mix."],
    )


def _ing(id_: str, line_index):
    return StructuredIngredient(
        id=id_, amount=1.0, unit="cup", name="thing",
        fallback_string="1 cup thing", line_index=line_index,
    )


def _refinement(indices):
    return CayenneRefinement(
        title="Cake",
        base_servings=4,
        structured_ingredients=[_ing(f"ing_{i:02d}", idx) for i, idx in enumerate(indices, 1)],
        tokenized_directions=[TokenizedDirection(step=1, text="Mix.")],
    )


def _run(raw, refinement):
    from recipeparser.core.stages.refine import refine
    with patch("recipeparser.core.stages.refine.refine_recipe_for_cayenne", return_value=refinement):
        return refine(raw, client=MagicMock())


def test_line_index_defaults_to_none():
    ing = StructuredIngredient(id="ing_01", name="x", fallback_string="x")
    assert ing.line_index is None


def test_valid_indices_pass():
    result = _run(_raw(3), _refinement([0, 1, 2]))
    assert [i.line_index for i in result.structured_ingredients] == [0, 1, 2]


def test_none_indices_pass():
    result = _run(_raw(2), _refinement([None, None]))
    assert len(result.structured_ingredients) == 2


def test_header_line_skipped_is_fine():
    # 3 raw lines, header at 1 produces no entry
    result = _run(_raw(3), _refinement([0, 2]))
    assert [i.line_index for i in result.structured_ingredients] == [0, 2]


def test_out_of_range_raises():
    with pytest.raises(ValueError, match="line_index 5"):
        _run(_raw(3), _refinement([0, 5]))


def test_negative_raises():
    with pytest.raises(ValueError, match="line_index -1"):
        _run(_raw(3), _refinement([-1]))


def test_duplicate_raises():
    with pytest.raises(ValueError, match="line_index 1 .*twice"):
        _run(_raw(3), _refinement([1, 1]))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/stages/test_refine_line_index.py -v`
Expected: FAIL — `StructuredIngredient` has no field `line_index` (pydantic `ValidationError` / `AttributeError`).

- [ ] **Step 3: Add the field to the model**

In `recipeparser/models.py`, inside `StructuredIngredient` after `is_ai_converted`:

```python
    line_index: Optional[int] = Field(
        default=None,
        description=(
            "0-based index of the raw ingredient line this entry was parsed from. "
            "Every ingredient line yields exactly one entry; section-header lines "
            "yield none. null only when the entry is not tied to a single line."
        ),
    )
```

- [ ] **Step 4: Add validation to the REFINE stage**

In `recipeparser/core/stages/refine.py`, add after `_validate_fat_tokens`:

```python
def _validate_line_index(refinement: CayenneRefinement, raw: RecipeExtraction) -> None:
    """
    Every non-null ``line_index`` must point inside ``raw.ingredients`` and no
    two entries may claim the same line.  Raises ValueError on the first
    violation.  ``None`` is permitted (the client then falls back to bumping
    body_rev on an amount edit — spec 4.3).
    """
    n_lines = len(raw.ingredients)
    seen: Dict[int, str] = {}
    for ing in refinement.structured_ingredients:
        idx = ing.line_index
        if idx is None:
            continue
        if idx < 0 or idx >= n_lines:
            raise ValueError(
                f"refine(): ingredient '{ing.id}' has line_index {idx} but the raw "
                f"recipe has {n_lines} ingredient line(s)."
            )
        if idx in seen:
            raise ValueError(
                f"refine(): line_index {idx} is claimed twice ('{seen[idx]}' and '{ing.id}')."
            )
        seen[idx] = ing.id
```

And in `refine()` after `_validate_fat_tokens(result)`:

```python
    _validate_line_index(result, raw)
```

- [ ] **Step 5: Ask the model for the index**

In `build_refine_prompt` (`recipeparser/gemini.py:655`), inside the `1. STRUCTURED INGREDIENTS:` block, add a bullet after the `fallback_string` line:

```
   - "line_index" is the 0-based position of the source line in the RAW RECIPE
     ingredients list. Every ingredient line gets exactly one entry with its
     index. A section header line (e.g. "For the sauce:") gets no entry.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/unit/stages/test_refine_line_index.py -v`
Expected: 7 PASS.

- [ ] **Step 7: Regenerate snapshots and run the whole suite**

Run: `pytest tests/goldens tests/snapshots --snapshot-update -q && pytest -q`

This task moves four snapshot files, not one. Read the diff and confirm each change is only the new field or the new rule:

| Snapshot | Why it moves |
|---|---|
| `tests/snapshots/__snapshots__/test_stage_snapshots.ambr` | the refine snapshot gains `line_index: None` |
| `tests/goldens/__snapshots__/test_stages_golden.ambr` | every fixture's refined `model_dump()` gains `line_index` |
| `tests/goldens/__snapshots__/test_prompts_snapshot.ambr` | the three `build_refine_prompt` snapshots gain the new bullet (`test_prompts_snapshot.py:122-129`) |
| `tests/goldens/__snapshots__/test_prompts_snapshot.ambr` | `test_cayenne_refinement_schema` — the JSON schema sent to Gemini gains `line_index` |

Recorded Gemini replies are keyed by prompt *body*, which this rule does not touch, so they still serve; expect `prompt_sha256 mismatch ... refine-NN.json` warnings. They do not fail the suite (there is no `-W error`). If they add noise, scope an `ignore:` filter the way `tests/goldens/test_stages_golden.py:63-79` already does for the phases rule — do not widen an existing one.

Expected: those snapshots updated, full suite green (759 passing at baseline, plus the 7 new tests).

- [ ] **Step 8: Commit**

```bash
git add recipeparser/models.py recipeparser/gemini.py recipeparser/core/stages/refine.py tests/unit/stages/test_refine_line_index.py tests/snapshots tests/goldens
git commit -m "feat: structured ingredients carry line_index; REFINE validates it"
```

---

### Task 2: Duration and servings parser

**Files:**
- Create: `recipeparser/core/durations.py`
- Create: `tests/fixtures/duration_cases.json`
- Create: `tests/unit/test_durations.py`

**Interfaces:**
- Produces: `Span(min: Optional[int], max: Optional[int], note: Optional[str])` dataclass.
- Produces: `parse_duration(text: Optional[str]) -> Span` (minutes).
- Produces: `parse_servings(text: Optional[str]) -> Span`.
- Produces: `duration_columns(prep_text, cook_text, servings_text, fallback_base_servings) -> Dict[str, Any]` returning the nine structured columns plus `base_servings`.
- Produces: the fixture file, which the Cayenne client plan copies verbatim for its TypeScript parser.

- [ ] **Step 1: Write the shared fixture**

```json
{
  "duration": [
    {"input": null, "min": null, "max": null, "note": null},
    {"input": "", "min": null, "max": null, "note": null},
    {"input": "   ", "min": null, "max": null, "note": null},
    {"input": "15 mins", "min": 15, "max": 15, "note": null},
    {"input": "30", "min": 30, "max": 30, "note": null},
    {"input": "30-45 minutes", "min": 30, "max": 45, "note": null},
    {"input": "20 to 25 min", "min": 20, "max": 25, "note": null},
    {"input": "1 hr 30 min", "min": 90, "max": 90, "note": null},
    {"input": "1.5 hours", "min": 90, "max": 90, "note": null},
    {"input": "1 1/2 hours", "min": 90, "max": 90, "note": null},
    {"input": "1½ hours", "min": 90, "max": 90, "note": null},
    {"input": "1-2 hours", "min": 60, "max": 120, "note": null},
    {"input": "1–2 hours", "min": 60, "max": 120, "note": null},
    {"input": "1h 15m", "min": 75, "max": 75, "note": null},
    {"input": "90 minutes", "min": 90, "max": 90, "note": null},
    {"input": "1 day", "min": 1440, "max": 1440, "note": null},
    {"input": "45 min plus chilling", "min": 45, "max": 45, "note": "plus chilling"},
    {"input": "10 minutes (plus resting)", "min": 10, "max": 10, "note": "plus resting"},
    {"input": "35 mins, plus 4 hrs marinating", "min": 35, "max": 35, "note": "plus 4 hrs marinating"},
    {"input": "overnight", "min": null, "max": null, "note": "overnight"},
    {"input": "until golden", "min": null, "max": null, "note": "until golden"}
  ],
  "servings": [
    {"input": null, "min": null, "max": null, "note": null},
    {"input": "", "min": null, "max": null, "note": null},
    {"input": "4", "min": 4, "max": 4, "note": null},
    {"input": "4 servings", "min": 4, "max": 4, "note": null},
    {"input": "Serves 6", "min": 6, "max": 6, "note": null},
    {"input": "2-4", "min": 2, "max": 4, "note": null},
    {"input": "6-8 as a starter", "min": 6, "max": 8, "note": "as a starter"},
    {"input": "makes 12 cookies", "min": 12, "max": 12, "note": "cookies"},
    {"input": "about 4", "min": 4, "max": 4, "note": "about"},
    {"input": "a crowd", "min": null, "max": null, "note": "a crowd"}
  ]
}
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/test_durations.py
"""Duration/servings parser — every case in the shared fixture (spec 3.6)."""
import json
from pathlib import Path

import pytest

from recipeparser.core.durations import Span, duration_columns, parse_duration, parse_servings

_CASES = json.loads((Path(__file__).parent.parent / "fixtures" / "duration_cases.json").read_text("utf-8"))


@pytest.mark.parametrize("case", _CASES["duration"], ids=lambda c: repr(c["input"]))
def test_parse_duration(case):
    assert parse_duration(case["input"]) == Span(case["min"], case["max"], case["note"])


@pytest.mark.parametrize("case", _CASES["servings"], ids=lambda c: repr(c["input"]))
def test_parse_servings(case):
    assert parse_servings(case["input"]) == Span(case["min"], case["max"], case["note"])


def test_duration_columns_shape():
    cols = duration_columns("15 mins", "1-2 hours", "2-4", fallback_base_servings=None)
    assert cols == {
        "prep_min_minutes": 15, "prep_max_minutes": 15, "prep_note": None,
        "cook_min_minutes": 60, "cook_max_minutes": 120, "cook_note": None,
        "servings_min": 2, "servings_max": 4, "servings_note": None,
        "base_servings": 2,
    }


def test_duration_columns_base_servings_fallback():
    cols = duration_columns(None, None, "a crowd", fallback_base_servings=6)
    assert cols["servings_min"] is None
    assert cols["servings_note"] == "a crowd"
    assert cols["base_servings"] == 6
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/unit/test_durations.py -q`
Expected: FAIL — `ModuleNotFoundError: recipeparser.core.durations`.

- [ ] **Step 4: Implement the parser**

```python
# recipeparser/core/durations.py
"""
recipeparser/core/durations.py — deterministic duration and servings parsing.

Spec 3.6: prep/cook times and servings are stored as min/max integers plus a
free-text note and displayed as "x to y".  This module is the Python half of
a two-implementation parser; the TypeScript half in Cayenne runs the same
fixture (tests/fixtures/duration_cases.json).  Change the rules here and there
together, and add the case to the fixture first.

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

_FRACTIONS = {
    "½": " 1/2", "¼": " 1/4", "¾": " 3/4", "⅓": " 1/3", "⅔": " 2/3",
    "⅛": " 1/8", "⅜": " 3/8", "⅝": " 5/8", "⅞": " 7/8",
}
_NUM = r"(\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)"
_UNIT = r"(hours?|hrs?|h|minutes?|mins?|m|days?|d)"
_UNIT_MINUTES = {
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "d": 1440, "day": 1440, "days": 1440,
}
_GROUP_RE = re.compile(rf"{_NUM}\s*{_UNIT}?")
_PURE_NUM_RE = re.compile(rf"^{_NUM}$")
_QUALIFIER_RE = re.compile(r"\s*(?:\bplus\b|\+|,|\()")
_RANGE_RE = re.compile(r"\s*(?:-|\bto\b)\s*")
_SERVING_WORDS_RE = re.compile(r"\b(serves|servings?|portions?|makes|yields?|people|persons?)\b")
_SERVING_RANGE_RE = re.compile(rf"^{_NUM}(?:\s*(?:-|\bto\b)\s*{_NUM})?")
_APPROX_RE = re.compile(r"^(about|approx\.?|approximately|roughly)\s+")


@dataclass(frozen=True)
class Span:
    min: Optional[int]
    max: Optional[int]
    note: Optional[str]


_EMPTY = Span(None, None, None)


def _number(token: str) -> float:
    token = token.strip()
    if " " in token:                      # mixed fraction "1 1/2"
        whole, frac = token.split(None, 1)
        return float(whole) + _number(frac)
    if "/" in token:
        num, den = token.split("/", 1)
        return float(num) / float(den)
    return float(token)


def _normalise(text: str) -> str:
    for glyph, ascii_ in _FRACTIONS.items():
        text = text.replace(glyph, ascii_)
    text = text.lower().replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def _split_qualifier(text: str) -> Tuple[str, Optional[str]]:
    """'45 min plus chilling' -> ('45 min', 'plus chilling')."""
    m = _QUALIFIER_RE.search(text)
    if not m:
        return text, None
    head = text[: m.start()].strip()
    tail = text[m.start():].strip().lstrip(",(").rstrip(")").strip()
    if tail.startswith("+"):
        tail = "plus " + tail[1:].strip()
    return head, (tail or None)


def _side_minutes(side: str) -> Optional[float]:
    """Minutes for one side of a range, or None if anything is unparseable."""
    side = side.strip()
    if not side:
        return None
    total = 0.0
    last_unit: Optional[str] = None
    consumed = 0
    for m in _GROUP_RE.finditer(side):
        if side[consumed: m.start()].strip():
            return None                   # stray text between groups
        n = _number(m.group(1))
        unit = m.group(2)
        if unit:
            total += n * _UNIT_MINUTES[unit]
            last_unit = unit
        elif last_unit is None:
            total += n                    # bare number: minutes
        else:
            total += n                    # "1 hr 30" -> trailing minutes
        consumed = m.end()
    if consumed == 0 or side[consumed:].strip():
        return None
    return total


def parse_duration(text: Optional[str]) -> Span:
    if text is None or not text.strip():
        return _EMPTY
    original = text.strip()
    head, note = _split_qualifier(_normalise(original))
    parts = _RANGE_RE.split(head, maxsplit=1)
    if len(parts) == 2:
        lo_text, hi_text = parts
        hi = _side_minutes(hi_text)
        if _PURE_NUM_RE.match(lo_text.strip()) and hi is not None:
            unit_m = re.search(_UNIT, hi_text)
            factor = _UNIT_MINUTES[unit_m.group(1)] if unit_m else 1
            lo: Optional[float] = _number(lo_text) * factor
        else:
            lo = _side_minutes(lo_text)
    else:
        lo = hi = _side_minutes(head)
    if lo is None or hi is None:
        return Span(None, None, original)
    return Span(int(round(lo)), int(round(hi)), note)


def parse_servings(text: Optional[str]) -> Span:
    if text is None or not text.strip():
        return _EMPTY
    original = text.strip()
    norm = _normalise(original)
    approx: Optional[str] = None
    m = _APPROX_RE.match(norm)
    if m:
        approx = m.group(1)
        norm = norm[m.end():]
    cleaned = re.sub(r"\s+", " ", _SERVING_WORDS_RE.sub(" ", norm)).strip()
    m = _SERVING_RANGE_RE.match(cleaned)
    if not m:
        return Span(None, None, original)
    lo = _number(m.group(1))
    hi = _number(m.group(2)) if m.group(2) else lo
    rest = cleaned[m.end():].strip(" ,")
    note = " ".join(p for p in (approx, rest) if p) or None
    return Span(int(round(lo)), int(round(hi)), note)


def duration_columns(
    prep_text: Optional[str],
    cook_text: Optional[str],
    servings_text: Optional[str],
    fallback_base_servings: Optional[int],
) -> Dict[str, Any]:
    """The nine structured columns plus base_servings, for a writer or backfill."""
    p, c, s = parse_duration(prep_text), parse_duration(cook_text), parse_servings(servings_text)
    return {
        "prep_min_minutes": p.min, "prep_max_minutes": p.max, "prep_note": p.note,
        "cook_min_minutes": c.min, "cook_max_minutes": c.max, "cook_note": c.note,
        "servings_min": s.min, "servings_max": s.max, "servings_note": s.note,
        "base_servings": s.min if s.min is not None else fallback_base_servings,
    }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/unit/test_durations.py -q`
Expected: all fixture cases PASS. If a case fails, fix the parser, not the fixture: the fixture is the contract shared with the client.

- [ ] **Step 6: Lint and commit**

```bash
ruff check recipeparser/core/durations.py
git add recipeparser/core/durations.py tests/fixtures/duration_cases.json tests/unit/test_durations.py
git commit -m "feat: deterministic duration and servings parser with shared fixture"
```

---

### Task 3: Carry raw lines and structured durations through ASSEMBLE

**Files:**
- Modify: `recipeparser/models.py:132-174` (`CayenneRecipe`)
- Create: `recipeparser/core/regen.py` (first half: `strip_fat_tokens`, `raw_lines_from_derived`)
- Modify: `recipeparser/core/stages/assemble.py`
- Modify: `recipeparser/core/pipeline.py:290-385` (three `assemble(...)` calls)
- Create: `tests/unit/stages/test_assemble_raw.py`
- Create: `tests/unit/test_regen.py` (first half)

**Interfaces:**
- Produces on `CayenneRecipe`: `ingredient_lines: List[str] = []`, `direction_steps: List[str] = []`, and the nine optional duration/servings fields `prep_min_minutes`, `prep_max_minutes`, `prep_note`, `cook_min_minutes`, `cook_max_minutes`, `cook_note`, `servings_min`, `servings_max`, `servings_note`.
- Produces: `strip_fat_tokens(text: str) -> str` and `raw_lines_from_derived(structured, tokenized) -> Tuple[List[str], List[str]]` in `recipeparser/core/regen.py`.
- Produces: `assemble(..., ingredient_lines: Optional[List[str]] = None, direction_steps: Optional[List[str]] = None, servings_text: Optional[str] = None)`. When lines are `None` or empty they are derived from the refinement.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_regen.py
"""Pure regen helpers (spec 5.2, 5.7)."""
from recipeparser.core.regen import raw_lines_from_derived, strip_fat_tokens
from recipeparser.models import StructuredIngredient, TokenizedDirection


def test_strip_fat_tokens_replaces_with_fallback():
    assert strip_fat_tokens("Mix {{ing_01|1.5 cups flour}} into {{ing_02|the eggs}}.") == \
        "Mix 1.5 cups flour into the eggs."


def test_strip_fat_tokens_leaves_plain_text():
    assert strip_fat_tokens("Bake for 20 minutes.") == "Bake for 20 minutes."


def test_raw_lines_from_derived():
    structured = [
        StructuredIngredient(id="ing_01", name="flour", fallback_string="1 1/2 cups flour"),
        StructuredIngredient(id="ing_02", name="egg", fallback_string="2 eggs"),
    ]
    tokenized = [
        TokenizedDirection(step=1, text="Mix {{ing_01|flour}} and {{ing_02|eggs}}."),
        TokenizedDirection(step=2, text="Bake."),
    ]
    lines, steps = raw_lines_from_derived(structured, tokenized)
    assert lines == ["1 1/2 cups flour", "2 eggs"]
    assert steps == ["Mix flour and eggs.", "Bake."]
```

```python
# tests/unit/stages/test_assemble_raw.py
"""assemble() carries raw lines and structured durations (spec 3.2, 3.6, 5.7)."""
from recipeparser.core.models import SourceMeta
from recipeparser.core.stages.assemble import assemble
from recipeparser.models import CayenneRefinement, StructuredIngredient, TokenizedDirection


def _refinement():
    return CayenneRefinement(
        title="Cake",
        base_servings=8,
        structured_ingredients=[
            StructuredIngredient(id="ing_01", amount=1.5, unit="cups", name="flour",
                                 fallback_string="1 1/2 cups flour", line_index=0),
        ],
        tokenized_directions=[TokenizedDirection(step=1, text="Mix {{ing_01|flour}}.")],
    )


def test_raw_lines_passed_through():
    r = assemble(_refinement(), [0.0] * 3, None, None, {},
                 ingredient_lines=["1 1/2 cups flour"], direction_steps=["Mix flour."])
    assert r.ingredient_lines == ["1 1/2 cups flour"]
    assert r.direction_steps == ["Mix flour."]


def test_raw_lines_derived_when_absent():
    r = assemble(_refinement(), [0.0] * 3, None, None, {})
    assert r.ingredient_lines == ["1 1/2 cups flour"]
    assert r.direction_steps == ["Mix flour."]


def test_durations_and_servings_parsed():
    r = assemble(_refinement(), [0.0] * 3, None, None, {},
                 prep_time="15 mins", cook_time="1-2 hours", servings_text="2-4")
    assert (r.prep_min_minutes, r.prep_max_minutes, r.prep_note) == (15, 15, None)
    assert (r.cook_min_minutes, r.cook_max_minutes) == (60, 120)
    assert (r.servings_min, r.servings_max) == (2, 4)
    assert r.base_servings == 2          # servings_min wins over REFINE's 8


def test_base_servings_falls_back_to_refinement():
    r = assemble(_refinement(), [0.0] * 3, None, None, {}, servings_text=None)
    assert r.base_servings == 8


def test_meta_time_wins_and_is_parsed():
    meta = SourceMeta(prep_time="45 min plus chilling")
    r = assemble(_refinement(), [0.0] * 3, None, None, {}, prep_time="10 mins", meta=meta)
    assert r.prep_time == "45 min plus chilling"
    assert (r.prep_min_minutes, r.prep_note) == (45, "plus chilling")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_regen.py tests/unit/stages/test_assemble_raw.py -q`
Expected: FAIL — `ModuleNotFoundError: recipeparser.core.regen`; `assemble() got an unexpected keyword argument 'ingredient_lines'`.

- [ ] **Step 3: Add fields to `CayenneRecipe`**

In `recipeparser/models.py`, inside `CayenneRecipe` after `difficulty`:

```python
    # Raw, user-owned body (spec 3.2). Empty lists on legacy objects; the writer
    # and regen worker derive them from the structured data when empty.
    ingredient_lines: List[str] = Field(default_factory=list)
    direction_steps: List[str] = Field(default_factory=list)
    # Structured durations and servings (spec 3.6). prep_time/cook_time text stay
    # for Paprika export and until the client reads these.
    prep_min_minutes: Optional[int] = None
    prep_max_minutes: Optional[int] = None
    prep_note: Optional[str] = None
    cook_min_minutes: Optional[int] = None
    cook_max_minutes: Optional[int] = None
    cook_note: Optional[str] = None
    servings_min: Optional[int] = None
    servings_max: Optional[int] = None
    servings_note: Optional[str] = None
```

- [ ] **Step 4: Create the pure regen helpers**

```python
# recipeparser/core/regen.py
"""
recipeparser/core/regen.py — pure helpers for regenerating derived recipe data.

Spec 5.2: no I/O here.  The adapter (recipeparser/adapters/regen_worker.py)
reads rows, calls these, and writes results.

No imports from recipeparser.io or recipeparser.adapters are permitted here.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Tuple

from recipeparser.models import (
    CayenneRefinement,
    RecipeExtraction,
    StructuredIngredient,
    TokenizedDirection,
)

_FAT_TOKEN_RE = re.compile(r"\{\{[^|]+\|([^}]+)\}\}")


def strip_fat_tokens(text: str) -> str:
    """'Mix {{ing_01|flour}}' -> 'Mix flour'."""
    return _FAT_TOKEN_RE.sub(r"\1", text)


def raw_lines_from_derived(
    structured: List[StructuredIngredient],
    tokenized: List[TokenizedDirection],
) -> Tuple[List[str], List[str]]:
    """Raw ingredient lines and direction steps recovered from derived data (spec 5.7)."""
    lines = [ing.fallback_string for ing in structured]
    steps = [strip_fat_tokens(step.text) for step in tokenized]
    return lines, steps
```

- [ ] **Step 5: Extend `assemble()`**

In `recipeparser/core/stages/assemble.py`:

Add imports:

```python
from recipeparser.core.durations import duration_columns
from recipeparser.core.regen import raw_lines_from_derived
```

Extend the signature (after `meta`):

```python
    ingredient_lines: Optional[List[str]] = None,
    direction_steps: Optional[List[str]] = None,
    servings_text: Optional[str] = None,
```

Add to the docstring `Args`:

```
        ingredient_lines: Raw ingredient lines from EXTRACT. When None or empty,
                          derived from the refinement's fallback strings.
        direction_steps:  Raw direction steps from EXTRACT. When None or empty,
                          derived from the tokenized text with tokens stripped.
        servings_text:    The extracted servings string ("4", "2-4"). Parsed into
                          servings_min/max/note; servings_min becomes base_servings.
```

After the `meta` block that resolves `prep_time`/`cook_time`, add:

```python
    derived_lines, derived_steps = raw_lines_from_derived(
        recipe.structured_ingredients, recipe.tokenized_directions
    )
    lines = list(ingredient_lines) if ingredient_lines else derived_lines
    steps = list(direction_steps) if direction_steps else derived_steps
    cols = duration_columns(prep_time, cook_time, servings_text, recipe.base_servings)
```

Then in the `IngestResponse(...)` constructor replace `base_servings=recipe.base_servings,` with `base_servings=cols["base_servings"],` and add:

```python
        ingredient_lines=lines,
        direction_steps=steps,
        prep_min_minutes=cols["prep_min_minutes"],
        prep_max_minutes=cols["prep_max_minutes"],
        prep_note=cols["prep_note"],
        cook_min_minutes=cols["cook_min_minutes"],
        cook_max_minutes=cols["cook_max_minutes"],
        cook_note=cols["cook_note"],
        servings_min=cols["servings_min"],
        servings_max=cols["servings_max"],
        servings_note=cols["servings_note"],
```

- [ ] **Step 6: Pass the raw values from the pipeline**

In `recipeparser/core/pipeline.py` `_process_chunk`, the full-pipeline `assemble(` call gains:

```python
                ingredient_lines=list(raw.ingredients),
                direction_steps=list(raw.directions),
                servings_text=getattr(raw, "servings", None),
```

Both fast-path `assemble(` calls gain:

```python
                ingredient_lines=list(getattr(pr, "ingredient_lines", []) or []),
                direction_steps=list(getattr(pr, "direction_steps", []) or []),
                servings_text=None,
```

(Empty lists fall through to derivation inside `assemble`.)

- [ ] **Step 7: Run the tests to verify they pass**

Run: `pytest tests/unit/test_regen.py tests/unit/stages/test_assemble_raw.py -q && pytest -q`
Expected: new tests PASS; the full suite green. Eleven new fields on `CayenneRecipe` change every snapshotted recipe shape, so re-approve both trees: `pytest tests/goldens tests/snapshots --snapshot-update -q`, then read the diff of `test_stage_snapshots.ambr` and `test_stages_golden.ambr` and confirm only the new fields appear. No prompt changes here, so `test_prompts_snapshot.ambr` must stay still — if it moves, something went into a prompt that should not have.

- [ ] **Step 8: Lint and commit**

```bash
ruff check recipeparser
git add recipeparser/models.py recipeparser/core/regen.py recipeparser/core/stages/assemble.py recipeparser/core/pipeline.py tests/unit/test_regen.py tests/unit/stages/test_assemble_raw.py tests/snapshots tests/goldens
git commit -m "feat: carry raw lines and structured durations through ASSEMBLE"
```

---

### Task 4: Writer emits the new columns

**Files:**
- Modify: `recipeparser/io/writers/supabase.py:186-208` (the `row` dict)
- Create: `tests/unit/writers/test_supabase_row.py`

**Interfaces:**
- Consumes: `IngestResponse` fields from Task 3.
- Produces: the `recipes` INSERT carries `ingredient_lines`, `direction_steps`, `body_rev = 0`, `derived_rev = 0`, `amount_overrides = {}`, and the nine duration/servings columns. `prep_time`/`cook_time` text still written.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/writers/test_supabase_row.py
"""write_recipe_to_supabase sends raw body, bookkeeping and duration columns (spec 5.7)."""
from unittest.mock import MagicMock, patch

import pytest

from recipeparser.io.writers.supabase import write_recipe_to_supabase
from recipeparser.models import IngestResponse, StructuredIngredient, TokenizedDirection


def _recipe() -> IngestResponse:
    return IngestResponse(
        title="Cake",
        base_servings=2,
        structured_ingredients=[StructuredIngredient(id="ing_01", name="flour",
                                                     fallback_string="1 cup flour", line_index=0)],
        tokenized_directions=[TokenizedDirection(step=1, text="Mix {{ing_01|flour}}.")],
        embedding=[0.0] * 1536,
        ingredient_lines=["1 cup flour"],
        direction_steps=["Mix flour."],
        prep_time="15 mins",
        prep_min_minutes=15, prep_max_minutes=15, prep_note=None,
        servings_min=2, servings_max=4, servings_note=None,
    )


@pytest.fixture()
def posted(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    monkeypatch.setenv("ALLOW_LIVE_WRITES_IN_TESTS", "1")
    resp = MagicMock(status_code=201, text="")
    with patch("recipeparser.io.writers.supabase.httpx.post", return_value=resp) as post:
        write_recipe_to_supabase(_recipe(), user_id="u1", recipe_id="r1")
        yield post.call_args.kwargs["json"]


def test_row_has_raw_body_and_bookkeeping(posted):
    assert posted["ingredient_lines"] == ["1 cup flour"]
    assert posted["direction_steps"] == ["Mix flour."]
    assert posted["body_rev"] == 0
    assert posted["derived_rev"] == 0
    assert posted["amount_overrides"] == {}


def test_row_has_structured_durations(posted):
    assert posted["prep_time"] == "15 mins"
    assert (posted["prep_min_minutes"], posted["prep_max_minutes"], posted["prep_note"]) == (15, 15, None)
    assert (posted["servings_min"], posted["servings_max"]) == (2, 4)
    assert posted["cook_min_minutes"] is None


def test_line_index_serialised(posted):
    assert posted["structured_ingredients"][0]["line_index"] == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/writers/test_supabase_row.py -q`
Expected: FAIL — `KeyError: 'ingredient_lines'`.

- [ ] **Step 3: Add the columns to the row**

In `write_recipe_to_supabase`, after the `"embedding": recipe.embedding,` line inside `row = {...}`:

```python
        # Raw, user-owned body + derived bookkeeping (spec 3.2/3.3). A freshly
        # ingested recipe is never stale: body_rev == derived_rev == 0.
        "ingredient_lines": list(recipe.ingredient_lines),
        "direction_steps": list(recipe.direction_steps),
        "body_rev": 0,
        "derived_rev": 0,
        "amount_overrides": {},
        # Structured durations and servings (spec 3.6).
        "prep_min_minutes": recipe.prep_min_minutes,
        "prep_max_minutes": recipe.prep_max_minutes,
        "prep_note": recipe.prep_note,
        "cook_min_minutes": recipe.cook_min_minutes,
        "cook_max_minutes": recipe.cook_max_minutes,
        "cook_note": recipe.cook_note,
        "servings_min": recipe.servings_min,
        "servings_max": recipe.servings_max,
        "servings_note": recipe.servings_note,
```

The row already carries `source`, `notes`, `rating`, `nutritional_info`, `description`, `difficulty` — merged to `master` in #17 and visible at `recipeparser/io/writers/supabase.py:196-203`. Leave them alone; this step only appends.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/writers -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add recipeparser/io/writers/supabase.py tests/unit/writers/test_supabase_row.py
git commit -m "feat: writer emits raw body, bookkeeping and duration columns"
```

---

### Task 5: Regen planner — `build_extraction` and `build_update`

**Files:**
- Modify: `recipeparser/core/regen.py`
- Modify: `tests/unit/test_regen.py`

**Interfaces:**
- Produces: `build_extraction(row: Mapping[str, Any]) -> RecipeExtraction` from a claimed row (`title`, `ingredient_lines`, `direction_steps`).
- Produces: `build_update(refinement: CayenneRefinement, embedding: List[float], read_rev: int) -> Dict[str, Any]` — the exact PATCH payload for spec 5.5.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_regen.py`)

```python
from recipeparser.core.regen import build_extraction, build_update
from recipeparser.models import CayenneRefinement


def test_build_extraction_from_row():
    row = {"title": "Cake", "ingredient_lines": ["1 cup flour", "2 eggs"], "direction_steps": ["Mix."]}
    ex = build_extraction(row)
    assert ex.name == "Cake"
    assert ex.ingredients == ["1 cup flour", "2 eggs"]
    assert ex.directions == ["Mix."]
    assert ex.servings is None


def test_build_extraction_tolerates_missing_lists():
    ex = build_extraction({"title": "Cake"})
    assert ex.ingredients == [] and ex.directions == []


def test_build_update_payload():
    ref = CayenneRefinement(
        title="Cake", base_servings=99,
        structured_ingredients=[StructuredIngredient(id="ing_01", name="flour",
                                                     fallback_string="1 cup flour", line_index=0)],
        tokenized_directions=[TokenizedDirection(step=1, text="Mix {{ing_01|flour}}.")],
        grid_categories={"Cuisine": ["Italian"]},
    )
    payload = build_update(ref, [0.1] * 3, read_rev=7)
    assert payload == {
        "structured_ingredients": [ref.structured_ingredients[0].model_dump()],
        "tokenized_directions": [{"step": 1, "text": "Mix {{ing_01|flour}}."}],
        "embedding": [0.1] * 3,
        "derived_rev": 7,
        "amount_overrides": {},
        "derived_error": None,
        "derived_attempts": 0,
        "claimed_at": None,
    }
    # base_servings, grid_categories and title are user-owned after ingest (D3, 3.6)
    assert "base_servings" not in payload and "title" not in payload
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_regen.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_extraction'`.

- [ ] **Step 3: Implement**

Append to `recipeparser/core/regen.py`:

```python
def build_extraction(row: Mapping[str, Any]) -> RecipeExtraction:
    """A REFINE input from a claimed recipes row (spec 5.4)."""
    return RecipeExtraction(
        name=str(row.get("title") or ""),
        ingredients=[str(s) for s in (row.get("ingredient_lines") or [])],
        directions=[str(s) for s in (row.get("direction_steps") or [])],
    )


def build_update(
    refinement: CayenneRefinement,
    embedding: List[float],
    read_rev: int,
) -> Dict[str, Any]:
    """
    The conditional write-back payload (spec 5.5).  Title, base_servings and
    grid_categories are deliberately absent: they are user-owned after ingest.
    amount_overrides is emptied because the new structured entries already
    reflect the rewritten lines.
    """
    return {
        "structured_ingredients": [i.model_dump() for i in refinement.structured_ingredients],
        "tokenized_directions": [d.model_dump() for d in refinement.tokenized_directions],
        "embedding": list(embedding),
        "derived_rev": read_rev,
        "amount_overrides": {},
        "derived_error": None,
        "derived_attempts": 0,
        "claimed_at": None,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_regen.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add recipeparser/core/regen.py tests/unit/test_regen.py
git commit -m "feat: regen planner builds REFINE input and write-back payload"
```

---

### Task 6: `RegenWorker` adapter

**Files:**
- Create: `recipeparser/adapters/regen_worker.py`
- Create: `tests/unit/test_regen_worker.py`

**Interfaces:**
- Consumes: `build_extraction`, `build_update` (Task 5); `refine` and `embed` stages; `SupabaseCategorySource.load_axes(user_id)`.
- Consumes RPCs: `claim_stale_recipes(p_limit)`, `regen_failed(p_id, p_msg)`.
- Produces: `class RegenWorker` with `__init__(self, supabase, gemini_client, *, refine_fn=refine, embed_fn=embed, axes_loader=None, batch=5, concurrency=2)` and `run_once(self) -> int` (rows processed).
- Produces: `async def run_workers(workers: Sequence[Any], stop: asyncio.Event, poll_seconds: float = 10.0) -> None` — calls each worker's `run_once` in a thread every `poll_seconds` until `stop` is set; exceptions are logged, never fatal.
- Produces: `load_profile_prefs(supabase, user_id) -> Tuple[str, str]` returning `(uom_system, measure_preference)`, defaults `("US", "Volume")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_regen_worker.py
"""RegenWorker: claim → REFINE → EMBED → guarded write-back (spec 5)."""
import asyncio
import logging
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from recipeparser.models import CayenneRefinement, StructuredIngredient, TokenizedDirection


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    """Records a chained supabase-py query and returns canned data on execute()."""
    def __init__(self, fake, table):
        self.fake, self.table, self.ops = fake, table, []
    def __getattr__(self, name):
        def _op(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self
        return _op
    def execute(self):
        self.fake.queries.append(self)
        return _Result(self.fake.responses.get(self.table, []))


class FakeSupabase:
    def __init__(self, responses: Dict[str, Any] = None, rpc_responses: Dict[str, Any] = None):
        self.responses = responses or {}
        self.rpc_responses = rpc_responses or {}
        self.queries: List[_Query] = []
        self.rpcs: List[tuple] = []
    def table(self, name):
        return _Query(self, name)
    def rpc(self, name, params):
        self.rpcs.append((name, params))
        q = _Query(self, f"rpc:{name}")
        self.responses[f"rpc:{name}"] = self.rpc_responses.get(name, [])
        return q


def _row(rev=3):
    return {"id": "r1", "user_id": "u1", "title": "Cake",
            "ingredient_lines": ["1 cup flour"], "direction_steps": ["Mix."], "body_rev": rev}


def _refinement():
    return CayenneRefinement(
        title="Cake", base_servings=4,
        structured_ingredients=[StructuredIngredient(id="ing_01", name="flour",
                                                     fallback_string="1 cup flour", line_index=0)],
        tokenized_directions=[TokenizedDirection(step=1, text="Mix {{ing_01|flour}}.")],
    )


def _worker(fake, refine_fn=None, embed_fn=None):
    from recipeparser.adapters.regen_worker import RegenWorker
    return RegenWorker(
        fake, gemini_client=MagicMock(),
        refine_fn=refine_fn or MagicMock(return_value=_refinement()),
        embed_fn=embed_fn or MagicMock(return_value=[0.5] * 3),
        axes_loader=lambda user_id: {"Cuisine": ["Italian"]},
        batch=5, concurrency=1,
    )


def _ops(fake, table):
    return [q.ops for q in fake.queries if q.table == table]


def test_claims_with_batch_size():
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": []})
    assert _worker(fake).run_once() == 0
    assert fake.rpcs == [("claim_stale_recipes", {"p_limit": 5})]


def test_success_writes_back_with_guard():
    fake = FakeSupabase(
        rpc_responses={"claim_stale_recipes": [_row(rev=3)]},
        responses={"profiles": [{"uom_system": "Metric", "measure_preference": "Weight"}],
                   "recipes": [{"id": "r1"}]},
    )
    refine_fn = MagicMock(return_value=_refinement())
    w = _worker(fake, refine_fn=refine_fn)
    assert w.run_once() == 1
    # REFINE got the row's text and the profile prefs
    kwargs = refine_fn.call_args.kwargs
    assert kwargs["uom_system"] == "Metric" and kwargs["measure_preference"] == "Weight"
    assert kwargs["user_axes"] == {"Cuisine": ["Italian"]}
    assert refine_fn.call_args.args[0].ingredients == ["1 cup flour"]
    # write-back guarded by id AND body_rev
    ops = _ops(fake, "recipes")[0]
    names = [o[0] for o in ops]
    assert names == ["update", "eq", "eq"]
    assert ops[0][1][0]["derived_rev"] == 3
    assert ops[0][1][0]["embedding"] == [0.5] * 3
    assert ("eq", ("id", "r1"), {}) in ops and ("eq", ("body_rev", 3), {}) in ops
    assert not any(n == "regen_failed" for n, _ in fake.rpcs)


def test_profile_defaults_when_missing():
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [_row()]},
                        responses={"recipes": [{"id": "r1"}]})
    refine_fn = MagicMock(return_value=_refinement())
    _worker(fake, refine_fn=refine_fn).run_once()
    assert refine_fn.call_args.kwargs["uom_system"] == "US"
    assert refine_fn.call_args.kwargs["measure_preference"] == "Volume"


def test_zero_rows_updated_is_not_a_failure(caplog):
    caplog.set_level(logging.INFO, logger="recipeparser.adapters.regen_worker")
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [_row()]},
                        responses={"recipes": []})       # body_rev moved during the run
    assert _worker(fake).run_once() == 1
    assert not any(n == "regen_failed" for n, _ in fake.rpcs)
    assert "edited again" in caplog.text


def test_exception_records_failure():
    fake = FakeSupabase(rpc_responses={"claim_stale_recipes": [_row()]})
    w = _worker(fake, refine_fn=MagicMock(side_effect=ValueError("bad tokens")))
    assert w.run_once() == 1
    assert ("regen_failed", {"p_id": "r1", "p_msg": "bad tokens"}) in fake.rpcs
    assert _ops(fake, "recipes") == []                    # no write-back attempted


def test_run_workers_loops_until_stopped():
    from recipeparser.adapters.regen_worker import run_workers
    calls = []
    class W:
        def run_once(self):
            calls.append(1)
            if len(calls) >= 2:
                stop.set()
            return 0
    stop = asyncio.Event()
    asyncio.run(run_workers([W()], stop, poll_seconds=0.01))
    assert len(calls) == 2


def test_run_workers_survives_exceptions(caplog):
    from recipeparser.adapters.regen_worker import run_workers
    n = {"count": 0}
    class W:
        def run_once(self):
            n["count"] += 1
            if n["count"] == 1:
                raise RuntimeError("supabase down")
            stop.set()
            return 0
    stop = asyncio.Event()
    asyncio.run(run_workers([W()], stop, poll_seconds=0.01))
    assert n["count"] == 2 and "supabase down" in caplog.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_regen_worker.py -q`
Expected: FAIL — `ModuleNotFoundError: recipeparser.adapters.regen_worker`.

- [ ] **Step 3: Implement the worker**

```python
# recipeparser/adapters/regen_worker.py
"""
recipeparser/adapters/regen_worker.py — the reprocess worker (spec 5).

Stale rows (derived_rev < body_rev) are the queue.  Each poll claims up to
``batch`` rows via the claim_stale_recipes RPC, runs REFINE then EMBED, and
writes the derived columns back guarded by body_rev.  A run that raises is
recorded through the regen_failed RPC.  This is the only module that knows
the table and RPC names.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from recipeparser.core.regen import build_extraction, build_update
from recipeparser.core.stages.embed import embed
from recipeparser.core.stages.refine import refine
from recipeparser.io.category_sources.supabase_source import SupabaseCategorySource

log = logging.getLogger(__name__)

_DEFAULT_PREFS: Tuple[str, str] = ("US", "Volume")


def load_profile_prefs(supabase: Any, user_id: str) -> Tuple[str, str]:
    """(uom_system, measure_preference) from profiles, defaulting to US / Volume."""
    res = (
        supabase.table("profiles")
        .select("uom_system,measure_preference")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return _DEFAULT_PREFS
    row = rows[0]
    return (row.get("uom_system") or _DEFAULT_PREFS[0],
            row.get("measure_preference") or _DEFAULT_PREFS[1])


class RegenWorker:
    def __init__(
        self,
        supabase: Any,
        gemini_client: Any,
        *,
        refine_fn: Callable[..., Any] = refine,
        embed_fn: Callable[..., List[float]] = embed,
        axes_loader: Optional[Callable[[str], Dict[str, List[str]]]] = None,
        batch: int = 5,
        concurrency: int = 2,
    ) -> None:
        self._sb = supabase
        self._client = gemini_client
        self._refine = refine_fn
        self._embed = embed_fn
        self._axes = axes_loader or (lambda uid: SupabaseCategorySource().load_axes(uid))
        self._batch = max(1, batch)
        self._concurrency = max(1, concurrency)

    # ── one poll ─────────────────────────────────────────────────────────────

    def run_once(self) -> int:
        rows = self._sb.rpc("claim_stale_recipes", {"p_limit": self._batch}).execute().data or []
        if not rows:
            return 0
        log.info("regen: claimed %d stale recipe(s).", len(rows))
        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            list(pool.map(self._process, rows))
        return len(rows)

    # ── one recipe ───────────────────────────────────────────────────────────

    def _process(self, row: Dict[str, Any]) -> None:
        rid, read_rev = row["id"], int(row["body_rev"])
        try:
            uom, measure = load_profile_prefs(self._sb, row["user_id"])
            refinement = self._refine(
                build_extraction(row),
                self._client,
                uom_system=uom,
                measure_preference=measure,
                user_axes=self._axes(row["user_id"]),
            )
            vector = self._embed(recipe=refinement, client=self._client)
            payload = build_update(refinement, vector, read_rev)
            res = (
                self._sb.table("recipes")
                .update(payload)
                .eq("id", rid)
                .eq("body_rev", read_rev)
                .execute()
            )
            if not res.data:
                log.info("regen: %s was edited again during the run (rev %d) — result dropped.",
                         rid, read_rev)
            else:
                log.info("regen: %s regenerated at rev %d.", rid, read_rev)
        except Exception as exc:  # noqa: BLE001 — every failure is recorded, never fatal
            log.warning("regen: %s failed at rev %d: %s", rid, read_rev, exc, exc_info=True)
            self._sb.rpc("regen_failed", {"p_id": rid, "p_msg": str(exc)[:2000]}).execute()


# ── the loop ─────────────────────────────────────────────────────────────────

async def run_workers(
    workers: Sequence[Any],
    stop: asyncio.Event,
    poll_seconds: float = 10.0,
) -> None:
    """Call every worker's run_once() in a thread, sleep, repeat until stop is set."""
    while not stop.is_set():
        for w in workers:
            try:
                await asyncio.to_thread(w.run_once)
            except Exception as exc:  # noqa: BLE001
                log.error("worker %s poll failed: %s", type(w).__name__, exc, exc_info=True)
            if stop.is_set():
                return
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            pass
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_regen_worker.py -q`
Expected: 7 PASS. (`asyncio.to_thread` exists on Python 3.9.)

- [ ] **Step 5: Lint and commit**

```bash
ruff check recipeparser
git add recipeparser/adapters/regen_worker.py tests/unit/test_regen_worker.py
git commit -m "feat: RegenWorker claims stale recipes, regenerates, writes back guarded by body_rev"
```

---

### Task 7: Lifespan hook starts the workers

**Files:**
- Modify: `recipeparser/adapters/api.py:148` (`app = FastAPI(...)`) and the imports
- Create: `tests/unit/test_worker_lifespan.py`

**Interfaces:**
- Consumes: `RegenWorker`, `run_workers` (Task 6); `RecatWorker` (Task 10 — imported lazily so this task ships first).
- Produces: `_worker_enabled(env) -> bool`; `_lifespan(app)` async context manager; `app = FastAPI(..., lifespan=_lifespan)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_worker_lifespan.py
"""REGEN_WORKER_ENABLED gates the background workers (spec 5.1)."""
import os
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISABLE_AUTH", "1")
os.environ.setdefault("TEST_USER_ID", "00000000-0000-4000-8000-000000000001")

from fastapi.testclient import TestClient  # noqa: E402

import recipeparser.adapters.api as api  # noqa: E402


def test_worker_enabled_parsing():
    assert api._worker_enabled({"REGEN_WORKER_ENABLED": "1"})
    assert api._worker_enabled({"REGEN_WORKER_ENABLED": "true"})
    assert not api._worker_enabled({"REGEN_WORKER_ENABLED": "0"})
    assert not api._worker_enabled({})


def test_lifespan_does_not_start_workers_by_default(monkeypatch):
    monkeypatch.delenv("REGEN_WORKER_ENABLED", raising=False)
    with patch("recipeparser.adapters.regen_worker.run_workers", new=AsyncMock()) as run:
        with TestClient(api.app):
            pass
    run.assert_not_called()


def test_lifespan_starts_and_stops_workers(monkeypatch):
    monkeypatch.setenv("REGEN_WORKER_ENABLED", "1")
    run = AsyncMock()
    with patch("recipeparser.adapters.regen_worker.run_workers", new=run), \
         patch.object(api, "_get_supabase_service_client", return_value=MagicMock()), \
         patch.object(api, "_get_client", return_value=MagicMock()):
        with TestClient(api.app):
            pass
    run.assert_awaited_once()
    workers, stop = run.await_args.args[0], run.await_args.args[1]
    assert [type(w).__name__ for w in workers] == ["RegenWorker", "RecatWorker"] or \
           [type(w).__name__ for w in workers] == ["RegenWorker"]
    assert stop.is_set()


def test_lifespan_skips_when_supabase_unavailable(monkeypatch):
    monkeypatch.setenv("REGEN_WORKER_ENABLED", "1")
    run = AsyncMock()
    with patch("recipeparser.adapters.regen_worker.run_workers", new=run), \
         patch.object(api, "_get_supabase_service_client", return_value=None):
        with TestClient(api.app):
            pass
    run.assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_worker_lifespan.py -q`
Expected: FAIL — `AttributeError: module has no attribute '_worker_enabled'`.

- [ ] **Step 3: Implement the lifespan**

In `recipeparser/adapters/api.py`, add to imports:

```python
from contextlib import asynccontextmanager
```

Add before `app = FastAPI(...)` (line 148):

```python
def _worker_enabled(env: Mapping[str, str]) -> bool:
    """REGEN_WORKER_ENABLED gates the background regen/recategorise workers."""
    return env.get("REGEN_WORKER_ENABLED", "").strip().lower() in _TRUTHY


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Start the background workers when enabled; stop them cleanly on shutdown."""
    task: Optional[asyncio.Task] = None
    stop = asyncio.Event()
    if _worker_enabled(os.environ):
        supabase = _get_supabase_service_client()
        if supabase is None:
            logger.warning("REGEN_WORKER_ENABLED is set but Supabase is not configured — workers not started.")
        else:
            # Imported here so the worker modules are not a hard dependency of the API import.
            from recipeparser.adapters import regen_worker as _rw  # noqa: PLC0415
            workers: list = [_rw.RegenWorker(supabase, _get_client())]
            try:
                from recipeparser.adapters.recat_worker import RecatWorker  # noqa: PLC0415
                workers.append(RecatWorker(supabase, _get_client()))
            except ImportError:
                pass  # Task 10 adds it; the regen worker runs alone until then.
            task = asyncio.create_task(_rw.run_workers(workers, stop))
            logger.info("Background workers started: %s", [type(w).__name__ for w in workers])
    try:
        yield
    finally:
        stop.set()
        if task is not None:
            await task
```

Change the app construction to:

```python
app = FastAPI(title="Cayenne Ingestion API", version="1.0.0", lifespan=_lifespan)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_worker_lifespan.py -q && pytest -q`
Expected: PASS; the full suite still green (existing tests build `TestClient(app)` without entering the context, so no lifespan runs there).

- [ ] **Step 5: Commit**

```bash
git add recipeparser/adapters/api.py tests/unit/test_worker_lifespan.py
git commit -m "feat: lifespan starts regen workers when REGEN_WORKER_ENABLED"
```

---

### Task 8: Ingestion reads unit preferences from `profiles`

**Files:**
- Modify: `recipeparser/adapters/api.py` (`submit_job` and `submit_file_job` where `RecipePipeline(` is built)
- Create: `tests/unit/test_profile_prefs_at_ingest.py`

**Interfaces:**
- Consumes: `_get_supabase_service_client()` (existing) and the `profiles` table (`id`, `uom_system`, `measure_preference`).
- Produces: `_resolve_prefs(user_id, body_uom, body_measure) -> Tuple[str, str]` — the profile row wins; the request body is the fallback when there is no Supabase client, no row, or a lookup error.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_profile_prefs_at_ingest.py
"""Ingestion takes uom/measure from profiles so it agrees with the worker (spec 5.7)."""
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("DISABLE_AUTH", "1")
os.environ.setdefault("TEST_USER_ID", "00000000-0000-4000-8000-000000000001")

import recipeparser.adapters.api as api  # noqa: E402


def _sb(rows):
    q = MagicMock()
    q.select.return_value = q
    q.eq.return_value = q
    q.limit.return_value = q
    q.execute.return_value = MagicMock(data=rows)
    sb = MagicMock()
    sb.table.return_value = q
    return sb


def test_profile_row_wins():
    with patch.object(api, "_get_supabase_service_client",
                      return_value=_sb([{"uom_system": "Metric", "measure_preference": "Weight"}])):
        assert api._resolve_prefs("u1", "US", "Volume") == ("Metric", "Weight")


def test_body_is_fallback_without_row():
    with patch.object(api, "_get_supabase_service_client", return_value=_sb([])):
        assert api._resolve_prefs("u1", "Imperial", "Weight") == ("Imperial", "Weight")


def test_body_is_fallback_without_client():
    with patch.object(api, "_get_supabase_service_client", return_value=None):
        assert api._resolve_prefs("u1", "Imperial", "Weight") == ("Imperial", "Weight")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/test_profile_prefs_at_ingest.py -q`
Expected: FAIL — `AttributeError: _resolve_prefs`.

- [ ] **Step 3: Implement and wire**

In `recipeparser/adapters/api.py`, after `_get_supabase_service_client`:

```python
def _resolve_prefs(user_id: str, body_uom: str, body_measure: str) -> tuple[str, str]:
    """Unit preferences for a job: the profiles row wins, the request body is the fallback.

    Same query as regen_worker.load_profile_prefs, but the fallback differs: the
    worker has no request body, so it defaults to US / Volume; ingestion has one.
    """
    supabase = _get_supabase_service_client()
    if supabase is None:
        return body_uom, body_measure
    try:
        res = (
            supabase.table("profiles")
            .select("uom_system,measure_preference")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("profiles lookup failed for %s (%s) — using request values.", user_id, exc)
        return body_uom, body_measure
    rows = res.data or []
    if not rows:
        return body_uom, body_measure
    row = rows[0]
    return (row.get("uom_system") or body_uom, row.get("measure_preference") or body_measure)
```

In both `submit_job._run` and `submit_file_job._run`, before `pipeline = RecipePipeline(`:

```python
            uom_system, measure_preference = await asyncio.to_thread(
                _resolve_prefs, user_id, body.uom_system, body.measure_preference
            )
```

and change the constructor arguments to `uom_system=uom_system, measure_preference=measure_preference`. In `submit_file_job` the request fields come from form parameters; use whatever names that endpoint already binds (`uom_system`, `measure_preference`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_profile_prefs_at_ingest.py tests/test_api.py -q`
Expected: PASS. If an existing `test_api.py` test asserts the pipeline was built with the request's `uom_system`, it still passes because `_get_supabase_service_client` returns `None` under pytest.

- [ ] **Step 5: Commit**

```bash
git add recipeparser/adapters/api.py tests/unit/test_profile_prefs_at_ingest.py
git commit -m "feat: ingestion reads unit preferences from profiles"
```

---

### Task 9: Categorise-only Gemini call and batch helpers

**Files:**
- Modify: `recipeparser/gemini.py` (append)
- Modify: `recipeparser/core/stages/categorize.py` (append)
- Create: `tests/unit/test_categorize_batch.py`
- Modify: `tests/goldens/test_prompts_snapshot.py` and `tests/goldens/__snapshots__/test_prompts_snapshot.ambr` (the new builder's snapshot)

**Interfaces:**
- Produces: `gemini.build_categorize_batch_prompt(recipes: List[Dict[str, Any]], new_axes: Dict[str, List[str]]) -> str` — the prompt on its own, per the repo's builder convention (`tests/goldens/test_prompts_snapshot.py` locks every builder).
- Produces: `gemini.categorize_batch(recipes: List[Dict[str, Any]], new_axes: Dict[str, List[str]], client) -> Dict[str, List[str]]` — recipe id → matching tags from `new_axes` only. Each recipe dict has `id`, `title`, `ingredient_lines`, `direction_steps`.
- Produces: `categorize.chunked(items: Sequence[T], size: int) -> List[List[T]]` and `categorize.filter_batch_result(result: Dict[str, List[str]], offered: Set[str]) -> Dict[str, List[str]]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_categorize_batch.py
"""Bulk recategorise helpers (spec 6.2, 6.4)."""
import json
from unittest.mock import MagicMock, patch

from recipeparser.config import GEMINI_MODEL
from recipeparser.core.stages.categorize import chunked, filter_batch_result


def test_chunked():
    assert chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert chunked([], 3) == []


def test_filter_drops_unoffered_and_dedups():
    result = {"r1": ["Italian", "Vegan", "Italian"], "r2": ["Nope"], "r3": []}
    assert filter_batch_result(result, {"Italian", "Vegan"}) == {"r1": ["Italian", "Vegan"]}


def test_categorize_batch_prompt_and_parse():
    from recipeparser.gemini import categorize_batch
    reply = MagicMock(text=json.dumps({"results": [
        {"recipe_id": "r1", "tags": ["Italian"]},
        {"recipe_id": "r2", "tags": []},
    ]}))
    with patch("recipeparser.gemini._call_with_retry", return_value=reply) as call:
        out = categorize_batch(
            [{"id": "r1", "title": "Lasagne", "ingredient_lines": ["pasta"], "direction_steps": ["Bake."]},
             {"id": "r2", "title": "Toast", "ingredient_lines": ["bread"], "direction_steps": ["Toast."]}],
            {"Cuisine": ["Italian", "Thai"]},
            client=MagicMock(),
        )
    assert out == {"r1": ["Italian"], "r2": []}
    prompt = call.call_args.kwargs["contents"]
    assert "Italian" in prompt and "Thai" in prompt and "Lasagne" in prompt and "r2" in prompt
    assert call.call_args.kwargs["model"] == GEMINI_MODEL
    assert call.call_args.kwargs["what"] == "categorize_batch"


def test_categorize_batch_empty_reply():
    from recipeparser.gemini import categorize_batch
    with patch("recipeparser.gemini._call_with_retry", return_value=MagicMock(text="")):
        assert categorize_batch([{"id": "r1", "title": "x", "ingredient_lines": [], "direction_steps": []}],
                                {"Cuisine": ["Italian"]}, client=MagicMock()) == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_categorize_batch.py -q`
Expected: FAIL — `ImportError: cannot import name 'chunked'`.

- [ ] **Step 3: Implement the core helpers**

Append to `recipeparser/core/stages/categorize.py`:

```python
from typing import Sequence, Set, TypeVar

_T = TypeVar("_T")


def chunked(items: Sequence[_T], size: int) -> List[List[_T]]:
    """Split items into consecutive lists of at most ``size`` (spec 6.2 batches of 10)."""
    size = max(1, size)
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def filter_batch_result(result: Dict[str, List[str]], offered: Set[str]) -> Dict[str, List[str]]:
    """Keep only offered tags, deduplicated, and drop recipes with no matches."""
    clean: Dict[str, List[str]] = {}
    for recipe_id, tags in result.items():
        kept: List[str] = []
        for tag in tags:
            if tag in offered and tag not in kept:
                kept.append(tag)
        if kept:
            clean[recipe_id] = kept
    return clean
```

- [ ] **Step 4: Implement the Gemini call**

Append to `recipeparser/gemini.py`:

```python
class _RecipeTags(BaseModel):
    recipe_id: str
    tags: List[str] = Field(default_factory=list)


class _BatchCategorization(BaseModel):
    results: List[_RecipeTags] = Field(default_factory=list)


def build_categorize_batch_prompt(
    recipes: List[Dict[str, Any]],
    new_axes: Dict[str, List[str]],
) -> str:
    """The bulk-recategorise prompt: several recipes against newly added tags only."""
    axes_text = "\n".join(f"- {axis}: {', '.join(tags)}" for axis, tags in new_axes.items())
    recipes_text = "\n\n".join(
        f"RECIPE ID: {r['id']}\nTITLE: {r.get('title', '')}\n"
        f"INGREDIENTS:\n" + "\n".join(f"  - {line}" for line in r.get("ingredient_lines", [])) + "\n"
        f"DIRECTIONS:\n" + "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(r.get("direction_steps", [])))
        for r in recipes
    )
    return f"""
You are a culinary classifier. The user has just added these tags to their taxonomy:
{axes_text}

For EACH recipe below, list which of the tags above apply. Rules:
- Use ONLY tags from the list above, spelled exactly. Never invent a tag.
- Return an empty list when none apply. Most recipes will match nothing.
- Return one result per recipe id, in any order.

{recipes_text}
"""


def categorize_batch(
    recipes: List[Dict[str, Any]],
    new_axes: Dict[str, List[str]],
    client,
) -> Dict[str, List[str]]:
    """
    Categorise several existing recipes against ONLY the newly added tags
    (spec 6.2).  Returns recipe_id -> tags.  Never used at ingest; REFINE does
    that.  An empty or unparseable reply returns {} so the caller skips the batch.
    """
    try:
        response = _call_with_retry(
            client,
            model=GEMINI_MODEL,
            contents=build_categorize_batch_prompt(recipes, new_axes),
            config={
                "response_mime_type": "application/json",
                "response_json_schema": _schema_for_gemini(_BatchCategorization),
                "temperature": 0.0,
            },
            what="categorize_batch",
        )
        if not response.text or not response.text.strip():
            log.error("categorize_batch: empty response")
            return {}
        parsed = _BatchCategorization.model_validate(json.loads(response.text))
    except Exception as exc:  # noqa: BLE001
        log.error("categorize_batch failed: %s", exc)
        return {}
    return {r.recipe_id: list(r.tags) for r in parsed.results}
```

(`BaseModel`, `Field`, `json`, `Any`, `Dict`, `List` are already imported in `gemini.py`; `GEMINI_MODEL` comes from the existing `from recipeparser.config import (...)` block at the top of the module. Add any that are missing.)

- [ ] **Step 5: Snapshot the new prompt**

Every builder is snapshotted. In `tests/goldens/test_prompts_snapshot.py`, alongside the existing prompt snapshots, add:

```python
    def test_categorize_batch_prompt(self, snapshot: SnapshotAssertion):
        assert gemini.build_categorize_batch_prompt(
            [{"id": "r1", "title": "Lasagne", "ingredient_lines": ["pasta"], "direction_steps": ["Bake."]}],
            FIXED_AXES,
        ) == snapshot
```

Run: `pytest tests/goldens/test_prompts_snapshot.py --snapshot-update -q`
Expected: one snapshot written. No existing snapshot moves — this task appends a prompt, it does not edit one.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/unit/test_categorize_batch.py tests/goldens/test_prompts_snapshot.py -q`
Expected: 4 PASS, prompt snapshots green.

- [ ] **Step 7: Lint and commit**

```bash
ruff check recipeparser
git add recipeparser/gemini.py recipeparser/core/stages/categorize.py tests/unit/test_categorize_batch.py tests/goldens
git commit -m "feat: categorise-only Gemini batch call and batch helpers"
```

---

### Task 10: `RecatWorker` adapter

**Files:**
- Create: `recipeparser/adapters/recat_worker.py`
- Create: `tests/unit/test_recat_worker.py`

**Interfaces:**
- Consumes: `categorize_batch`, `chunked`, `filter_batch_result` (Task 9); the `FakeSupabase` test double from Task 6's test file (copy it, do not import across test modules).
- Consumes tables: `ingestion_jobs` (`kind`, `params`, `status`, `progress_pct`, `recipe_count`, `stage`, `updated_at`), `categories` (`id`, `name`, `parent_id`, `user_id`), `recipes` (`id`, `title`, `ingredient_lines`, `direction_steps`), `recipe_categories` (`id`, `recipe_id`, `category_id`, `user_id`).
- Produces: `class RecatWorker` with `__init__(self, supabase, gemini_client, *, categorize_fn=categorize_batch, batch_size=10)` and `run_once(self) -> int` (jobs processed, 0 or 1).
- Produces: `resolve_new_axes(rows, category_ids) -> Tuple[Dict[str, List[str]], Dict[str, str]]` — `(axis -> [tag], tag -> category_id)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_recat_worker.py
"""RecatWorker: additive, scoped, cancellable bulk recategorise (spec 6)."""
from typing import Any, Dict, List
from unittest.mock import MagicMock


class _Result:
    def __init__(self, data, count=None):
        self.data, self.count = data, count


class _Query:
    def __init__(self, fake, table):
        self.fake, self.table, self.ops = fake, table, []
    def __getattr__(self, name):
        def _op(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self
        return _op
    def execute(self):
        self.fake.queries.append(self)
        handler = self.fake.handlers.get(self.table)
        return handler(self) if handler else _Result(self.fake.responses.get(self.table, []))


class FakeSupabase:
    def __init__(self):
        self.responses: Dict[str, Any] = {}
        self.handlers: Dict[str, Any] = {}
        self.queries: List[_Query] = []
    def table(self, name):
        return _Query(self, name)


def _ops(fake, table):
    return [q.ops for q in fake.queries if q.table == table]


CATS = [
    {"id": "ax1", "name": "Cuisine", "parent_id": None},
    {"id": "t1", "name": "Italian", "parent_id": "ax1"},
    {"id": "t2", "name": "Thai", "parent_id": "ax1"},
    {"id": "ax2", "name": "Quick", "parent_id": None},
]


def test_resolve_new_axes():
    from recipeparser.adapters.recat_worker import resolve_new_axes
    axes, ids = resolve_new_axes(CATS, ["t2", "ax2"])
    assert axes == {"Cuisine": ["Thai"], "Quick": ["Quick"]}
    assert ids == {"Thai": "t2", "Quick": "ax2"}


def _fake_with_job(job_status_sequence, recipes, count):
    fake = FakeSupabase()
    fake.responses["categories"] = CATS
    statuses = list(job_status_sequence)

    def jobs(q):
        names = [o[0] for o in q.ops]
        if names[0] == "select" and ("eq", ("status", "pending"), {}) in q.ops:
            return _Result([{"id": "j1", "user_id": "u1", "kind": "recategorize",
                             "status": "pending", "params": {"category_ids": ["t2"]}}])
        if names[0] == "select":                       # cancel check
            return _Result([{"status": statuses.pop(0) if statuses else "running"}])
        if names[0] == "update" and ("eq", ("status", "pending"), {}) in q.ops:
            return _Result([{"id": "j1"}])            # claim succeeded
        return _Result([{"id": "j1"}])
    fake.handlers["ingestion_jobs"] = jobs

    def recipes_h(q):
        if any(o[0] == "select" and o[2].get("count") == "exact" for o in q.ops):
            return _Result([], count=count)
        cursor = next((o[1][1] for o in q.ops if o[0] == "gt"), "")
        page = [r for r in recipes if r["id"] > cursor][:10]
        return _Result(page)
    fake.handlers["recipes"] = recipes_h
    return fake


def _recipes(n):
    return [{"id": f"r{i:03d}", "title": f"R{i}", "ingredient_lines": [], "direction_steps": []}
            for i in range(n)]


def _worker(fake, categorize_fn):
    from recipeparser.adapters.recat_worker import RecatWorker
    return RecatWorker(fake, gemini_client=MagicMock(), categorize_fn=categorize_fn, batch_size=10)


def test_no_pending_job():
    fake = FakeSupabase()
    fake.responses["ingestion_jobs"] = []
    assert _worker(fake, MagicMock()).run_once() == 0


def test_job_runs_in_batches_and_inserts_additively():
    fake = _fake_with_job(["running"] * 5, _recipes(25), count=25)
    cat = MagicMock(side_effect=lambda recipes, axes, client: {recipes[0]["id"]: ["Thai", "Nope"]})
    assert _worker(fake, cat).run_once() == 1
    assert cat.call_count == 3                                   # 10 + 10 + 5
    assert cat.call_args.args[1] == {"Cuisine": ["Thai"]}        # only the new tag offered
    inserts = _ops(fake, "recipe_categories")
    assert len(inserts) == 3
    rows = inserts[0][0][1][0]
    assert rows == [{"id": rows[0]["id"], "recipe_id": "r000", "category_id": "t2", "user_id": "u1"}]
    assert inserts[0][0][2] == {"on_conflict": "recipe_id,category_id", "ignore_duplicates": True}
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "done" and final["stage"] == "DONE" and final["progress_pct"] == 100
    assert final["recipe_count"] == 3


def test_cancel_between_batches():
    fake = _fake_with_job(["running", "cancelled"], _recipes(25), count=25)
    cat = MagicMock(return_value={})
    _worker(fake, cat).run_once()
    assert cat.call_count == 2
    statuses = [o[0][1][0].get("status") for o in _ops(fake, "ingestion_jobs") if o[0][0] == "update"]
    assert "done" not in statuses


def test_too_many_failed_batches_errors_the_job():
    fake = _fake_with_job(["running"] * 5, _recipes(30), count=30)
    cat = MagicMock(side_effect=RuntimeError("gemini down"))
    _worker(fake, cat).run_once()
    final = _ops(fake, "ingestion_jobs")[-1][0][1][0]
    assert final["status"] == "error" and "3 of 3 batches failed" in final["error_message"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_recat_worker.py -q`
Expected: FAIL — `ModuleNotFoundError: recipeparser.adapters.recat_worker`.

- [ ] **Step 3: Implement the worker**

```python
# recipeparser/adapters/recat_worker.py
"""
recipeparser/adapters/recat_worker.py — bulk recategorise worker (spec 6).

A client-inserted ingestion_jobs row with kind = 'recategorize' and
params.category_ids = [...] asks for every recipe of that user to be checked
against ONLY those newly added tags.  Inserts are additive (on conflict do
nothing); nothing is ever removed.  Progress, cursor and cancellation all
live on the job row so PowerSync shows them and the client can stop it.
"""
from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from recipeparser.core.stages.categorize import chunked, filter_batch_result
from recipeparser.gemini import categorize_batch

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_new_axes(
    rows: List[Dict[str, Any]],
    category_ids: List[str],
) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """
    (axis -> [new tag names], tag name -> category id) for the ids in a job.
    A level-1 row (no parent) is an axis with a single tag of its own name,
    matching SupabaseCategorySource._build_axes.
    """
    by_id = {r["id"]: r for r in rows}
    axes: Dict[str, List[str]] = {}
    ids: Dict[str, str] = {}
    for cid in category_ids:
        row = by_id.get(cid)
        if row is None:
            continue
        name = (row.get("name") or "").strip()
        parent = by_id.get(row.get("parent_id") or "")
        axis = (parent.get("name") or "").strip() if parent else name
        axes.setdefault(axis, [])
        if name not in axes[axis]:
            axes[axis].append(name)
        ids[name] = cid
    return axes, ids


class RecatWorker:
    def __init__(
        self,
        supabase: Any,
        gemini_client: Any,
        *,
        categorize_fn: Callable[..., Dict[str, List[str]]] = categorize_batch,
        batch_size: int = 10,
    ) -> None:
        self._sb = supabase
        self._client = gemini_client
        self._categorize = categorize_fn
        self._batch_size = max(1, batch_size)

    # ── one poll: at most one job ────────────────────────────────────────────

    def run_once(self) -> int:
        jobs = (
            self._sb.table("ingestion_jobs").select("*")
            .eq("kind", "recategorize").eq("status", "pending")
            .order("created_at").limit(1).execute().data or []
        )
        if not jobs:
            return 0
        job = jobs[0]
        claimed = (
            self._sb.table("ingestion_jobs")
            .update({"status": "running", "stage": "CATEGORIZING", "updated_at": _now()})
            .eq("id", job["id"]).eq("status", "pending").execute().data
        )
        if not claimed:
            return 0
        try:
            self._run_job(job)
        except Exception as exc:  # noqa: BLE001
            log.error("recat job %s crashed: %s", job["id"], exc, exc_info=True)
            self._finish(job["id"], "error", error=str(exc)[:2000])
        return 1

    # ── the job ──────────────────────────────────────────────────────────────

    def _run_job(self, job: Dict[str, Any]) -> None:
        user_id = job["user_id"]
        params = dict(job.get("params") or {})
        cat_rows = self._sb.table("categories").select("id,name,parent_id").eq("user_id", user_id).execute().data or []
        new_axes, tag_ids = resolve_new_axes(cat_rows, list(params.get("category_ids") or []))
        offered = set(tag_ids)
        if not offered:
            self._finish(job["id"], "done", progress=100, count=0)
            return

        total = self._sb.table("recipes").select("id", count="exact").eq("user_id", user_id).execute().count or 0
        total_batches = max(1, math.ceil(total / self._batch_size))
        cursor = str(params.get("cursor") or "")
        done_batches = failed = matched = 0

        while True:
            page = (
                self._sb.table("recipes").select("id,title,ingredient_lines,direction_steps")
                .eq("user_id", user_id).gt("id", cursor).order("id").limit(self._batch_size)
                .execute().data or []
            )
            if not page:
                break
            for batch in chunked(page, self._batch_size):
                try:
                    raw = self._categorize(batch, new_axes, self._client)
                    hits = filter_batch_result(raw, offered)
                    rows = [
                        {"id": str(uuid.uuid4()), "recipe_id": rid, "category_id": tag_ids[tag], "user_id": user_id}
                        for rid, tags in hits.items() for tag in tags
                    ]
                    if rows:
                        self._sb.table("recipe_categories").upsert(
                            rows, on_conflict="recipe_id,category_id", ignore_duplicates=True
                        ).execute()
                    matched += len(rows)
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    log.warning("recat job %s: batch failed (%s) — skipped.", job["id"], exc)
                done_batches += 1
                cursor = batch[-1]["id"]
                params["cursor"] = cursor
                self._sb.table("ingestion_jobs").update({
                    "params": params,
                    "progress_pct": min(99, int(100 * done_batches / total_batches)),
                    "recipe_count": matched,
                    "updated_at": _now(),
                }).eq("id", job["id"]).execute()
                status = (self._sb.table("ingestion_jobs").select("status").eq("id", job["id"])
                          .limit(1).execute().data or [{}])[0].get("status")
                if status == "cancelled":
                    log.info("recat job %s cancelled after %d batch(es).", job["id"], done_batches)
                    return

        if failed and failed > done_batches / 10:
            self._finish(job["id"], "error", count=matched,
                         error=f"{failed} of {done_batches} batches failed")
        else:
            self._finish(job["id"], "done", progress=100, count=matched)

    def _finish(self, job_id: str, status: str, *, progress: Optional[int] = None,
                count: Optional[int] = None, error: Optional[str] = None) -> None:
        payload: Dict[str, Any] = {
            "status": status,
            "stage": "DONE" if status == "done" else "ERROR",
            "updated_at": _now(),
        }
        if progress is not None:
            payload["progress_pct"] = progress
        if count is not None:
            payload["recipe_count"] = count
        if error:
            payload["error_message"] = error
        self._sb.table("ingestion_jobs").update(payload).eq("id", job_id).execute()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_recat_worker.py tests/unit/test_worker_lifespan.py -q`
Expected: PASS, and the lifespan test now sees both `RegenWorker` and `RecatWorker`.

- [ ] **Step 5: Lint and commit**

```bash
ruff check recipeparser
git add recipeparser/adapters/recat_worker.py tests/unit/test_recat_worker.py
git commit -m "feat: RecatWorker runs additive, scoped bulk recategorise jobs"
```

---

### Task 11: Duration backfill script

**Files:**
- Create: `scripts/backfill_durations.py`
- Create: `tests/unit/test_backfill_durations.py`

**Interfaces:**
- Consumes: `duration_columns` (Task 2).
- Produces: `plan_backfill(rows: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any], bool]]` — `(recipe_id, payload, note_only)` per row, importable without side effects.
- Produces: a CLI `python scripts/backfill_durations.py [--live] [--verbose]` — dry run by default — that pages through `recipes`, updates the nine columns (never `base_servings` when a value already exists), and prints rows whose text landed entirely in a note.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_backfill_durations.py
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "backfill_durations", Path(__file__).parents[2] / "scripts" / "backfill_durations.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_plan_backfill_parses_and_flags_note_only():
    rows = [
        {"id": "a", "prep_time": "15 mins", "cook_time": "overnight", "base_servings": 4},
        {"id": "b", "prep_time": None, "cook_time": None, "base_servings": None},
    ]
    plan = mod.plan_backfill(rows)
    assert plan[0][0] == "a"
    assert plan[0][1]["prep_min_minutes"] == 15
    assert plan[0][1]["cook_note"] == "overnight"
    assert plan[0][1]["base_servings"] == 4            # existing value kept
    assert plan[0][2] is True                          # cook text became note-only
    assert plan[1][2] is False
    assert "servings_min" in plan[1][1]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/test_backfill_durations.py -q`
Expected: FAIL — file not found.

- [ ] **Step 3: Write the script**

```python
# scripts/backfill_durations.py
"""
One-off: parse prep_time / cook_time text into the structured duration columns
(spec 7.2).  Servings text was never stored, so servings_min/max are seeded
from base_servings.  Rows whose text landed entirely in a note are printed for
a manual look.

Mirrors the CLI shape of `scripts/backfill_paprika_metadata.py` (on master): a dry
run is the default and writing is an explicit opt-in, so a mistyped invocation
costs nothing.

    # dry run -- prints what it would do and writes nothing
    python scripts/backfill_durations.py

    # for real
    python scripts/backfill_durations.py --live

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipeparser.core.durations import duration_columns  # noqa: E402

PAGE = 500


def plan_backfill(rows: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any], bool]]:
    plan = []
    for row in rows:
        base = row.get("base_servings")
        base_int = int(base) if base is not None else None
        servings_text = str(base_int) if base_int is not None else None
        cols = duration_columns(row.get("prep_time"), row.get("cook_time"), servings_text, base_int)
        note_only = any(
            cols[f"{k}_note"] and cols[f"{k}_min_minutes"] is None and row.get(f"{k}_time")
            for k in ("prep", "cook")
        )
        plan.append((row["id"], cols, note_only))
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill the structured duration and servings columns.")
    ap.add_argument("--live", action="store_true", help="Actually write. Omit for a dry run.")
    ap.add_argument("--verbose", action="store_true", help="List every row that would change.")
    args = ap.parse_args()
    from supabase import create_client  # noqa: PLC0415
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    offset, updated, flagged = 0, 0, []
    while True:
        rows = (sb.table("recipes").select("id,title,prep_time,cook_time,base_servings")
                .order("id").range(offset, offset + PAGE - 1).execute().data or [])
        if not rows:
            break
        titles = {r["id"]: r.get("title") for r in rows}
        for rid, cols, note_only in plan_backfill(rows):
            if note_only:
                flagged.append((rid, titles[rid], cols.get("prep_note"), cols.get("cook_note")))
            if args.verbose:
                print(f"  {rid}  {titles[rid]!r}  {cols}")
            if args.live:
                sb.table("recipes").update(cols).eq("id", rid).execute()
            updated += 1
        offset += PAGE

    # Printed on a dry run and a live run alike, so the two are comparable.
    print(f"{'updated' if args.live else 'would update'} {updated} recipe(s)")
    if not args.live:
        print("DRY RUN — nothing was written. Re-run with --live to apply.")
    if flagged:
        print(f"{len(flagged)} row(s) with note-only durations — eyeball these:")
        for rid, title, p, c in flagged:
            print(f"  {rid}  {title!r}  prep_note={p!r}  cook_note={c!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/unit/test_backfill_durations.py -q`
Expected: PASS.

- [ ] **Step 5: Dry-run against the live project, then run**

Run: `python scripts/backfill_durations.py` (with the env vars set) — the bare invocation is the dry run.
Expected: a count equal to the library size, the `DRY RUN` banner, and a short flagged list. Read the flagged list; if any entry is a parser gap rather than genuinely unparseable text, add the case to `tests/fixtures/duration_cases.json`, fix the parser (Task 2), and re-run. Only then `python scripts/backfill_durations.py --live`.

- [ ] **Step 6: Commit**

```bash
git add scripts/backfill_durations.py tests/unit/test_backfill_durations.py tests/fixtures/duration_cases.json recipeparser/core/durations.py
git commit -m "feat: one-off duration/servings backfill script"
```

---

### Task 12: Documentation

**Files:**
- Modify: `ARCHITECTURE.md` (append a section after "12. Ingestion Job Status")
- Modify: `CHANGELOG.md` (new entry at the top)
- Modify: `README.md` (env var table, if one exists; otherwise the configuration section)

- [ ] **Step 1: Add the architecture section**

Append to `ARCHITECTURE.md`:

```markdown
## 13. Recipe Edits and Regeneration

Spec: `docs/superpowers/specs/2026-09-07-recipe-edit-philosophy-design.md`.

The client edits **raw** columns only (`title`, `ingredient_lines`,
`direction_steps`, metadata) and bumps `body_rev` on free-text changes. The
server is the sole writer of **derived** columns (`structured_ingredients`,
`tokenized_directions`, `embedding`, `derived_rev`). A recipe is stale when
`derived_rev < body_rev`; stale rows are the queue.

| Module | Role |
|--------|------|
| `core/durations.py` | Deterministic prep/cook/servings parser. Shares `tests/fixtures/duration_cases.json` with the Cayenne TypeScript parser. |
| `core/regen.py` | Pure: REFINE input from a row, write-back payload, raw lines from derived data. |
| `adapters/regen_worker.py` | `RegenWorker.run_once()`: `claim_stale_recipes` RPC → REFINE → EMBED → `update … where id = ? and body_rev = ?`. Failures via `regen_failed` RPC. `run_workers()` is the shared poll loop. |
| `adapters/recat_worker.py` | `RecatWorker.run_once()`: one pending `ingestion_jobs` row with `kind = 'recategorize'` → batches of 10 recipes → `categorize_batch` → additive junction upserts. |

Workers start from the FastAPI lifespan when `REGEN_WORKER_ENABLED=1` and the
service-role Supabase client is configured. Poll every 10 s; regen concurrency 2.

REFINE's `base_servings` and `grid_categories` are discarded on regen: both are
user-owned after ingest. `amount_overrides` is emptied on every successful
regen because the new structured entries reflect the rewritten lines.
```

- [ ] **Step 2: Add the changelog entry**

Insert below the `---` under the header in `CHANGELOG.md`:

```markdown
## [Unreleased] — recipe edit backend

### ✨ Added
- `StructuredIngredient.line_index`, emitted by REFINE and validated (in range, unique).
- `core/durations.py`: deterministic duration and servings parser; shared fixture `tests/fixtures/duration_cases.json`.
- Raw `ingredient_lines` / `direction_steps` and structured duration/servings columns carried through ASSEMBLE and written by `SupabaseWriter`.
- `RegenWorker` and `RecatWorker` background workers behind `REGEN_WORKER_ENABLED`, started from the FastAPI lifespan.
- `gemini.categorize_batch()` — categorise-only call for bulk recategorise.
- Ingestion reads `uom_system` / `measure_preference` from `profiles`; request values are the fallback.
- `scripts/backfill_durations.py` one-off backfill.

### Requires
- Cayenne migrations 013 (`recipe_edit_columns`) and 014 (`regen_rpcs`). The workers no-op without them; `REGEN_WORKER_ENABLED` stays unset until they are applied.
```

- [ ] **Step 3: Document the env var in the README**

Add to the environment variable list in `README.md`:

```markdown
| `REGEN_WORKER_ENABLED` | `0` | Set to `1` to run the regen and bulk-recategorise workers inside the API process. Requires `SUPABASE_SERVICE_ROLE_KEY`. |
```

- [ ] **Step 4: Full suite and commit**

Run: `pytest -q && ruff check recipeparser`
Expected: green, no lint errors.

```bash
git add ARCHITECTURE.md CHANGELOG.md README.md
git commit -m "docs: recipe edit regeneration architecture and changelog"
```

---

## Self-review

**Spec coverage.**
- 3.2 raw columns written at ingest → Task 3 (model, assemble, pipeline) + Task 4 (writer).
- 3.3 derived bookkeeping written at ingest (`body_rev = derived_rev = 0`, `amount_overrides = {}`) → Task 4.
- 3.6 durations/servings: parser → Task 2; populated at ingest → Task 3/4; backfill → Task 11; `base_servings` from REFINE discarded on regen → Task 5 (`build_update` omits it).
- 4.3 `line_index` emitted and validated → Task 1.
- 5.1 placement, env flag, poll 10 s, concurrency 2 → Tasks 6, 7.
- 5.2 code layout core/adapters → Tasks 5, 6, 10.
- 5.3 claiming → Task 6 via RPC (SQL in the Cayenne plan).
- 5.4 stages, profile prefs, discard categories → Task 6 (`build_update` omits `grid_categories`; the `user_axes` are still passed so the prompt is unchanged).
- 5.5 guarded write-back, zero rows dropped → Task 6.
- 5.6 failure via RPC → Task 6.
- 5.7 writer changes, Paprika fast path derivation, ingestion reads profiles → Tasks 3, 4, 8.
- 6.1 job row shape → consumed by Task 10 (client inserts it; Cayenne plan).
- 6.2 worker steps 1–5 → Task 10. 6.3 cancel + 10% failure rule → Task 10. 6.4 new code → Tasks 9, 10.
- 8 RecipeParser tests → each task carries its tests; the golden/snapshot refresh is Task 1 step 7, Task 3 step 7 and Task 9 step 5.

**Placeholders.** None. Every code step contains the code; every run step names the command and the expected result.

**Rebased onto `master` (cfb54dd) on 2026-09-08.** The plan was written against `612bdf7`; 30 commits landed on top of it (extraction goldens, the Gemini prompt/config refactor, the Paprika backfill script). What that changed here:

| Was | Now | Where |
|---|---|---|
| `model="gemini-2.5-flash"` hardcoded | `model=GEMINI_MODEL` from `recipeparser.config` (default `gemini-3.1-flash-lite`) | Global Constraints, Task 9 |
| `_call_with_retry(client, model, contents, config)` | the same plus `what=` for usage/cost logging | Global Constraints, Task 9 |
| REFINE prompt inline at `gemini.py:522-540` | `build_refine_prompt()` at `gemini.py:655-699` | Task 1 files, Task 1 step 5 |
| a new prompt written inline at its call site | every prompt is a `build_*_prompt()` with a snapshot in `tests/goldens/test_prompts_snapshot.py` | Global Constraints, Task 9 steps 4-5 |
| `pytest tests/snapshots --snapshot-update` | `tests/goldens` moves too — prompt, schema and stage snapshots | Task 1 step 7, Task 3 step 7 |
| "check whether the six Paprika columns are there" | they are, at `supabase.py:196-203`; this step only appends | Task 4 step 3 |
| backfill writes by default, `--dry-run` opts out | dry run by default, `--live` opts in, matching `scripts/backfill_paprika_metadata.py` | Task 11 |
| Cayenne schema listed as a prerequisite | called out as a **gate** — the migrations are not in the sibling checkout, so Tasks 6/7/10 cannot be integration-verified yet | Global Constraints |

Verified unchanged and still correct: `models.py:97-108` / `:132-175`, the three `assemble(` calls at `pipeline.py:298/322/375`, `row = {` at `supabase.py:188`, `app = FastAPI(...)` at `api.py:148` (still no lifespan, so Task 7 is a clean add), `RecipePipeline(` at `api.py:733` and `:862`, `config.live_writes_blocked()` at `:145`, the ruff TID rule, `target-version = "py39"`, ARCHITECTURE.md ending at §12, and an empty `[Unreleased]` in CHANGELOG.md.

**Type consistency.** `build_update(refinement, embedding, read_rev)` (Task 5) is called with those positional args in Task 6. `load_profile_prefs(supabase, user_id)` (Task 6) is reused in Task 8. `categorize_batch(recipes, new_axes, client)` positional order (Task 9) matches `self._categorize(batch, new_axes, self._client)` and the test's `side_effect=lambda recipes, axes, client:` (Task 10). `run_workers(workers, stop, poll_seconds)` (Task 6) matches the lifespan call (Task 7). RPC names and parameter keys (`p_limit`, `p_id`, `p_msg`) match Cayenne migration 014. The junction upsert's `on_conflict="recipe_id,category_id"` matches the unique index in Cayenne migration 013.
