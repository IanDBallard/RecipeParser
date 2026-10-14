# Pan contract — REFINE emits tins, bake type, bake step and core temperatures; a pans-only backfill

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** planned 2026-10-14, not executed.

**Goal:** REFINE (new ingest and regen) emits the Cayenne pan-aware scaling contract: `pans`, `bake_type`, `bake_step`, `{{pan_NN|words}}` tokens and a `,core` flag on internal-temperature `temp:` tokens. A pans-only backfill (D22) gives existing recipes the same fields without re-running REFINE.

**Architecture:** Pure helpers in `core/fat_tokens.py` (the `pan_` prefix, the `core` flag) and a new `core/pans.py` (normalise the model's tins, unwrap bad pan tokens, check bake type/step and core flags, place backfill quotes). `models.py` gains `RecipePan` and the backfill answer models; REFINE's prompt gains rule 5; `refine()` runs the new checks; the Supabase insert row and the regen payload carry the new columns, regen keeping a cook's edited tins. A new `gemini.tag_pans` call and `scripts/backfill_pans.py` follow `backfill_temperatures.py`'s skeleton.

**Tech Stack:** Python ≥3.9 (CI 3.11/3.12; sandbox 3.13), pydantic v2, google-genai, supabase-py, pytest (+xdist, syrupy), ruff, mypy --strict.

**Spec:** Cayenne repo `docs/superpowers/specs/2026-10-13-pan-aware-scaling-design.md` — *Data model*, *Parser contract*, D9, D10, D20–D24 and *Doneness targets*. Cayenne #287 (merged) is the client that reads every field below; its `domain/pans.ts`, `domain/bakeTypes.ts`, `domain/kitchenRecipe.ts` (`parsePans`) and `domain/temperature.ts` (`parseTempValue` with `core`) are the readers this contract must satisfy.

## What the investigation found before Task 1

- REFINE: prompt `gemini.py:723 build_refine_prompt` (rules 1–4; the TEMPERATURES bullet ~785; braces doubled), schema `gemini.py:839 refine_json_schema()` from `CayenneRefinement`, call `gemini.py:851 refine_recipe_for_cayenne`. Checks in `core/stages/refine.py:refine()` (~480): … `_check_temperatures` (124) → `_validate_fat_tokens` (149, **raises** on any non-`temp:` id not an ingredient) → `_check_mentions` → … → `_check_detection`.
- Token grammar `core/fat_tokens.py`: `TOKEN_RE`, `TEMP_PREFIX`, `parse_temp_value` (51, `[main][,oven][,fan=…]`, rejects unknown parts), `is_temp_id`, `temp_token`, `_mentions` (198, skips temp ids at 202), `is_tagged` (411, skips temp ids), `locate` (321/408). `core/temperatures.py` `place_temperature_mentions` (225) is the quote-placement pattern; `repair_tokens` (251) passes non-temp tokens through.
- **Every place that special-cases `temp:` ids must also pass `pan_` ids through**: `refine.py:134,157`, `fat_tokens.py:202,414`, `scripts/backfill_direction_amounts.py:105` (treats a recipe with `temp:` tokens as already tagged), `scripts/backfill_temperatures.py:109,131`.
- Writes: new ingest row `io/writers/supabase.py` (~300-335; jsonb as real lists); `core/stages/assemble.py:136-137` copies REFINE fields into `CayenneRecipe` (`models.py:256`, detection fields at 290-291); regen payload `core/regen.py:93 build_update` (asserted exactly in `tests/unit/test_regen.py`), called from `adapters/regen_worker.py:_process` (72-86). The claim RPC returns only `id, user_id, title, ingredient_lines, direction_steps, body_rev` — so keeping a cook's edited tins needs one extra read of `pans`.
- Backfill skeleton `scripts/backfill_temperatures.py` (364 lines): paged `_read_rows`, `--user-id|--all-users`, `--recipe-id`, `--limit`, `--workers`, `--live` (requires `--record`/`--restore`), `--review`, pure `plan_row(row, ask, force) -> RowResult`, conditional `write_row` on unchanged `body_rev`/`derived_rev` (False = MOVED), stale rows skipped.
- Tests: REFINE unit tests patch `recipeparser.core.stages.refine.refine_recipe_for_cayenne` (`tests/unit/stages/test_refine_temperatures.py`, `test_refine_verbatim.py`); Gemini calls patch `recipeparser.gemini._call_with_retry` (`tests/unit/test_gemini_temperatures.py`); prompt snapshot `tests/goldens/test_prompts_snapshot.py` (syrupy); stage snapshots include `CayenneRefinement`/`CayenneRecipe` fields. Baseline: **1985 passed, 1 skipped** (`python -m pytest tests/ -q`, 2026-10-14, Python 3.13).
- Version 9.11.0; CHANGELOG Keep-a-Changelog with `### ✨ Added / ⚠️ Operational / 🧪 Tests`; `pyproject.toml` and `installer.iss` versions must match for a tag.

## Plan rulings

| # | Question | Ruling | Why |
|---|---|---|---|
| R1 | Units of `dims_mm` | The model gives millimetres, converting inches ×25.4; the checks keep 10–1000 mm only. | Cayenne's catalogue and solver are in mm. |
| R2 | `catalog_hint` | Only for tins the source names **without dimensions**: weight-named loaf tins, cup-named Bundts, sheet pans, Blech, lamington/slab/bar/Swiss-roll. The prompt carries that short key list; any other hint is nulled. | Cayenne resolves a sized tin by dimensions and depth (plan P2 there); a hint is only needed where there are no dimensions. A long key list in every prompt costs tokens and invites guesses. |
| R3 | A tin with nothing to size it | Dropped with a warning (no `d`, no `l`+`w`, no capacity, no hint). | Spec: "a tin with no size → `[]`". |
| R4 | `bake_step` | 0-based index into `tokenized_directions`; null when out of range or that step is a phase heading. | Cayenne reads `recipe.steps[bake_step]` (its ruling R2). |
| R5 | `core` validity | Kept only on a °C/°F reading with no `oven`/`fan`, between 30 and 110 °C; otherwise the flag is removed and the token stays. | Mirrors Cayenne's `parseTempValue`; internal food temperatures sit in that band. |
| R6 | Regen and edited tins | `build_update` takes the current `pans`; when any has `edited: true`, the cook's `pans` are written back unchanged and REFINE's pan tokens whose id is not among them are unwrapped to words. | Spec: "the parser never overwrites a pan with `edited: true`". |
| R7 | What the backfill counts as done | A row with `bake_type` not null, or a non-empty `pans`, or any `pan_` token, is `already-done` unless `--force`. It writes `pans` (possibly `[]`), `bake_type`, `bake_step`, `tokenized_directions` only. | D22: pans-only; a recipe with no tins still needs its bake type for the doneness callout. |
| R8 | Release order | Cayenne #287 is live (2026-10-14). This deploy next; the backfill after it. | A client without #287 shows a `,core` token as words. |

## Global Constraints

- Repo `/home/user/recipeparser`, branch `feat/pan-contract` from `master` (`14e816e`).
- Gate before every commit: `python -m pytest tests/ -q` (baseline 1985 passed, 1 skipped) plus `ruff check` and `mypy` with **no new finding in touched files** (compare with `baseline_ruff.txt` / `baseline_mypy.txt`).
- Python 3.9 syntax only (no `match`, no `X | Y` unions at runtime; `from __future__ import annotations` is fine).
- `core/` imports nothing from `io/` or `adapters/`.
- A model slip never costs the recipe: degrade with `log.warning`, never raise, for anything in this contract.
- Vocabularies, exactly (Cayenne `domain/pans.ts`, `bakeTypes.ts`):
  - shapes: `round`, `square`, `rect`, `loaf`, `ring`, `flat-round`, `sheet`
  - families: `round-cake`, `square`, `traybake`, `sheet`, `loaf`, `ring`, `tart`, `pizza`
  - bake types: `sponge`, `butter-cake`, `quick-bread`, `bar`, `bread-lean`, `bread-enriched`, `cheesecake`, `custard`, `pizza`, `pie-fruit`, `pie-custard`, `pie-nut`
- Pan token: `{{pan_NN|words}}`, ids `pan_01`, `pan_02`, …; no third field.
- No live Gemini key in a session: prompt quality is verified by the owner's dry-run review, not here.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `recipeparser/core/fat_tokens.py` | token grammar | `PAN_PREFIX`, `is_pan_id`, `pan_token`, `is_special_id`; `TempValue.core`; `parse_temp_value` reads `core`; `_mentions`/`is_tagged` skip pan ids |
| `recipeparser/models.py` | contracts | `RecipePan`, `PanDims`; `CayenneRefinement`/`CayenneRecipe` gain `pans`, `bake_type`, `bake_step`; backfill answer `PanAnswer` |
| `recipeparser/core/pans.py` (new) | pure checks and placement | vocab, `normalise_pans`, `check_pan_tokens`, `check_bake`, `check_core_flags`, `apply_pan_checks`, `place_pan_mentions`, `mark_core` |
| `recipeparser/core/stages/refine.py` | REFINE checks | skip pan ids in `_validate_fat_tokens`/`_check_temperatures`; call `apply_pan_checks` |
| `recipeparser/gemini.py` | prompts and calls | REFINE rule 5 + core in TEMPERATURES; `build_pans_prompt`, `tag_pans` |
| `recipeparser/core/stages/assemble.py`, `recipeparser/io/writers/supabase.py` | new ingest | carry and write the three columns |
| `recipeparser/core/regen.py`, `recipeparser/adapters/regen_worker.py` | regen | payload with R6 |
| `scripts/backfill_pans.py` (new) | D22 backfill | `backfill_temperatures.py` skeleton |
| `scripts/backfill_direction_amounts.py`, `scripts/backfill_temperatures.py` | old backfills | pass pan tokens through |
| `CHANGELOG.md`, `pyproject.toml`, `installer.iss` | release | 9.12.0 |

---

## Task 1: The token grammar learns `pan_` and `core`

**Produces:** `PAN_PREFIX = "pan_"`, `is_pan_id(id) -> bool`, `pan_token(pan_id, words) -> str`, `is_special_id(id) -> bool` (temp or pan); `TempValue(main, fan, oven, core=False)`; `parse_temp_value` accepts `[main][,core]` (core only after a °C/°F main, never with oven/fan/gas).

**Steps:**
- [ ] Write failing tests in `tests/unit/test_fat_tokens.py`:

```python
from recipeparser.core.fat_tokens import (
    TOKEN_RE, is_pan_id, is_special_id, is_tagged, pan_token, parse_temp_value,
)
from recipeparser.models import TokenizedDirection


def test_pan_ids_are_special_and_never_ingredients():
    assert is_pan_id("pan_01") and is_special_id("pan_01")
    assert is_special_id("temp:180C,oven")
    assert not is_pan_id("ing_pan_01") and not is_special_id("ing_01")
    assert pan_token("pan_01", "two 20cm tins") == "{{pan_01|two 20cm tins}}"


def test_core_flag_parses_after_a_degree_reading_only():
    v = parse_temp_value("93C,core")
    assert v is not None and v.core and not v.oven and v.main == ("C", 93.0)
    assert parse_temp_value("205F,core").core
    assert parse_temp_value("180C,oven").core is False
    for bad in ("gas4,core", "93C,core,oven", "93C,oven,core", "93C,core,fan=80C"):
        assert parse_temp_value(bad) is None, bad


def test_is_tagged_ignores_pan_tokens():
    steps = [TokenizedDirection(step=1, text="Grease {{pan_01|two tins}} and add {{ing_01|flour|all}}")]
    assert is_tagged(steps)
    only_pan = [TokenizedDirection(step=1, text="Grease {{pan_01|two tins}}")]
    assert not is_tagged(only_pan)
```

- [ ] Run `python -m pytest tests/unit/test_fat_tokens.py -q` → FAIL (names missing).
- [ ] Implement in `core/fat_tokens.py`. Docstring paragraph after the `temp:` one: *A token whose id starts ``pan_`` is a tin (Cayenne pan-aware scaling D21): ``{{pan_01|two 20cm sandwich tins}}``; no ``use``, names no ingredient. A ``temp:`` value may end ``,core``: an internal temperature, the source's own doneness figure (D24).* Then:

```python
PAN_PREFIX = "pan_"


class TempValue(NamedTuple):
    main: Optional[Tuple[str, float]]  # ("C" | "F" | "gas", number)
    fan: Optional[Tuple[str, float]]   # ("C" | "F", number)
    oven: bool
    core: bool = False                 # an internal (probe) temperature (pan-aware scaling D24)


def parse_temp_value(raw: str) -> Optional[TempValue]:
    """The value after ``temp:``, or None when it is malformed: ``[main][,core]`` or ``[main][,oven][,fan=...]``."""
    parts = [p.strip() for p in raw.split(",")]
    i, main, fan, oven, core = 0, None, None, False, False
    if (m := _TEMP_MAIN.match(parts[0])):
        main, i = (m.group(2), float(m.group(1))), 1
    elif (g := _TEMP_GAS.match(parts[0])):
        main, oven, i = ("gas", float(g.group(1))), True, 1
    if main is not None and main[0] != "gas" and i < len(parts) and parts[i] == "core":
        core, i = True, i + 1
    if not core and i < len(parts) and parts[i] == "oven":
        oven, i = True, i + 1
    if not core and i < len(parts) and (f := _TEMP_FAN.match(parts[i])):
        fan, oven, i = (f.group(2), float(f.group(1))), True, i + 1
    if i != len(parts) or (main is None and fan is None):
        return None
    for reading in (main, fan):
        if reading is not None and reading[0] != "gas" and not _TEMP_MIN_C <= _celsius(*reading) <= _TEMP_MAX_C:
            return None
    return TempValue(main, fan, oven, core)


def is_pan_id(token_id: str) -> bool:
    """Whether a token's first field names a tin."""
    return token_id.startswith(PAN_PREFIX)


def is_special_id(token_id: str) -> bool:
    """A temperature or a tin: a token that names no ingredient."""
    return is_temp_id(token_id) or is_pan_id(token_id)


def pan_token(pan_id: str, words: str) -> str:
    return "{{" + pan_id + "|" + words + "}}"
```

  and replace `is_temp_id(...)` with `is_special_id(...)` at `_mentions` (202) and `is_tagged` (414), updating `is_tagged`'s docstring ("Temperature and tin tokens …").
- [ ] Replace the `is_temp_id` checks with `is_special_id` in `core/stages/refine.py:157` (`_validate_fat_tokens`), `scripts/backfill_direction_amounts.py:105` (a recipe with any temp or pan token is already tagged — update its CHANGELOG-worthy comment), and leave `refine.py:134`, `temperatures.py:261`, `backfill_temperatures.py:109,131` as they are (they look only at temperature tokens and already pass others through). Add one test each: `_validate_fat_tokens` accepts a `{{pan_01|tin}}` token (in `tests/unit/stages/test_refine_temperatures.py` style), and `tests/unit/scripts/test_backfill_direction_amounts.py` reports `already-tagged` for a recipe whose only token is `{{pan_01|tin}}`.
- [ ] Run the gate. Commit: `feat: the token grammar knows a tin (pan_) and a core temperature (Cayenne pan-aware scaling D21, D24)`.

**Definition of done:** all three tests pass; nothing that reads ingredient tokens treats `pan_01` as an ingredient; `parse_temp_value` agrees with Cayenne's `parseTempValue` on the six values above.

## Task 2: Models

**Produces:** in `models.py`: `PanDims(d, w, l, depth: Optional[float])`, `RecipePan(id, raw, count, shape, family, dims_mm, capacity_ml, catalog_hint, edited)`; `CayenneRefinement` and `CayenneRecipe` gain `pans: List[RecipePan] = []`, `bake_type: Optional[str] = None`, `bake_step: Optional[int] = None`; `PanMention(step, quote, context, pan_id)`, `CoreMention(step, quote)`, `PanAnswer(pans, bake_type, bake_step, mentions, core)`.

**Steps:**
- [ ] Failing test in `tests/unit/test_models.py`:

```python
from recipeparser.models import CayenneRefinement, PanAnswer, RecipePan


def test_refinement_defaults_to_no_tins_and_no_bake():
    r = CayenneRefinement(title="T", base_servings=4, structured_ingredients=[], tokenized_directions=[])
    assert r.pans == [] and r.bake_type is None and r.bake_step is None


def test_recipe_pan_round_trips_with_mm_and_hint():
    p = RecipePan.model_validate({"id": "pan_01", "raw": "two 20cm tins", "count": 2, "shape": "round",
                                  "family": "round-cake", "dims_mm": {"d": 200}, "capacity_ml": None,
                                  "catalog_hint": None})
    assert p.dims_mm.d == 200 and p.edited is False and p.model_dump()["dims_mm"]["depth"] is None


def test_pan_answer_tolerates_free_text_vocab():
    a = PanAnswer.model_validate({"pans": [{"id": "pan_01", "raw": "a tin", "count": 1, "shape": "blob",
                                            "family": "cake", "dims_mm": {}}], "bake_type": "souffle",
                                  "bake_step": 9, "mentions": [], "core": []})
    assert a.pans[0].shape == "blob"  # vocab is enforced by core/pans.py, never by validation
```

- [ ] Implement. Shapes, families and bake types are **plain `Optional[str]`**, not Literals, with the vocab in each field's description: a word outside the list must not fail validation (the `source_uom_system_detected` precedent, `models.py` comment). `count: int = 1`, `edited: bool = False` (the model is never asked for it: exclude it from the Gemini schema by giving it `json_schema_extra` or by popping it in `refine_json_schema()` alongside `grid_categories`, matching how that file already pops). Descriptions (they are the model's instructions in the schema):
  - `RecipePan.id`: "pan_01, pan_02, … in order of first mention."
  - `raw`: "The recipe's words naming the tin(s), copied exactly."
  - `count`: "How many of this tin the recipe uses (two 20 cm tins → 2)."
  - `dims_mm`: "Inside size in millimetres (inches × 25.4): d for a round, l and w for a rectangle or loaf, depth only if the recipe states it."
  - `capacity_ml`: "Only when the recipe names the tin by volume ('a 1.5 litre tin', 'a 10-cup Bundt')."
  - `catalog_hint`: "Only for the named tins listed in the prompt; otherwise null."
  - `bake_type`: one-line list of the 12 values; `bake_step`: "0-based index into tokenized_directions of the step that bakes; null when nothing bakes."
  - `PanMention`: step (1-based), quote, context, pan_id; `CoreMention`: step (1-based), quote ("the words of the internal temperature, copied exactly").
- [ ] `CayenneRecipe` (256) gains the same three fields after the detection fields.
- [ ] Run the gate; **update syrupy snapshots** that now show the new default fields: `python -m pytest tests/ -q --snapshot-update`, then read the snapshot diff and confirm it only adds `pans: []`, `bake_type: None`, `bake_step: None` (and the schema snapshot's new properties). Commit: `feat: models carry a recipe's tins, bake type and bake step (pan-aware scaling data model)`.

**Definition of done:** the three tests pass; snapshot changes are additive only.

## Task 3: `core/pans.py` — the pure checks

**Produces:** `BAKE_TYPES`, `PAN_SHAPES`, `PAN_FAMILIES`, `HINT_KEYS` (R2's list); `normalise_pans(pans) -> Tuple[List[RecipePan], List[str]]`; `check_pan_tokens(directions, ids, source) -> Tuple[List[TokenizedDirection], List[str]]`; `check_bake(bake_type, bake_step, directions, heading_steps) -> Tuple[Optional[str], Optional[int]]`; `check_core_flags(directions) -> Tuple[List[TokenizedDirection], List[str]]`; `apply_pan_checks(refinement, raw_directions) -> List[str]`.

**Steps:**
- [ ] Failing tests `tests/unit/test_pans.py` (write all of these):

```python
from recipeparser.core.pans import (
    HINT_KEYS, apply_pan_checks, check_bake, check_core_flags, check_pan_tokens, normalise_pans,
)
from recipeparser.models import CayenneRefinement, RecipePan, TokenizedDirection

def pan(**kw):
    base = {"id": "pan_01", "raw": "two 20cm tins", "count": 2, "shape": "round", "family": "round-cake",
            "dims_mm": {"d": 200}, "capacity_ml": None, "catalog_hint": None}
    base.update(kw)
    return RecipePan.model_validate(base)

def steps(*texts):
    return [TokenizedDirection(step=i + 1, text=t) for i, t in enumerate(texts)]


def test_normalise_keeps_a_sized_tin_and_drops_an_unsized_one():
    kept, warnings = normalise_pans([pan(), pan(id="pan_02", raw="a cake tin", dims_mm={}, shape=None)])
    assert [p.id for p in kept] == ["pan_01"] and len(warnings) == 1


def test_normalise_nulls_unknown_vocab_and_bad_numbers_and_forces_edited_false():
    kept, _ = normalise_pans([pan(shape="blob", family="cake", count=0, dims_mm={"d": 5000, "depth": 40},
                                  catalog_hint="made-up", edited=True)])
    # d out of range is dropped; depth alone cannot size it, so the tin goes
    assert kept == []
    kept, _ = normalise_pans([pan(shape="blob", family="cake", count=0, edited=True)])
    p = kept[0]
    assert p.shape is None and p.family is None and p.count == 1 and p.edited is False


def test_normalise_keeps_a_hint_only_tin_from_the_list_and_drops_duplicate_ids():
    kept, _ = normalise_pans([pan(raw="a 2 lb loaf tin", shape="loaf", family="loaf", dims_mm={},
                                  catalog_hint="uk-loaf-2lb"), pan()])
    assert [p.catalog_hint for p in kept] == ["uk-loaf-2lb"]
    assert "uk-loaf-2lb" in HINT_KEYS


def test_check_pan_tokens_unwraps_unknown_ids_and_words_the_source_never_wrote():
    out, warnings = check_pan_tokens(
        steps("Grease {{pan_01|two 20cm tins}}.", "Pour into {{pan_09|the tins}}.", "Line {{pan_01|three tins}}."),
        {"pan_01"}, "Grease two 20cm tins.\nPour into the tins.\nLine the tins.")
    assert [d.text for d in out] == ["Grease {{pan_01|two 20cm tins}}.", "Pour into the tins.", "Line three tins."]
    assert len(warnings) == 2


def test_check_bake_keeps_known_type_and_in_range_step_not_a_heading():
    d = steps("Heat the oven.", "**Bake**", "Bake 25 min.")
    assert check_bake("sponge", 2, d, {1}) == ("sponge", 2)
    assert check_bake("souffle", 2, d, {1}) == (None, 2)
    assert check_bake("sponge", 1, d, {1}) == ("sponge", None)
    assert check_bake("sponge", 7, d, set()) == ("sponge", None)


def test_core_flags_survive_only_on_a_probe_temperature():
    out, warnings = check_core_flags(steps(
        "until it reads {{temp:93C,core|93°C}}", "to {{temp:200F,core|200°F}}", "oil at {{temp:180C,core|180°C}}"))
    assert [d.text for d in out] == [
        "until it reads {{temp:93C,core|93°C}}", "to {{temp:200F,core|200°F}}", "oil at {{temp:180C|180°C}}"]
    assert len(warnings) == 1


def test_apply_pan_checks_wires_everything_and_unwraps_tokens_of_dropped_tins():
    r = CayenneRefinement(title="T", base_servings=8, structured_ingredients=[],
                          tokenized_directions=steps("Grease {{pan_01|two 20cm tins}} and {{pan_02|a tin}}.", "Bake."),
                          pans=[pan(), pan(id="pan_02", raw="a tin", dims_mm={}, shape=None)],
                          bake_type="sponge", bake_step=1)
    warnings = apply_pan_checks(r, ["Grease two 20cm tins and a tin.", "Bake."])
    assert [p.id for p in r.pans] == ["pan_01"]
    assert r.tokenized_directions[0].text == "Grease {{pan_01|two 20cm tins}} and a tin."
    assert (r.bake_type, r.bake_step) == ("sponge", 1) and warnings
```

- [ ] Run → FAIL (module missing).
- [ ] Implement `recipeparser/core/pans.py`:

```python
"""
recipeparser/core/pans.py — the tins a recipe names, its bake type and baking step, and the
internal temperatures it states (Cayenne pan-aware scaling: data model, parser contract, D9, D10,
D21, D22, D24). Checks what REFINE and the pans backfill say; a model slip costs the field, never
the recipe. The vocabularies are Cayenne's domain/pans.ts and domain/bakeTypes.ts; keep them alike.

Pure: no I/O, no imports from recipeparser.io or recipeparser.adapters.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from recipeparser.core.fat_tokens import TEMP_PREFIX, TOKEN_RE, is_pan_id, is_temp_id, parse_temp_value
from recipeparser.models import CayenneRefinement, RecipePan, TokenizedDirection

BAKE_TYPES = ("sponge", "butter-cake", "quick-bread", "bar", "bread-lean", "bread-enriched",
              "cheesecake", "custard", "pizza", "pie-fruit", "pie-custard", "pie-nut")
PAN_SHAPES = ("round", "square", "rect", "loaf", "ring", "flat-round", "sheet")
PAN_FAMILIES = ("round-cake", "square", "traybake", "sheet", "loaf", "ring", "tart", "pizza")
# R2: tins a source names without dimensions; Cayenne domain/pans.ts keys.
HINT_KEYS = ("uk-loaf-1lb", "uk-loaf-2lb", "us-bundt-6", "us-bundt-10", "us-bundt-12", "us-tube-10",
             "us-jellyroll", "us-quarter-sheet", "us-half-sheet", "us-rect-13x9", "uk-traybake-30x23",
             "uk-swissroll-33x23", "eu-blech-40x30", "eu-gugelhupf-22", "au-lamington-30x20",
             "au-slab-30x20", "au-bar-25x8")
_ID = re.compile(r"^pan_\d{2}$")
_MM_MIN, _MM_MAX = 10.0, 1000.0
_ML_MAX = 10000.0
_CORE_MIN_C, _CORE_MAX_C = 30.0, 110.0


def _mm(value: Optional[float]) -> Optional[float]:
    return value if value is not None and _MM_MIN <= value <= _MM_MAX else None


def normalise_pans(pans: Iterable[RecipePan]) -> Tuple[List[RecipePan], List[str]]:
    kept: List[RecipePan] = []
    warnings: List[str] = []
    seen: Set[str] = set()
    for p in pans:
        if not _ID.match(p.id) or p.id in seen:
            warnings.append(f"tin {p.id!r} dropped: bad or repeated id")
            continue
        dims = p.dims_mm.model_copy(update={k: _mm(getattr(p.dims_mm, k)) for k in ("d", "w", "l", "depth")})
        capacity = p.capacity_ml if p.capacity_ml is not None and 0 < p.capacity_ml <= _ML_MAX else None
        hint = p.catalog_hint if p.catalog_hint in HINT_KEYS else None
        if dims.d is None and not (dims.l and dims.w) and capacity is None and hint is None:
            warnings.append(f"tin {p.id!r} ({p.raw!r}) dropped: nothing gives its size")
            continue
        seen.add(p.id)
        kept.append(p.model_copy(update={
            "count": p.count if p.count >= 1 else 1,
            "shape": p.shape if p.shape in PAN_SHAPES else None,
            "family": p.family if p.family in PAN_FAMILIES else None,
            "dims_mm": dims, "capacity_ml": capacity, "catalog_hint": hint, "edited": False,
        }))
    return kept, warnings


def _plain(text: str) -> str:
    return " ".join(text.split())


def check_pan_tokens(directions: Sequence[TokenizedDirection], ids: Set[str], source: str
                     ) -> Tuple[List[TokenizedDirection], List[str]]:
    """A pan token whose id is not a kept tin, or whose words the source does not write, becomes its words."""
    plain = _plain(source)
    warnings: List[str] = []

    def check(m: "re.Match[str]") -> str:
        if not is_pan_id(m.group(1)) or (m.group(1) in ids and m.group(3) is None and _plain(m.group(2)) in plain):
            return m.group(0)
        warnings.append(f"pan token unwrapped: {m.group(0)}")
        return m.group(2)

    out = [d.model_copy(update={"text": TOKEN_RE.sub(check, d.text)}) for d in directions]
    return out, warnings


def check_bake(bake_type: Optional[str], bake_step: Optional[int], directions: Sequence[TokenizedDirection],
               heading_steps: Set[int]) -> Tuple[Optional[str], Optional[int]]:
    kind = bake_type if bake_type in BAKE_TYPES else None
    step = bake_step if bake_step is not None and 0 <= bake_step < len(directions) and bake_step not in heading_steps else None
    return kind, step


def check_core_flags(directions: Sequence[TokenizedDirection]) -> Tuple[List[TokenizedDirection], List[str]]:
    """R5: ``,core`` stays only on a degree reading between 30 and 110 °C; otherwise it is removed."""
    warnings: List[str] = []

    def check(m: "re.Match[str]") -> str:
        if not is_temp_id(m.group(1)) or ",core" not in m.group(1):
            return m.group(0)
        value = parse_temp_value(m.group(1)[len(TEMP_PREFIX):])
        if value is not None and value.main is not None:
            scale, n = value.main
            c = n if scale == "C" else (n - 32) * 5 / 9
            if _CORE_MIN_C <= c <= _CORE_MAX_C:
                return m.group(0)
        warnings.append(f"core flag removed: {m.group(0)}")
        return "{{" + m.group(1).replace(",core", "") + "|" + m.group(2) + "}}"

    return [d.model_copy(update={"text": TOKEN_RE.sub(check, d.text)}) for d in directions], warnings


def heading_steps(directions: Sequence[TokenizedDirection]) -> Set[int]:
    """0-based indexes of steps that are a phase heading: bold, short, no token (REFINE rule 3)."""
    return {i for i, d in enumerate(directions)
            if d.text.strip().startswith("**") and d.text.strip().endswith("**") and "{{" not in d.text}


def apply_pan_checks(refinement: CayenneRefinement, raw_directions: Sequence[str]) -> List[str]:
    """Every check above, in place on ``refinement``. Returns the warnings."""
    pans, warnings = normalise_pans(refinement.pans)
    directions, w = check_pan_tokens(refinement.tokenized_directions, {p.id for p in pans}, "\n".join(raw_directions))
    warnings += w
    directions, w = check_core_flags(directions)
    warnings += w
    refinement.pans = pans
    refinement.tokenized_directions = directions
    refinement.bake_type, refinement.bake_step = check_bake(
        refinement.bake_type, refinement.bake_step, directions, heading_steps(directions))
    return warnings
```

  (A `{{temp:93C,core|...}}` token where `parse_temp_value` returns None is left to `_check_temperatures`, which unwraps malformed temperature tokens; `check_core_flags` only removes a flag from an otherwise valid reading. Note the regex `",core" not in m.group(1)` also guards `fan=…` values that contain no "core".)
- [ ] Run the tests → PASS. If `normalise_pans` rejects the first test's case because `count=0` is not valid for the model, give `RecipePan.count` no lower bound in Task 2 (the check owns it).
- [ ] Gate. Commit: `feat: core/pans checks a recipe's tins, pan tokens, bake type and step, and core flags (pan-aware scaling)`.

**Definition of done:** the seven tests pass; no function raises on any input.

## Task 4: REFINE asks for it and checks it

**Produces:** `build_refine_prompt` rule 5 (TINS AND BAKING) and the core bullet in TEMPERATURES; `refine()` calls `apply_pan_checks` after `_check_temperatures` and before `_validate_fat_tokens`, logging its warnings like the other checks.

**Steps:**
- [ ] Failing tests:
  - `tests/unit/stages/test_refine_pans.py` (pattern of `test_refine_temperatures.py`: build a `CayenneRefinement` by hand, patch `recipeparser.core.stages.refine.refine_recipe_for_cayenne`, call `refine(RAW, client=MagicMock())`):
    1. a refinement with `pans=[pan_01 sized]`, `{{pan_01|two 20cm sandwich tins}}` in step 1, `bake_type="sponge"`, `bake_step=2` → result keeps all three and the token;
    2. a `{{pan_02|a tin}}` token with no `pan_02` → unwrapped, the recipe kept, a warning in `caplog`;
    3. `{{temp:180C,core|180°C}}` on an oil step → core removed, token kept;
    4. `bake_type="souffle"` → `None`, recipe kept.
  - `tests/unit/test_gemini_cayenne.py` (or the prompt tests file): `build_refine_prompt("x", None)` contains `"5. TINS AND BAKING"`, `"pan_01"`, `",core"`, every `BAKE_TYPES` value and every `HINT_KEYS` value.
- [ ] Implement the prompt. In the TEMPERATURES bullet, after the `fan=` sub-bullets add:

```
       * ",core" (and nothing else after the figure) when it is the food's internal temperature,
         the doneness check: "until the centre reads 200°F", "an internal temperature of 93°C".
```

  and an example line `"Bake until it reads 93°C in the centre" -> "Bake until it reads {{{{temp:93C,core|93°C}}}} in the centre"`. Add rule 5 after rule 4 (before `SOURCE HOST:`), building the lists from `core.pans` so they cannot drift:

```python
5. TINS AND BAKING:
   - "pans": every baking tin, pan, tray or dish the recipe names with a size, in order of first
     mention, ids pan_01, pan_02, ... One entry per size: "two 20cm sandwich tins" is ONE entry with
     "count": 2. "raw" is the recipe's words, copied exactly.
   - "shape": one of {shapes}. "family": one of {families}.
   - "dims_mm": inside size in MILLIMETRES (inches x 25.4): "d" for a round, "l" and "w" for a
     rectangle, square or loaf; "depth" only when the recipe states it. Never guess a size.
   - "capacity_ml" only when the recipe names the tin by volume ("a 1.5 litre tin", "a 10-cup Bundt").
   - "catalog_hint" only for a tin the recipe names WITHOUT a size, from this list, else null:
     {hints}.
   - A tin with no size and none of those names ("a cake tin", "a baking dish") is not listed.
   - In the directions, wrap the words naming each listed tin in a tin token {{{{pan_01|words}}}},
     copying the words exactly: "Grease two 20cm sandwich tins" -> "Grease {{{{pan_01|two 20cm sandwich tins}}}}".
     Wrap the first mention in each step only.
   - "bake_type": one of {bake_types}, or null when nothing is baked. sponge = whisked or creamed
     sponge cakes; butter-cake = layer, pound, Bundt, loaf cakes, cupcakes; quick-bread = banana bread,
     muffins, scones, cornbread; bar = brownies, blondies, slices; bread-lean / bread-enriched = yeast
     breads without / with butter, eggs or sugar (brioche, challah, rolls); cheesecake; custard =
     baked custards, bread pudding, flan; pizza; pie-fruit, pie-custard (custard, cream, pumpkin
     pies), pie-nut (pecan).
   - "bake_step": the 0-based position in tokenized_directions of the step that bakes the main
     item, or null.
```

  with `shapes=", ".join(PAN_SHAPES)` etc. (and remember the f-string doubling: the literal braces of the token examples are written `{{{{`, the substituted names single `{…}`).
- [ ] Wire `refine()`: right after `_check_temperatures(result, raw)` add

```python
    for line in apply_pan_checks(result, raw.directions):
        log.warning("refine(): '%s': %s", result.title, line)
```

- [ ] Update the prompt snapshot (`python -m pytest tests/goldens/test_prompts_snapshot.py --snapshot-update`) and read the diff: only rule 5, the core bullet and its example are new.
- [ ] Gate. Commit: `feat: REFINE asks for a recipe's tins, bake type and step and core temperatures, and checks them (pan-aware scaling)`.

**Definition of done:** the four refine tests and the prompt test pass; the golden replay suite still passes (recorded replies lack the new fields and default to empty).

## Task 5: New ingest and regen write the columns

**Produces:** `assemble` copies `pans`, `bake_type`, `bake_step` into `CayenneRecipe`; the Supabase insert row writes `pans` (list of dicts), `bake_type`, `bake_step`; `build_update(refinement, embedding, read_rev, current_pans=None)` writes them under R6; `RegenWorker._process` reads the row's current `pans` before building the update.

**Steps:**
- [ ] Failing tests:
  - `tests/unit/test_regen.py`: extend the exact-dict assertion with `"pans": [...]`, `"bake_type": "sponge"`, `"bake_step": 1`; add `test_build_update_keeps_a_cooks_edited_tins`: `current_pans=[{"id": "pan_01", ..., "edited": True}]` and a refinement whose step text has `{{pan_01|two tins}}` and `{{pan_02|a tray}}` → payload `pans` equals `current_pans` unchanged, and the payload's step text keeps `pan_01`'s token and unwraps `pan_02`'s to `a tray`.
  - `tests/unit/test_regen_worker.py`: `_process` selects `pans` for the row (fake supabase records a `.table("recipes").select("pans").eq("id", rid)` call) and passes it to `build_update`; a select failure passes `None` (regen still writes REFINE's tins — the edit guard is best-effort, logged).
  - Writer and assemble tests (find the existing ones asserting `source_uom_system_detected` in the insert row and in `CayenneRecipe`; extend them): the row has `"pans": [ {…} ]` as a list, `"bake_type"`, `"bake_step"`.
- [ ] Implement:
  - `assemble.py` beside lines 136-137: `pans=refinement.pans, bake_type=refinement.bake_type, bake_step=refinement.bake_step`.
  - `io/writers/supabase.py` row: `"pans": [p.model_dump() for p in recipe.pans], "bake_type": recipe.bake_type, "bake_step": recipe.bake_step`.
  - `core/regen.py`:

```python
def build_update(refinement, embedding, read_rev, current_pans=None):
    """... Tins, bake type and step are derived data like the detected system, with one exception
    (pan-aware scaling, parser contract): a tin the cook edited is theirs, so when any current tin is
    edited the cook's tins are written back unchanged and REFINE's pan tokens for other ids unwrap."""
    edited = bool(current_pans) and any(isinstance(p, dict) and p.get("edited") is True for p in current_pans)
    directions = refinement.tokenized_directions
    pans = [p.model_dump() for p in refinement.pans]
    if edited:
        pans = list(current_pans)
        keep = {p.get("id") for p in pans if isinstance(p, dict)}
        directions, _ = check_pan_tokens(directions, keep, strip_fat_tokens("\n".join(d.text for d in directions)))
    return {
        ...existing keys...,
        "tokenized_directions": [d.model_dump() for d in directions],
        "pans": pans,
        "bake_type": refinement.bake_type,
        "bake_step": refinement.bake_step,
    }
```

  (passing the stripped text as `source` keeps every kept token whose words are in its own step).
  - `adapters/regen_worker.py:_process`: before `build_update`, read

```python
        try:
            current = self._sb.table("recipes").select("pans").eq("id", rid).single().execute().data or {}
            current_pans = current.get("pans")
        except Exception as exc:  # the guard is best-effort; regen must not fail for it
            log.warning("regen: %s: could not read current tins (%s); writing REFINE's", rid, exc)
            current_pans = None
```

  and pass `current_pans=current_pans if isinstance(current_pans, list) else None`.
- [ ] Gate (update stage snapshots if `CayenneRecipe` output changed). Commit: `feat: new ingest and regen write a recipe's tins, bake type and step; regen keeps a cook's edited tins (pan-aware scaling)`.

**Definition of done:** a new ingest row and a regen payload carry the three columns as Cayenne's migration types them (`pans` jsonb array, `bake_type` text, `bake_step` integer ≥ 0); an edited tin survives regen.

## Task 6: The backfill's Gemini call and placement

**Produces:** `gemini.build_pans_prompt(steps, ingredients, source_system) -> str`; `gemini.tag_pans(steps, ingredients, source_system, client, *, limiter=None) -> PanAnswer` (temperature 0, raises on failure); in `core/pans.py`: `place_pan_mentions(text, mentions: Sequence[Tuple[str, str, str]]) -> Tuple[str, List[str]]` (quote, context, pan_id) and `mark_core(text, quotes: Sequence[str]) -> Tuple[str, List[str]]`.

**Steps:**
- [ ] Failing tests:
  - `tests/unit/test_pans.py`:

```python
from recipeparser.core.pans import mark_core, place_pan_mentions


def test_place_pan_mentions_wraps_a_free_quote_once():
    text, dropped = place_pan_mentions("Grease two 20cm tins. Bake.", [("two 20cm tins", "", "pan_01")])
    assert text == "Grease {{pan_01|two 20cm tins}}. Bake." and dropped == []


def test_place_pan_mentions_drops_a_missing_or_ambiguous_quote_and_never_enters_a_token():
    text, dropped = place_pan_mentions("Use a tin. Use a tin. {{ing_01|tin foil|all}}",
                                       [("a tin", "", "pan_01"), ("tin foil", "", "pan_02"), ("x", "", "bad")])
    assert text == "Use a tin. Use a tin. {{ing_01|tin foil|all}}" and len(dropped) == 3


def test_mark_core_adds_the_flag_to_the_matching_temperature_token_only():
    text, notes = mark_core("Bake at {{temp:180C,oven|180°C}} until {{temp:93C|93°C}} in the centre.", ["93°C"])
    assert text == "Bake at {{temp:180C,oven|180°C}} until {{temp:93C,core|93°C}} in the centre."
    text, notes = mark_core("Bake at {{temp:180C,oven|180°C}}.", ["180°C"])
    assert "core" not in text and notes  # an oven token never takes core
```

  - `tests/unit/test_gemini_pans.py` (pattern of `test_gemini_temperatures.py`): patch `recipeparser.gemini._call_with_retry` to return `MagicMock(text=json.dumps({"pans": [...], "bake_type": "sponge", "bake_step": 1, "mentions": [...], "core": []}))`; assert `tag_pans` returns a `PanAnswer`, the call used `temperature: 0` and `response_json_schema`; assert `build_pans_prompt` numbers the steps 1-based, lists the ingredient lines, and contains the same TINS AND BAKING wording as REFINE (build both from one shared constant, `_TINS_RULES`, so they cannot drift).
- [ ] Implement:
  - In `gemini.py`, factor the rule-5 body into `_TINS_RULES` (a module-level template filled with the vocab lists) used by `build_refine_prompt` and `build_pans_prompt`. `build_pans_prompt` asks for: `pans`, `bake_type`, `bake_step` (here **0-based** into the numbered steps, said explicitly), `mentions` (step 1-based, quote, context, pan_id: the first mention per step), and `core` (step 1-based, quote: the words of each internal-temperature figure, which may already be inside a `{{temp:…}}` token — quote the figure's words). `tag_pans` mirrors `tag_temperatures` (`_call_with_retry`, `_schema_for_gemini(PanAnswer)`, `temperature: 0`, empty → RuntimeError).
  - In `core/pans.py`: `place_pan_mentions` mirrors `place_temperature_mentions` without unit widening or ranges: id must match `_ID`; quote non-empty with no `{}|`; spans of existing tokens and earlier placements excluded; exactly one free start (`locate`) else dropped; splice with `pan_token`. `mark_core` walks `TOKEN_RE` matches that are temperature tokens; when a match's words contain one of `quotes` (folded with `fat_tokens._fold` — export it as `fold` beside `locate`), its value gains `,core` only if `parse_temp_value(value + ",core")` parses and `check_core_flags` would keep it (30–110 °C); otherwise a note.
- [ ] Gate. Commit: `feat: tag_pans asks for a recipe's tins, bake type and step and its core temperatures; quotes are placed without changing a word (pan-aware scaling D22)`.

**Definition of done:** five tests pass; a misplaced quote changes nothing.

## Task 7: `scripts/backfill_pans.py`

**Produces:** a CLI with `backfill_temperatures.py`'s flags (`--user-id|--all-users`, `--recipe-id`, `--limit`, `--workers`, `--review`, `--live`, `--record`, `--restore`, `--force`, `--verbose`, `--progress`; no `--no-model`/`--repair`); pure `plan_row(row, ask, force=False) -> RowResult`; `write_row(sb, row, result) -> bool`.

**Steps:**
- [ ] Failing tests `tests/unit/scripts/test_backfill_pans.py` (pattern of `test_backfill_temperatures.py`), with a stub `ask(steps, ingredients, system) -> PanAnswer`:
  1. a fresh row (bake_type null, pans `[]`, directions without tokens) → status `tagged`; the result's `pans` are normalised; `tokenized_directions` gains `{{pan_01|…}}`; an existing `{{temp:93C|93°C}}` in the bake step gains `,core`; `bake_type`/`bake_step` checked;
  2. a row with `bake_type` set → `already-done` (R7) and `ask` not called; with `force=True` it is asked;
  3. `derived_rev < body_rev` → `stale`, `ask` not called;
  4. `tokenized_directions` not a list → `malformed`;
  5. `ask` raises → `failed`;
  6. the answer has no tins and `bake_type="pie-fruit"` → status `tagged` with `pans == []`, `bake_type == "pie-fruit"`;
  7. `write_row` sends exactly `pans`, `bake_type`, `bake_step`, `tokenized_directions` with `.eq("id")`, `.eq("body_rev")`, `.eq("derived_rev")` and returns False on empty `data`.
- [ ] Implement by copying `backfill_temperatures.py`'s skeleton (keep its docstring structure and run-order note). Specifics:
  - `COLUMNS = "id,title,ingredient_lines,structured_ingredients,tokenized_directions,body_rev,derived_rev,pans,bake_type,source_uom_system,source_uom_system_detected"`.
  - `plan_row`: skip order stale → malformed → already-done (unless force) → empty (no steps) → ask. With the answer: `pans, w1 = normalise_pans(answer.pans)`; for each step, `place_pan_mentions` with that step's mentions (only ids in `pans`), then `mark_core` with that step's core quotes; `bake_type, bake_step = check_bake(answer.bake_type, answer.bake_step, directions, heading_steps(directions))`; final `check_pan_tokens` pass. Steps are the stored `tokenized_directions` texts (tokens and all); the prompt receives them with tokens stripped (`strip_fat_tokens`) so the model quotes plain words — placement runs on the tokenised text, where `locate` skips token spans.
  - Statuses: `tagged`, `already-done`, `stale`, `empty`, `malformed`, `failed`.
  - `--review FILE.csv` columns: `id, title, pans (JSON), bake_type, bake_step, step, pan_tokens, core_tokens`.
  - Record CSV columns: `id, title, pans, bake_type, bake_step, tokenized_directions` (JSON for the lists); `--restore` writes those four back with `.eq("id")` only, as the temperatures script does.
  - `ask` closure: `lambda steps, ingredients, system: tag_pans(steps, ingredients, system, client, limiter=limiter)`; ingredients are `ingredient_lines` (raw) when present, else the structured entries' `fallback_string`.
- [ ] Gate (ruff/mypy: `scripts/**` exempt from TID, not from the rest). Commit: `feat: backfill_pans gives existing recipes their tins, bake type and step and core flags, pans-only (pan-aware scaling D22)`.

**Definition of done:** the seven tests pass; a dry run never writes; `--live` without `--record`/`--restore` refuses.

## Task 8: Release notes and version

**Steps:**
- [ ] `pyproject.toml` and `installer.iss` → `9.12.0`.
- [ ] `CHANGELOG.md`, newest first:

```
## [9.12.0] — 2026-10-14

A recipe's tins, bake type and baking step, and the internal temperature it states, for Cayenne's pan-aware scaling (Cayenne docs/superpowers/specs/2026-10-13-pan-aware-scaling-design.md: parser contract, D9, D10, D21, D22, D24). Needs Cayenne migration 20261014090000_pan_aware_scaling (merged with Cayenne #287). Order: Cayenne #287 live, then this deploy, then the backfill.

### ✨ Added
- REFINE writes `pans` (each tin: id pan_NN, raw words, count, shape, family, dims_mm, capacity_ml, catalog_hint), `bake_type` (12 values) and `bake_step` (0-based) on a new ingest and on regen, and wraps the tin's words in the directions in `{{pan_NN|words}}`.
- A `temp:` value may end `,core`: the food's internal temperature, the source's own doneness figure (`{{temp:93C,core|93°C}}`). Kept only on a °C/°F reading between 30 and 110 °C.
- `recipeparser/core/pans.py`: the checks; a model slip costs the field, never the recipe.
- `scripts/backfill_pans.py`: gives existing recipes the same fields, pans-only. Run order: dry run with `--review pans-review.csv`, read the CSV, then `--live --record pans-before.csv`.

### ⚠️ Operational
- Regen reads the row's current `pans` first: a tin the cook edited (`edited: true`) is written back unchanged.
- `scripts/backfill_direction_amounts.py` treats a recipe with a `pan_` token as already tagged, like one with a `temp:` token.

### 🧪 Tests
- Unit tests for the grammar, the checks, REFINE, regen, the Gemini call and the backfill; the REFINE prompt snapshot is updated.
```

- [ ] Gate. Commit: `docs: 9.12.0 - tins, bake type and step, core temperatures (pan-aware scaling)`.

## Self-review

1. Every field in the Cayenne *Parser contract* is produced on both ingest paths and the backfill: `pans`, `bake_type`, `bake_step`, `pan_` tokens, `,core`. ✔ Tasks 4, 5, 7.
2. Every place that assumed "a token is an ingredient unless `temp:`" now says "unless temp or pan". ✔ Task 1 list.
3. The vocabularies are literal copies of Cayenne's and live in one place (`core/pans.py`), used by the prompt. ✔ Tasks 3, 4, 6.
4. A model slip never drops a recipe: every check unwraps, nulls or drops a tin with a warning. ✔ Tasks 3–5.
5. The cook's edited tins survive regen. ✔ Task 5.
6. Not verifiable here: prompt quality on real recipes (no Gemini key). The owner's backfill dry run with `--review` is that check.

## Rulings made during execution

- R-A/R-D: the tins rule is one set of shared building blocks. `build_refine_prompt` puts them together byte-identically, and `build_pans_prompt` reuses the vocabulary with its own step wording.
- R-B: `check_core_flags` is a pre-pass. A `,core` on an oven, fan or gas reading is removed and the token is kept, so refine runs it before `_check_temperatures`. Parts are trimmed, so `180C, core` is checked too.
- R-C: a heading step is one bold span (`**…**`, optional colon).
- R-E: an unknown bake type drops its bake step too.
- R-F: when the cook has edited tins, regen keeps a REFINE tin token only if its id and words still name the cook's tin.
- R-G: the pans prompt asks for `bake_step` 1-based like everything else, and `tag_pans` converts it to 0-based once.
- R-H/R-I: the backfill sends Gemini plain steps. Quotes are copied exactly. Tin placement needs word boundaries. A `mark_core` quote that is ambiguous marks nothing.
- Backfill: `cook-edited` is never touched, even with `--force`. `--force` unwraps old tin tokens. `none-found` writes nothing. `--restore` skips a row whose tins the cook has since edited.
- Final review: the Cayenne zip writer and archive restore carry tins, bake type and bake step.
- Accepted risks:
  - a tin edit not yet synced can be overwritten by regen;
  - removing a tin is not remembered (Cayenne follow-up);
  - R7 has no "looked, found nothing" marker, so `--limit` is for trials.
- Follow-ups:
  - merge the fold/plain text helpers;
  - the temperature placer matches mid-word;
  - check rule-5 wording in the dry-run review;
  - run `backfill_direction_amounts` before `backfill_pans`.
