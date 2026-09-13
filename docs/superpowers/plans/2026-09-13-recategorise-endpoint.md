# Recategorise Endpoint and Worker Implementation Plan (Stage 7C)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status: executed 2026-09-13**, on `claude/recategorise-endpoint`. Suite 1109 passed, from a 1071 baseline. What landed differently is under *Rulings made during execution* at the end. **Gate 7C is partly unmet on merge** — see ruling 5.

**Goal:** Make the bulk recategorise reachable and make it survive its first real run. The capability has been schema-complete and worker-complete since RecipeParser#26 and has never been callable: nothing queued a job, because the path the philosophy spec chose could not work. Stage 7B fixed the schema and the specification; this stage builds the endpoint that queues the job, the cancel that stops it, and the four worker defects that bite the first time a real job runs.

**Architecture:** Two new endpoints on the existing FastAPI app, both following the shape `POST /jobs` already uses — bearer token, caller from `sub`, the service-role client for the write. `POST /jobs/recategorize` does the ownership check and the subtree expansion **on the server**, which is the whole reason D2 chose an endpoint over a PowerSync insert. `POST /jobs/{id}/cancel` gains a second path: the in-memory controller for `kind = 'ingest'`, a row update for `kind = 'recategorize'`. Inside `RecatWorker` the changes are surgical — a lease in the poll, a retry-and-record around the batch, a finish on the cancel path, and an error when no id resolves. One new pure module holds the root walk that `resolve_new_axes` and `SupabaseCategorySource._build_axes` must share.

**Tech Stack:** Python 3.11/3.12, FastAPI, supabase-py, pytest.

**Spec:** `docs/superpowers/specs/2026-09-07-recipe-edit-philosophy-design.md` §6.1–6.3 **as amended by stage 7B** (RecipeParser#45) — read those sections, not the pre-amendment text. The stage design is Cayenne's `docs/superpowers/specs/2026-09-12-category-delete-and-recategorise-design.md`, Part 3 (*The endpoint*, *Cancel*) and Part 4 (the worker fixes), plus D4 and D6. Stitching: Cayenne `SpecificationDocumentation/ROADMAP.md` Stage 7, row 7C.

## What the investigation found before Task 1

Read from the code, not inherited from the design:

- **`resolve_new_axes` and `_build_axes` disagree about what an axis is.** `_build_axes` (`io/category_sources/supabase_source.py:215-243`) walks from each **root** and flattens every descendant into that root's axis. `resolve_new_axes` (`adapters/recat_worker.py:50-51`) takes `by_id[row["parent_id"]]` — the **immediate parent**. On a three-deep tree `Cuisine > Asian > Thai`, ingest tells the model Thai belongs to axis *Cuisine* and recategorise tells it Thai belongs to axis *Asian*. Same taxonomy, two descriptions, and nothing has ever tested the deeper case.
- **The cancel path does not finish the job.** `recat_worker.py:150-152` logs and `return`s. Whoever set `status = 'cancelled'` set only that: `stage` stays `CATEGORIZING` and `progress_pct` stays mid-flight, so a cancelled job is indistinguishable from one whose worker died.
- **A failed batch is counted and forgotten.** `:136-138` increments `failed` and logs a warning. The recipe ids in that batch are never recorded, the cursor advances past them, and `skipped`/`skipped_count` — columns that exist and that the ingest path already writes — stay empty. The client can show that nothing was skipped while dozens of recipes were never examined.
- **No resolved ids is reported as success.** `:106-108` finishes `done` with `recipe_count = 0` when `offered` is empty, which is what a job naming only deleted categories does. It reads as "checked everything, nothing matched".
- **The poll claims only `pending`.** `:78` filters `.eq("status", "pending")`, so a job left `running` by a restart is never reclaimed, even though the worker writes `updated_at` after every batch and `params.cursor` is exactly the resume point.
- **`ingestion_jobs.kind` and `.params` default correctly.** Cayenne's migration 013 gives `kind text not null default 'ingest'` and `params jsonb not null default '{}'`, so the existing ingest insert (`api.py:613-625`), which names neither, keeps working untouched.
- **`status = 'cancelled'` is storable as of 2026-09-13**, by Cayenne's `20260913125453_ingestion_jobs_cancelled_status.sql` (merged in #85, deployed). Before it the CHECK admitted four values, so the cancel this stage builds would have been refused by the database.

## Global Constraints

- **Branch:** `claude/recategorise-endpoint`, cut from `master` at `9357638` (after the 7B twin). One RecipeParser pull request. **No Cayenne change**: the client action is 7D.
- **7B's migration must be live before this deploys.** It is: merged as Cayenne `9bfeb01` and the post-merge `sync-rules` deploy ran. The worker must never write a status the check refuses, which is why 7B went first.
- **No RLS change, and no client write.** The endpoint holds the service role and does the ownership check itself. If any part of this stage needs a policy on `ingestion_jobs`, the design has been misread.
- **Additive only (D4).** A job adds category links and removes none. The upsert stays `on_conflict="recipe_id,category_id"` with duplicates ignored.
- **Failures inside a batch must not fail the job**, and must not be silent either. That balance is Part 4 item 2 and is the point of the `skipped` list.
- **Ownership and subtree expansion happen on the server**, never from ids the client supplies unchecked. A 422 names the count of unknown ids without echoing them.
- **Gate before pushing:** `.venv/bin/python -m pytest tests/ -q` (baseline **1071 passed**, 2026-09-13), and no new lint finding in a touched file.
- **What this session cannot do:** redeploy the container, or run a job by `curl` against the live API. Gate 7C names both. They are owed after the merge and the plan says so rather than implying otherwise.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `recipeparser/core/taxonomy.py` | `root_of` / `axes_from_rows`: the one root walk | **Create** |
| `recipeparser/io/category_sources/supabase_source.py` | `_build_axes` delegates to it; `load_category_ids` raises on a duplicate name (D6) | Modify |
| `recipeparser/adapters/recat_worker.py` | `resolve_new_axes` uses the shared walk and raises on a duplicate name; the lease; retry-and-record; cancel finishes; missing ids error | Modify |
| `recipeparser/adapters/api.py` | `POST /jobs/recategorize`; cancel branches on `kind` | Modify |
| `recipeparser/gemini.py` | `build_categorize_batch_prompt` reads body columns through `raw_body_column` | Modify |
| `tests/unit/test_recat_worker.py` | The four fixes, the three-deep axis, the duplicate name | Modify |
| `tests/unit/test_taxonomy.py` | The shared walk, three-deep | **Create** |
| `tests/unit/test_category_sources.py` (or nearest) | `_build_axes`'s first direct test, on the same fixture | Modify / Create |
| `tests/unit/test_api_*.py` (nearest job-endpoint suite) | The endpoint: 202, 422, ownership, expansion; cancel by kind | Modify |
| `SpecificationDocumentation/INGESTION_API.md` (**Cayenne**) | Only if it documents the job endpoints — the ownership rule, checked not assumed | Modify if needed |

---

## Task 1: One root walk, shared

**Produces:** `recipeparser/core/taxonomy.py`, and both callers using it.

**Steps:**
- [x] Create the module with two pure functions over a flat list of `{id, name, parent_id}` rows: `root_of(category_id, rows) -> str | None` (walk `parent_id` to the top, cycle-safe with a visited set) and `axes_from_rows(rows) -> Dict[str, List[str]]` (the existing `_build_axes` behaviour: every descendant flattened into its root's axis, a childless root becoming a single-tag axis of its own name).
- [x] Say in the docstring why it exists: ingest and recategorise described the same taxonomy differently until 2026-09-13, and a prompt that disagrees with itself between the two paths misfiles recipes in a way no test caught.
- [x] `SupabaseCategorySource._build_axes` becomes a thin delegation. Its behaviour must not change — the sorted tag list, the childless-root case and the empty-name skip all stay.
- [x] `resolve_new_axes` uses `root_of` for each requested id instead of reading `parent_id` once, so a level-3 tag is offered under its root axis exactly as ingest offers it.

**Definition of done:**
- [x] `tests/unit/test_taxonomy.py`: `root_of` on a three-deep tree returns the root from every depth; on a root returns itself; on an unknown id returns None; on a cycle terminates rather than hanging.
- [x] `_build_axes` gets its **first direct unit test**, on the three-deep fixture, asserting the flattening is unchanged.
- [x] `test_resolve_new_axes` is extended: on `Cuisine > Asian > Thai`, requesting Thai offers it under **Cuisine**, not Asian — the assertion that would have failed before this task.

---

## Task 2: Duplicate names raise (D6)

**Produces:** `load_category_ids` and `resolve_new_axes` refusing a taxonomy with two categories of the same name.

Today `load_category_ids` builds its map last-write-wins, and `resolve_new_axes` does the same with `ids[name] = cid`. The unique `(user_id, name)` index makes duplicates unrepresentable **today**, which is why this has never bitten; D6 keeps that rule and has the parser assert it rather than silently pick a winner, so the day names are scoped per axis this fails loudly instead of misfiling.

**Steps:**
- [x] `load_category_ids` raises `ValueError` naming the duplicated name when two rows share a name (after strip).
- [x] `resolve_new_axes` does the same for the ids it is given.
- [x] Both messages name the category, not just the count: it is the one thing the operator needs.

**Definition of done:**
- [x] A test per function: two rows named "Quick" raise, and the message contains `Quick`; the single-name case is unchanged.

---

## Task 3: The worker survives its first real run

**Produces:** the four Part 4 fixes in `RecatWorker`.

**Steps:**
- [x] **A lease.** `run_once` also claims a `recategorize` job whose `status = 'running'` and whose `updated_at` is older than ten minutes, and resumes it from `params.cursor`. The claim stays a compare-and-swap on the row so two workers cannot both take it. Name the constant (`STALE_LEASE_MINUTES = 10`) and say in a comment that the worker writes `updated_at` after every batch, which is what makes the age meaningful.
- [x] **Retry once, then record, then pass.** Around the batch: on the first exception retry the same batch once; on the second failure append that batch's recipe ids to the row's `skipped` with the exception text as the reason, raise `skipped_count` by the batch size, and only then advance the cursor. The 10% threshold stays and counts the recorded batches.
- [x] **Cancelled reaches the finish path.** Replace the bare `return` with `_finish(job_id, "cancelled", ...)` carrying the last `progress_pct`, `recipe_count` and `skipped` intact. `_finish` maps `cancelled` to `stage = 'DONE'`, not `'ERROR'` — a job a cook stopped did not fail, and 7D's notice reads "Stopped. N recipes tagged so far" off exactly these fields.
- [x] **Missing ids error the job.** When **no** requested id resolves, finish `error` with `error_message = 'The categories no longer exist'`. When **some** resolve, proceed on those and do not offer the missing names.

**Definition of done:**
- [x] One test per fix: a stale `running` job is claimed and resumes from its cursor while a fresh `running` one is not; a batch that fails twice is recorded in `skipped` with its recipe ids and the cursor still advances, and one that fails once then succeeds is not recorded; a cancelled job ends `status='cancelled'`, `stage='DONE'` with its counts; a job naming only unknown ids ends `error` with that message, and one naming a mix proceeds on the known ids.
- [x] The existing `test_too_many_failed_batches_errors_the_job` and `test_cancel_between_batches` still pass, updated where the new behaviour changes what they should assert.

---

## Task 4: `POST /jobs/recategorize`

**Produces:** the endpoint that makes the capability reachable.

**Steps:**
- [x] A `RecategorizeRequest` model: `category_ids: list[str]`.
- [x] The handler, on `POST /jobs`'s shape: resolve the caller from the bearer token (`_verify_supabase_jwt`), refuse a blank subject.
- [x] Select `id, name, parent_id` from `categories` for that user with the service-role client. **Reject with 422 any id not in the result**, naming how many were unknown and not echoing them back.
- [x] Expand each requested id to itself plus its descendants (D4), de-duplicated, using the Task 1 module so the expansion matches the axes the worker will build.
- [x] Insert the row with the service role: `kind='recategorize'`, `status='pending'`, `stage='IDLE'`, `params={"category_ids": [...]}`, `source_hint` the requested names joined with `, ` and cut at 80 characters, counters at zero. Follow the existing insert's column list so `skipped`/`skipped_count` are initialised as the ingest path initialises them.
- [x] Return 202 `{"job_id": ...}` in the `AsyncJobResponse` shape.
- [x] Refuse an empty `category_ids` with 422 rather than queueing a job that finishes instantly.

**Definition of done:**
- [x] Tests: 202 and a row with the expected `kind`, `status`, `params` and `source_hint`; 422 for an id the caller does not own **and** for one that does not exist, without the ids in the message; the subtree expansion (requesting a folder queues the folder plus its descendants); 401 without a token; the empty list refused.
- [x] No test asserts on live Supabase: the service client is faked as the existing endpoint tests fake it.

---

## Task 5: Cancel branches on kind

**Produces:** `POST /jobs/{id}/cancel` working for a recategorise job.

**Steps:**
- [x] Before the in-memory lookup, read the job row by id **and owner** with the service client. When it is `kind = 'recategorize'`: write `status='cancelled'` if the row is `pending` or `running` and answer 200 with the row's status; answer **409** for a terminal row.
- [x] When it is not a recategorise job, fall through to the existing `_owned_controller` path unchanged — the ingest cancel keeps its in-memory controller exactly as it is.
- [x] A row that is not the caller's answers 404, not 403, matching `_owned_controller`'s existing reasoning about enumerability. Say so in a comment.

**Definition of done:**
- [x] Tests: cancelling a `running` recategorise job writes `cancelled` and answers 200; a `done` one answers 409; another user's answers 404; an ingest job still takes the controller path and is unaffected.

---

## Task 6: The batch prompt cannot be fed a double-encoded column

**Produces:** `build_categorize_batch_prompt` reading through `core.regen.raw_body_column`.

It currently does `r.get("ingredient_lines", [])` directly. A double-encoded jsonb column arrives as a `str`, and iterating a `str` yields characters — the prompt would carry forty one-character ingredients and the model would classify nonsense. `raw_body_column` exists for exactly this and documents that all 786 rows in the live library once carried this encoding.

**Steps:**
- [x] Read both body columns through `raw_body_column`, so a bad column raises rather than silently degrading the prompt.
- [x] Note in the docstring that the raise is the point: a recategorise batch that cannot be described is a batch to record in `skipped`, which Task 3 now does.

**Definition of done:**
- [x] A test: a recipe whose `ingredient_lines` is a JSON string raises rather than producing a character-per-ingredient prompt.
- [x] The existing prompt snapshot tests still pass.

---

## Task 7: The gate and the record

**Steps:**
- [x] Run `.venv/bin/python -m pytest tests/ -q` and record the number against the 1071 baseline.
- [x] Check whether Cayenne's `SpecificationDocumentation/INGESTION_API.md` documents the job endpoints; if it does, the ownership rule says amend it to find `POST /jobs/recategorize` and the cancel branch done. **That is a Cayenne change and therefore a second, separate pull request** — do not smuggle it into this one.
- [x] Open the pull request, and say plainly that Gate 7C is **partly unmet on merge**: the container redeploy and the live `curl` job are owed and cannot be done from a session.
- [x] Add *Rulings made during execution* to this file.

**Definition of done:**
- [x] The suite green, or each failure named with its cause.
- [x] The pull request names exactly what of the gate is owed and who can do it.

---

## Self-review

- **Did any part of this add an RLS policy, or have the client write `ingestion_jobs`?** It must not; D2 exists to avoid both.
- **Does the endpoint trust any id the client sent?** Every id must be checked against the caller's own rows before expansion.
- **Is a cancelled job distinguishable from a failed one** in `status`, `stage` and the counts?
- **Does a twice-failed batch leave its recipe ids somewhere a reader can see?** If `skipped` is still empty, the fix is not done.
- **Do ingest and recategorise now describe the same taxonomy?** The three-deep test is the proof; without it this is a claim.
- **Is there any Cayenne change in this diff?** There must not be — the client action is 7D, and the API document is its own pull request.

---

## Rulings made during execution (2026-09-13)

1. **The investigation section is the finding.** `resolve_new_axes` reading the immediate parent while `_build_axes` walked to the root is not a style difference: on `Cuisine > Asian > Thai` the two paths offered Thai under two different axes, and every existing test used a two-level fixture where the answers coincide. The fix was verified as a real regression test — with the old parent-based line restored, `test_resolve_new_axes_uses_the_root_not_the_immediate_parent` and `test_resolve_new_axes_and_build_axes_agree_on_a_three_deep_tree` both fail; restored, both pass.

2. **`core/taxonomy.py` gained a third function the plan did not name.** `descendants_of` is D4's subtree expansion. It was going to live in the endpoint, but putting it beside the axis walk is what guarantees the job's `category_ids` and the worker's offered axes are computed from the same tree — the drift this whole stage exists to remove.

3. **The old cancel test asserted nothing.** `test_cancel_between_batches` checked only that `"done"` never appeared in the written statuses, which was true before the fix too, because the worker returned without writing any terminal state at all. It is rewritten to assert what matters: `status='cancelled'`, `stage='DONE'`, and the counts intact. A test that passes under both the bug and the fix is not a test.

4. **The endpoint's insert raises where the ingest insert does not.** `_create_ingestion_job` deliberately swallows its failure, because the pipeline is already running and a missing row is bad UX rather than lost work. Here the row *is* the work: with no row, nothing ever happens, so a failed insert answers 503 rather than 202.

5. **Gate 7C is partly unmet on merge, and cannot be otherwise from a session.** Its three clauses are: merged, the container redeployed, and a job queued by `curl` against the live API finishing `done`. Only the first is in reach here — a redeploy and a live call against production need credentials and a deploy this session does not have. The pull request says so rather than implying a green suite covers it.

6. **One Cayenne change is owed and deliberately not in this pull request.** If `SpecificationDocumentation/INGESTION_API.md` documents the job endpoints, the ownership rule says it should record `POST /jobs/recategorize` and the cancel branch. That is a different repository, so it is its own pull request; smuggling it in here would make a cross-repo pair out of a single-repo stage.

7. **A fake that does not honour the user fence proves nothing.** The first version of `test_an_id_the_caller_does_not_own_is_refused` failed, and the fault was the double: it returned the owner's categories to every caller, so it could not tell a real ownership check from a missing one. The fake now honours `.eq("user_id", …)`, which is what makes that test meaningful.
