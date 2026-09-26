# Changelog

All notable changes to RecipeParser are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [9.0.0] — 2026-09-26

Verbatim ingestion, the Add Recipe intake and its bulk fix, the recategorise endpoint, a cook's own picture, and readers that refuse what they cannot read by name: 45 commits over pull requests #41–#59 since v8.0.0.

**Breaking:** the CLI's `--units` flag, the GUI's Units row, and the unit-preference arguments of `RecipePipeline` and `run_cli_pipeline` are removed (see *Removed*). Two Cayenne migrations are required (see *Requires*).

### ⚠️ Requires — apply the Cayenne migrations **before** deploying this version
- **`20260925180000_verbatim_ingestion.sql`** — `SupabaseWriter` now puts `source_uom_system_detected` and `source_uom_system_evidence` into every recipe INSERT, and the regen worker reads `source_url` from the `claim_stale_recipes` RPC. Against a schema without the migration every ingest fails with `PGRST204`, and with `REGEN_WORKER_ENABLED=1` every regen fails too.
- **`20260913125453_ingestion_jobs_cancelled_status.sql`** — `POST /jobs/{id}/cancel` on a recategorise job writes `status = 'cancelled'`, which the check written in 008 refuses. Without the migration a recategorise job cannot be stopped.

Both are applied to the live project (checked 2026-09-26). The warnings stand for every other environment.

### ✨ Changed — ingestion copies what the writer wrote (verbatim ingestion, #58, #59)
- **EXTRACT copies every ingredient line verbatim** — one rule for every input, no conversion and no reader's preference. A number guard (`core/numbers.py`) checks that every number an ingredient line writes is one the source writes, by value. When a recipe fails it, the chunk is extracted once more and only a clean recipe of a failed one's name is taken from the retry; the first attempt's clean recipes are always kept, a retry that raises recovers nothing rather than losing them, and every failed recipe not recovered is dropped **by name**, never silently.
- The guard reads the source generously: number words count by value ("HALF AN OUNCE" is 0.5, "half a dozen" 6, "twenty-five" 25), a decimal comma is a decimal point, superscript and subscript digits are digits, and an EPUB's typeset fraction (`1<sup>1</sup>&frasl;<sub>2</sub>`, which the reader's text turns into "1 1 / 2") or a PDF's run-together "11⁄2" also reads as 1 1/2 — each as a union with the plain reading, so "Serves 4 / 6" still states 4 and 6.
- **REFINE keeps the writer's two measures.** A line that states a second measure fills it from the line, marked as the writer's; a second measure the line never writes is re-marked as the AI's. An AI conversion is admitted only into grams or millilitres — a cup or spoon the source system would resize is dropped.
- **REFINE detects the source's measuring system** (`US`, `UK`, `EU`, `AU`, `Imperial`, in any case, written canonically) with a quoted piece of evidence it verifies: a quote from the recipe text, or the source host it was given (the host, or a dot-boundary suffix of it such as `com.au`). Unverified, both are written null. The pair is stored on the recipe row.
- **The regen worker passes the host of the recipe's `source_url` to REFINE**, instead of reading the owner's profile preferences.
- **REFINE states whether each ingredient is `liquid` or `solid`** (`StructuredIngredient.state`) before it computes the conversion, because the conversion depends on it; null when the line has no amount or no volume or weight unit (#58). The mapping from the profile's cookbook locale to an extract mode that #58 also added was removed by #59, with the rest of the reader preferences, before this release.

### 🗑️ Removed — reader preferences at ingest (#59)
- The API no longer reads `profiles` for a unit preference. `uom_system` and `measure_preference` in a `/jobs` body are **ignored, not refused**, so an older client keeps working; the file job no longer takes them as query parameters.
- The CLI's `--units` flag and the GUI's Units row are gone; `RecipePipeline` and `run_cli_pipeline` take no unit-preference arguments.

### 🐛 Fixed — a book's photos reach its recipes again (#53)
- EPUB and PDF recipes never got their photo. The readers extracted images to a temporary directory deleted before `read()` returned, and the pipeline never read the `photo_filename` the extract reply names — the legacy monolith did both; `PIPELINE_REFACTOR.md` marked the hero-image logic MOVE and `90a4a54` deleted it instead. Each book `Chunk` now carries the bytes of the photos its text marks (`Chunk.images`), and each recipe takes the one the model named: stored through the `ImageStore` when there is one (the API), kept on the result either way.
- A photo-only page or chapter hands its photo to the recipe after it as `[HERO IMAGE: …]` again (`HERO_INJECT_MAX_STUB_CHARS`, which had outlived its only reader).
- The `.paprikarecipes` export (`PaprikaWriter`, the CLI's and GUI's output) embeds each recipe's photo as `photo` + `photo_data`, for book photos and a Paprika entry's own. The keys are still omitted when there is no photo. The bytes ride `IngestResponse.photo`, a private attribute, so they never reach an API response or a `_cayenne_meta`.
- A photo of a recipe page (`ImageReader`) still does not become the recipe's picture, by decision.
- New `tests/goldens/test_image_recovery_golden.py`: per corpus fixture, which source image each recipe ends up with, through a store and through the Paprika export. The e2e goldens had pinned `image_url: null`, so the loss passed CI. `dual-units.epub`'s two photos now differ byte for byte, and `saved-page.html` carries an `og:image` hero and a decoy logo.

### ✨ Added — a cook can change a recipe's picture (#48, #49)
- `POST /recipes/{recipe_id}/image` (multipart `file`) stores a picture chosen in Cayenne's recipe editor and answers `200 { image_url }`; `DELETE /recipes/{recipe_id}/image` removes it and answers `{ image_url: null }`. Both verify the caller owns the recipe and answer **404, never 403**, when they do not — `_owned_controller`'s rule, so the table cannot be enumerated by id. The service keeps its place as the bucket's only writer: no storage policy is widened for the browser, and the device never writes `image_url` itself — PowerSync delivers the row this endpoint updates.
- Accepts JPEG, PNG, WebP and GIF. Wider than `/jobs/file` on purpose: that list is what PyMuPDF can open for OCR, and nothing OCRs a picture the cook chose — the browser renders it. HEIC is refused with the reader's own sentence, since no browser decodes it either. The ceiling and its 413 sentence are `/jobs/file`'s (`config.MAX_UPLOAD_BYTES`), checked after the type so a small file of the wrong kind is still a 422.
- The stored URL carries a `v=<epoch>` stamp. The object key is the recipe id, so a replacement lands on the address the old picture had, and without the stamp the browser, the service worker and Supabase's CDN would all go on showing the picture that was just replaced. `SupabaseImageStore` gains `remove()`, which drops the recipe's objects under the other extensions — a JPEG replaced by a PNG left the first one in the bucket for ever.
- CORS allows `DELETE` (#49). The middleware's `allow_methods` still read `GET`, `POST`, `OPTIONS`, so the browser's preflight refused the only verb that clears a picture while every server-side test passed. A new `TestCors` asks the app for a preflight on each verb the client uses, and asserts an unlisted verb is still refused.

### ✨ Added — the bulk recategorise can be queued and stopped (Stage 7C, #46)
- `POST /jobs/recategorize` holds the service role and does the two things a device must not be trusted with: it checks the caller owns every category id, and it expands each to its subtree. A 422 names how many ids were unknown, never which.
- `POST /jobs/{id}/cancel` branches on the job's kind: a row update for a recategorise job, the in-memory controller for an ingest job. It answers 409 for a finished job, and 404 rather than 403 for someone else's.

### 🐛 Fixed — the recategorise worker, before its first real run (#46)
- A job a restart left running is reclaimed after a ten-minute lease and resumes from `params.cursor`.
- A batch that fails twice has its recipe ids written to `skipped` with the reason **before** the cursor moves past them. Until now only a counter survived, so the client could show "nothing skipped" while recipes were never examined.
- A cancelled job finishes with stage `DONE` and its counts intact, instead of stopping at `CATEGORIZING` and looking like a worker that died. A job whose categories have all gone finishes `error`, not `done`.
- A tag's axis is found by walking to the root in one place, `core/taxonomy.py`. Ingest and recategorise disagreed on three-level trees (Cuisine > Asian > Thai put Thai under Cuisine for one and under Asian for the other).
- `build_categorize_batch_prompt` reads body columns through `raw_body_column`, so a double-encoded jsonb column raises instead of putting one "ingredient" per character in the prompt. Duplicate category names raise in `load_category_ids` and `resolve_new_axes` instead of the last one winning.

### ✨ Changed — readers refuse unreadable input by name (#50, #52)
- A reader refuses what it cannot turn into text, before any model call, with an `UnreadableInputError` whose message is a sentence about the input: "is DRM-protected: its chapters are encrypted and cannot be read.", "is password-protected.", "contains no readable text.", "is not a Paprika export: not a ZIP archive.", "could not be fetched (HTTP 404).". The API writes the job's `error_message` as "<the user's filename or URL> <sentence>". It never names the server's temp path, and the "could not be opened as a PDF / an EPUB / an image." sentences no longer pass the library's own text through.
- New refusals: an EPUB whose encryption manifest declares more than font obfuscation (DRM); a book whose chapters decode mostly to replacement and control characters (the threshold is 5%) or hold no text; a Paprika archive with no recipe entries; a page that cannot be fetched, times out, or comes back empty. A reader no longer finishes a job with nothing in it.
- `configure_logging()` installs a console handler at `LOG_LEVEL` (default INFO) on the bare root logger uvicorn leaves behind, so the container log shows what every job did, not only its warnings and errors. On 2026-09-18 a DRM-protected EPUB ran to "done" with 17 chunks and 0 recipes, and the log showed nothing after the upload.

### 🐛 Fixed — readers
- An EPUB whose NCX is not XML still yields its chapters; only its table of contents is lost, with a warning (#50). One package-level `read_epub` routes every EPUB through the tolerant reader.
- A bot-protection page ("Just a moment…" plus a Ray ID, served with HTTP 200) is refused as a fetch failure instead of being read as a page with zero recipes: in `UrlReader` (#54) and in the `/jobs` endpoint's own fetch (#55), which shares `looks_like_bot_challenge`.

### ✨ Added — the intake reads photos and scans (Stage D, #41)
Stage D of the Add Recipe workstream (Cayenne `docs/superpowers/plans/2026-09-11-add-recipe-workstream.md`; roadmap Stage 6 row D).
- `POST /jobs/file` accepts `image/jpeg` and `image/png`. A new `ImageReader` opens the photo with PyMuPDF and transcribes it through the vision OCR the command-line PDF path already owned; one chunk, routed like a book chunk, no citation of its own (the transcript's stated source is used).
- `load_pdf` gains the same fallback behind a `client` parameter, so a scanned PDF is transcribed on the job path instead of refused by the pre-flight. Without a client the refusal stands. A scan is transcribed one vision call per page, so `PDF_OCR_MAX_PAGES` (40) caps it; a longer scan is refused before any model call.
- The 422 for a file the API cannot read is a sentence the client shows verbatim: `Cayenne can't read .docx files yet.`; HEIC is refused plainly: `Cayenne can't read HEIC photos yet. Share it as a JPEG instead.`; WebP the same way: `Cayenne can't read WebP photos yet. Share it as a JPEG instead.` — no decoder for either is in the stack.
- A present file extension now decides the reader on its own; the content type is consulted only when there is no extension. A `download.bin` sent as `application/pdf` used to reach the PDF reader and is now refused by name — browsers mislabel content types far more often than users misname files.

### ✨ Added — what a page states is no longer lost
- The URL path fetches the page itself once, with a browser user-agent, and takes `og:image` / `twitter:image` and the meta description from its `<head>` before consulting the scraper's markdown; the markdown fallback refuses badges and logos. On 2026-09-12 an NYT recipe stored the Edamam "Powered by" logo as its hero because the scraper's markdown carried no `og:image` line and that logo was its only image.
- `RecipeExtraction` gains `total_time`, `description` and `nutritional_info` (all `repr=False`, so the refine prompt body and the golden recordings do not move). `assemble()` fills the structured cook span from a stated total when no cook time is known, and keeps the extracted description and nutrition where no source (a Paprika entry, the page's meta) states them — since `607671d` both columns were Paprika-only, so a URL ingest had neither.
- A logo served as `og:image` is not the hero either: the page's own image is badge-checked by the same rule as the markdown fallback, and badge words match whole tokens (`iconic-lasagna.jpg` is a photograph).

### ✨ Added — the job row points at its source
- `ingestion_jobs.source_hint` becomes the recipes' `source_key`: written with `total_chunks` from the chunks' citations (books and sites), and again at finalize from the written rows (pasted text and photos). The Add Recipe screen's Recent-imports rows open the library on it.

### ✨ Added — the bulk fix after Stage E (2026-09-13)
- `POST /jobs/file` refuses a body over 50 MB (`config.MAX_UPLOAD_BYTES`) with a 413 whose `detail` is a sentence the client shows verbatim: "This file is 120.3 MB. Cayenne takes files up to 50 MB." The type check still runs first. Cayenne refuses the same size before the upload with the same sentence.
- `scripts/extract_unmatched_paprika.py`: the Paprika export's entries with no recipe row (Stage C counted 68), written into a small `.paprikarecipes` for one re-import through Add Recipe. The restore script's matching rule; the original member bytes.

### 🐛 Fixed — the bulk fix after Stage E (2026-09-13)
- A malformed page address (`http://[::1`) no longer fails a URL job: the private-host check runs inside `_fetch_page_meta`'s `try`, so it degrades to no meta as the docstring promises.
- The author's notes the extractor reads reach the row: `assemble()` takes `notes` with the rule `description` has — a Paprika entry's own notes win, else the extracted ones, else null. Since 2026-09-07 they were extracted and dropped on every path but Paprika.

### 🚀 Deployment (#43)
- `.github/workflows/deploy.yml`: a merge to `master` builds the ingestion image, pushes it to `ghcr.io/<owner>/ingestion-api` (tagged with the commit and `latest`) and deploys it to the Cayenne VM. A pull request only builds it. Until `CAYENNE_VM_HOST` is set, the image is pushed and the deploy is skipped with a message saying so.

### 🧪 Testing
- 1259 passed at this version (970 at 8.0.0).

### 📝 Documentation
- The philosophy spec's 6.1 and 6.3 describe a recategorise that can work (Stage 7B, the twin of Cayenne's, #45).
- The intake (#42), 7C (#47), book photos (#56) and intake bulk-fix plans carry their `**Merged:**` headers and the rulings made during execution.

## [8.0.0] — 2026-09-12

The recipe edit backend, the recipe source citation, and everything that landed between them: 47 commits over fourteen pull requests (#24–#37) since v7.0.0, plus #38 and #39.

### ⚠️ Requires — apply the Cayenne migrations **before** deploying this version
`SupabaseWriter` puts every new column into **every** recipe INSERT, unconditionally and behind no feature flag. Against a schema missing any of them PostgREST rejects the row with `PGRST204` ("column … does not exist") and `write_recipe_to_supabase` raises `RuntimeError`, so every ingest fails, for every user, on every path — URL, file, and Paprika alike. `REGEN_WORKER_ENABLED` does **not** protect the writer; leaving the flag unset changes nothing here.

- **Cayenne migration 013 (`recipe_edit_columns`)** — `ingredient_lines`, `direction_steps`, `body_rev`, `derived_rev`, `amount_overrides`, and the nine `prep_*` / `cook_*` / `servings_*` duration columns.
- **Cayenne migration `recipe_source_citation` (Cayenne PR #58)** — `source_kind`, `source_key`, `source_title`, `source_author`.
- **Cayenne migration 014 (`regen_rpcs`)** is required in addition before setting `REGEN_WORKER_ENABLED=1`: `claim_stale_recipes` and `regen_failed` do not exist without it and every poll raises. The RPCs' required semantics are in `docs/sql/regen-rpcs.md`; they were implemented, verified against a real Postgres, and applied 2026-09-09 (#27).
- **The live API's environment must carry both `REGEN_WORKER_ENABLED=1` and the service-role key, or the process will not come up** (#34). With the flag set and no service client the server refuses to start with a `RuntimeError` naming `SUPABASE_URL` and the key, instead of answering every request while draining nothing; with the flag unset it warns.

All three migrations are applied to the live project (013 and 014 on 2026-09-09, the citation columns on 2026-09-12). The warnings stand for every other environment.

### ✨ Added — recipe edit backend (#26, #27, #32, #34)
- `StructuredIngredient.line_index`, emitted by REFINE and normalised (in range, unique). The field is optional by design, so an out-of-range or duplicated index degrades that entry to `null` with a warning rather than failing the recipe; the client falls back to `fallback_string` matching (spec 4.3).
- `core/durations.py`: deterministic duration and servings parser; shared fixture `tests/fixtures/duration_cases.json`, byte-identical with the Cayenne client's copy, including the `"2.5 min"` → 2 case that pins round-half-even on both sides.
- Raw `ingredient_lines` / `direction_steps` and structured duration/servings columns carried through ASSEMBLE and written by `SupabaseWriter`.
- `RegenWorker` and `RecatWorker` background workers behind `REGEN_WORKER_ENABLED`, started from the FastAPI lifespan.
- `gemini.categorize_batch()` — categorise-only call for bulk recategorise.
- Ingestion reads `uom_system` / `measure_preference` from `profiles`; request values are the fallback.
- **`/health` publishes `regen_workers`** — `disabled` (the flag is not truthy), `misconfigured` (the flag is on but no service-role client resolves — the silent failure worth naming), or `started`. It initialises to `disabled` at module scope, so a server whose lifespan never ran never claims to have started workers. An empty regen queue looks identical whether the workers are running or were never started, so the state is published rather than inferred, the same argument `auth_mode` already makes (#32).
- `scripts/backfill_durations.py` one-off backfill. Run `--live` 2026-09-10 over 788 rows: `cook_min_minutes` 0 → 199, `prep_min_minutes` 0 → 3, `servings_min` 0 → 69.
- `docs/sql/regen-rpcs.md`: the required semantics and reference SQL for `claim_stale_recipes` / `regen_failed` — the 20-second quiet window, the 5-minute lease, three failures per `body_rev` — restated as what the implementation must keep satisfying now that it exists (#27).

### ✨ Added — recipe source citation (#36)
- Four citation columns — `source_kind`, `source_key`, `source_title`, `source_author` — written on every insert, derived reader-first in `core/citation.py` (`resolve_citation`): a book or an unknown book is settled entirely by the reader (EPUB/PDF metadata); a page keeps its host as the key and takes the site's stated name and byline from the model; with nothing known from the reader, the model's stated source is classified by the same seven rules the backfill uses.
- The EPUB and PDF readers no longer write `"Title — Author"` into `source_url`; the string form stays only as `Citation.display()`, used by the Paprika export.
- `Chunk.citation`, carried alongside `source_url` from every reader through to `assemble()`.
- The extraction model states `stated_source` and `byline` (both `repr=False`, so the refine prompt and the golden corpus are unchanged) and is told never to name itself as the source.
- `scripts/backfill_sources.py` — the seven classification rules applied to existing rows, per-rule counts reported, dry run by default, `--live` writes; `--force` reclassifies rows whose `source_kind` is already set (it overwrites a cook's Set source, so use it knowingly).
- `scripts/restore_source_urls.py` — restores the Paprika archive's per-entry clip URLs that the bulk import dropped, matched by title with `backfill_paprika_metadata`'s rule; ambiguous and unmatched entries are reported, never written. Dry run by default.
- Shared key fixture `tests/fixtures/citation_keys.json`, the contract `normalise_key` and its Cayenne client twin (`cayenne-web/src/lib/domain/citation.ts`) must both satisfy.

### ✨ Added — REFINE emits the other measure for every quantified line (#29)
- One rule in `build_refine_prompt`: always give the equivalent in the other measure, for every quantified volume-or-weight line, **regardless of the cook's Measure Preference**. Previously `converted_*` was filled only when the preference was Weight and the source was Volume, so a line a cook later re-entered in the other measure permanently lost its volume rendering for every reader. No schema change — `converted_amount`, `converted_unit` and `is_ai_converted` already exist — and no backfill: existing rows keep what they have until next regenerated. Only the refine-prompt snapshot moved; the recorded Gemini replies are unchanged.

### ✨ Added — one-off scripts, each already run against the live library
- `scripts/backfill_paprika_metadata.py` (#24) — fills `source`, `notes`, `rating`, `nutritional_info`, `description` and `difficulty` from a Paprika archive onto rows that predate migration 012. Titles are the only key, compared on letters and digits alone; ambiguity is skipped, nulls only, dry run by default, `--archive` and `--user-id` required. Run 2026-09-08: 743 of 788 rows filled, 9 ambiguous, 68 archive entries with no row in the library (reported, never created).
- `scripts/backfill_recipe_images.py` (#28) — uploads the photographs a Paprika archive carries for recipes ingested before the image path existed. Run 2026-09-08: 2 → 417 of 788 recipes with an image; 17 blocked on duplicated titles.
- `scripts/measure_match_band.py` (#31) — **read-only**; measures the cosine band a corpus and embedding model actually produce, so Cayenne's match-strength bar has a floor and ceiling that were measured rather than guessed. Deterministic (`--seed`), twenty fixed queries. Measured 2026-09-10 against `gemini-embedding-001` at 1536 dimensions over 788 recipes: `MATCH_FLOOR = 0.535548`, `MATCH_CEILING = 0.837484`. Committed because the constants expire with the model, the dimensionality, or the embedded text.

### 🐛 Fixed
- **The unparseable-duration fallback tidies its note** (`core/durations.py`, #33) — `parse_duration` and `parse_servings` normalised every successful path but returned the raw input on failure, so a malformed source field reached `prep_note` / `cook_note` / `servings_note` verbatim. Three live rows carried 37,114, 10,242 and 5,562 characters of newline padding in **synced** columns, and the same leak was live on every ingest through `SupabaseWriter`. The fallback now collapses whitespace runs and nothing else — not `_normalise`, which also lowercases and rewrites fraction glyphs, and this note is display text. Worst note after: 60 characters.
- **The refine prompt no longer instructs the model to null a non-nullable boolean** (`gemini.py`, in #29) — `is_ai_converted` is `bool` and the schema handed to Gemini has no null variant, so "leave all three null" asked for exactly what the schema forbids.

### 🧪 Testing
- **The suite runs in parallel by default** (#25): `pytest-xdist` is declared in `pyproject.toml`, the worker count is capped at 6 and scales down on small hosts (xdist's own `auto` counts logical cores and was slower than serial at 16). `--record-gemini`, `--update-goldens` and `--snapshot-update` demote the run to serial, each for a stated reason, and say so.
- **The suite decides its own worker state** (#38): `tests/conftest.py` sets `REGEN_WORKER_ENABLED` empty for the session, so a developer's `.env` carrying `=1` no longer collides with the test-time refusal to build a live service client and fail seven `tests/test_api.py` tests that CI, having no `.env`, never sees.
- 970 passed at this version.

### 📝 Documentation
- The philosophy spec: the five repairs the recipe-edit seams design found and two they imply (#30), and `prep_time` / `cook_time` staying on the server as the duration text as imported, out of sync, never displayed (#35). Both copies — this repository's and Cayenne's — are byte-identical and merged together.
- The extraction goldens, recipe edit backend, and source citation plans carry `**Merged:**` headers recording what execution changed and why (#37, #39) — including the one deliberate override of the recipe edit plan (`line_index` degrades rather than raises) and the migration requirement that three documents had stated backwards until `5d07702`.

---

## [7.0.0] — 2026-09-08

### ⚠️ Model migration — action may be required
- **Generation model moved to `GEMINI_MODEL` and now defaults to
  `gemini-3.1-flash-lite`** (`recipeparser/config.py`) — `gemini-2.5-flash`
  retires **2026-10-16**, and anyone still deployed on v6.0.0 will start
  failing calls after that date. The model name was previously a literal
  repeated at nine call sites (`gemini.py`, `toc.py`, `categories.py`); it
  is now one constant, overridable via the `GEMINI_MODEL` env var without a
  code change. The embedding model gets the same treatment as
  `GEMINI_EMBEDDING_MODEL`, unchanged in value — it is not implicated in the
  retirement. **Upgrading to v7.0.0 is the fix; no other action needed
  unless you were pinning `GEMINI_MODEL` yourself.**

### Added
- Golden test suite (`tests/goldens/`): a seven-file real-input corpus, recorded
  Gemini replies replayed offline, and goldens for readers, prompts, schemas,
  stage parsing, and the full pipeline through both zip writers.

### Fixed
- **`split_large_chunk` falls back to single-newline splitting** (`recipeparser/io/readers/epub.py`) — it previously split only on blank lines, so EPUB chapter text, which carries single newlines, was never split and `MAX_CHUNK_CHARS` had no effect for EPUBs; one real chapter reached 138,167 characters against a 30,000 limit. Behaviour for text that already split on blank lines is unchanged.
- **`HTTP_TIMEOUT_SECS` is applied to Gemini calls** (`recipeparser/gemini.py`) — the constant was defined and documented but referenced nowhere, so `generate_content` had no timeout and a stalled call could hang indefinitely. Now passed as `http_options.timeout` (180 s, expressed as 180000 ms, the SDK's unit).
- **Transient server errors are retried** (`recipeparser/gemini.py`) — only `429`/quota errors went through the exponential back-off ladder; a `500`/`502`/`503`/`504`/`UNAVAILABLE`/`DEADLINE_EXCEEDED`/`INTERNAL` raised on the first attempt instead of retrying. Client errors still raise immediately, and client-side timeouts still raise on the first attempt by design.

### Changed
- `gemini.py` and `toc.py` build their prompts through named functions
  (`build_extract_prompt`, `build_refine_prompt`, `build_table_prompt`,
  `build_plain_text_prompt`, `build_toc_parse_prompt`,
  `build_toc_classify_prompt`). Behaviour is unchanged; the prompts are now
  snapshot-tested.
- **Thinking disabled by default on every Gemini call**
  (`GEMINI_THINKING_BUDGET`, defaults to `0`) — every call in this package is
  a bounded extraction/refinement/classification task with one correct
  answer, not open-ended reasoning, and thinking tokens bill at the output
  rate for no benefit here.
- **Every Gemini reply's `usage_metadata` is now logged**
  (`_log_usage_metadata` in `recipeparser/gemini.py`) — prompt, candidate,
  thinking and total token counts, tagged by call site (extraction,
  refinement, categorisation, TOC parsing, vision OCR, embeddings). Ingestion
  cost was previously only estimable from prompt length; real per-call
  numbers now reach the log.

---

## [6.0.0] — 2026-03-20

### 🐛 Bug Fixes

- **`_select_reader()` unreachable branch** (`recipeparser/adapters/api.py`) — the `elif media_type == "application/epub+zip"` branch was dead code because the preceding `if` block already handled EPUBs and returned early. The branch has been restructured so all media-type routing is reachable and exercised by tests.
- **Deprecated `response_schema` + `response.parsed`** (`recipeparser/gemini.py`) — calls to the Gemini SDK were using the deprecated `response_schema` config key and `response.parsed` accessor, which are removed in `google-genai >= 1.38.0`. Updated to use `config=types.GenerateContentConfig(response_mime_type="application/json", ...)` and `json.loads(response.text)` respectively.

### 🧪 Testing

- **73 tests, 0 failures** — regression tests added for both bug fixes:
  - `tests/unit/test_select_reader.py` — covers all `_select_reader()` branches (EPUB, PDF, text, unknown media type, missing content-type)
  - `tests/test_gemini.py` — covers `generate_content` call signature, JSON parsing path, and schema passthrough

---

## [5.0.0] — 2026-03-17

### 💥 Breaking Changes

- **Layered architecture refactor** — the monolithic `recipeparser/` flat layout has been replaced with a clean three-layer structure. Any code importing directly from old module paths must be updated:
  - `recipeparser.gemini` → `recipeparser.core.engine` (orchestration) / `recipeparser.core.providers` (LLM/embedding ABCs)
  - `recipeparser.pipeline` → `recipeparser.core.fsm` (FSM) + `recipeparser.core.engine` (pure logic)
  - `recipeparser.supabase_writer` → `recipeparser.io.writers.supabase`
  - Category sources: `recipeparser.io.category_sources.{yaml_source,paprika_db,supabase_source}`
- **`POST /ingest` replaced by `POST /jobs`** — the API now uses a fire-and-forget job pattern. The endpoint returns `202 Accepted` with `{ "job_id": "uuid" }` immediately; the completed recipe is written directly to Supabase by the worker. Callers must poll `GET /jobs/{job_id}` for status.
- **`categories` field removed from `CayenneRecipe`** — category assignment is now handled by the multipolar grid system and written to the `recipe_categories` junction table in Supabase. The flat `List[str]` field is no longer returned in the API response.

### ✨ New Features

#### Multipolar Grid Categorization
- Recipes are now categorized against a **user-defined set of axes** (e.g., "Cuisine", "Protein", "Meal Type"), each with its own list of valid tags.
- The LLM receives a **dynamically generated Pydantic schema** (via `create_model()`) that enforces the exact tag vocabulary per axis — hallucinated tags are structurally impossible.
- **Zero-tag mandate**: the LLM returns `[]` for any axis that doesn't apply to the recipe; a post-validation pass strips any tags that slipped through.
- **0–2 tags per axis** — recipes are never over-categorized; the constraint is enforced both in the prompt and in the response schema.
- Categorization is merged into the existing **refinement pass** (Fat Tokens + UOM + Categories in a single Gemini call), eliminating a separate API round-trip.
- Results are written to the `recipe_categories` junction table in Supabase, partitioned by `user_id` for PowerSync compatibility.

#### `CategorySource` ABC (Pluggable Taxonomy)
- New abstract base class `recipeparser.io.category_sources.base.CategorySource` with a single `load() -> MultipolarGrid` method.
- Three built-in implementations:
  - `YamlCategorySource` — loads axes + tags from a local `categories.yaml` file (default for CLI/GUI)
  - `PaprikaDbCategorySource` — reads live taxonomy from Paprika 3's SQLite database
  - `SupabaseCategorySource` — fetches the authenticated user's category tree from Supabase (used by the API adapter)
- The engine accepts any `CategorySource` implementation — new sources can be added without touching core logic.

#### Layered Architecture (`recipeparser/core/` + `recipeparser/io/`)
- **`recipeparser/core/engine.py`** — pure `RecipeEngine` orchestrator with zero I/O; accepts reader, writer, and category source as injected dependencies.
- **`recipeparser/core/fsm.py`** — `ExtractionFSM` state machine (externalized, observable); fires callbacks on every state transition for adapter-level progress reporting.
- **`recipeparser/core/providers/`** — `LLMProvider` and `EmbeddingProvider` ABCs with a `GeminiProvider` implementation; swappable without touching the engine.
- **`recipeparser/io/readers/`** — `EpubReader`, `PdfReader`, `UrlReader`, `PaprikaReader` (source adapters).
- **`recipeparser/io/writers/`** — `SupabaseWriter`, `CayenneZipWriter`, `PaprikaZipWriter` (output adapters).
- **`recipeparser/adapters/`** — thin CLI, GUI, and API wrappers that wire readers/writers/sources to the engine.

#### Fire-and-Forget Job API (`recipeparser/adapters/api.py`)
- `POST /jobs` — accepts `{ url?, text?, uom_system?, measure_preference? }`, enqueues a background worker, returns `202 { "job_id": "uuid" }` immediately.
- `GET /jobs/{job_id}` — returns current job status: `pending | running | done | error`, FSM stage, `progress_pct`, `recipe_count`, and `error_message`.
- Job state is written to the `ingestion_jobs` table in Supabase; PowerSync syncs it to the mobile app in real time — zero polling from the client.
- `.env` is excluded from the Docker image (`.dockerignore` updated); `DISABLE_AUTH=1` environment variable added for CI test jobs.

#### Live End-to-End Test Suites
- Three standalone live E2E scripts (excluded from standard `pytest` run; require a running Docker server):
  - `tests/live_api_test.py` — exercises `POST /jobs` + `GET /jobs/{id}` against a live container
  - `tests/live_cli_test.py` — runs the CLI adapter end-to-end with a real Gemini API call
  - `tests/live_gui_test.py` — drives the GUI adapter headlessly through a full parse run
- `pyproject.toml` updated: `python_files = ["test_*.py"]` ensures `live_*` scripts are never picked up by the standard test runner.

### 🔧 Improvements

- **`toc.py` bare `Link` crash fixed** — `toc.py` now handles EPUB `Link` nodes that have no `title` attribute without raising `AttributeError`.
- **Docker `.env` exclusion** — `.dockerignore` updated to prevent `.env` from being baked into the image; secrets are injected at runtime via environment variables.
- **`DISABLE_AUTH` CI flag** — GitHub Actions CI test job sets `DISABLE_AUTH=1` so the containerised API accepts unauthenticated requests during automated testing without requiring a live Supabase JWT secret.

### 🧪 Testing

- **384 tests, 0 failures** (up from 356 in v3.0.0)
- New test coverage:
  - Multipolar grid schema generation and zero-tag validation
  - `CategorySource` ABC implementations (YAML, Paprika DB, Supabase)
  - Fire-and-forget job API (`POST /jobs`, `GET /jobs/{id}`, background worker lifecycle)
  - `RecipeEngine` with injected mock dependencies (pure unit tests, zero I/O)
  - `ExtractionFSM` state transition invariants
- **10/10 live E2E tests passing** against a running Docker container (API, CLI, GUI adapters)

### 📦 Architecture Summary

```
recipeparser/
├── core/
│   ├── engine.py          ← RecipeEngine orchestrator (pure — no I/O)
│   ├── fsm.py             ← ExtractionFSM state machine
│   └── providers/         ← LLMProvider + EmbeddingProvider ABCs + GeminiProvider
├── io/
│   ├── readers/           ← EpubReader, PdfReader, UrlReader, PaprikaReader
│   ├── writers/           ← SupabaseWriter, CayenneZipWriter, PaprikaZipWriter
│   └── category_sources/  ← CategorySource ABC + YAML / PaprikaDB / Supabase impls
└── adapters/              ← CLI, GUI, API thin wrappers
```

---

## [3.0.0] — 2026-03-12

### ✨ New Features

#### Cayenne Ingestion API (`recipeparser/api.py`)
- New **FastAPI service** exposing two endpoints designed for the Project Cayenne mobile app:
  - `POST /ingest` — full 3-step pipeline: extract recipes from raw text or PDF → refine into structured Cayenne schema → embed with `text-embedding-004`
  - `POST /embed` — standalone query vectorisation for semantic search
- **Supabase JWT authentication** (HS256 via PyJWT) on all endpoints; unauthenticated requests are rejected with `401`
- URL ingestion reserved (`400 Not Yet Implemented`) — groundwork laid for Phase 2

#### Cayenne Refinement Pass (`recipeparser/gemini.py`)
- New `refine_recipe_for_cayenne()` function powered by **Gemini 2.5 Flash** (upgraded from 2.0 Flash for native thinking support)
- Produces fully structured `CayenneRecipe` output:
  - `StructuredIngredient` list with `id`, `amount`, `unit`, `name`, `fallback_string`, `converted_amount`, `converted_unit`, `is_ai_converted`
  - `TokenizedDirection` list using **Fat Token** format (`{{ing_01|fallback text}}`) — ingredient references embedded directly in direction text for deterministic math-scaling
  - AI-powered Volume-to-Weight conversion flagged with `is_ai_converted` for UI transparency
- New `get_embeddings()` using `text-embedding-004` (1536-dimensional vectors, compatible with `pgvector` / `sqlite-vec`)

#### Pipeline Resumability (`recipeparser/pipeline.py`)
- **Checkpoint persistence** — pipeline state (completed segment indices) saved to `<output_dir>/.recipeparser_checkpoints/<book_hash>.json` after each segment; automatically resumed on re-run of the same book
- **Cooperative pause/resume** — `PipelineController.check_pause_point()` called between segments; orchestrator-level pause guard handles the race condition where a worker transitions `PAUSING → PAUSED` before the orchestrator checks
- **FSM correctness fix** — `transition("done")` now called at end of `process_epub` so the controller correctly reaches `IDLE` on successful completion
- **Rate-limit auto-pause** — `PipelineController` tracks RPM consumption and automatically pauses + resumes when the Gemini free-tier window resets

### 🔧 Improvements

- **Gemini 2.5 Flash** used for the refinement pass (was 2.0 Flash); native thinking mode improves structured output accuracy
- **Docker smoke test** (`tests/smoke_test_docker.py`) added to CI; validates the containerised API starts and responds correctly
- **Dockerfile dependencies** updated to match `requirements.txt` (FastAPI, Uvicorn, HTTPx, PyJWT)

### 🧪 Testing

- **356 tests, 0 failures** (up from 350 in v2.2.0)
- New test modules:
  - `tests/test_api.py` — 43 tests covering `/ingest` and `/embed` endpoints, auth, error paths, schema validation, and UOM passthrough
  - `tests/test_gemini_cayenne.py` — 4 tests for `get_embeddings` and `refine_recipe_for_cayenne`
  - `tests/test_gui.py` — 6 tests for `_parse_run_config` logic (free-tier / paid-tier concurrency rules)
  - `tests/test_pipeline_resumability.py` — 3 integration tests: checkpoint save/load, cancel, and pause/resume
- **Headless GUI test support** — `conftest.py` now injects lightweight `tkinter` / `customtkinter` stubs into `sys.modules` when the C extension is unavailable (e.g. PlatformIO's embedded Python), allowing GUI logic tests to run in any environment without a display

### 🔒 Security

- API key (`GOOGLE_API_KEY`) never exposed in responses or logs
- Supabase JWT secret validated server-side; all ingestion requests require a valid bearer token

### 📦 Dependencies Added

| Package | Version | Purpose |
|---|---|---|
| `fastapi` | ≥ 0.115.0 | Cayenne Ingestion API |
| `uvicorn` | ≥ 0.30.0 | ASGI server for FastAPI |
| `httpx` | ≥ 0.27.0 | Async HTTP client (test client) |
| `PyJWT` | ≥ 2.8.0 | Supabase JWT verification |

---

## [2.2.0] — 2026-03-08

### ✨ New Features

- **Folder processing** (`recipeparser folder <dir>`) — batch-process all EPUBs and PDFs in a directory
- **`PipelineController` FSM** — Finite State Machine wrapping the pipeline with states `IDLE → RUNNING → PAUSING → PAUSED → RESUMING → RUNNING → DONE`; GUI Pause/Resume/Cancel buttons wired to FSM transitions
- **Rate-limit auto-pause** — when RPM budget is exhausted, pipeline automatically pauses and resumes after the Gemini rate-limit window resets (no manual intervention required)
- **`recategorize` command** — re-run AI categorisation on an existing `.paprikarecipes` export without re-parsing; produces a new archive with updated categories
- **Export merge** (`recipeparser merge`) — deduplicate and merge multiple `.paprikarecipes` archives into one; accent- and case-insensitive deduplication

### 🔧 Improvements

- `PipelineController` checkpoint subdir renamed to `.recipeparser_checkpoints` (hidden directory)
- GUI concurrency spinner disabled when free-tier checkbox is active
- CLI `--concurrency` clamped to 1–10; `--rpm` passed through to rate limiter

### 🧪 Testing

- 350 tests, 0 failures
- New: `test_pipeline_controller.py` (561 lines), `test_merge_exports.py`, `test_recategorize.py`, `test_cli.py` expansions

---

## [2.1.0] — 2026-02-xx

### ✨ New Features

- **PDF support** — text-based PDFs extracted via PyMuPDF; scanned PDFs fall back to Gemini Vision OCR (page-by-page)
- **TOC extraction** — programmatic EPUB/PDF table of contents used to segment books by recipe title; AI TOC classification fallback when no programmatic TOC is available
- **Recon report** — post-run reconciliation compares TOC entries against extracted recipe names; highlights missed or extra recipes
- **Run summary** — printed at end of each run: total segments, extracted recipes, skipped segments, elapsed time

---

## [2.0.x] — 2026-01-xx

### 2.0.6
- RPM rate limit (`--rpm`) and concurrency cap (`--concurrency`) CLI flags
- Free-tier GUI checkbox (5 req/min, concurrency=1)

### 2.0.5
- First fully-tested 4-job CI pipeline: test → build → smoke-test → release
- GitHub Actions builds Windows installer automatically on `v*` tag push

### 2.0.4
- GitHub Actions automated installer build

### 2.0.3
- Build requires python.org Python (tkinter bundled)

### 2.0.2
- Fix customtkinter packaging for PyInstaller

### 2.0.1
- User data stored in writable paths (`%APPDATA%` / `~/.local/share`)
- Minimal default `categories.yaml` shipped with installer

### 2.0.0
- **Paprika DB category sync** — `recipeparser --sync-categories` reads live taxonomy from Paprika 3's SQLite database
- GUI Categories tab with two-panel editor (parent / subcategory)
- CLI `--sync-categories` flag

---

## [0.2.0] — 2025-12-xx

- CustomTkinter GUI with Parse tab, log panel, progress bar, Pause/Cancel controls
- Windows installer (Inno Setup + PyInstaller)

---

## [0.1.0] — 2025-11-xx

- Initial working implementation: EPUB → Paprika 3 recipe export
- Parallel extraction with `ThreadPoolExecutor`
- Category taxonomy via `categories.yaml`
- Hero image injection into Paprika export
- Calibre folder path support
