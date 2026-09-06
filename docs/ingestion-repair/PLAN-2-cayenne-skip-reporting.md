# Cayenne Skip Reporting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a lossy import legible in the app — the count and names of what was dropped, a banner that does not congratulate the user on a partial result — and finish the nullable-quantity change end to end.

**Architecture:** Two Supabase migrations carry the new shape; the sync rules, the PowerSync schema, the row type and the query all list the new columns; one new `Banner` status and a rewritten terminal branch in `JobProgress` present them. Separately, `amount` becomes nullable through the domain layer, which removes the display hack that stood in for the missing representation.

**Tech Stack:** SvelteKit 2 with Svelte 5 runes, TypeScript, Tailwind 4, PowerSync (web SDK, OPFS), Supabase, Vitest with `@testing-library/svelte`, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-05-ingestion-repair-design.md`. Read section 5 before starting. This plan implements spec section 5 in full.

**Repository:** this one (`IanDBallard/Cayenne`), branch off `main` at `8573259`. All paths below are relative to the repository root; the app lives in `cayenne-web/`.

## Global Constraints

- **D13, virgin release:** no fallback, compatibility or legacy-handling code in releasable code. When migration 010 has run there are no zero amounts left, so the client reads `null` and nothing else. Do not write a branch that accepts both.
- Spec section 20 governs UI: every colour is a token in `cayenne-web/src/app.css`, and `cayenne-web/tests/unit/tokens.test.ts` is the canonical token list that guards against drift. A new colour that is not in that test is a spec violation, not a shortcut.
- Migrations are numbered SQL files under `supabase/migrations/`, applied by hand in the Supabase SQL editor as one transaction. Follow the header format of `008_profiles_and_constraints.sql`.
- Run `npm run test:unit` from `cayenne-web/` before each commit; run `npm run lint` and `npm run check` before the PR.
- **Merge-order note:** the phase 9a UI foundations plan (`docs/superpowers/plans/2026-09-05-cayenne-web-phase-9a-ui-foundations.md`) also edits `tokens.test.ts`. Whichever lands second rebases; the token list is additive, so the conflict is mechanical.

---

### Task 1: The migrations

**Files:**
- Create: `supabase/migrations/009_ingestion_jobs_skipped.sql`
- Create: `supabase/migrations/010_ingredient_amount_null.sql`

**Interfaces:**
- Consumes: nothing.
- Produces: `ingestion_jobs.skipped_count int not null default 0` and `ingestion_jobs.skipped jsonb not null default '[]'::jsonb`; every `recipes.structured_ingredients` element with a zero `amount` rewritten to `null`.

**Why 010 is a migration and not a script.** It is a one-time data rewrite that must be auditable, idempotent and run in the same place as every other schema change. A zero quantity is meaningless in a recipe, so no information is lost by the rewrite; the finding recorded on 2026-09-04 was precisely that a client cannot tell "to taste" from a real zero.

- [ ] **Step 1: Write migration 009**

Create `supabase/migrations/009_ingestion_jobs_skipped.sql`:

```sql
-- ============================================================
-- Project Cayenne — Migration 009
-- 009_ingestion_jobs_skipped.sql
--
-- Applies to: Supabase (PostgreSQL). Run in the SQL editor as one transaction.
-- Spec: docs/superpowers/specs/2026-09-05-ingestion-repair-design.md section 5.1
--
-- A job that dropped chunks reported plain success: the 2026-09-04 import of an
-- 827-recipe Paprika export lost twenty recipes and told the client nothing.
-- skipped_count is the true number dropped; skipped names them, capped at fifty
-- entries by the writer so a pathological job cannot inflate a row every client
-- re-downloads on each stage change.
-- ============================================================

begin;

alter table public.ingestion_jobs
  add column if not exists skipped_count integer not null default 0,
  add column if not exists skipped jsonb not null default '[]'::jsonb;

comment on column public.ingestion_jobs.skipped_count is
  'Chunks that produced no recipe because something failed. The true total, uncapped.';
comment on column public.ingestion_jobs.skipped is
  'Up to fifty {label, index, reason} objects naming the dropped chunks. label is null when the source names nothing (a PDF or EPUB region).';

commit;
```

- [ ] **Step 2: Write migration 010**

Create `supabase/migrations/010_ingredient_amount_null.sql`:

```sql
-- ============================================================
-- Project Cayenne — Migration 010
-- 010_ingredient_amount_null.sql
--
-- Applies to: Supabase (PostgreSQL). Run in the SQL editor as one transaction.
-- Spec: docs/superpowers/specs/2026-09-05-ingestion-repair-design.md section 5.2
--
-- The ingestion API had no way to say an ingredient came without a quantity, so
-- "salt to taste" was stored as an amount of zero and no client could tell that
-- from a real measurement. A zero quantity is meaningless in a recipe, so every
-- existing zero is an absent amount and is rewritten as null.
--
-- Idempotent: a second run matches no rows.
-- ============================================================

begin;

-- Census first, so the operator sees the scale before the rewrite commits.
do $$
declare
  affected integer;
begin
  select count(*) into affected
  from public.recipes r
  where exists (
    select 1 from jsonb_array_elements(r.structured_ingredients) e
    where e->>'amount' is not null and (e->>'amount')::numeric = 0
  );
  raise notice 'Migration 010: % recipe row(s) contain a zero amount.', affected;
end $$;

update public.recipes r
set structured_ingredients = (
  select jsonb_agg(
    case
      when e->>'amount' is not null and (e->>'amount')::numeric = 0
        then jsonb_set(e, '{amount}', 'null'::jsonb)
      else e
    end
    order by ord
  )
  from jsonb_array_elements(r.structured_ingredients) with ordinality as t(e, ord)
)
where exists (
  select 1 from jsonb_array_elements(r.structured_ingredients) e
  where e->>'amount' is not null and (e->>'amount')::numeric = 0
);

commit;
```

The `with ordinality` and `order by ord` matter: `jsonb_agg` over `jsonb_array_elements` is not otherwise guaranteed to preserve ingredient order, and the order is load-bearing — `tokenized_directions` references ingredients by id, but the list is also read top to bottom by a cook.

- [ ] **Step 3: Verify the SQL parses before you trust it**

Paste each file into the Supabase SQL editor with `begin;` … `rollback;` substituted for the final `commit;`, and confirm both run clean and that 010's notice reports a plausible count. Then run them for real, 009 first.

- [ ] **Step 4: Commit**

```bash
git add supabase/migrations/009_ingestion_jobs_skipped.sql supabase/migrations/010_ingredient_amount_null.sql
git commit -m "feat(db): count and name what an import dropped; store no quantity as null

009 adds skipped_count and skipped to ingestion_jobs. A job that lost chunks
reported plain success - the 2026-09-04 import lost twenty recipes and the row
said nothing about it.

010 rewrites every zero amount in structured_ingredients to null. A zero
quantity is meaningless in a recipe, so each one is an absent amount that the
extraction schema had no way to express. It runs a census first and preserves
ingredient order explicitly, since jsonb_agg over jsonb_array_elements does not
guarantee it and the order is what a cook reads.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Carry the columns down to the device

**Files:**
- Modify: `powersync/sync-rules.yaml:49-53`
- Modify: `cayenne-web/src/lib/services/powersync.ts:35-47`
- Modify: `cayenne-web/src/lib/domain/recipe.ts:99-111`
- Modify: `cayenne-web/src/lib/services/recipeDb.ts:51-56`, `:178-188`
- Test: `cayenne-web/tests/unit/services/recipeDb.test.ts` (append; create if absent)

**Interfaces:**
- Consumes: migration 009 (Task 1), **applied to the project** — PowerSync cannot sync a column the database does not have.
- Produces:
  - `SkippedChunk = { label: string | null; index: number; reason: string }` exported from `$lib/domain/recipe`.
  - `IngestionJobRow` gains `skipped_count: number` and `skipped: string` (the raw jsonb text as SQLite stores it).
  - `parseSkipped(raw: string | null): SkippedChunk[]` exported from `$lib/domain/recipe` — total, never throws, returns `[]` for anything unparseable.

**Why the row keeps the raw text.** PowerSync stores jsonb columns as text (`recipes.structured_ingredients` already does, `powersync.ts:28`). Parsing at the row boundary would mean either a lying type or a parse in the query layer; a small total function at the point of use is simpler and testable.

- [ ] **Step 1: Write the failing test**

Append to `cayenne-web/tests/unit/services/recipeDb.test.ts`:

```typescript
import { parseSkipped } from '$lib/domain/recipe';

describe('parseSkipped', () => {
	it('parses the jsonb text the sync layer delivers', () => {
		const raw = '[{"label":"Sticky Toffee Pudding","index":3,"reason":"MAX_TOKENS"}]';
		expect(parseSkipped(raw)).toEqual([{ label: 'Sticky Toffee Pudding', index: 3, reason: 'MAX_TOKENS' }]);
	});

	it('returns nothing for null, empty, or malformed text rather than throwing', () => {
		expect(parseSkipped(null)).toEqual([]);
		expect(parseSkipped('')).toEqual([]);
		expect(parseSkipped('{not json')).toEqual([]);
		expect(parseSkipped('{"not":"an array"}')).toEqual([]);
	});

	it('keeps a null label, which is what a book chunk has', () => {
		expect(parseSkipped('[{"label":null,"index":7,"reason":"timed out"}]')).toEqual([
			{ label: null, index: 7, reason: 'timed out' }
		]);
	});
});

describe('the ingestion jobs query', () => {
	it('selects the skip columns', () => {
		const sql = recipeDb.queries.ingestionJobs().sql;
		expect(sql).toContain('skipped_count');
		expect(sql).toContain('skipped');
	});
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd cayenne-web && npx vitest run tests/unit/services/recipeDb.test.ts`
Expected: FAIL — `parseSkipped` is not exported.

- [ ] **Step 3: Extend the row type and add the parser**

In `cayenne-web/src/lib/domain/recipe.ts`, add above `IngestionJobRow`:

```typescript
/** One chunk an import dropped. `label` is null when the source names nothing — a PDF or EPUB region. */
export interface SkippedChunk {
	label: string | null;
	index: number;
	reason: string;
}

/**
 * Reads the `skipped` jsonb column, which sync delivers as text.
 * Total by construction: a job's reporting must never be what breaks the screen
 * that reports it.
 */
export function parseSkipped(raw: string | null): SkippedChunk[] {
	if (!raw) return [];
	try {
		const parsed: unknown = JSON.parse(raw);
		return Array.isArray(parsed) ? (parsed as SkippedChunk[]) : [];
	} catch {
		return [];
	}
}
```

and inside `IngestionJobRow`, after `recipe_count: number;`:

```typescript
	/** Chunks that produced no recipe because something failed. The true total. */
	skipped_count: number;
	/** Raw jsonb text; read it with parseSkipped. Capped at fifty entries by the API. */
	skipped: string | null;
```

- [ ] **Step 4: Extend the sync rules and the schema**

`powersync/sync-rules.yaml`, the `ingestion_jobs` select at `:51-52`:

```yaml
      - select id, user_id, status, stage, progress_pct, recipe_count,
               skipped_count, skipped, source_hint, error_message,
               created_at, updated_at
        from ingestion_jobs
        where user_id = bucket.user_id
```

`cayenne-web/src/lib/services/powersync.ts`, inside the `ingestion_jobs` table, after `recipe_count`:

```typescript
			skipped_count: column.integer,
			skipped: column.text,
```

- [ ] **Step 5: Extend the query and the seed helper**

`cayenne-web/src/lib/services/recipeDb.ts`, the `ingestionJobs` descriptor:

```typescript
		ingestionJobs: (): QueryDescriptor => ({
			sql: 'SELECT id, user_id, status, stage, progress_pct, recipe_count, skipped_count, skipped, source_hint, error_message, created_at, updated_at FROM ingestion_jobs ORDER BY created_at DESC LIMIT 20',
			params: [],
			tables: ['ingestion_jobs']
		}),
```

and `seedIngestionJobs`:

```typescript
				await tx.execute(
					'INSERT OR REPLACE INTO ingestion_jobs (id, user_id, status, stage, progress_pct, recipe_count, skipped_count, skipped, source_hint, error_message, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
					[j.id, j.user_id, j.status, j.stage, j.progress_pct, j.recipe_count, j.skipped_count, j.skipped, j.source_hint, j.error_message, j.created_at, j.updated_at]
				);
```

- [ ] **Step 6: Run the tests**

Run: `cd cayenne-web && npm run test:unit`
Expected: PASS. Any test constructing an `IngestionJobRow` literal now needs the two fields; add `skipped_count: 0, skipped: null` to those fixtures.

- [ ] **Step 7: Commit**

```bash
git add powersync/sync-rules.yaml cayenne-web/src/lib/domain/recipe.ts cayenne-web/src/lib/services/powersync.ts cayenne-web/src/lib/services/recipeDb.ts cayenne-web/tests/unit/services/recipeDb.test.ts
git commit -m "feat(web): sync the skip count and the list of what was dropped

The columns ride the ingestion_jobs row that already syncs, so there is no new
table, no new bucket and no change to the twenty-row window. jsonb arrives as
text, the way structured_ingredients already does, and parseSkipped reads it -
total by construction, because a job's reporting must not be what breaks the
screen that reports it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: A warning is not an error and not a success

**Files:**
- Modify: `cayenne-web/src/app.css` (two tokens)
- Modify: `cayenne-web/tests/unit/tokens.test.ts:15-30` (the canonical token list)
- Modify: `cayenne-web/src/lib/ui/Icon.svelte` (register `triangle-alert`)
- Modify: `cayenne-web/src/lib/ui/Banner.svelte:7`, `:27-32`
- Test: `cayenne-web/tests/unit/ui/Banner.test.ts` (append; create if absent)

**Interfaces:**
- Consumes: nothing.
- Produces: `Banner`'s `Status` union gains `'warning'`; `IconName` gains `'triangle-alert'`; tokens `--color-warning` and `--color-warning-bg`.

**Why a new status rather than reusing `info`.** A partial import is not neutral information — something was lost and the user may want to act. `info` is styled as `border-border bg-surface`, indistinguishable from the progress banner. Reusing it would put the whole weight of "fifteen recipes did not arrive" on the wording, which is the failure this work exists to fix.

- [ ] **Step 1: Write the failing test**

Append to `cayenne-web/tests/unit/ui/Banner.test.ts`:

```typescript
it('renders a warning distinctly from success and error', () => {
	const { unmount } = render(Banner, { props: { status: 'warning', message: '812 of 827 imported', testId: 'b' } });
	const warning = screen.getByTestId('b');
	expect(warning).toHaveTextContent('812 of 827 imported');
	expect(warning.className).toContain('warning');
	expect(screen.getByTestId('b-icon')).toBeTruthy();
	unmount();
});
```

And in `cayenne-web/tests/unit/tokens.test.ts`, add `'text-warning'` and `'bg-warning-bg'` to the candidate array in the first `buildCss([...])` call, and `'.text-warning'` to the assertion loop.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd cayenne-web && npx vitest run tests/unit/ui/Banner.test.ts tests/unit/tokens.test.ts`
Expected: FAIL — `warning` is not an allowed `Status`; the token classes compile to nothing.

- [ ] **Step 3: Add the tokens**

In `cayenne-web/src/app.css`, beside the existing `--color-success` / `--color-error-bg` pairs:

```css
	--color-warning: #B45309;
	--color-warning-bg: #FEF3C7;
```

Amber, chosen to sit between the existing `--color-success` green and `--color-danger` red, and dark enough at `#B45309` to hold contrast on its own background.

- [ ] **Step 4: Register the icon**

In `cayenne-web/src/lib/ui/Icon.svelte`, beside the other lucide imports:

```typescript
	import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
```

and in the `ICONS` map:

```typescript
		'triangle-alert': TriangleAlert,
```

A triangle rather than reusing `circle-alert`: colour alone should not be the only thing separating a warning from an error.

- [ ] **Step 5: Add the status**

In `cayenne-web/src/lib/ui/Banner.svelte`:

```typescript
	type Status = 'info' | 'progress' | 'success' | 'warning' | 'error';
```

and in `STYLE`, between `success` and `error`:

```typescript
		warning: { classes: 'border-warning bg-warning-bg text-warning', icon: 'triangle-alert' },
```

Note `border-warning` needs no extra token — Tailwind derives the border utility from the same `--color-warning`.

- [ ] **Step 6: Run the tests**

Run: `cd cayenne-web && npm run test:unit`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add cayenne-web/src/app.css cayenne-web/src/lib/ui/Icon.svelte cayenne-web/src/lib/ui/Banner.svelte cayenne-web/tests/unit/ui/Banner.test.ts cayenne-web/tests/unit/tokens.test.ts
git commit -m "feat(ui): give Banner a warning status

A partial import is neither a success nor a failure, and the palette had no way
to say so. Reusing info would have styled it exactly like the progress banner,
putting the whole weight of 'fifteen recipes did not arrive' on the wording -
which is the failure this work exists to fix.

Amber tokens between the existing success green and danger red, and a triangle
rather than the error circle, so colour is not the only thing distinguishing
the two.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Say what the import actually did

**Files:**
- Modify: `cayenne-web/src/lib/features/import/JobProgress.svelte`
- Test: `cayenne-web/tests/unit/features/import/JobProgress.test.ts` (append and amend)

**Interfaces:**
- Consumes: `SkippedChunk`, `parseSkipped` (Task 2); the `warning` status (Task 3).
- Produces: no new exports.

**The three terminal branches** (spec 5.1), for a job whose `status` is `done`:

| Condition | Status | Message |
|---|---|---|
| `skipped_count === 0` | `success` | `N recipes added`, or `Recipe added!` when `recipe_count === 1` |
| `recipe_count === 0` | `error` | `Nothing could be imported` |
| otherwise | `warning` | `${recipe_count} of ${recipe_count + skipped_count} imported` |

A `status` of `error` keeps its existing branch and wording. In the `warning` and `error` cases the skipped entries are listed.

**Note the ordering:** the zero-recipe test must come before the general partial case, and both before the success case. Today's expression tests `recipe_count > 1`, so a job that imported nothing falls through to the singular `Recipe added!` — a job that lost everything currently congratulates the user.

- [ ] **Step 1: Write the failing tests**

Amend the fixture at the top of `cayenne-web/tests/unit/features/import/JobProgress.test.ts` to include the new fields:

```typescript
const job = (over: Partial<IngestionJobRow>): IngestionJobRow => ({
	id: 'j1', user_id: 'u1', status: 'running', stage: 'EXTRACTING', progress_pct: 40, recipe_count: 0,
	skipped_count: 0, skipped: null,
	source_hint: null, error_message: null, created_at: 't', updated_at: 't', ...over
});
```

and append:

```typescript
const SKIPPED = JSON.stringify([
	{ label: 'Sticky Toffee Pudding', index: 3, reason: 'MAX_TOKENS' },
	{ label: null, index: 9, reason: 'timed out after 600s' }
]);

describe('JobProgress terminal reporting', () => {
	it('warns rather than celebrates when recipes were dropped', () => {
		render(JobProgress, {
			props: {
				state: 'terminal',
				job: job({ status: 'done', stage: 'DONE', progress_pct: 100, recipe_count: 812, skipped_count: 15, skipped: SKIPPED }),
				error: null,
				ondismiss: () => {}
			}
		});
		const banner = screen.getByTestId('ingestion-banner');
		expect(banner).toHaveTextContent('812 of 827 imported');
		expect(banner.className).toContain('warning');
	});

	it('names the dropped entries, and falls back to the index when the source named nothing', () => {
		render(JobProgress, {
			props: {
				state: 'terminal',
				job: job({ status: 'done', stage: 'DONE', recipe_count: 812, skipped_count: 2, skipped: SKIPPED }),
				error: null,
				ondismiss: () => {}
			}
		});
		const list = screen.getByTestId('ingestion-banner-skipped');
		expect(list).toHaveTextContent('Sticky Toffee Pudding');
		expect(list).toHaveTextContent('MAX_TOKENS');
		expect(list).toHaveTextContent('#9');
	});

	it('calls a job that imported nothing an error, not a success', () => {
		render(JobProgress, {
			props: {
				state: 'terminal',
				job: job({ status: 'done', stage: 'DONE', recipe_count: 0, skipped_count: 4, skipped: SKIPPED }),
				error: null,
				ondismiss: () => {}
			}
		});
		const banner = screen.getByTestId('ingestion-banner');
		expect(banner).toHaveTextContent('Nothing could be imported');
		expect(banner).not.toHaveTextContent('Recipe added!');
	});

	it('still celebrates a clean import', () => {
		render(JobProgress, {
			props: {
				state: 'terminal',
				job: job({ status: 'done', stage: 'DONE', recipe_count: 12, skipped_count: 0 }),
				error: null,
				ondismiss: () => {}
			}
		});
		expect(screen.getByTestId('ingestion-banner')).toHaveTextContent('12 recipes added');
		expect(screen.queryByTestId('ingestion-banner-skipped')).toBeNull();
	});
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd cayenne-web && npx vitest run tests/unit/features/import/JobProgress.test.ts`
Expected: FAIL — the partial job renders `812 recipes added` in a success banner; there is no `ingestion-banner-skipped`.

- [ ] **Step 3: Rewrite the terminal branch**

In `cayenne-web/src/lib/features/import/JobProgress.svelte`, replace the `doneMessage` derivation with:

```svelte
	import { parseSkipped } from '$lib/domain/recipe';

	const skipped = $derived(parseSkipped(job?.skipped ?? null));
	const attempted = $derived(job ? job.recipe_count + job.skipped_count : 0);
	/**
	 * A done job has three readings, not two. A partial import must not render green
	 * with different words: the defect being fixed is a lossy import that looked clean,
	 * and the wording is the smaller half of that. A job that imported nothing reads as
	 * an error even though the server finished, because from here nothing arrived.
	 */
	const done = $derived.by(() => {
		if (!job) return null;
		if (job.skipped_count === 0) {
			return { status: 'success' as const, message: job.recipe_count > 1 ? `${job.recipe_count} recipes added` : 'Recipe added!' };
		}
		if (job.recipe_count === 0) {
			return { status: 'error' as const, message: 'Nothing could be imported' };
		}
		return { status: 'warning' as const, message: `${job.recipe_count} of ${attempted} imported` };
	});
```

and replace the `{:else if state === 'terminal' && job}` block's `done` branch with:

```svelte
{:else if state === 'terminal' && job}
	{#if job.status === 'done' && done}
		<Banner status={done.status} message={done.message} testId="ingestion-banner" dismissible {ondismiss}>
			{#if skipped.length > 0}
				<ul data-testid="ingestion-banner-skipped" class="flex flex-col gap-4 text-14">
					{#each skipped as entry (entry.index)}
						<li>{entry.label ?? `Chunk #${entry.index}`} — {entry.reason}</li>
					{/each}
				</ul>
			{/if}
		</Banner>
	{:else}
		<Banner status="error" message="Ingestion failed" testId="ingestion-banner" dismissible {ondismiss}>
			{#if job.error_message}<p data-testid="ingestion-banner-error" class="text-14">{job.error_message}</p>{/if}
		</Banner>
	{/if}
```

- [ ] **Step 4: Run the tests**

Run: `cd cayenne-web && npm run test:unit`
Expected: PASS, including the pre-existing "celebrates a finished job" test — a clean job still reads `Recipe added!`.

- [ ] **Step 5: Commit**

```bash
git add cayenne-web/src/lib/features/import/JobProgress.svelte cayenne-web/tests/unit/features/import/JobProgress.test.ts
git commit -m "fix(web): stop a lossy import reading as a clean one

A done job had two readings, success and failure, and every partial import took
the success path: the 2026-09-04 run lost twenty recipes and showed a green
banner. A job that imported nothing at all was worse - the count test was
recipe_count > 1, so zero fell through to the singular 'Recipe added!'.

Three branches now. A clean import is unchanged. A partial one warns and says
how many of how many arrived. An import that produced nothing is an error, even
though the server finished, because from the user's side nothing came. The
warning and error cases list what was dropped, by name where the source gave
one and by chunk index where it did not, which is what makes the count
actionable rather than merely honest.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Nullable quantities through the client

**Files:**
- Modify: `cayenne-web/src/lib/domain/recipe.ts:13`, `:120`
- Modify: `cayenne-web/src/lib/domain/culinaryMath.ts:33-68`
- Modify: `cayenne-web/src/lib/features/kitchen/IngredientRow.svelte:8-22`
- Test: `cayenne-web/tests/unit/domain/culinaryMath.test.ts`, `cayenne-web/tests/unit/features/kitchen/IngredientRow.test.ts` (append/amend)

**Interfaces:**
- Consumes: migration 010 (Task 1), applied — after it there are no zero amounts in the data.
- Produces: `StructuredIngredient.amount: number | null`; `ScaledIngredient.scaled_amount: number | null`.

**Why the zero check must go, not gain a sibling.** D13 forbids compatibility branches in releasable code. Once 010 has run, `0` means a real zero again and `null` means absent; a client that treats both as absent would be carrying a legacy encoding that no longer exists in the database. `formatQuantity.ts:37` is deliberately untouched — it still renders a real zero as `"0"`, because whether to show a quantity is the caller's decision, not the formatter's.

- [ ] **Step 1: Write the failing tests**

Append to `cayenne-web/tests/unit/domain/culinaryMath.test.ts`:

```typescript
it('carries an absent amount through scaling untouched', () => {
	const salt: StructuredIngredient = {
		id: 'ing_01', amount: null, unit: null, name: 'Kosher salt', fallback_string: 'Kosher salt',
		converted_amount: null, converted_unit: null, is_ai_converted: false
	};

	const [scaled] = scaleIngredients([salt], 4, 8, 'Metric', 'Weight');

	expect(scaled.scaled_amount).toBeNull();
	expect(scaled.scaled_converted_amount).toBeNull();
	expect(scaled.approximate).toBe(false);
	expect(scaled.unit).toBeNull();
});

it('still scales a real quantity', () => {
	const flour: StructuredIngredient = {
		id: 'ing_02', amount: 2, unit: 'cups', name: 'flour', fallback_string: '2 cups flour',
		converted_amount: null, converted_unit: null, is_ai_converted: false
	};

	const [scaled] = scaleIngredients([flour], 4, 8, 'US', 'Volume');

	expect(scaled.scaled_amount).toBe(4);
});
```

Append to `cayenne-web/tests/unit/features/kitchen/IngredientRow.test.ts`:

```typescript
it('prints only the name when the source gave no quantity', () => {
	expect(quantityText({ ...base, scaled_amount: null, unit: 'cups', name: 'Kosher salt' }, false)).toBe('Kosher salt');
});

it('prints a real zero, which is now a real measurement', () => {
	expect(quantityText({ ...base, scaled_amount: 0, unit: 'g', name: 'salt' }, false)).toBe('0 g salt');
});
```

using whatever `base` fixture the file already defines.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd cayenne-web && npx vitest run tests/unit/domain/culinaryMath.test.ts tests/unit/features/kitchen/IngredientRow.test.ts`
Expected: FAIL — TypeScript rejects `amount: null`, and `quantityText` returns `'salt'` for the real zero.

- [ ] **Step 3: Widen the types**

`cayenne-web/src/lib/domain/recipe.ts:12-13`:

```typescript
	/** Numeric quantity for scaling; null when the source stated none ("to taste"). */
	amount: number | null;
```

and `:119-120`:

```typescript
	/** The scaled quantity; null when the ingredient had none to scale. */
	scaled_amount: number | null;
```

- [ ] **Step 4: Short-circuit the scaler**

In `cayenne-web/src/lib/domain/culinaryMath.ts`, as the first statement inside the `ingredients.map((ing) => {` callback:

```typescript
		// An ingredient the source never quantified has nothing to scale and nothing to
		// convert. Returning early keeps every branch below reasoning about real numbers.
		if (ing.amount === null) {
			return { ...ing, scaled_amount: null, scaled_converted_amount: null, approximate: false };
		}
```

The existing `const scaledBase = ing.amount * factor;` on the next line then needs no null handling, and TypeScript narrows `ing.amount` to `number` for the rest of the callback.

- [ ] **Step 5: Read the new representation in the row**

`cayenne-web/src/lib/features/kitchen/IngredientRow.svelte`, replace the comment at `:8-15` and the two lines at `:17-18`:

```svelte
	/**
	 * An ingredient the source never quantified arrives with a null amount, so the line is
	 * just the name: a bare unit beside no number would read as a mistake in the recipe.
	 * A zero is a real measurement again and prints as one — until migration 010 the API
	 * had no way to say "no quantity" and stored zeros, which this used to paper over.
	 */
	export function quantityText(ingredient: ScaledIngredient, useFractions: boolean): string {
		const unquantified = ingredient.scaled_amount === null;
		const qty = unquantified ? '' : formatQuantity(ingredient.scaled_amount, useFractions);
```

TypeScript will require a non-null assertion or a narrowing local for `formatQuantity`; use a local:

```svelte
		const amount = ingredient.scaled_amount;
		const qty = amount === null ? '' : formatQuantity(amount, useFractions);
		const unit = amount === null ? '' : (ingredient.unit ?? '');
```

- [ ] **Step 6: Run everything and fix the fallout**

Run: `cd cayenne-web && npm run check && npm run test:unit`
Expected: `svelte-check` names every remaining place that assumed a non-null amount. Fix each by narrowing, never by defaulting to `0` — a default would reintroduce the sentinel migration 010 just removed.

- [ ] **Step 7: Commit**

```bash
git add cayenne-web/src/lib/domain/recipe.ts cayenne-web/src/lib/domain/culinaryMath.ts cayenne-web/src/lib/features/kitchen/IngredientRow.svelte cayenne-web/tests/unit/domain/culinaryMath.test.ts cayenne-web/tests/unit/features/kitchen/IngredientRow.test.ts
git commit -m "refactor(web): read an absent quantity as null, not as zero

The kitchen row dropped any zero quantity because the ingestion API stored 'to
taste' as an amount of zero and had no way to say otherwise. With the API
emitting null and migration 010 having rewritten every stored zero, that
display hack is both wrong and unnecessary: a zero is a real measurement again.

Scaling short-circuits on a null amount rather than multiplying it, which also
keeps every conversion branch reasoning about real numbers. formatQuantity is
untouched and still renders a real zero as '0' - whether to show a quantity
belongs to the line doing the showing, not to the formatter.

Per D13 there is no branch accepting both encodings.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Open the pull request

- [ ] **Step 1: Full local verification**

Run, from `cayenne-web/`: `npm run lint && npm run check && npm run test:unit`
Expected: all clean.

- [ ] **Step 2: Browser and flow suites**

Run: `npm run test:browser` then `npm run test:e2e`
Expected: PASS. The import flow seeds job rows through `seedIngestionJobs`; if it asserts on the terminal banner's text, update it to the new wording rather than weakening the assertion.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin <branch>
gh pr create --title "Skip reporting, and nullable ingredient quantities" --body "$(cat <<'BODY'
Spec section 5 of `docs/superpowers/specs/2026-09-05-ingestion-repair-design.md`.

- Migrations 009 (skipped_count, skipped) and 010 (zero amounts to null)
- Both columns through the sync rules, PowerSync schema, row type and query
- A `warning` status on Banner, with tokens and a triangle icon
- Three terminal branches in JobProgress: clean, partial, and nothing imported
- `amount` nullable through the domain layer, removing the zero-drop hack

Migrations 009 and 010 must be applied to the Supabase project before this merges.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```
