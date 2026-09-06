# Extraction goldens — design

**Date:** 2026-09-06
**Status:** approved design, awaiting implementation plan
**Base:** master, after PR #13 (ingestion fixes) merges

## 1. Problem

Nothing in the RecipeParser suite compares a real input against a checked-in
expected output. The five Syrupy tests in `tests/snapshots/` patch Gemini and
snapshot the fixture they were fed, so they only catch a field added to or
removed from a Pydantic model. Every reader test builds books from
`MagicMock`. Every stage test hands the stage a hand-built object, so the
parse path (`json.loads`, `model_validate`, string-to-list coercion, dynamic
grid schema round-trip, clean-grid tag stripping) has never seen real Gemini
output. The prompts in `recipeparser/gemini.py` are the actual extraction
logic and no test reads them. The live scripts under `tests/` hit real Gemini,
assert nothing, and never run in CI.

A gap survey on 2026-09-06 found six gaps. This spec closes four of them:

1. No real-input corpus and no reader goldens.
2. No recorded Gemini replies and no stage replay.
3. No prompt or schema snapshots.
4. No end-to-end golden through the writers.

Deferred (see §9): a live quality eval, and migrating the older mock-based
tests onto the corpus.

## 2. Goals and non-goals

**Goals**

- Lock the output of every deterministic step (readers, prompt rendering,
  schema generation, writers) against real inputs.
- Exercise the real `gemini.py` code, including the PR #13 parse retry, with
  real recorded replies, fully offline.
- Run on every push in the existing CI unit-test job with no network access.
- Make drift visible as a reviewable diff: prompt, recorded reply, and
  resulting output move together in one commit.

**Non-goals**

- Asserting Gemini's live output quality. Recorded replies are data; a model
  update cannot break the suite until someone deliberately re-records.
- Replacing the existing unit, shape-snapshot, or live tests. None are
  deleted or changed.
- HTTP-level cassettes. The google-genai transport makes them brittle and the
  fixtures unreadable.

## 3. Layout

Everything lives in one package so it is one directory to reason about and
one pytest path to run:

```
tests/goldens/
  conftest.py                 --record-gemini / --update-goldens flags,
                              golden_client fixture, corpus path helpers
  golden_client.py            GoldenClient (record + replay)
  corpus/                     real inputs (§4), plus README.md with provenance
                              and licence per file
  readers/<fixture>.json      expected reader output (§6.1)
  gemini/<fixture>/<chunk>/   recorded replies (§5)
  __snapshots__/              syrupy files for prompts, schemas, stage output
  e2e/<fixture>/              expected IngestResponse list and zip manifests
  test_readers_golden.py
  test_stages_golden.py
  test_prompts_snapshot.py
  test_e2e_golden.py
```

The corpus stays under 2 MB in total so checkout time is unaffected.

## 4. Corpus

Seven files, each chosen for one hard case the current suite never sees.
Sources are a mix of public-domain material and short hand-rebuilt excerpts.
Hand-rebuilt excerpts keep only the layout features, carry no author or book
name, and are at most two recipes long, which limits copyright exposure to a
couple of ingredient lists.

| Fixture | Source | Hard case it covers |
|---|---|---|
| `gutenberg-multi.epub` | Project Gutenberg cookbook, trimmed to about six chapters | Real nav/NCX TOC; front matter the candidate filter must reject; several recipes per chapter |
| `dual-units.epub` | Hand-rebuilt excerpt, two recipes | Dual-unit lines such as "2 cups/250g"; hero image before the title; hero image after the ingredient list |
| `phases-bakers.epub` | Hand-rebuilt excerpt, two recipes | Multi-phase bread recipe; baker's percentage table that triggers `needs_table_normalisation` |
| `text-pages.pdf` | Built from the Gutenberg text, four pages, one embedded image | PDF preflight pass; page chunks with image markers; PDF outline TOC |
| `scanned.pdf` | Two rendered page images, no text layer | Preflight failure; vision OCR fallback |
| `saved-page.html` | One saved recipe web page | `UrlReader` through `html_to_text`, with `requests.get` patched to serve the file |
| `legacy-photo.paprikarecipes` | Built in code from one Gutenberg recipe plus a photo | Paprika legacy reader with `photo_data`, which PR #13 touches |

`corpus/README.md` lists, for every file, where it came from, its licence,
what was trimmed or rebuilt, and which hard case it exists for. A file with
no README entry is a review failure.

## 5. GoldenClient

One class in `tests/goldens/golden_client.py` exposing the surface the code
already calls: `client.models.generate_content(model, contents, config)` and
`client.models.embed_content(model, contents, config)`. Because every stage
and every `gemini.py` function takes a `client` argument, no production code
changes are needed to inject it.

### 5.1 Modes

- **Replay (default).** Serves recorded files. No network.
- **Record (`pytest --record-gemini`).** Builds a real `google.genai.Client`
  from `GOOGLE_API_KEY`, forwards each call, and writes the reply before
  returning it. Record mode without a key fails at fixture setup with a clear
  message. Record mode never runs in CI.

The `golden_client(fixture_id)` fixture in `conftest.py` returns the client
for one corpus fixture.

### 5.2 Keys

Recordings are keyed by the identity of the work unit in the prompt, not by
a global call counter, so replay is stable at any pool size:

```
gemini/<fixture>/<body-sha8>/<stage>-<nn>.json
```

- `body-sha8` is the first eight hex digits of the SHA-256 of the prompt
  body: everything after the prompt's fixed marker ("Text chunk:", "Text:",
  "RAW RECIPE:", or the whole prompt for vision, TOC, and connectivity
  calls). For extract and table calls the body is the chunk text; for refine
  it is the raw recipe repr; for vision it is the constant OCR instruction.
  Every call for one body happens on one worker thread in a fixed order, so
  per-body ordinals are stable regardless of how the thread pool interleaves
  chunks.
- `stage` comes from a prompt sniff on the first line of `contents`:
  `extract` ("culinary data extractor"), `refine` ("culinary data refiner"),
  `table` ("baker's percentage"), `toc-parse` and `toc-classify` (their own
  headers), `vision` (the OCR prompt), `connectivity` ("Reply with the single
  word OK"). An unrecognised prompt fails loudly in both modes.
- `nn` counts calls per stage for that body. A PR #13 parse retry is
  therefore `extract-00.json` (the reply that would not parse) and
  `extract-01.json` (the retry). Each recipe's refine call lands in its own
  body directory as `refine-00`. Vision pages share one body and count up in
  page order, since OCR runs pages sequentially on one thread.
- After table normalisation the extract call carries the normalised text, so
  it keys under a different body than the `table` call that produced it.

Vision calls hash the text part only, since the image bytes are corpus
files.

### 5.3 File shape

```json
{
  "stage": "extract",
  "ordinal": 0,
  "model": "gemini-2.5-flash",
  "config": { "response_mime_type": "application/json", "temperature": 0.1 },
  "prompt_sha256": "…",
  "response_text": "…"
}
```

`config` omits `response_json_schema`; the schema is covered by its own
snapshot (§6.3). `response_text` is stored verbatim, including any reply that
did not parse, so the retry path replays faithfully.

### 5.4 Replay behaviour

- Returns an object with a `.text` attribute, which is all `gemini.py` reads.
- A missing file fails the test with the exact path expected. Never a silent
  skip.
- A `prompt_sha256` mismatch on replay is a warning, not a failure. Prompt
  drift is the prompt snapshot's job (§6.3); replay's job is to serve data.
- The replay client never raises, so `_call_with_retry` passes straight
  through.

### 5.5 Embeddings

Never recorded. Both modes return a deterministic 1536-float vector seeded
from the SHA-256 of the input text. The embed stage has no parse logic worth
locking, and 1536 floats per recipe would bloat the fixtures. The assemble
stage still receives a real-length vector.

### 5.6 Concurrency

Record mode takes a lock only around the file write. Replay is read-only.
Ordinals are per body, so no cross-thread counter exists. The end-to-end
test runs at the production pool size (§6.4).

## 6. Test families

### 6.1 Reader goldens (`test_readers_golden.py`)

One parametrised test per corpus file. It instantiates the matching reader
(`EpubReader`, `PdfReader`, `UrlReader`, `PaprikaReader`), reads the file,
and serialises the result to:

```json
{
  "chunks": [ { "input_type": "epub", "source_url": "…", "text": "…" } ],
  "qualifying_images": [ "…sorted filenames…" ]
}
```

That compares exactly against `readers/<fixture>.json`. `--update-goldens`
rewrites the expected file. The URL fixture patches `requests.get` to serve
`saved-page.html`. The scanned PDF's reader golden asserts only that
preflight raises with the expected message; OCR belongs to the stage layer.
No Gemini calls occur in this family.

### 6.2 Stage replay goldens (`test_stages_golden.py`)

For each fixture: load its reader golden, feed each chunk through `extract`
with the GoldenClient, feed each extraction through `refine` with a fixed
axes dict (`{"Cuisine": [...], "Meal Type": [...]}` declared once in
`conftest.py`), then through `categorize`. The parsed models serialise via
`model_dump()` and compare against a syrupy snapshot, which shows a readable
diff when a parse rule or model field changes.

This is the first family to exercise `json.loads`, `model_validate`,
string-to-list coercion, dynamic grid schema round-trip, clean-grid tag
stripping, and fat-token validation with real Gemini output. The
`phases-bakers` fixture also covers `needs_table_normalisation` and
`normalise_baker_table` via a recorded `table-00.json`. The `scanned.pdf`
fixture covers `extract_text_via_vision` via recorded `vision-nn.json` files.

### 6.3 Prompt and schema snapshots (`test_prompts_snapshot.py`)

Small refactor in `gemini.py` and `toc.py`: pull the inline f-strings into
pure functions, and make the call sites use them so behaviour is unchanged.

| Builder | Module |
|---|---|
| `build_extract_prompt(text, units)` | gemini.py |
| `build_plain_text_prompt(text)` | gemini.py |
| `build_refine_prompt(raw, uom_system, measure_preference, axes)` | gemini.py |
| `build_table_prompt(text)` | gemini.py |
| `build_toc_parse_prompt(...)` | toc.py |
| `build_toc_classify_prompt(...)` | toc.py |

Snapshot tests render each builder with a short placeholder body across this
matrix: the four `units` modes; plain text; refine with no axes, with axes,
and with `measure_preference="Weight"`; both TOC prompts. The placeholder
body is fixed text, so the snapshot changes only when the prompt template
changes.

The same file snapshots `_schema_for_gemini` for `RecipeList`,
`CayenneRefinement`, the dynamic grid model from `_build_dynamic_grid_schema`
with the fixed axes, and the TOC models. This is the only guard that Gemini's
`additionalProperties` rejection cannot creep back.

### 6.4 End-to-end goldens (`test_e2e_golden.py`)

For three fixtures (`dual-units.epub`, `phases-bakers.epub`,
`legacy-photo.paprikarecipes`): read the file, run the chunks through
`RecipePipeline` with the GoldenClient at the pipeline module's own pool size
(`MAX_CONCURRENT_API_CALLS` in `core/pipeline.py`, currently 4), then through
`PaprikaWriter` and `CayenneZipWriter` into `tmp_path`.

Comparison:

- The `IngestResponse` list is sorted by title and compared as a multiset
  against `e2e/<fixture>/ingest.json`, so order independence is asserted
  explicitly. A regression that made output depend on completion order fails.
- Each zip compares against `e2e/<fixture>/paprika.json` and
  `e2e/<fixture>/cayenne.json`: a manifest of entry names plus the parsed
  JSON inside each entry.
- Normalisation before comparison: `uid`, `hash`, and `created` become
  fixed placeholders; `photo_data` becomes its SHA-256; the embedding becomes
  its length plus SHA-256.

Running at pool size 4 puts the `ThreadPoolExecutor` path, the per-chunk
error boundary, and the progress callback under golden coverage. Today only
the routing tests touch them, with every stage patched.

## 7. CI and maintenance

- **CI.** The existing unit-test job runs `pytest tests`, so replay goldens
  run on every push with no workflow change.
- **Re-recording.** Only when a prompt or model changes on purpose. Run
  `pytest tests/goldens --record-gemini`, review the diff under `gemini/`
  and the resulting stage and e2e changes, and commit all of it together so
  a reviewer sees prompt, reply, and output move as one. A failing prompt
  snapshot is the signal that a re-record is due.
- **Updating expected files.** `--update-goldens` rewrites `readers/` and
  `e2e/`. Syrupy's `--snapshot-update` handles prompts, schemas, and stage
  output. Both are opt-in and documented in `corpus/README.md`.
- **Determinism.** No network, seeded embeddings, per-chunk keys, multiset
  comparison. The golden tests are independent and use `tmp_path`, so
  pytest-xdist could parallelise them later; it is not added here.
- **Existing tests.** `tests/snapshots/` and the live scripts stay as-is. The
  only production change is the prompt-builder refactor in §6.3.

## 8. Error handling summary

| Situation | Behaviour |
|---|---|
| Replay file missing | Test fails naming the expected path |
| Record mode, no `GOOGLE_API_KEY` | Fixture setup fails with a clear message |
| Unrecognised prompt header | Fails in both modes |
| `prompt_sha256` mismatch on replay | Warning only |
| Corpus file with no README entry | Fails a cross-check test in `test_readers_golden.py` that compares `corpus/` against the README table |
| Gemini reply that will not parse | Stored verbatim; retry replays from the next ordinal |

## 9. Deferred

- **Live quality eval.** Run the corpus against real Gemini on demand and
  report `run_recon` results with tolerance (expected titles, ingredient
  counts, hero photo filenames). Reported, not failed.
- **Broader OCR coverage.** More than the one scanned fixture.
- **Migrating older mock-based reader and TOC tests** onto the corpus.
- **Supabase writer cassette.** Out of scope; the writer is covered by its
  existing httpx-mocked tests.

## 10. Sequencing

Implement after PR #13 merges so replies are recorded once against the code
that ships. Within the plan, the corpus (§4) and GoldenClient (§5) come
first; the four test families (§6) depend only on those two and can be built
in parallel.
