# Servings at import, with their provenance (RecipeParser) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Merged:** [#118](https://github.com/IanDBallard/RecipeParser/pull/118) (9.17.0), squash-merged 2026-10-17T10:35:28Z (`e8d9714`), onto `master`, and deployed by its `Deploy` run; then the follow-up the plan did not foresee, the owner's ruling D52 (a container or volume yield keeps its own count and unit, never the model's), as [#119](https://github.com/IanDBallard/RecipeParser/pull/119) (9.17.1), squash-merged 2026-10-17T12:38:10Z (`f328f07`), onto `master`, and deployed. The Cayenne halves are [Cayenne#328](https://github.com/IanDBallard/Cayenne/pull/328) (the `servings_source` column), [Cayenne#331](https://github.com/IanDBallard/Cayenne/pull/331) (the scale sheet) and [Cayenne#334](https://github.com/IanDBallard/Cayenne/pull/334) (D52). The live servings backfill ran 2026-10-17 after a dry run the owner reviewed. The record is Cayenne's `SpecificationDocumentation/ROADMAP.md`, pre-beta item 6 and its *Document amendments* entry; the deferred minor is Cayenne's FIX_ROADMAP F-394. The checkboxes are not ticked: D52 amended the classifier after the plan was written.

**Goal:** Every recipe leaves ingest with a real `base_servings` and a `servings_source` saying how it was reached (D44, D47, D50), the existing library is backfilled the same way with saved scales preserved, and the yield's own words stay untouched.

**Architecture:** A pure, fixture-driven classifier (`core/servings.py`) applies the deterministic rule to the yield text and the method; a pure tin table (`core/tin_servings.py`) turns the recipe's tins into an estimate; `assemble` composes them with REFINE's `base_servings` as the last resort. No new REFINE output field: the model's existing `base_servings` becomes its `estimated` figure, with a description that says so. A backfill script in the `backfill_pans.py` mould settles the library, calling the model only for rows the rule cannot place.

**Tech Stack:** Python 3.12, Pydantic v2, pytest (syrupy snapshots), the Gemini client via `_call_with_retry`, supabase-py for the script.

**Spec:** Cayenne `docs/superpowers/specs/2026-10-16-scale-sheet-redesign-design.md` (revision 3): D44, D47, D50, *Estimating servings*, *Data*, builder-check items 8–11. Its research note: `2026-10-16-servings-per-tin-research.md`.

**Version:** 9.17.0. Cayenne's migration (`servings_source` column) must be applied before this version deploys (D28's order); say so in the CHANGELOG's *Requires* section.

## Global Constraints

- Branch `feat/servings-source` in the worktree `RecipeParser-wt-servings`; never the main checkout. Never run tests with the repo `.env` pointing at the live project unless the test is pure: every test here is pure (fakes, as `test_backfill_pans.py` does).
- Commit messages end `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Each task is test-first: the failing test, then the code.
- `servings_source` values: exactly `stated`, `counted`, `tin`, `estimated`, or `None`.
- The deterministic rule is one function with one shared fixture, `tests/fixtures/servings_source_cases.json`, copied verbatim to Cayenne `cayenne-web/tests/fixtures/` by the Cayenne plan (the `duration_cases.json` precedent, `core/durations.py:1-11`). Change the rule here and there together.
- The tin table's figures are the spec's: layer cake 38 cm², single layer 55 cm², bar 31 cm², cheesecake 35 cm², tart 50 cm², loaf 2.2 cm per serving, ring 240 ml per serving. Round to the nearest whole number, at least 1.
- `regen.build_update` never writes `base_servings` or `servings_source` (ARCHITECTURE.md:801 stands).
- Run `ruff check .` and `pytest -q -p no:xdist` (or the repo's usual `pytest`) before each commit; the snapshot updates are deliberate and reviewed.

## Review Focus

1. A yield with a number and a noun that is neither an item nor a bake ("6-8 as a starter", "4 generous portions") must stay `stated`, never `counted`. Pinned in Task 1's fixture.
2. "Cut into 1-inch cubes" must not read as a count of 1. Pinned in Task 1's fixture (method counts need a bare number followed by a plural noun or punctuation).
3. A tin with `removed: true` (a tombstone) must not count toward the tin estimate. Pinned in Task 2.
4. A row whose `cook_servings` is set and whose base changes must have `cook_servings` rescaled in the same update; a row whose base does not change must not be written at all. Pinned in Task 4.
5. A restore from a pre-9.17 archive must never call the model and must leave `base_servings` as exported when the rule gives no tin estimate. Pinned in Task 3.

---

### Task 1: The deterministic classifier and its shared fixture

**Files:**
- Create: `recipeparser/core/servings.py`
- Create: `tests/fixtures/servings_source_cases.json`
- Create: `tests/unit/test_servings.py`

**Interfaces:**
- Consumes: `parse_servings(text) -> Span` and `_normalise` from `recipeparser/core/durations.py:59-149`.
- Produces:
  ```python
  @dataclass(frozen=True)
  class Classified:
      source: Literal["stated", "counted", "bake", "none"]
      base: Optional[int]        # None for "bake" and "none"
      span: Span                 # min, max, note as parse_servings gives them

  def classify_servings(servings_text: Optional[str], directions: Sequence[str]) -> Classified
  ```
  `bake` means "the yield is the bake itself" ("one 9-inch pan", "1 loaf", "2 loaves"); `none` means no usable yield at all. Both are placed by Task 3.

The rule, in order (D50, D47, builder-check item 8):
1. `span = parse_servings(servings_text)`. Let `text = _normalise(servings_text or "")`.
2. If `text` contains a serving word (`serves|servings?|people|persons?|portions?`) and `span.min is not None` → `stated`, `base = span.min`.
3. If `span.min is not None` and the note contains an item noun → `counted`, `base = span.min`. Item nouns (one module constant `ITEM_NOUNS`): cookies, biscuits, muffins, cupcakes, scones, buns, rolls, bagels, pretzels, crackers, bars, squares, brownies, blondies, slices, pieces, tarts, tartlets, pies (plural only, i.e. "6 mini pies"), pancakes, crêpes, crepes, waffles, doughnuts, donuts, macarons, meringues, truffles, balls, dumplings, pasties, empanadas, sausage rolls, each matched as a whole word, plural or singular when the count is 1.
4. If `span.min is not None` and the note contains a bake noun → `bake`. Bake nouns (`BAKE_NOUNS`): cake, cakes, loaf, loaves, pan, pans, tin, tins, pie, tart, pizza, pizzas, cheesecake, bundt, ring, sheet, tray, traybake, dish, batch, quart, quarts, cup, cups, litre, litres, liter, liters, jar, jars, pint, pints (the last nine are the "volumes and containers" of item 8, which also fall to the model).
5. If `span.min is not None` (a bare number, "about 4", "6-8 as a starter") → `stated`.
6. No number: scan `directions` for a method count (D47): the first match of `\b(?:cut|slice|divide|portion)\b[^.;]{0,40}?\binto\b\s+(\d{1,3})\b(?!\s*[-–]?\s*(?:inch|in\b|cm|mm|centimet|millimet|"))` or `\b(?:makes|yields)\s+(\d{1,3})\b(?!\s*(?:cup|cups|quart|litre|liter|ml|g\b|kg|oz|lb))`, case-insensitive, with the captured number ≥ 2 → `counted`, `base` = that number.
7. No number and the text is non-empty (it names the bake: "One 8-inch round layer cake") → `bake`.
8. Otherwise → `none`.

Rules 6 and 7 are checked in that order so "one 9-inch pan" with "cut into 16 squares" is `counted` 16.

**Amended by the whole-branch review's fix wave (2026-10-17; the fixture is the contract).** The rule, in order:
1. A number of one or more adjacent to a serving word anywhere in the text (`serves[:] [about] N`, `servings|portions[:] N`, `N[-M] [word] servings|people|persons|portions`) → `stated` N. "Makes 1 loaf (serves 10)" is 10.
2. With a leading number N ≥ 1 (a number below one is no number): the note, leading "about" dropped, a leading "dozen" multiplying N by 12. A phrase holding only a bare number ("12 ( 6)", "24 cookies ( 12)", "1 pie, 8"), or a second number straight after the first ("12 24 bars") → `bake` (which number serves is lost). An empty note, or one that begins a separate phrase ("(", ",", ";", "one ", "a ", "an ") but is not a parenthesised size of a bake noun ("2 (9-inch) loaves") → `stated` N, even when the phrase holds a count ("8 (16 slices)"). Else a number attached to an item noun anywhere (`N[-M] [dozen] [word] item-noun`, never a bake noun such as "swiss rolls") → `counted` (×12 for dozen): "2 loaves (24 slices)" is 24. Else a bake noun → `bake` (before the singular item noun: "1 swiss roll", "1 bar cake"); else an item noun, or its singular at N = 1 → `counted` N; else `stated` N. This keeps a stored "12 one 9-inch pan" (parse_servings drops "Serves") `stated`.
3. No leading number: the item count anywhere as in 2; "a dozen" + item noun → `counted` 12; "N dozen" or "a dozen" anywhere → `stated` N×12 ("Makes about 2 dozen", "Yield: 2 dozen").
4. No number at all: the method count, which now needs a portion noun after the number (the item nouns but "pieces", plus triangles, fingers, servings, portions; one optional word between, never a dimension) → `counted`; then `bake` if the text is non-empty; else `none`.

**Amended by the owner's ruling D52 (2026-10-17, RecipeParser 9.17.1): containers and volumes keep their own count.** The 9.17.0 dry run gave condiments and preserves large model estimates (Garam Masala "1 cup" → 40, Tomato Ketchup "2 cups" → 32, marmalade "3–4 jars" → 40). Cup, cups, quart(s), litre(s), liter(s), jar(s) and pint(s) leave `BAKE_NOUNS` for a new `CONTAINER_NOUNS`: jar(s), bottle(s), pot(s), tub(s), cup(s), pint(s), quart(s), gallon(s), litre(s)/liter(s), l, ml, millilitre(s)/milliliter(s), fl oz, fluid ounce(s), matched also straight after a number ("750ml"). After the stated checks (rule 1, and in rule 2 the empty note or separate phrase, so "Serves 4 (makes 2 cups)" stays stated 4), a yield that names a container and no bake noun → `counted`, never the tins or the model: the count is the parsed span's number (a range's low end: "3-4 jars" is 3; "3 × 1½ lb jars" is 3, the leading count, never the size inside), else the leading number after makes/yields/about ("Makes about 1 litre" is 1), else "a"/"an"/"one" → 1 ("a jar"), else the number on the noun ("enough for 2 cups" is 2); a count below one is no count. Numbers round as `parse_servings` does (Python's `round`, half to even: "1.5 litres" is 2, "2.5 litres" is 2), so a stored form classifies the same. `Classified.noun` names the container (the last one before any "(", "," or ";": "2 × 500 ml jars" steps in jars). A bake noun beside a container keeps the bake path ("1 x 2 litre dish"). The parenthesised-size exception of rule 2 takes container nouns too ("2 (500 ml) jars" is counted 2). The fixture rows "Makes 4 cups", "3 jars" and "4 cups" in Step 1 below are now `counted` 4, 3 and 4. Accepted edge: "makes 4 cups soup" steps in cups.

**Further coordinator rulings on D52 (2026-10-17).** An item count beats a container count: the item count of rule 2/3 is checked before the container rule ("2 cups (about 24 truffles)" is counted 24, noun "truffles"; "4 cups, makes 16 muffins" is 16), and stated stays first ("1 jar, about 12 servings" is stated 12). `Classified.noun` carries the item noun for such counts. Basin(s), pudding basin, mould(s), mold(s) and dish(es) are bake nouns, so "1 x 1.2 litre pudding basin" and "a 2 litre dish" are `bake`. The bare-number check of rule 2 accepts an approximation word ("1 jar, about 12", the stored form of "1 jar, about 12 servings") and returns `bake`.

- [ ] **Step 1: Write the fixture** `tests/fixtures/servings_source_cases.json`, shape `{"cases": [{"servings": <text|null>, "directions": [<step>...], "source": "...", "base": <int|null>}]}`, with at least these cases (expected in parentheses):
  - `"Serves 6"`, `[]` (stated 6); `"4 servings"` (stated 4); `"6-8 as a starter"` (stated 6); `"about 4"` (stated 4); `"8"` (stated 8); `"Makes 8"` (stated 8); `"4 generous portions"` (stated 4);
  - `"Makes 24 cookies"` (counted 24); `"makes 12 cookies"` (counted 12); `"Yields 16 bars"` (counted 16); `"24 small pancakes"` (counted 24); `"6 mini pies"` (counted 6);
  - `"Serves 12, one 9-inch pan"` (stated 12); `"serves 8 (one 20 cm cake)"` (stated 8);
  - `"Makes 1 loaf"` (bake); `"2 loaves"` (bake); `"One 8-inch round layer cake"` (bake); `"Yields one 9-inch-square pan of brownies"` (bake); `"Makes 4 cups"` (bake); `"3 jars"` (bake); `"1 cake"` (bake);
  - `"one 9-inch-square pan"`, `["Cut into 16 squares."]` (counted 16); `"a crowd"`, `["Divide the dough into 12 balls."]` (counted 12); `null`, `["Makes 24 muffins."]` (counted 24);
  - `null`, `["Cut into 1-inch cubes."]` (none); `null`, `["Cut into 2 cm pieces."]` (none); `null`, `["Makes 3 cups of sauce."]` (none); `"a crowd"`, `[]` (bake); `null`, `[]` (none); `""`, `[]` (none).
- [ ] **Step 2: Write the failing tests** in `tests/unit/test_servings.py`: `test_classify_fixture` parametrised over the fixture (ids from the input), asserting `source` and `base`; `test_classify_keeps_span_note` ("Makes 24 cookies" → `span.note == "cookies"`); `test_item_and_bake_nouns_are_disjoint`.
- [ ] **Step 3: Run** `pytest tests/unit/test_servings.py -q` — expect `ModuleNotFoundError: recipeparser.core.servings`.
- [ ] **Step 4: Implement `classify_servings`** in `recipeparser/core/servings.py` with `ITEM_NOUNS`, `BAKE_NOUNS`, `SERVING_WORDS_RE`, `_METHOD_COUNT_RES` as module constants; pure, no I/O.
- [ ] **Step 5: Run** the file — all pass. Run `ruff check recipeparser/core/servings.py`.
- [ ] **Step 6: Commit** `feat(core): classify a yield as stated, counted, the bake itself, or none (D44, D47, D50)`.

---

### Task 2: The tin table and the hint-tin dimensions

**Files:**
- Create: `recipeparser/core/tin_servings.py`
- Create: `tests/fixtures/hint_tins.json`
- Create: `tests/unit/test_tin_servings.py`
- Modify: `recipeparser/core/pans.py:46-64` (nothing but a comment pointing at the fixture; `HINT_KEYS` stays the source of valid hints)

**Interfaces:**
- Consumes: `RecipePan` (`models.py:238-270`: `count`, `family`, `shape`, `dims_mm.{d,w,l,depth}`, `capacity_ml`, `catalog_hint`, `removed`), `BAKE_TYPES` (`pans.py:28-42`).
- Produces:
  ```python
  HINT_TINS: Dict[str, HintTin]   # loaded from tests/fixtures/hint_tins.json at import? No: a module literal; the fixture is the test's copy (see Step 1)
  def estimate_from_tins(pans: Sequence[RecipePan], bake_type: Optional[str]) -> Optional[int]
  ```
  Cayenne's plan copies `hint_tins.json` beside `pans.ts` and asserts `PANS` agrees with it, so the two cannot drift.

Rules (spec *Estimating servings*; first match wins):
- Drop pans with `removed`. Resolve each pan's geometry: `dims_mm` when `d` or `l`+`w` is present; else `HINT_TINS[catalog_hint]`; else no geometry. A pan with no geometry and no capacity contributes nothing. If no pan contributes → `None`. Family comes from the pan, else from the hint.
- `pizza` bake type, `stovetop` family, a null family, or families of two kinds among the contributing pans → `None`.
- `loaf` family: `Σ count × length_cm / 2.2` where length is `l` (or `d` when only that is given).
- `ring` family: `Σ count × capacity_ml / 240`; capacity from the pan, else the hint's.
- `tart` family: `Σ count × area / 50`.
- `bar` bake type: `Σ count × area / 31`.
- `cheesecake`: `Σ count × area / 35`.
- Cake group (`round-cake`, `square`, `traybake`, `sheet`): layer cake when the contributing pans are one size with `count ≥ 2`, or one pan with `depth ≥ 75` mm → `one tin's area / 38`; else `Σ count × area / 55`.
- Area in cm²: round `π(d/20)²`; rectangle `(l/10)(w/10)`.
- Result `max(1, round(x))`.

- [ ] **Step 1: Write `tests/fixtures/hint_tins.json`** with the 17 `HINT_KEYS` (`pans.py:46-64`), each `{"key", "family", "shape", "dims_mm": {d?, w?, l?, depth}, "capacity_ml": {min, max}?}` copied from Cayenne `cayenne-web/src/lib/domain/pans.ts:132-201` (the `round/rect/loaf/ring/flat` helpers' arguments). Write the same values as the `HINT_TINS` literal in `tin_servings.py`; `test_hint_tins_match_fixture` asserts equality, so the fixture is the cross-repo contract and the literal keeps the module import-free.
- [ ] **Step 2: Write the failing tests** in `tests/unit/test_tin_servings.py`, building pans with a `_pan(**kw)` helper:
  - `test_layer_cake_two_20cm` (2 × d=200, bake `butter-cake` → 8); `test_two_23cm` (→ 11); `test_sheet_13x9_single` (l=330, w=230, `butter-cake` → 14); `test_bars_9x13` (`bar` → 24); `test_bars_9x9` (l=w=229 → 17); `test_cheesecake_23_springform` (d=230, `cheesecake` → 12); `test_tart_23` (family tart, d=230 → 8); `test_loaf_8_5_inch` (family loaf, l=216 → 10); `test_bundt_12_cup` (family ring, capacity 2840 → 12, and via hint `us-bundt-12` with no dims → 12);
  - `test_deep_single_tin_is_a_layer_cake` (d=200, depth=75 → area/38);
  - `test_pizza_is_none`, `test_stovetop_is_none`, `test_two_families_is_none`, `test_tombstone_is_skipped` (Review Focus 3), `test_no_geometry_is_none`, `test_at_least_one`.
- [ ] **Step 3: Run** `pytest tests/unit/test_tin_servings.py -q` — fails on import.
- [ ] **Step 4: Implement** `recipeparser/core/tin_servings.py`.
- [ ] **Step 5: Run** the file and `ruff` — pass.
- [ ] **Step 6: Commit** `feat(core): estimate servings from the recipe's tins (spec table; hint tins fixture)`.

---

### Task 3: Compose at ingest and restore; the column on the model

**Files:**
- Modify: `recipeparser/models.py:309` (`CayenneRefinement.base_servings` description), `:345` (`CayenneRecipe.servings_source: Optional[str] = None` beside `base_servings`, with a `Literal`-checked validator that nulls an unknown value)
- Modify: `recipeparser/core/stages/assemble.py:119-128`
- Modify: `recipeparser/core/pipeline.py:653-710` (`_pre_parsed_to_refinement`, `_SPAN_KEYS`, `_restore_spans`)
- Modify: `recipeparser/io/writers/supabase.py:260` (`"servings_source": recipe.servings_source` after `base_servings`)
- Modify: `recipeparser/core/servings.py` (add the composer)
- Tests: `tests/unit/test_servings.py`, `tests/unit/stages/test_assemble.py`, `tests/unit/stages/test_assemble_raw.py`, `tests/unit/test_pipeline.py:1145-1240`, `tests/unit/writers/test_supabase_row.py`, `tests/fixtures/cayenne_contract_rows.py`, `tests/unit/test_regen.py:83-111`, `tests/unit/test_models.py`, snapshots

**Interfaces:**
- Consumes: Task 1's `classify_servings`, Task 2's `estimate_from_tins`.
- Produces:
  ```python
  @dataclass(frozen=True)
  class Servings:
      base: Optional[int]
      source: Optional[str]     # stated | counted | tin | estimated | None

  def servings_for_recipe(servings_text, directions, pans, bake_type, model_estimate: Optional[int]) -> Servings
  def servings_for_restore(span: Span, directions, pans, bake_type) -> Servings   # never the model (item 11)
  ```
  `servings_for_recipe`: `stated`/`counted` as classified; `bake`/`none` → tin estimate (`tin`) → `model_estimate` (`estimated`) → `Servings(None, None)`.
  `servings_for_restore`: rebuilds a text from the span (`f"{min}-{max} {note}"`, or the note alone when min is None) and runs the same steps without the model; a `stated`/`counted` result keeps the exported `base` and returns source `None` (item 11: a pre-9.17 archive's number reads as the recipe's own), a tin result returns (`tin`, estimate), else `(exported base, None)`.

- [ ] **Step 1: Failing tests.** In `test_servings.py`: `test_compose_stated_ignores_tins_and_model`, `test_compose_bake_takes_tin`, `test_compose_bake_without_tin_takes_model`, `test_compose_none_without_anything_is_none`, `test_restore_never_estimates_with_the_model` (passes a tin-less bake → `(exported, None)`), `test_restore_takes_tin`. In `test_assemble.py`: `test_assemble_sets_servings_source` (a `_refined()` with `base_servings=2` and `servings_text="Serves 2"` → `stated`; with `servings_text=None`, no pans → `estimated`, base 2). In `test_assemble_raw.py:37-43`: the existing "servings_min wins" case now also asserts `servings_source == "stated"`. In `test_pipeline.py`: `test_restore_carries_servings_source` (meta with `servings_source: "tin"` round-trips) and `test_restore_without_servings_source_uses_the_tin_table` (meta from 9.16 with pans and `base_servings=1`, note "one 9-inch pan" → `tin`, base from Task 2). In `test_supabase_row.py`: the row has `servings_source`; `CONTRACT_ROWS` gain `servings_source: "stated"` (LEMON, XMAS, SCONES all state servings). In `test_regen.py`: `"servings_source" not in payload`.
- [ ] **Step 2: Run** the four files — fail.
- [ ] **Step 3: Implement.** `models.py`: description on `CayenneRefinement.base_servings`: "How many servings the recipe makes. When the recipe states it, that number; otherwise your best estimate for a home cook from the whole recipe." `CayenneRecipe.servings_source`. `assemble.py`: after `cols` is chosen, `sv = servings_for_recipe(servings_text, direction_steps or [], recipe.pans, recipe.bake_type, cols["base_servings"])` when `spans is None`; else `sv = Servings(cols["base_servings"], cols.get("servings_source"))`; set `base_servings=sv.base if sv.base is not None else cols["base_servings"]`, `servings_source=sv.source`. `pipeline.py`: `_SPAN_KEYS` gains `servings_source`; `_restore_spans` returns it from `pr` when `"servings_source" in pr.model_fields_set`, else computes `servings_for_restore(...)` from the restored span and `pr.pans`/`pr.bake_type` and merges `base_servings`/`servings_source` into the dict. `supabase.py`: the key.
- [ ] **Step 4: Update snapshots** (`pytest --snapshot-update tests/goldens/test_prompts_snapshot.py tests/snapshots -p no:xdist`) and the e2e goldens (`pytest --update-goldens tests/goldens -p no:xdist`): the schema gains the description; `cayenne.json` goldens gain `servings_source`. Review the diffs: only those keys change.
- [ ] **Step 5: Run** the full suite — pass.
- [ ] **Step 6: Commit** `feat(ingest): base_servings for every recipe with servings_source (stated, counted, tin, estimated); restore keeps or derives it without the model`.

---

### Task 4: The backfill script

**Files:**
- Create: `scripts/backfill_servings.py`
- Modify: `recipeparser/gemini.py` (add `estimate_servings`)
- Create: `tests/unit/scripts/test_backfill_servings.py`, `tests/unit/test_gemini_servings.py`

**Interfaces:**
- Consumes: Task 3's `servings_for_recipe`; `_call_with_retry` (`gemini.py`); the `backfill_pans.py` shapes (`plan_row`, `write_row`, `write_review`, `_read_rows`, `main`, `tests/unit/scripts/test_backfill_pans.py`'s `FakeTable` and `_run_main`).
- Produces:
  ```python
  # gemini.py
  def estimate_servings(client, title: str, ingredients: Sequence[str], directions: Sequence[str], *, limiter=None) -> Optional[int]
  # backfill_servings.py
  COLUMNS = "id,title,ingredient_lines,tokenized_directions,servings_min,servings_max,servings_note,base_servings,servings_source,pans,bake_type,cook_servings,body_rev,derived_rev"
  def plan_row(row, ask: Asker, force=False) -> RowResult   # RowResult: id, title, status, old_base, new_base, source, new_cook, yield_text, used_model, notes
  def write_row(sb, row, result) -> bool
  ```
  `estimate_servings` asks the model with a JSON schema `{"servings": int}` and the same rate limiter; it returns None on any failure.

Statuses, in order: `stale` (`derived_rev < body_rev`), `stated` (row's `servings_source == "stated"`: never touched, even with `--force`), `unchanged` (same base and source already), `set` (written), `model` (set via the model), `unplaced` (no base at all; logged, nothing written), `error`. The text classified is rebuilt from `servings_min/max/note` as `servings_for_restore` does; the directions' text comes from `tokenized_directions[].text`; the model is asked only when the rule and the tin give nothing. `new_cook = round(cook × new/old, 4)` when `cook_servings` and the old base are set and `old > 0`, else unchanged. `write_row` updates `base_servings`, `servings_source` and `cook_servings` (only when it changed), `.eq("id").eq("body_rev").eq("derived_rev")`. Review CSV header: `id,title,status,old_base,new_base,source,yield,new_cook,used_model,moved_pct,flag` with `flag = "check"` when `moved_pct > 25` (builder-check item 9). Record header: `id,base_servings,servings_source,cook_servings`. `--restore` writes those three back. A summary line prints the status counts and how many rows used the model.

- [ ] **Step 1: Failing tests.** `test_gemini_servings.py`: `test_estimate_servings_parses_the_reply` (patch `_call_with_retry` → `MagicMock(text='{"servings": 10}')` → 10), `test_estimate_servings_returns_none_on_garbage`. `test_backfill_servings.py` (load the script as `test_backfill_pans.py:11-15` does): `test_stated_rows_are_never_touched`, `test_counted_from_note_sets_without_the_model` (note "cookies", base 24 → `set`, `counted`, no ask), `test_bake_with_tin_sets_tin`, `test_bake_without_tin_asks_the_model` (`model`, `estimated`), `test_unchanged_when_nothing_moves`, `test_cook_servings_rescales` (old 1, cook 1.5, new 16 → new_cook 24.0; Review Focus 4), `test_write_row_is_conditional_and_omits_cook_when_unchanged` (exact payload key sets), `test_review_flags_a_big_move` (1 → 16 is 1500%: `check`), `test_live_requires_record`, `test_restore_writes_three_columns`.
- [ ] **Step 2: Run** both files — fail.
- [ ] **Step 3: Implement** `estimate_servings` and the script (copy `backfill_pans.py`'s CLI, paging, threading, `--live/--record/--restore/--force/--recipe-id/--user-id|--all-users/--limit/--workers/--review/--verbose/--progress`; the stub-asker probe so `--limit` counts model calls).
- [ ] **Step 4: Run** — pass; `ruff`.
- [ ] **Step 5: Commit** `feat(scripts): backfill_servings - deterministic first, the model only for the leftovers, saved scales rescaled`.

---

### Task 5: Release 9.17.0 and the documents

**Files:**
- Modify: `pyproject.toml:7`, `installer.iss:3,6` (9.17.0)
- Modify: `CHANGELOG.md` (new top entry), `ARCHITECTURE.md:474,792,799-801`
- Modify: `recipeparser/io/readers/paprika.py:214-215` (the stale "no servings parser" comment)

- [ ] **Step 1: CHANGELOG** `## [9.17.0] — <date>`: summary citing the Cayenne spec; *Requires*: apply Cayenne migration `servings_source` before deploying; *Added*: `core/servings.py`, `core/tin_servings.py`, `servings_source`, `scripts/backfill_servings.py`; *Changed*: REFINE's `base_servings` is now the model's estimate and is used only when the yield gives nothing; restore derives `servings_source` without the model; *Operational*: the backfill's commands, in order — `python scripts/backfill_servings.py --all-users --review ../backfill-records/servings-review-<date>.csv`, review the `check` rows, then `--live --record ../backfill-records/servings-before-<date>.csv`.
- [ ] **Step 2: ARCHITECTURE.md:** the restore paragraph (474) gains `servings_source`; the module table (792) gains the two modules; the writer column list and the regen ownership line (799-801) name `servings_source` as user-owned after ingest.
- [ ] **Step 3: Bump the version** in both files; `git grep 9.16.0` shows none left in code.
- [ ] **Step 4: Run** the full suite once more — pass. **Commit** `chore: release 9.17.0`.
- [ ] **Step 5: Push and open the PR** against `master`, titled `feat: servings at import with their provenance - servings_source, the tin table, the backfill (9.17.0)`, body naming the Cayenne spec, the deploy order and the backfill commands; end it with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. **Do not merge** until Cayenne's migration is live (the Cayenne plan's Task 1 PR).

## Rulings made during the build (2026-10-16/17)

Recorded from the execution ledger before it was discarded. Each says what it costs if wrong.

- Rulings from the scan:
- Ruling: T3 exports `text_from_span(span: Span) -> str` from `core/servings.py` (rebuilds "min-max note" / note) and T4 uses it — avoids two copies; cost if wrong: a one-line helper.
- Ruling: the `HINT_TINS` literal + fixture equality test stands as the plan wrote it (the fixture is the contract Cayenne's drift test reads; shipping tests/fixtures as package data would be the alternative) — cost if wrong: one list maintained twice, guarded by a test.
- Ruling: the `estimated` figure is REFINE's `base_servings` (recipe.base_servings), not `cols["base_servings"]` — the spec's D44 says the model's estimate; the yield's "1 cake" must never become 1 serving. Cost if wrong: none visible (the brief's text was the error).
- Ruling: `servings_for_restore(span, directions, pans, bake_type, base)` carries the exported base — it must return it unchanged for stated/counted. Cost if wrong: a signature.
- Ruling: on restore of a pre-9.17 archive, a `none` classification (no yield text) takes the tin estimate exactly as `bake` does — the exported number was REFINE's old guess (a cook's typed servings always land in servings_min, which would classify as stated), and a restored recipe must look like a backfilled one. CONTRACT_ROWS keep `servings_source` None; `RESTORE_DERIVES` holds LEMON's derived (6, "tin"). Cost if wrong: a restored no-yield recipe shows "≈ 6" where its archive said 8.
- Task 4: implemented (DONE, concerns). Ruling: without `--force`, a row whose `servings_source` is already set (counted/tin/estimated) is `unchanged` and skipped — the backfill is idempotent and a re-run costs no model calls; `--force` reclassifies every non-stated row; `stated` is never touched either way. Cost if wrong: an extra flag to explain.
- Task 5: fix round 2/5 (1 addressed; commit 25739bb; a one-sentence CHANGELOG edit dictated verbatim, verified by diff rather than a re-review seat — Ruling: a literal-text fix I specified needs no reviewer; cost if wrong: none, the sentence is quoted in the ledger)
- Rulings for the fix wave:
- Ruling: `parse_servings` (the duration_cases.json twin) is NOT changed: stored rows already hold its first-number result and nothing can recover a lost "(serves 10)" but the model. The classifier gets the stored-form heuristic (a note beginning "(", ",", "one", "a", "an" after a number → the number is servings → stated). Rows whose stored form is irrecoverable classify as bake → an honest "≈" estimate, never a wrong stated/counted number. The round-trip property is therefore: classify(stored) == classify(raw) OR classify(stored).kind in {bake, none}. Cost if wrong: some "Makes 1 loaf (serves 10)" rows show ≈ after the backfill instead of 10. Filed as an F-ID follow-up (teach parse_servings the serving-word number, both twins).
- Ruling: the method-count regex requires a portion noun (the item nouns, plus wedges, slices, squares, bars, servings, portions, triangles, fingers, balls) and rejects x/by/inch/cm dimensions, bake nouns and the word "pieces". Cost if wrong: a few "cut into 8 pieces" counts lost to the tin table (≈).
- Ruling: no REFINE `servings_source` output field (spec item 8 said one): D50's deterministic rule makes it redundant; the model's only job is `base_servings` as the estimate. Cost if wrong: none at runtime.
- Ruling: the layer-cake rule narrows to round-cake/square (M2) as the spec table says.
- Ruling (deviates from "no second fix wave"): the residual writes wrong bases to live recipes and the PR is not yet open, so ONE targeted correction with its own scoped re-review runs before the PR — cost if wrong: one more review seat. The rule: a leading number followed by a separate phrase ("(", ",", ";") is stated N even when the phrase holds a count; a separate phrase holding only a bare number (a stripped serving word) → bake (irrecoverable → estimate), never the leading number; "N dozen" / "a dozen" anywhere in the text → ×12 (counted with a noun, stated without).

## Deferred minors for the post-merge books

- Task 1: minor (deferred): "Makes 1 square pan" → counted 1 (an item noun and a bake noun in one note; brief-literal). Minor: `_normalise` imported privately (brief-mandated); the note's lower-casing is implicit.
- Task 2: minor (deferred): file-wide `# ruff: noqa: E501` in tin_servings.py (prefer a per-file-ignore in pyproject); Python round() half-to-even vs JS Math.round (moot: the client never estimates, D44); a loaf given only `l` is dropped by `_has_geometry` (the loaf rule's `l or d` is partly dead); layer-cake depth read from the first entry only; no test for a pan's own dims/capacity overriding its hint's.
- Task 3: minor (deferred, FIX IN THE FINAL WAVE): assemble.py:312 falls back to cols["base_servings"] (= servings_min = 1 for "1 cake") when the composition gives (None, None) — store None for a bake/none classification instead (the spec's anti-goal). Also: no guard around estimate_from_tins for NaN dims (math.isfinite in Task 2's _score); ingest/restore repeat the classify→tin steps; one restore test asserts little beyond construction.
- Task 4: minor (deferred): no tests for malformed pans → error, record-before-failing-write, or the rescaled cook_servings payload (18.0) in the live-record test; plan_row has unguarded lines outside its try (202-208, 241-243); a direction without `text` is skipped silently; --restore writes unconditionally by id (mirrors backfill_pans); --limit rows missing from the review CSV; db5b660 fails alone (bisectability).
