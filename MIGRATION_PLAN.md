# RecipeParser Migration Plan

## 1. Philosophy

This is a **big-bang replacement**. The new architecture replaces the old one entirely in a single cutover. There are no incremental migration steps, no compatibility shims, and no legacy code paths. The system either runs the new architecture or it does not run.

**Pre-condition:** All validation checks in Section 2 must pass before cutover begins.

---

## 2. Pre-conditions

Before cutover, the following must be true:

- [ ] All new module unit tests pass (`pytest recipeparser/tests/ -v`)
- [ ] `MockProvider` + `MockEmbeddingProvider` produce deterministic output for all test fixtures
- [ ] `GOOGLE_API_KEY` is set and `gemini-embedding-001` connectivity verified (no second key needed)
- [ ] Supabase `ingestion_jobs` table created (Section 3)
- [ ] Supabase Storage `recipe-images` bucket created with RLS policy (Section 3)
- [ ] PowerSync sync rules updated for `ingestion_jobs` (Section 4)
- [ ] Cayenne app local SQLite migration `006_ingestion_jobs.sql` applied (Section 4)
- [ ] `.env` updated with `EMBEDDING_PROVIDER=gemini` (Section 5)

---

## 3. Supabase Changes

### 3a. New Table: `ingestion_jobs`

Run in Supabase SQL editor:

```sql
create table ingestion_jobs (
    id              uuid primary key default uuid_generate_v4(),
    user_id         uuid references auth.users not null,
    status          text not null default 'pending'
                    check (status in ('pending', 'running', 'done', 'error')),
    stage           text not null default 'IDLE'
                    check (stage in ('IDLE','LOADING','CHUNKING','EXTRACTING',
                                     'CATEGORIZING','REFINING','EMBEDDING','DONE','ERROR')),
    progress_pct    integer not null default 0 check (progress_pct between 0 and 100),
    recipe_count    integer not null default 0,
    source_hint     text,
    error_message   text,
    created_at      timestamp with time zone default timezone('utc', now()),
    updated_at      timestamp with time zone default timezone('utc', now())
);

alter table ingestion_jobs enable row level security;
create policy "user_jobs" on ingestion_jobs
    for all using (auth.uid() = user_id);
```

### 3b. Supabase Storage Bucket

```sql
-- Create bucket via Supabase dashboard or CLI:
-- supabase storage create recipe-images --public false

-- RLS policy for the bucket:
create policy "user_images" on storage.objects
    for all using (auth.uid()::text = (storage.foldername(name))[1]);
```

### 3c. Schema Verification: `vector(1536)`

The existing `recipes.embedding` column is declared as `vector(1536)`. `GeminiEmbeddingProvider` uses `output_dimensionality=1536`, matching exactly. **No schema migration required.**

Verify with:
```sql
select column_name, data_type, udt_name
from information_schema.columns
where table_name = 'recipes' and column_name = 'embedding';
-- Expected: udt_name = 'vector'
```

---

## 4. PowerSync + Cayenne App Changes

### 4a. PowerSync Sync Rules

Add to `cayenne-app/powersync/sync-config.yaml`:

```yaml
- table: ingestion_jobs
  parameters:
    - name: user_id
      value: token_parameters.user_id
  where: user_id = :user_id
```

### 4b. Local SQLite Migration

Create `cayenne-app/src/db/migrations/006_ingestion_jobs.sql`:

```sql
create table if not exists ingestion_jobs (
    id              text primary key,
    user_id         text not null,
    status          text not null,
    stage           text not null,
    progress_pct    integer not null,
    recipe_count    integer not null,
    source_hint     text,
    error_message   text,
    created_at      text not null,
    updated_at      text not null
);
```

### 4c. TypeScript Type Addition

Add to `cayenne-app/src/types/recipe.ts`:

```typescript
export interface IngestionJobRow {
  id: string;
  user_id: string;
  status: 'pending' | 'running' | 'done' | 'error';
  stage: 'IDLE' | 'LOADING' | 'CHUNKING' | 'EXTRACTING' |
         'CATEGORIZING' | 'REFINING' | 'EMBEDDING' | 'DONE' | 'ERROR';
  progress_pct: number;
  recipe_count: number;
  source_hint: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}
```

### 4d. OmniBox Simplification

`OmniBox.tsx` is simplified to fire-and-forget:

- **Remove:** `recipeDb.saveIngestedRecipe()` call after ingestion
- **Remove:** Local recipe insertion logic from ingestion flow
- **Add:** `POST /jobs` call → receive `job_id` → show "Processing…" toast
- **Add:** Dismiss on `job_id` received (no waiting for completion)

Recipes appear in the Library automatically when PowerSync syncs the new `recipes` row written by the API adapter.

### 4e. New Hook: `useIngestionJobs`

Create `cayenne-app/src/hooks/useIngestionJobs.ts`:

```typescript
import { usePowerSyncQuery } from './usePowerSyncQuery';
import { IngestionJobRow } from '../types/recipe';

export function useIngestionJobs(): IngestionJobRow[] {
  return usePowerSyncQuery<IngestionJobRow>(
    'SELECT * FROM ingestion_jobs ORDER BY created_at DESC LIMIT 20'
  );
}
```

---

## 5. RecipeParser Repo Changes

### 5a. Files Deleted

The following files are **deleted entirely** — no content is preserved:

```
recipeparser/pipeline.py          → replaced by core/engine.py + adapters/
recipeparser/gemini.py            → replaced by core/providers/gemini.py
recipeparser/api.py               → replaced by adapters/api.py
recipeparser/gui.py               → replaced by adapters/gui.py
recipeparser/categories.py        → replaced by io/category_sources/
recipeparser/export.py            → replaced by io/writers/
recipeparser/recategorize.py      → absorbed into adapters/cli.py
```

### 5b. Files Retained (Unchanged)

```
recipeparser/models.py            ← source of truth, no changes
recipeparser/config.py            ← constants, no changes
recipeparser/exceptions.py        ← error hierarchy, no changes
recipeparser/paths.py             ← path helpers, no changes
recipeparser/paprika_db.py        ← Paprika SQLite reader, no changes
recipeparser/checkpoint.py        ← checkpoint logic, no changes (used by engine)
```

### 5c. Files Created

```
recipeparser/core/__init__.py
recipeparser/core/engine.py
recipeparser/core/chunker.py
recipeparser/core/fsm.py
recipeparser/core/providers/__init__.py
recipeparser/core/providers/base.py
recipeparser/core/providers/factory.py
recipeparser/core/providers/gemini.py        # GeminiProvider + GeminiEmbeddingProvider
recipeparser/core/providers/openai.py        (stub — future)
recipeparser/core/providers/anthropic.py     (stub — future)
recipeparser/core/providers/mock.py

recipeparser/io/__init__.py
recipeparser/io/readers/__init__.py
recipeparser/io/readers/base.py
recipeparser/io/readers/epub.py
recipeparser/io/readers/pdf.py
recipeparser/io/readers/url.py
recipeparser/io/readers/text.py
recipeparser/io/readers/paprika.py
recipeparser/io/writers/__init__.py
recipeparser/io/writers/base.py
recipeparser/io/writers/cayenne_zip.py
recipeparser/io/writers/paprika_zip.py
recipeparser/io/category_sources/__init__.py
recipeparser/io/category_sources/base.py
recipeparser/io/category_sources/yaml_source.py
recipeparser/io/category_sources/paprika_db_source.py
recipeparser/io/category_sources/supabase_source.py

recipeparser/adapters/__init__.py
recipeparser/adapters/cli.py
recipeparser/adapters/gui.py
recipeparser/adapters/api.py
```

### 5d. `__main__.py` Updated

```python
# recipeparser/__main__.py
from recipeparser.adapters.cli import main
main()
```

### 5e. Dependencies Added (`pyproject.toml`)

```toml
[project.dependencies]
# Existing: google-genai, fastapi, uvicorn, pydantic, ebooklib, pypdf2, pyyaml, ...
supabase = ">=2.0"        # SupabaseCategorySource + image upload
# NOTE: openai package NOT required — embedding uses gemini-embedding-001 via existing google-genai SDK
```

### 5f. `.env` Updated

```
# Existing (unchanged)
GOOGLE_API_KEY=AIza...

# New
LLM_PROVIDER=gemini
EMBEDDING_PROVIDER=gemini     # reuses GOOGLE_API_KEY — no second API key needed

# For CLI/GUI image upload + category source
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=eyJ...   # service role key
```

---

## 6. Cutover Sequence

Execute in this exact order:

1. **Supabase** — Apply `ingestion_jobs` DDL + Storage bucket + RLS policies
2. **RecipeParser** — Deploy new code to Cloud Run / Render (new Docker image)
3. **Cayenne App** — Release new build with migration `006_ingestion_jobs.sql` + updated `OmniBox.tsx` + `useIngestionJobs` hook
4. **Verify** — Run validation checklist (Section 7)

---

## 7. Validation Checklist

End-to-end test matrix. All must pass before declaring cutover complete.

| Test | Input | Expected Output |
|------|-------|----------------|
| URL ingest (API) | `POST /jobs { url: "https://..." }` | `202 job_id`; job reaches `DONE`; recipe appears in app via PowerSync |
| EPUB ingest (CLI) | `recipeparser cookbook.epub --format cayenne` | `.cayennerecipes` ZIP created; image URL in recipe JSON |
| EPUB ingest (CLI, Paprika format) | `recipeparser cookbook.epub --format paprika` | `.paprikarecipes` ZIP created; image bytes embedded; `_cayenne_meta` present |
| Paprika import Flow A (API) | `POST /jobs` with legacy `.paprikarecipes` | Full Gemini pipeline runs; recipe in Supabase |
| Paprika import Flow B (API) | `POST /jobs` with Cayenne `.paprikarecipes` | Gemini bypassed; `_cayenne_meta` mapped directly; $0 API cost |
| Job status in app | Any API ingest | `useIngestionJobs()` returns live stage/progress via PowerSync |
| Category assignment | EPUB with YAML categories | Recipes assigned to correct leaf categories |
| Category assignment (API) | URL ingest for user with Supabase categories | Recipes assigned from user's Supabase taxonomy |
| Embedding dimension | Any ingest | `embedding` array length === 1536 |
| Provider swap | `--provider mock` (CLI) | Deterministic output; no API calls made |

---

## 8. Rollback

Since this is a big-bang replacement, rollback means reverting both repos to the prior git tag and dropping the new Supabase table.

```bash
# RecipeParser
git checkout tags/v-pre-refactor

# Cayenne app
git checkout tags/v-pre-refactor

# Supabase
DROP TABLE ingestion_jobs;
-- Storage bucket: delete recipe-images bucket via dashboard
```

No data migration is required on rollback because the `recipes` table schema is unchanged.
