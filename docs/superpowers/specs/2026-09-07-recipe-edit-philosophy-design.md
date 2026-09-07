# Recipe Edit Philosophy — Design

Date: 2026-09-07
Status: Draft for review
Scope: Cayenne (SvelteKit + PowerSync) and RecipeParser (FastAPI). Supabase schema owned by the Cayenne repo.

## 1. Problem

Cayenne stores recipes in their AI-derived shape only: `structured_ingredients`
(with per-ingredient ids), `tokenized_directions` (fat tokens `{{id|fallback}}`
that reference those ids) and a 1536-dim `embedding`. Nothing in the schema
holds the plain text a user would edit. Any edit therefore either corrupts the
derived data (tokens pointing at ingredients that no longer exist, stale
vectors) or has to be re-derived by the server.

This design makes the server the sole writer of derived data, gives the user
plain-text columns to edit, and defines exactly which edits trigger
re-derivation, how re-derivation runs, and what the client shows while it is
pending. It also settles what happens to categories when the user changes the
category taxonomy.

The AI chat feature (RAG over the user's recipes) depends on this: it needs
fresh embeddings and a server-side copy of the raw text. Chat is out of scope
here except where a decision affects it.

## 2. Decisions

| # | Decision | Chosen |
|---|----------|--------|
| D1 | Edit surface | Free-text body (title, ingredient lines, direction steps) plus ingredient amount/unit editable without AI regen. All Paprika metadata fields are plain user-owned columns. |
| D2 | Amount/unit edit semantics | A recipe change: the client rewrites the raw ingredient line and records an override. `body_rev` is not bumped. |
| D3 | Categories after ingest | Never recategorised automatically. Regen ignores the categorisation output of REFINE. |
| D4 | Taxonomy changes | Renames, moves and deletes need no AI. Additions offer an explicit, additive-only, scoped bulk pass. |
| D5 | Regen mechanism | Stale rows are the queue (`derived_rev < body_rev`). A polling worker inside the FastAPI process claims them with a lease. No webhook, no client API call. |
| D6 | Client vectors | Chat is the only consumer. `embedding` is removed from the PowerSync sync rules and lives only in Postgres. |
| D7 | Derived columns are server-only | The client never writes `structured_ingredients`, `tokenized_directions`, `embedding` or the derived bookkeeping columns. |

## 3. Schema and ownership

Every column on `recipes` belongs to exactly one group.

### 3.1 User-owned metadata (never triggers regen)

`base_servings`, `prep_time`, `cook_time`, `source`, `source_url`, `notes`,
`description`, `nutritional_info`, `difficulty`, `rating` (nullable, null =
unrated), `image_url`. Categories live in `recipe_categories` and are
user-owned after ingest.

### 3.2 User-owned body (feeds the AI stages)

| Column | Type | Notes |
|--------|------|-------|
| `title` | text | existing |
| `ingredient_lines` | jsonb array of strings, default `[]` | one entry per line; section headers are lines |
| `direction_steps` | jsonb array of strings, default `[]` | one entry per step |
| `body_rev` | integer, default 0 | client increments on any free-text change to the three columns above |
| `amount_overrides` | jsonb object, default `{}` | keyed by ingredient id → `{amount, unit}`; written by the client, cleared by the server after REFINE |

### 3.3 Server-derived

| Column | Type | Notes |
|--------|------|-------|
| `structured_ingredients` | jsonb | existing |
| `tokenized_directions` | jsonb | existing |
| `embedding` | vector(1536) | existing; no longer synced to the client |
| `derived_rev` | integer, default 0 | the `body_rev` the derived columns were built from |
| `refined_from_hash` | text, nullable | sha256 over canonical `ingredient_lines` + `direction_steps` at last REFINE |
| `embedded_from_hash` | text, nullable | sha256 over canonical `title` + `ingredient_lines` at last EMBED |
| `derived_error` | text, nullable | last regen failure, cleared on success |
| `derived_attempts` | integer, default 0 | failures against `derived_attempts_rev` |
| `derived_attempts_rev` | integer, default 0 | the `body_rev` the attempts count against |
| `claimed_at` | timestamptz, nullable | worker lease; expires after 5 minutes |
| `updated_at` | timestamptz | maintained by a Postgres trigger, not the client |

A recipe is **stale** exactly when `derived_rev < body_rev`. There is no stale
flag and no enqueue trigger.

A null hash means "run this stage next time".

### 3.4 Canonical hash input

Computed in Python only (`recipeparser/core/regen.py`):

- `refine_hash = sha256(json.dumps([ingredient_lines, direction_steps], ensure_ascii=False, separators=(",", ":")))`
- `embed_hash  = sha256(json.dumps([title, ingredient_lines], ensure_ascii=False, separators=(",", ":")))`

Backfill leaves both null rather than reproducing this in SQL.

### 3.5 Sync rules

Add to the `recipes` query: `ingredient_lines`, `direction_steps`, `body_rev`,
`derived_rev`, `amount_overrides`, `derived_error`. Remove `embedding`. The
hash, attempt, claim and `updated_at` columns stay server-only. The client
SQLite schema (`AppSchema`) mirrors the synced columns.

### 3.6 Rendering invariant

Fat tokens are rendered by looking up the ingredient id in the current
`structured_ingredients` (with `amount_overrides` applied). The baked fallback
text inside the token is used only when the id no longer exists. Once amounts
can change without regen the baked fallback can be wrong, so this becomes a
tested rule rather than a convention.

## 4. Client rules (Cayenne)

The recipe editor always edits raw columns, never derived ones, stale or not.

### 4.1 Metadata edit
Write the column through PowerSync. Nothing else. Category edits write the
junction table.

### 4.2 Free-text body edit
Title is a field; ingredient lines and direction steps are one text row each.
On save the client writes the changed columns and sets
`body_rev = body_rev + 1` in the same PATCH.

### 4.3 Amount or unit edit
A structured widget on an ingredient. On save the client writes two raw
columns and no derived ones, without bumping `body_rev`:

1. Rewrite the matching entry in `ingredient_lines` with a pure function
   `rewriteAmount(line, oldAmount, oldUnit, newAmount, newUnit)`:
   - if the line starts with a numeric expression (`1`, `1.5`, `1 1/2`, `1/2`,
     ranges like `2-3`), replace it with the formatted new amount; if the
     following token equals the old unit, replace it with the new unit;
     the remainder of the line is preserved ("2 cups flour, sifted");
   - otherwise fall back to `"{amount} {unit} {structured name}"`.trim();
   - amounts are formatted from a small fraction table (0.25 → "1/4",
     0.333 → "1/3", 1.5 → "1 1/2"), decimals otherwise.
2. Set `amount_overrides[ingredient.id] = {amount, unit}`.

Rendering applies the override on top of the structured entry with that id.
The next REFINE (for any reason) parses the rewritten line and clears
`amount_overrides` in the same write. If no regen ever happens the override
persists and stays correct.

Matching between a structured entry and its line: each structured entry
gains a `line_index` field (the 0-based index into `ingredient_lines` it was
parsed from). REFINE is asked to emit it; the stage validates that every
`line_index` is in range and unique. Header lines produce no entry. The
client uses `line_index` to find the line to rewrite. Backfilled rows get
`line_index = position` since backfill derives lines from the structured
entries in order.

### 4.4 Upload handler
`connector.ts` already sends only `opData` on PATCH. A test pins this: a
PATCH on `recipes` never includes a column the client did not change, and
never includes a server-only column.

### 4.5 Client-created recipes
Inserted as a PUT with raw columns, `structured_ingredients = []`,
`tokenized_directions = []`, `body_rev = 1`, `derived_rev = 0`. The worker
refines it like any edit. Hand-entered recipes get fat tokens and embeddings
with no extra client work.

### 4.6 Rendering a stale recipe
When `derived_rev < body_rev`:
- ingredients and directions render as plain text from the raw columns;
- the scaling control is disabled;
- a badge reads "Updating…" when online or "Will update when online" otherwise;
- old fat tokens are never rendered against edited text.

When `derived_error` is set, the badge shows the message and a **Retry**
action that bumps `body_rev` (which also resets the attempt counter on the
server side).

When not stale: render from structured ingredients with overrides applied and
tokenized directions resolved by id.

## 5. Reprocess worker (RecipeParser)

### 5.1 Placement
- Background `asyncio` task started from a FastAPI lifespan hook (new).
- Disabled when `REGEN_WORKER_ENABLED` is not `1` (tests, CLI runs).
- Poll interval 10 s. At most 2 concurrent regens per process (semaphore).

### 5.2 Code layout
- `recipeparser/core/regen.py` — pure: `plan(row) -> RegenPlan` (which stages
  to run), hash functions, `build_extraction(row) -> RecipeExtraction`,
  `build_update(plan, refinement, embedding) -> dict`. No I/O imports.
- `recipeparser/adapters/regen_worker.py` — polling loop, Supabase reads and
  writes (service-role client), Gemini client, lease handling. The only module
  that knows table names.

### 5.3 Claiming
Poll:

```sql
select ... from recipes
where derived_rev < body_rev
  and updated_at < now() - interval '20 seconds'
  and (claimed_at is null or claimed_at < now() - interval '5 minutes')
  and (derived_attempts_rev <> body_rev or derived_attempts < 3)
order by updated_at
limit :n
```

Claim per row with a conditional update setting `claimed_at = now()` where the
previous claim is null or expired. No row locks are held across Gemini calls,
so client uploads to the same row are never blocked. The 20-second quiet window
is the debounce: a burst of edits produces one regen.

### 5.4 Stage selection
Read `profiles.uom_system` and `profiles.measure_preference` for the owner
(defaults US / Volume when absent).

- If `refine_hash(row) != refined_from_hash` (or the stored hash is null):
  run REFINE via `refine(build_extraction(row), client, uom_system=…,
  measure_preference=…, user_axes=…)`. Discard `grid_categories` (D3).
  Otherwise reuse the stored structured ingredients and tokenized directions.
- If `embed_hash(row) != embedded_from_hash` (or null) **or REFINE ran**:
  run EMBED using the existing `embed()` (title + fallback strings), so old and
  new vectors remain comparable. Changing the embedding input is a separate
  decision; this worker is how it would be rolled out.

A title-only edit costs one embedding call. A direction edit costs one REFINE
and one embed.

### 5.5 Write-back
One conditional update, guarded by `where id = :id and body_rev = :read_rev`:

- `structured_ingredients`, `tokenized_directions` (if REFINE ran)
- `embedding` (if EMBED ran)
- `refined_from_hash`, `embedded_from_hash` (for stages that ran)
- `derived_rev = :read_rev`
- `amount_overrides = '{}'` **only if REFINE ran**
- `derived_error = null`, `derived_attempts = 0`, `claimed_at = null`

Zero rows updated means the user edited again during the run. The result is
dropped; the row is still stale and is picked up on a later poll.

### 5.6 Failure
On any exception:

```sql
update recipes set
  derived_error = :msg,
  derived_attempts = case when derived_attempts_rev = body_rev
                          then derived_attempts + 1 else 1 end,
  derived_attempts_rev = body_rev,
  claimed_at = null
where id = :id
```

After three failures on the same revision the row drops out of the poll until
`body_rev` changes.

### 5.7 Ingestion writer changes
`SupabaseWriter.write_recipe` additionally writes `ingredient_lines` and
`direction_steps` from the extraction's raw strings, both hashes,
`body_rev = 0`, `derived_rev = 0`, `amount_overrides = {}`. For the Paprika
fast path (`_cayenne_meta`, Gemini bypassed) lines come from each structured
entry's `fallback_string` and steps from tokenized text with tokens replaced by
their fallbacks. Ingestion reads `uom_system` / `measure_preference` from
`profiles` as well, so the two paths agree.

## 6. Bulk recategorise

### 6.1 Trigger
After a taxonomy save in Cayenne that **added** tags or an axis, the app offers
"Apply to existing recipes". Accepting inserts a row into `ingestion_jobs`
through PowerSync with `kind = 'recategorize'` and
`params = {"category_ids": [...]}`. The existing user-fence RLS policy allows
the write. Nothing runs without the tap.

Renames, moves between axes, and deletes need no job: junction rows reference
category UUIDs and cascade on delete.

### 6.2 Worker
The same polling loop runs a second query for pending recategorise jobs. Per
job:

1. Load the user's axes; resolve `category_ids` to axis → new tag names.
2. Walk the user's recipes in id order from `params.cursor` (updated after
   each batch so a restart resumes).
3. Send batches of 10 recipes (title, ingredient lines, direction steps) to a
   new categorise-only Gemini function with a structured-output schema
   listing, per recipe id, the matching new tags. Only the new tags are
   offered in the prompt.
4. Insert `recipe_categories` rows for matches, `on conflict do nothing`.
   Never delete.
5. Update `progress_pct` and `recipe_count` on the job row.

### 6.3 Control and failure
The client cancels by setting `status = 'cancelled'`; the worker checks between
batches. A failed batch is logged and skipped. The job errors only if more than
10% of batches fail.

### 6.4 New code
- `gemini.categorize_batch(recipes, new_axes, client) -> dict[id, list[str]]`
- `core/stages/categorize.py`: pure batch planner and result filter (drops
  tags not in the offered set).
- `ingestion_jobs` columns: `kind text not null default 'ingest'`,
  `params jsonb not null default '{}'`.

## 7. Migration and backfill

Cayenne repo, next migration after the pending Paprika metadata migration.

### 7.1 DDL

```sql
alter table recipes
  add column ingredient_lines     jsonb not null default '[]',
  add column direction_steps      jsonb not null default '[]',
  add column body_rev             integer not null default 0,
  add column derived_rev          integer not null default 0,
  add column refined_from_hash    text,
  add column embedded_from_hash   text,
  add column amount_overrides     jsonb not null default '{}',
  add column derived_error        text,
  add column derived_attempts     integer not null default 0,
  add column derived_attempts_rev integer not null default 0,
  add column claimed_at           timestamptz,
  add column updated_at           timestamptz not null default timezone('utc', now());

create index recipes_stale_idx on recipes (updated_at)
  where derived_rev < body_rev;

create extension if not exists moddatetime;
create trigger recipes_updated_at before update on recipes
  for each row execute procedure moddatetime(updated_at);

alter table ingestion_jobs
  add column kind   text  not null default 'ingest',
  add column params jsonb not null default '{}';
```

### 7.2 Backfill (no AI)

```sql
update recipes set
  ingredient_lines = coalesce(
    (select jsonb_agg(e->>'fallback_string' order by ord)
       from jsonb_array_elements(structured_ingredients) with ordinality t(e, ord)),
    '[]'),
  direction_steps = coalesce(
    (select jsonb_agg(regexp_replace(e->>'text', '\{\{[^|]+\|([^}]+)\}\}', '\1', 'g') order by ord)
       from jsonb_array_elements(tokenized_directions) with ordinality t(e, ord)),
    '[]'),
  structured_ingredients = coalesce(
    (select jsonb_agg(e || jsonb_build_object('line_index', ord - 1) order by ord)
       from jsonb_array_elements(structured_ingredients) with ordinality t(e, ord)),
    '[]');
```

Hashes stay null: the first edit of a legacy recipe runs a full REFINE even if
only the title changed. One-time cost per recipe.

### 7.3 Deploy order
1. Migration + backfill (all defaults; nothing reads the columns yet).
2. Sync rules + client (renders from raw columns; edits start bumping `body_rev`).
3. Worker (starts draining stale rows).
4. Ingestion writer change (new recipes arrive with raw columns populated).

Each step is safe with the previous one still running. Between steps 2 and 3
edited recipes simply stay stale, which the client already renders.

## 8. Testing

### RecipeParser
- `core/regen.py`: hash determinism; plan selection for title-only, lines-only,
  steps-only, null-hash, and unchanged rows; `build_extraction` round-trips raw
  columns; `build_update` clears `amount_overrides` only when REFINE ran.
- `core/stages/refine.py`: `line_index` validation (in range, unique, headers
  skipped).
- `adapters/regen_worker.py` (mocked Supabase + Gemini): claims respect the
  quiet window and lease expiry; write-back is skipped when `body_rev` moved;
  failure path increments attempts and resets them when `body_rev` changes;
  worker does not start when the env flag is off.
- Categorise batch: result filter drops unoffered tags; cursor advances per
  batch; cancel is honoured between batches; junction inserts are additive.
- Writer: new columns present on insert; Paprika fast path derives lines and
  steps from structured data.
- Golden/snapshot tests: REFINE snapshots updated once for the added
  `line_index` field; no other prompt change.

### Cayenne
- `rewriteAmount` pure function: leading integer, decimal, mixed fraction,
  range, unit change, no-leading-number fallback, tail preservation.
- Fraction formatting table.
- Upload handler: PATCH carries only changed columns and never a server-only
  column (pins the invariant in 4.4).
- Stale rendering: raw text shown, scaling disabled, correct badge for
  online/offline/error; retry bumps `body_rev`.
- Token rendering: id lookup wins over baked fallback; fallback used only for
  a missing id.
- Editor writes: free-text save bumps `body_rev`; amount save does not and
  writes both `ingredient_lines[line_index]` and `amount_overrides[id]`.

### End-to-end (manual, live project)
Edit a direction offline → reconnect → badge clears within ~30 s and tokens
resolve. Change an amount → scaling reflects it immediately; later direction
edit → overrides cleared, amount preserved. Add a tag → accept the offer →
junction rows appear, nothing removed.

## 9. Out of scope
- AI chat endpoint and drawer (separate design; depends on this one).
- Changing the embedding input text or model.
- Structured editing of ingredient names or direction text in place.
- Full library re-categorisation that replaces AI-assigned tags.
