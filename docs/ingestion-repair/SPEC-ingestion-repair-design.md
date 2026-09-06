# Ingestion Fixes and Library Repair Design

**Date:** 2026-09-05
**Status:** v1, approved in brainstorming on 2026-09-05. Sections 1 and 2 of the in-chat design were approved before this document was written. §6.3's deploy order corrected 2026-09-05 by the whole-branch review of `feat/ingestion-fixes`: migrations 009/010 must be applied *before* the deploy, not after.
**Scope:** the seven `RecipeParser` findings recorded in `SpecificationDocumentation/EXECUTION_PLAN.md` under "The ingestion API", one cross-repo data-shape change, and a one-time repair of the live library.
**Repositories:** `RecipeParser` (`IanDBallard/RecipeParser`, HEAD `667f5ef`) and this one (HEAD `8573259`). The spec lives here because this repository holds the deferred ledger the findings were filed into, per commit `8a1c11e`.
**Citations:** every `file:line` was read at those two HEADs. Both working trees are clean apart from this repository's untracked `cayenne-app/` and `spike-sveltekit/` build leftovers, which are not cited.

## 1. Summary

A live import of an 827-recipe Paprika export on 2026-09-04 lost twenty recipes and every one of the 466 embedded photographs, reported no progress while it ran, and reported success at the end. Seven defects in the ingestion API produced that outcome. This design fixes all seven, carries one of them — a count of what was skipped — through to the web client, and repairs the damage already in the database.

The work splits into two units that ship separately: the code fixes, and the repair that runs against the deployed result.

## 2. Decisions log

Made with the user on 2026-09-05 during brainstorming.

| # | Decision | Choice |
|---|---|---|
| R1 | Repair strategy | Surgical backfill against the archive. Only the lost recipes are re-extracted; photos and amounts are patched in place. Category assignments, edits and ratings made since the import survive. |
| R2 | "No quantity" representation | `null` everywhere. `amount` becomes nullable in the extraction schema, the stored jsonb, and the client type. Every existing zero is migrated. No dual encoding survives in the data or the client. |
| R3 | Client scope | Skip reporting only — the count, the list of what went, and the banner that distinguishes a partial import from a clean one. Global import visibility and a cancel button stay deferred to phase 9b. |
| R4 | Repair execution | Split. The lost recipes go back in through the running, fixed API as a real authenticated import, which doubles as the end-to-end acceptance test. The photo backfill and the amount rewrite are one-off scripts and a migration. |
| R5 | Shipping units | Two. The fixes merge and deploy first; the repair runs against the deployed code. Not one long branch. |
| R6 | Naming what was dropped | A `skipped` jsonb column on the job row, written once at finalize, listing each dropped chunk. Additive: no new table, no new sync bucket, no new query. Identification is best-effort and the design says so where it cannot be. |
| R7 | How a partial import reads | Its own `warning` banner status, not a green one with different words. A job that imported nothing reads as an error. |

## 3. Goals and non-goals

**Goals.** Close the seven findings. Make a partial import legible instead of silently successful. Restore the twenty lost recipes and the missing photographs. Leave one representation of "the source gave no quantity" in the data.

**Non-goals.** Import progress visible outside `/import`; a cancel button; anything else on the deferred list. Restructuring the pipeline beyond what these fixes require. Re-extracting recipes that are already correct.

## 4. The seven fixes

### 4.1 A truncated reply is retried

The recorded finding says the extractor catches a parse error and returns nothing. The precise mechanism is narrower and matters for the fix: `_call_with_retry` already backs off on transport and quota errors, but `json.loads` and `RecipeList.model_validate` sit outside it at `recipeparser/gemini.py:303`, and the bare `except Exception` on the following line converts a well-formed HTTP response carrying truncated JSON into `None`. A retry never happens because the transport never failed.

The parse moves inside the retried unit. When every attempt fails, the function raises `ExtractionParseError` (new, in `recipeparser/exceptions.py`) instead of returning `None`, carrying the reply's `finish_reason` and its first line — the two facts that distinguish truncation from content the model genuinely could not parse. Both extraction entry points are affected: `extract_recipes` (`gemini.py:303`) and `extract_recipe_from_text` (`gemini.py:230`).

`recipeparser/core/stages/extract.py:64` stops treating `None` as an empty chunk. An empty chunk remains a legitimate, non-error outcome; a failed parse no longer masquerades as one.

### 4.2 Skipped chunks are counted

`RecipePipeline.run` (`recipeparser/core/pipeline.py:101`) excludes failed chunks from its results and logs them, so nothing downstream can tell a barren chunk from a lost one. It gains a callback in the style of the `on_progress` it already accepts:

```
on_skip: Optional[Callable[[Chunk, str], None]] = None
```

fired for every chunk that produces no result because of a failure — parse failures from 4.1 included, and every other per-chunk exception the pipeline already isolates. The reason string is the exception summary. The API tallies the calls.

**Naming the chunk.** A tally alone says twenty recipes were lost without saying which, so `Chunk` (`recipeparser/core/models.py:74-80`) gains `label: Optional[str] = None`. It is a defaulted dataclass field no stage reads, so no existing consumer changes. Readers fill it with the best identifier the source actually provides:

| Reader | Label |
|---|---|
| Paprika | the entry's `name`, which `paprika.py:119` already reads and currently folds into the chunk text and discards |
| EPUB | the chapter title |
| PDF | the page range |

For a book chunk the label names a region, not a recipe: what recipes a chunk contained is unknown until extraction succeeds, and extraction is what failed. The design does not pretend otherwise — the label is null when the source names nothing, and the chunk index in 5.1 carries the rest of the identification.

### 4.3 Writes are incremental

`submit_file_job` runs the entire pipeline and then writes once (`recipeparser/adapters/api.py:738-739`), so a long import shows an empty library for hours and a restart loses the run. `run` gains:

```
on_result: Optional[Callable[[IngestResponse], None]] = None
```

fired per assembled recipe. The API's callback writes that recipe through `write_recipe_to_supabase` (`recipeparser/io/writers/supabase.py:146`); the trailing `writer.write(results)` goes away. A write that fails is logged and added to the same tally the API keeps for 4.2 — the callback increments it directly, since the failure happens in the adapter and never reaches the pipeline's `on_skip`. One bad row must not discard the rest of an 827-recipe import.

`run` still returns its list, so `adapters/cli.py:136`, `adapters/gui.py` and the existing tests are unaffected: they pass neither callback and keep batch behaviour.

### 4.4 Progress moves

No new machinery is needed. `run` already accepts `on_progress(stage, completed, total)` and the API passes `None` at `api.py:738` and `api.py:644`. The API supplies a callback that writes `progress_pct = round(100 * completed / total)`, throttled to whole-percent changes so an 827-chunk import issues at most one hundred updates rather than 827.

`_finalize_ingestion_job` (`api.py:449`) keeps writing 100 on success. On failure it stops writing `progress_pct: 0` (`api.py:469`) and omits the field entirely, leaving whatever the last progress callback wrote: a job that died at 60% should not claim it never started.

### 4.5 Photographs are stored

`PaprikaReader` already pulls each entry's photo out of the archive and attaches the bytes (`recipeparser/io/readers/paprika.py:113`, `:128`, into `Chunk.image_bytes` at `recipeparser/core/models.py:78`). Nothing reads that field. The only upload helper, `_upload_image_to_storage` (`api.py:294`), takes a web address, downloads it, and is called from one place that Paprika entries never reach.

`core/` may not import `io/` or `adapters/`, so the capability arrives as a port. `recipeparser/core/ports.py` gains, beside the existing `CategorySource`:

```
class ImageStore(ABC):
    def put(self, image_bytes: bytes, recipe_id: str, content_type: str) -> Optional[str]
```

returning the public URL, or `None` on any failure — a missing photograph must never fail a recipe. `SupabaseImageStore` implements it in `recipeparser/io/writers/`, holding the bucket logic currently inline at `api.py:316-325`. `RecipePipeline` takes one in its constructor and, when a chunk carries `image_bytes` and no `image_url`, uploads before ASSEMBLE and passes the returned URL where `chunk.image_url` is read today (`pipeline.py:241`, `:264`, `:316`).

`_upload_image_to_storage` becomes a thin URL-to-bytes wrapper over the same store, leaving one upload path in the codebase instead of one live and one dead.

### 4.6 Job endpoints are authenticated and owned

`GET /jobs/{job_id}` (`api.py:766`) and the pause, resume and cancel endpoints (`api.py:786`, `:799`, `:812`) take a job id and nothing else, while the three ingestion endpoints verify a bearer token. Anyone who can reach the API can cancel someone else's import.

All four gain `Depends(_verify_supabase_jwt)`. The registry at `api.py:532` becomes:

```
_active_jobs: Dict[str, tuple[str, PipelineController]]   # user_id, controller
```

and a caller who does not own the job receives 404, not 403, so the registry cannot be probed for which job ids exist. The two writers of the registry (`api.py:594`, `:690`) and its `pop` sites change with it.

### 4.7 One name for the service key

`SUPABASE_SERVICE_ROLE_KEY` is canonical: it is what `adapters/api.py` already reads in four places. `recipeparser/io/category_sources/supabase_source.py:63` and `cleanup_jobs.py:7` read `SUPABASE_SERVICE_KEY` instead, so setting only one name disables a subset of writes with a warning rather than an error. Both move to the canonical name, along with `.env` and the documentation in `supabase_source.py:25-30`.

A startup check in `api.py` refuses to boot when `SUPABASE_SERVICE_ROLE_KEY` is unset but the legacy name is present, rather than degrading silently.

### 4.8 An unquantified ingredient is null

`recipeparser/models.py:99` declares `amount: float = Field(description="Numeric quantity, e.g., 1.5. 0 if none.")`, so a client cannot distinguish an absent quantity from a real zero. It becomes `Optional[float]`, described as null when the source states no amount, and the extraction prompts in `gemini.py` are updated to say so. The writer passes the value through unchanged (`io/writers/supabase.py:187`).

## 5. Cross-repo data shape

### 5.1 What was skipped, and what the client says about it

`supabase/migrations/009_ingestion_jobs_skipped.sql` adds two columns to `ingestion_jobs`:

- `skipped_count int not null default 0` — the true number of dropped chunks.
- `skipped jsonb not null default '[]'` — a list of `{ label, index, reason }`, one per dropped chunk, where `label` is `Chunk.label` from 4.2 and may be null.

The list rides the row that already syncs. There is no new table, no new sync bucket, no new query, and no change to the twenty-row window in `recipeDb.ts:52`; jsonb travelling as text is the established pattern here, as `recipes.structured_ingredients` does at `powersync.ts:28`.

Two constraints on the column:

- **Written once, at finalize.** The row re-syncs on every stage change and every percent tick from 4.4, so a list that grows during the run multiplies that traffic across an 827-chunk import to no purpose: nothing can act on it until the job ends.
- **Capped at fifty entries.** `skipped_count` still reports the true total, so a pathologically bad job cannot inflate a synced row while still being counted honestly.

The columns then travel:

| Where | Change |
|---|---|
| `powersync/sync-rules.yaml:51` | both join the select list for `ingestion_jobs` |
| `cayenne-web/src/lib/services/powersync.ts:41-42` | `skipped_count: column.integer`, `skipped: column.text` |
| `cayenne-web/src/lib/domain/recipe.ts:105-106` | `skipped_count: number`, and a `SkippedChunk[]` parsed from the text column |
| `cayenne-web/src/lib/services/recipeDb.ts:52`, `:182-183` | both statements list both columns |
| `cayenne-web/src/lib/ui/Banner.svelte:7` | `Status` gains `warning`, with its own classes and icon in `STYLE` |
| `cayenne-web/src/lib/features/import/JobProgress.svelte:9` | the terminal branch below |

**The terminal banner** stops having exactly two outcomes. Given a `done` job:

| Condition | Status | Message |
|---|---|---|
| `skipped_count === 0` | `success` | `N recipes added`, or `Recipe added!` when `recipe_count` is 1 |
| `recipe_count === 0` | `error` | `Nothing could be imported`, with the reasons below |
| otherwise | `warning` | `812 of 827 imported`, denominator `recipe_count + skipped_count` |

The zero case is called out because the present expression tests `recipe_count > 1` (`JobProgress.svelte:9`), so a job that imported nothing at all falls through to the singular `Recipe added!`. A partial import likewise must not render green with different words — the whole defect being fixed is a lossy import that reads as a clean one, and the wording is the smaller half of that.

In the `warning` and `error` cases the banner lists the `skipped` entries behind a disclosure: the label where there is one, the chunk index where there is not, and the reason. That is what makes "812 of 827" actionable — the named entries can be re-imported.

### 5.2 Nullable amounts

`supabase/migrations/010_ingredient_amount_null.sql` rewrites `recipes.structured_ingredients`, replacing every element whose `amount` casts to numeric zero with the same element carrying `null`. It is idempotent — a second run matches nothing — and it touches only rows that contain such an element.

The client follows, and per D13 keeps no compatibility branch:

- `recipe.ts:13` becomes `amount: number | null`, its comment restated; `recipe.ts:120` becomes `scaled_amount: number | null`.
- `culinaryMath.ts` short-circuits: an ingredient with a null `amount` yields `scaled_amount: null`, `scaled_converted_amount: null`, `approximate: false`, skipping the conversion branches entirely (`culinaryMath.ts:40-68`).
- `IngredientRow.svelte:17` tests `=== null` in place of `=== 0`, and its explanatory comment (`:10-15`) is rewritten: the reasoning about scaling never manufacturing a zero no longer applies, because absence is now stated rather than inferred.
- `formatQuantity.ts:37` is untouched. It still renders a real zero as "0"; whether to show anything remains the caller's decision.

## 6. The repair

### 6.1 Safety

All three scripts live in a new `scripts/` directory in the `RecipeParser` repository — neither repository has one today, and they import `PaprikaReader` and `SupabaseImageStore`, so that is where they belong. Each takes the archive path and the target user id as arguments rather than inferring either.

Every step defaults to a dry run and requires an explicit `--live` flag. Before the first write, `scripts/snapshot_recipes.py` dumps that user's `recipes` rows to a timestamped JSON file outside both repositories. These operations are irreversible and there is one database; the test suite has written phantom rows into this project before (fixed in `667f5ef`), so the guard belongs in the scripts rather than in the operator's care.

### 6.2 Identifying what was lost

`scripts/diff_archive.py` normalises titles on both sides — case, whitespace, punctuation — and matches the 827 archive entries against the user's recipe rows. Its output is three lists: entries with exactly one match, entries with no match (the lost recipes), and entries whose title is ambiguous, either duplicated in the archive or matching more than one row. The third list is **reported, never guessed at**, and the user approves the second list before it becomes an import.

### 6.3 Order of operations

The migrations go first. `_create_ingestion_job`'s INSERT and `JobSink.finalize_payload` both name the `skipped_count` and `skipped` columns that migration 009 creates, and PostgREST rejects an INSERT that names a column which does not yet exist. That INSERT's failure is deliberately swallowed (the job still runs even without a row), so deploying the fixes before 009 is applied does not merely leave a stale terminal state — it gives every import **no `ingestion_jobs` row at all**, and the client, which only ever polls that row, shows nothing, forever, for every job.

1. Apply migrations 009 and 010.
2. Merge and deploy the fixes (unit one).
3. Snapshot the recipes.
4. Run the diff; the user approves the missing list.
5. Build a trimmed `.paprikarecipes` containing only the approved entries and submit it to the running API as a real authenticated `POST /jobs/file`. This is the acceptance test: it exercises the retry, the photo upload, incremental writes, `progress_pct` and `skipped_count` on the shipped path.
6. Run `scripts/backfill_photos.py`, which walks the archive, matches each photo-bearing entry to its row, uploads the bytes through `SupabaseImageStore`, and patches `image_url` **only where it is currently null** — so the recipes restored in step 5, which arrive with their photographs attached, are left alone.
7. Verify: no recipe row from the archive is missing; no archive entry with a photograph has a null `image_url`; no `structured_ingredients` element has a zero amount.

Steps 5 and 6 are re-runnable. Step 6 is idempotent by its null filter; step 5 would duplicate rows if repeated, so it re-runs only after the duplicates are removed.

## 7. Error handling

A failed photograph upload logs and leaves `image_url` null; the recipe is still written. A failed single-recipe write in 4.3 logs and counts as a skip. A chunk that fails every extraction attempt counts as a skip and the job continues. A job that ends with skips still reports `status: done` — it did finish — but carries a non-zero `skipped_count` and the list naming what went, which is the whole point: the client says what was lost rather than the server pretending nothing was. A job whose every chunk was dropped is still `done` on the server, because the server did complete; it is the client that renders it as an error, per 5.1, since from the user's side nothing arrived.

The stage callback keeps its fail-loud behaviour (`api.py:521-525`); nothing in this design relaxes it.

## 8. Testing

Test-driven, one test before each fix.

- A Gemini reply of truncated JSON is retried, and after the final attempt raises `ExtractionParseError` carrying the finish reason — the regression test for the twenty lost recipes.
- A chunk that fails extraction fires `on_skip` once and appears in no result.
- The Paprika reader sets `Chunk.label` from the entry name, and a dropped Paprika chunk reaches the job row named — the test that proves the twenty lost recipes would now be identifiable.
- The skipped list is written once, at finalize, and not by the progress or stage callbacks.
- A run with more than fifty drops stores fifty entries and a truthful `skipped_count`.
- `on_result` fires per recipe, before the run completes, and a callback that raises does not abort the run.
- `on_progress` drives whole-percent updates and does not write the same percentage twice.
- A chunk carrying `image_bytes` is uploaded through a fake `ImageStore` and its URL reaches the assembled recipe; a store returning `None` still yields a recipe.
- Each of the four job endpoints rejects an absent token, and rejects a token whose subject does not own the job, with 404.
- The app refuses to boot with only the legacy key name set.
- An extraction with no stated amount round-trips as null through the writer payload.
- Client-side: `culinaryMath` propagates null through scaling; `quantityText` prints the bare name for a null amount and "0" is never special-cased again.
- `JobProgress` renders all three terminal branches of 5.1 — success, warning, and the zero-recipe error — and lists the skipped entries in the latter two. A job with `recipe_count` 0 never renders "Recipe added!".

The repair scripts get tests for the title normaliser and the ambiguity report, run against the archive rather than the database.

## 9. Units of work

**Unit one — the fixes.** One branch in `RecipeParser` for sections 4.1 to 4.8, plus one branch here for section 5. Both migrations live on the Cayenne branch, but 009 must be **applied to the Supabase project** before the client change reaches a device: PowerSync cannot sync a column the database does not have.

**Unit two — the repair.** The scripts of section 6 and the operation itself, run against the deployed result of unit one, with the user's approval at step 4.

## 10. The ledger and the issues

Last, when each fix has a shape worth describing: seven issues on `IanDBallard/RecipeParser`, one per finding of section 4, each linked from its bullet under "The ingestion API" in `SpecificationDocumentation/EXECUTION_PLAN.md`, and each closed as its change merges. The cut-over checklist item for RecipeParser CORS and bearer verification is ticked in the same pass — `667f5ef` closed it and the box was never marked.

The two client-side import items — visibility outside `/import`, and cancellation — stay on the deferred list and are noted as inputs to phase 9b.

## 11. Risks

- **Title matching is the repair's weak joint.** A recipe renamed in the app since the import reads as missing and would be imported a second time. The ambiguity report of 6.2 and the user's approval at step 4 are the mitigation; there is no automatic resolution.
- **A dropped book chunk cannot be named.** For Paprika the label is a real recipe title, which is what this import needed. For PDF and EPUB the label names a chapter or a page range, because what a chunk contained is only known once extraction succeeds. The list stays honest about that — a null label and an index — rather than guessing from the chunk text, but a user importing a book still learns only roughly where the loss was.
- **Deployment is undecided.** The repair needs the fixed API running somewhere reachable with a real token. Locally is sufficient and is what step 5 assumes; hosting (D9) remains open and is not settled here.
- **Incremental writes change what a cancelled job leaves behind.** Before this change a cancelled import left nothing; after it, it leaves the recipes written so far. That is the intended behaviour — it is what makes a long import usable — but it is a behaviour change, and cancellation has no client surface yet to explain it.
