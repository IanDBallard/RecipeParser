# Changelog

All notable changes to RecipeParser are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [9.52.0] — 2026-11-28

The shopping classifier estimates the grams a shopper buys, counts fresh herbs in bunches and counts whole produce whatever measure the recipe uses (Cayenne batch 90, F-882 and F-883).

### ✨ Added
- The classify prompt gains a `grams` rule: the weight bought at the amount given, set for a solid (spoonfuls included), a herb measure or counted produce or meat, and null for a liquid, a food bought by volume, eggs, an unmeasured line and a container counted whole (a tin).
- Fresh herbs are counted in `bunch`, `count` being the fraction of a supermarket bunch, never `sprig` or `leaf`; dried herbs have no count. Produce sold as separate items is counted whatever measure the recipe uses ("1/2 cup diced shallot" is a shallot count). `count` is null for a food bought by weight or volume, never an invented pack ("1 block" of cheese). `grams` is for the food as bought (not cooked) and never 0.

### 🧪 Tests
- `tests/goldens/test_classify_golden.py` gains goldens for gruyere grams, shallot by the cup, herbs in bunches, dried herbs, no grams for liquid, tin, eggs and a garnish, counted meat, and meat and mince by weight. All classify goldens re-recorded with this prompt (owner approved 2026-11-28).

## [9.51.4] — 2026-11-28

Every package the service imports is declared, and every version it runs is locked (architecture compliance roadmap, Phase 3b). No change to the package's behaviour.

### 🔧 Changed
- `requests` and `postgrest` are declared in `pyproject.toml`; both were imported but arrived only through other packages. `google-genai`'s minimum is 1.60, as `requirements.txt` already had it.
- `requirements.txt` is generated from `pyproject.toml` by `scripts/ci/lock_requirements.py` and pins every package, transitive ones included. The versions are the ones the production image built on 2026-11-28 installed. Until now most moved to the newest release at every image build.
- A `dependencies` CI job fails when the lock is stale, or when `recipeparser/` imports an undeclared package (`deptry`).

### 📝 Docs
- `docs/architecture/ACF_ROADMAP.md`: the item is done, and its proposal to replace `requests` with `httpx` is rejected: Dotdash Meredith's sites refuse `httpx` through the page proxy (Cayenne F-687).
- `README.md`: "Dependency Management" describes the lock.

## [9.51.3] — 2026-11-28

Two more project rules are checked on every pull request, both by ruff (architecture compliance roadmap, Phase 3). No change to the package's behaviour.

### 🔧 Changed
- `layers.model-sdk-owners`: ruff's banned-api (`TID251`) now refuses a `google.genai` or `anthropic` import outside the modules that use one today (`gemini.py`, `core/picture_gen.py`, `adapters/ai_search/**`, the entry points that build a client, and the adapters that type or catch it). Tests and scripts are exempt.
- `layers.one-clock`: ruff `DTZ` is on under `recipeparser/`, so a naive datetime cannot be added. The one existing case, the local resume time in `core/quota_wait.py`'s quota-pause message, is now aware (`datetime.now().astimezone()`); the message reads the same.
- The core layer ban moves from a "everything but core" file ignore in `pyproject.toml` to `recipeparser/core/ruff.toml`, so the two `TID251` bans can have different scopes. ruff's other findings are unchanged (222 before and after).

### 🐛 Fixed
- Host detector files (`config/architecture/detectors/*.py`) check out with LF on every platform (`.gitattributes`). A fresh Windows checkout wrote them with CRLF, so their SHA-256 no longer matched the approved bytes and the pre-push gate refused with a stale approval.

### 📝 Docs
- `config/architecture/ARCHITECTURE.md` gains "Model SDK owners" and "One clock", each with a recount; "Layer imports" names where the ruff ban lives.
- `docs/architecture/ACF_ROADMAP.md`: both rules marked done, with the corrected owner list and scope.

## [9.51.2] — 2026-11-28

A broad exception handler can no longer be added that swallows a job cancellation or an exhausted daily quota (architecture compliance roadmap, Phase 3). No change to the package's behaviour.

### 🔧 Changed
- New mandate `fail-loud.quota-cancel-propagate`, checked on pull requests and before every push by a host detector, `config/architecture/detectors/quota_cancel.py`. On an `except` line a change adds, a bare, `Exception`, `BaseException` or `RecipeParserError` clause must re-raise, or follow a clause of the same `try` that re-raises `JobCancelledError` and `DailyQuotaExhaustedError`. A clause that names either error itself is allowed. Exemption: `# fail-loud.quota-cancel-ok: <reason>`.
- The 88 existing handlers that do not let them through are not flagged until their `except` line is edited.

### 📝 Docs
- `config/architecture/ARCHITECTURE.md` gains "Quota and cancel propagate": the rule, the re-raise pattern, approval, and a recount snippet.
- `docs/architecture/ACF_ROADMAP.md`: status and "What is checked today" brought up to date (Phases 0 to 2 done, Phase 3 started); Phase 0's fix PRs and baseline re-run marked done.

**For developers:** host detectors run locally only after an approval. Run `/acf-approve` once in this repository (it covers every worktree) before your next push; CI needs nothing.

## [9.51.1] — 2026-11-27

The shopping classifier's food leaves out words that only name a grade, grain, origin or brand of one food (Cayenne F-891), so "Maldon salt" or "granulated sugar" no longer reaches the list as its own food.

### 🐛 Fixed
- The classify prompt gains a rule over that class of words, not a list of foods: "kosher salt", "Maldon salt", "flaky sea salt" and "Diamond Crystal kosher salt" are "salt", "granulated sugar" is "sugar", "freshly ground black pepper" is "black pepper", and the rule holds for foods it does not name. Cayenne batch 87's variant classes (`FOOD_VARIANT_CLASSES`, #652) stay the predictable floor for salt and black pepper.
- A word that makes it another purchase stays, and the prompt names the ones a word-level rule would wrongly fold: celery salt, smoked paprika, caster sugar, brown sugar, white pepper; sea bass, flaky pastry, granulated garlic, fine green beans; Dijon mustard, Greek yogurt, Thai basil.
- The functionally-distinct guardrail's third example, "table salt is not flaky finishing salt", becomes "celery salt is not salt": batch 87 folds flaky sea salt into salt (owner-approved 2026-11-27).

### 🧪 Tests
- `tests/goldens/test_classify_golden.py` gains a fold set and a keep set. **Not recorded yet:** both skip until someone with a real key runs `pytest tests/goldens/test_classify_golden.py --record-gemini-missing -n0` (two paid calls). The other classify recordings replay with a prompt-drift warning.
- Labels already stored keep their old food until they are classified again.

## [9.51.0] — 2026-11-27

`scripts/backfill_f888.py` brings recipes stored before 9.48.1 and 9.50.1 in line with the parser fixes new imports have (Cayenne F-888). No change to the package's behaviour.

### ✨ Added
- Four classes, run one at a time. `sources`: a source title that is a web address becomes the page's host (F-878). `sizes`: a size in brackets becomes the line's second measure by `fill_bracketed_size` (F-858, as fixed by F-892). `totals`: a cook time taken whole from a stated total loses the prep (F-877). `fetch-times`: a web recipe with no cook time gets its page's JSON-LD times, one fetch a second, no model, filling only what is empty (F-877).
- The script only reads and plans. `--sql` writes one transaction: a guarded UPDATE per row (every column written must still hold its planned-from value), a verification row, and ROLLBACK, or COMMIT with `--commit`. `--commit` needs `--record`, which writes the before-image and is never overwritten. `restore <record> --sql` writes the reverse transaction. `--skip-record` keeps `totals` from repairing a row twice. No body column is written, so nothing regenerates or re-embeds.

## [9.50.1] — 2026-11-27

A size in brackets after the food's name is the size of each item only when the count is 1 or the bracket says "each" (Cayenne F-892).

### 🐛 Fixed
- 9.48.1 (Cayenne F-858) took a lone size after the food's name as each item's whatever the count, so "9 large egg whites (about 280-300 gms)" stored 300 g per egg white and "3 ripe Bosc pears (about 1 pound)" 1 lb per pear. Such a size is now kept only on a count of exactly 1 ("1 standing rib roast (about 8 lb)") or when it says each ("4 pork chops (about 8 ounces each)"); otherwise the line keeps no second measure, as before 9.48.1. A size straight after the count ("2 (15-ounce) cans") is unchanged.
- `fill_bracketed_size(ing)` is the per-line rule, shared with the F-888 backfill of stored recipes.

## [9.50.0] — 2026-11-27

A batter pudding is its own bake type, `batter-pudding`, chosen by how the batter is made, not by the title (Cayenne F-890). **Deploy note:** needs the Cayenne PR for F-890 deployed first: its migration admits `batter-pudding` to `recipes_bake_type_check`, and without it a recipe typed `batter-pudding` is refused on write.

### ✨ Added
- `BAKE_TYPES` gains `batter-pudding` after `quick-bread` (now 15). Cayenne gives it a quick bread's tins and band but its own doneness cue: risen, deep golden and crisp, with the oven door kept shut. Cayenne #648 had given that cue only to a quick-bread whose title said Yorkshire, popover, toad in the hole or Dutch baby.

### 🔧 Changed
- Bake-type step 6 is `batter-pudding / quick-bread`: `batter-pudding` when a thin pouring batter of eggs, milk and flour with no baking powder or soda is poured into hot fat or a hot tin and puffs in a hot oven (Yorkshire puddings, popovers, toad in the hole, Dutch baby), whatever it is called; otherwise `quick-bread`. The fixed order of D29 is unchanged. Before, popovers, Yorkshire puddings and toad in the hole were `quick-bread` on purpose.
- The bake-type probe set's Toad in the Hole and Perfect Yorkshire Puddings move from `quick-bread` to `batter-pudding`. The live probe (`scripts/probe_bake_types.py --runs 2`) gave both `batter-pudding` in both runs; 76/79 cases as expected, the three misses not batter puddings.
- Stored recipes keep `quick-bread`; Cayenne's F-888 backfill retypes them.

## [9.49.2] — 2026-11-27

A Cayenne restore reads its recipe's fields directly instead of probing for them (architecture review 2026-11-25, `soft.no-shims`). No change to the package's behaviour.

### 🔧 Changed
- `Chunk.pre_parsed` is typed `CayenneRecipe`, the only type a reader puts there (`IngestResponse` subclasses it). The restore fast paths, `_citation_from`, `_pre_parsed_to_refinement` and `_restore_spans` read `ingredient_lines`, `direction_steps`, the four citation columns, `pans`, `bake_type`, `bake_step`, `yield_spec` and the detected measures as fields. An export that predates a column still restores: the column parses with the model's default, as it did before. A rename of one of these fields now fails loudly instead of restoring the recipe without it.
- `check_yield` loses its `pans` argument, which nothing has read since D97; `regen._checked_against` and the yield backfill scripts follow.
- `recipeparser.core.regen` no longer re-exports `strip_fat_tokens`; import it from `recipeparser.core.fat_tokens`.

## [9.49.1] — 2026-11-27

The count of broad exception handlers can only fall, and reasoned handlers carry the architecture gate's exemption marker (architecture compliance roadmap, Phase 2). No change to the package's behaviour.

### 🔧 Changed
- New mandate `fail-loud.ratchet`, checked on pull requests and before every push. `config/architecture/fail_loud_ratchet.json` holds each file's count of bare or broad `except` handlers under `recipeparser/` and `scripts/` (141 in 58 files). A change that raises a file's count is refused unless it raises the baseline too.
- 31 handlers whose `# noqa: BLE001` carried a reason now read `# noqa: BLE001  # fail-loud.bare-except-ok: <reason>`, the marker `fail-loud.bare-except` honours. The 50 with no reason on the line are unchanged.
- `no-shims.hasattr` also runs on Claude Code edits (the `hook` site).
- The `soft.fail-loud` review rule knows the exception base (`RecipeParserError` in `recipeparser/exceptions.py`), so the architecture review checks that raised errors come from it.

### 📝 Docs
- `config/architecture/ARCHITECTURE.md` gains a "Fail-loud ratchet" section: what it holds, how to regenerate the baseline, and why a deliberate new handler raises the baseline. `ACF_ROADMAP.md` marks the Phase 2 items done, including the shim removal in 9.49.0, and corrects its claim that the ratchet would quiet the edit hook. The layer-imports auditor's example no longer names the deleted `recipeparser/epub.py` as present.

## [9.49.0] — 2026-11-27

The package-root compatibility shims are gone: import from the real modules (architecture review 2026-11-25, `soft.no-shims`).

**For Cayenne and other callers:** no wire change, and the server entrypoint is unchanged (`recipeparser.adapters.api:app`, as the Dockerfile and `start_server.py` already use). Six Python import paths no longer exist; anything importing or `patch()`-ing them must move:

| Removed | Import from |
|---|---|
| `recipeparser.api` | `recipeparser.adapters.api` |
| `recipeparser.epub` | `recipeparser.io.readers.epub` |
| `recipeparser.pdf` | `recipeparser.io.readers.pdf` |
| `recipeparser.export` | `recipeparser.io.writers.paprika_zip` |
| `recipeparser.supabase_writer` | `recipeparser.io.writers.supabase` |
| `recipeparser.io.category_sources.base` | `recipeparser.core.ports` (`CategorySource`; still re-exported by `recipeparser.io.category_sources`) |

### 🔧 Changed
- Deleted `recipeparser/api.py` (a `sys.modules` alias), `epub.py`, `pdf.py`, `export.py`, `supabase_writer.py` and `io/category_sources/base.py`. The CLI's `--merge` now imports `merge_exports` from `io.writers.paprika_zip`; `io/category_sources/__init__.py` imports `CategorySource` from `core.ports`; the tests import and patch the real modules.

## [9.48.2] — 2026-11-27

A web page's times come from its own JSON-LD, and a page's source title is never its URL (Cayenne F-877, F-878, fix roadmap batch 88).

**For Cayenne:** no wire change. A URL import's `prep_*`/`cook_*` columns now follow the page's schema.org times; `source_title` is a site name or the host, never an address.

### 🐛 Fixed
- The Best Yorkshire Pudding (Serious Eats, imported 2026-11-26) stored prep 5 min and no cook, so Cayenne's total read "5 min"; the same page imported on 11-22 had cook 30 mins. The page's JSON-LD times reached the model only as text, and it dropped two. `page_meta_from_html` now reads `prepTime`, `cookTime` and `totalTime` from the page's JSON-LD (read once, with its pictures) and the URL job passes them as the source's own statement (`SourceMeta`, which gains `total_time`), which beats the model's reading.
- A stated total with a single prep figure and no cook time now fills the cook span with the total less the prep ("Prep 5 min, Total 35 min" cooks for 30), since Cayenne adds prep and cook; it filled it with the whole total. A range, a note, or a total no longer than the prep is the total, as before.
- `resolve_citation` took the model's stated source for a web page as its site name without checking it was a name: the Chilli Con Carne showed its URL. A URL, a bare host or a host with a path now falls back to the page's own title, its host.

## [9.48.1] — 2026-11-27

A size in brackets after the food's name is kept as the line's second measure (Cayenne F-858, fix roadmap batch 84).

**For Cayenne:** no wire change. `converted_amount`/`converted_unit` are now filled (the writer's, `is_ai_converted` false) for lines like "1 standing rib roast (about 8 lb)".

### 🐛 Fixed
- "1 standing rib roast (about 8 lb)" kept `converted_amount` null, so the list read "≈ 1 roast" (owner's holiday test, 2026-11-26). REFINE's bracketed-size pass (F-686, which took only a bracket straight after the count) now also takes a size bracket after the food's name: "(about 8 lb)", "(7 pounds), rind on", "(4 to 5 lb)" (the larger), "(400g each)". Only when the line's unit is a count or none, nothing holds the second measure yet, and the line has exactly one bracketed size ("1 ham (about 7 lb) or 2 small (3 lb) hams" is left alone).

## [9.48.0] — 2026-11-27

The search look offers recipes only: a round-up or guide page is no longer offered (Cayenne F-866, fix roadmap batch 86).

**For Cayenne:** no wire change. A look's `candidates` line can hold fewer rows than before, and takes up to about 4 s longer. Find (`POST /search/find`) is unchanged.

### 🐛 Fixed
- `POST /search/look` returned roundup and hub pages ("13 Crowd-Pleasing Christmas Dinner Sides", "Roasting Guides"), and adding one failed with "Nothing there looked like a recipe" (owner's holiday test, 2026-11-26). Before the look answers, every candidate page from both lanes (the agent's checked finds and the links lane's links) is fetched in parallel, 3 s per page and 4 s for the whole check, and dropped when its HTML declares no schema.org Recipe in JSON-LD. A page that cannot be fetched (a timeout, an error status, a refusal, a challenge page, or still loading after 4 s) is kept: the import says why. Excluded and unread pages are never fetched.
- The links lane's Brave query adds " recipe" to the question the way find's does (`web_query`), and keeps it whole when a long question is trimmed to Brave's 400-character, 50-word limit.

### 🔧 Changed
- The URL reader's direct fetch is now `fetch_page_html(url, timeout)` in `io/readers/url.py` (same user-agent, page proxy, private-address checks and refusals), used by the reader and by the look's recipe check (`adapters/ai_search/recipe_pages.py`). Through the proxy, the connect timeout is now the smaller of 10 s and the read timeout; the reader's 30 s is unchanged.
- `links_query` takes a `suffix` kept whole beside the `site:` group; `build_router` and `look_stream` take a `fetch_page` (the fetch by default; a fake in tests).

## [9.47.0] — 2026-11-26

A restore's bucket picture that cannot be downloaded is now counted as a lost picture, `PipelineController` loses its unused checkpoint and 429 auto-pause code, and the background workers are driven through a typed protocol (architecture review 2026-11-25: the two blocking findings and the `picture_copy.py` important one).

**For Cayenne:** a restore whose old bucket picture cannot be copied now adds a `{"picture": true, ...}` entry to the job's `refused_links`, as a failed upload already did in 9.45.0. No new columns or fields.

### 🐛 Fixed
- `download_bucket_picture` raises `ImageStoreError` when the download fails or comes back empty instead of returning `None`, the same as "no picture". The restore's `_restore_bucket_picture` catches `ImageStoreError` (not every `Exception`), keeps the recipe without its picture and reports it through `on_picture_lost`. A URL that is not a bucket picture still returns `None`; `JobCancelledError` and `DailyQuotaExhaustedError` pass through untouched.

### 🗑️ Removed
- `PipelineController`'s checkpoint persistence (`save_checkpoint`, `load_checkpoint`, `delete_checkpoint`, `_book_hash`, `_checkpoint_path`, the `output_dir` argument) and 429 auto-pause policy (`record_429`, `reset_429_counter`, `trigger_rate_limit_pause`, the auto-resume timer). Nothing outside their own tests called them. With them go `CHECKPOINT_SUBDIR`, `RATE_LIMIT_PAUSE_THRESHOLD` and `RATE_LIMIT_AUTO_RESUME_SECS` (`config.py`) and `CheckpointError` and `RateLimitPauseError` (`exceptions.py`). The controller is now the transition table plus pause, cancel and progress signalling.

### 🔧 Changed
- `run_workers` takes `Sequence[Worker]`, a protocol with `run_once()` and `keeps_loop_busy`, and reads `keeps_loop_busy` directly instead of probing for it. Every worker declares it: `RegenWorker` and `EmbedWorker` `False`, `RecatWorker` and `ShareWorker` `True`, as before.

## [9.46.2] — 2026-11-26

The architecture rules are checked on every pull request and before every branch push (architecture compliance roadmap, Phase 1). No change to the package's behaviour.

### 🔧 Changed
- `.github/workflows/arch-compliance.yml` runs on pull requests. It has two jobs: the arch-compliance plugin's `check-diff` action at 0.3.4 (the `ci` mandates in `config/architecture/mandates.yml`), and ruff on the lines the pull request changed.
- `scripts/ci/ruff_changed_lines.py` runs ruff on the changed Python files and reports only the findings on added or changed lines. The repository's 236 older ruff findings don't fail a pull request that didn't write them.
- `.githooks/pre-push` runs `acf-check-diff --call-site precommit` against `origin/master` whenever a branch is pushed. A finding or a refusal blocks the push. Without the package, the push is refused unless `ACF_PREPUSH_OPTIONAL=1` is set in that environment.
- ruff's layer ban (`TID251`: no `recipeparser.io` or `recipeparser.adapters` imports) now applies only to `recipeparser/core/`, which is the rule's subject. One negated pattern replaces 33 per-file exemptions, and the ban no longer fires on the package-root entry points.
- `.githooks/*` are kept LF on every checkout.

### 🐛 Fixed
- `adapters/gui.py` names `PipelineController` in an annotation without importing it (ruff `F821`). It is now imported under `TYPE_CHECKING`. Annotations there are strings, so this was never a runtime error.

### 📝 Docs
- README and `.githooks/README.md`: enabling the hooks and installing the arch-compliance package. `ACF_ROADMAP.md`: Phase 0 and Phase 1 items marked done; making the CI job a required check is still the owner's decision.

## [9.46.1] — 2026-11-25

The servings fixture's byte check passes on Windows checkouts (architecture compliance roadmap, Phase 0).

### 🐛 Fixed
- `tests/fixtures/servings_source_cases.json` is kept LF in `.gitattributes`, like the other two fixtures shared with Cayenne. With `core.autocrlf=true` it was checked out as CRLF, so `test_fixture_is_byte_identical_to_the_copy_cayenne_runs` failed its SHA-256 pin on every Windows checkout. The committed file was already LF; nothing in it changes.

## [9.46.0] — 2026-11-25

No Gemini call claims temperature 0 any more, and a call that sets a deprecated sampling parameter is refused instead of silently stripped (architecture review 2026-11-25, `soft.no-shims`). Google has deprecated `temperature`, `top_p`, `top_k` and `thinking_budget`: Gemini 4 and 3.6+ ignore them, and for earlier Gemini 3 models (the pinned `gemini-3.1-flash-lite`) Google says to keep the defaults. Since #100 (2026-10-06) none had reached the model; what was false was the claim. No change to what is sent.

### 🐛 Fixed
- `tag_temperatures`, `tag_pans`, `tag_yield` and `estimate_servings` no longer pass `temperature: 0`, which `_finalize_config` had been dropping: every call runs at the model's default sampling, so none is deterministic.
- `_finalize_config` raises `UnsupportedGenerationParameterError` (a `RecipeParserError`) for any of the four parameters, before the retry ladder and before any request; a test also checks that no call site in `gemini.py` sets one.

### 📝 Docs
- `ARCHITECTURE.md` (tagging section) no longer says the TAG stage samples at 0.0, and `config.py` says the tagging accuracy figures were measured on 2026-10-03, under the old sampling.

## [9.45.1] — 2026-11-25

Typed dispatch instead of attribute probing, and a diagnosable search log (architecture review 2026-11-25, `soft.fail-loud` nits and suggestions). No behaviour change for a real PostgREST answer.

### 🔧 Changed
- The household and sharing endpoints map PostgREST's `APIError` by type instead of probing any exception for `.code`; anything else is the existing 503.
- The search look's links lane stays non-fatal but logs an unexpected failure with its traceback (no secret can reach it: Brave errors are converted with `from None`).
- The book-photo check picks Pillow's pixel API by `PIL.__version__` in one helper instead of probing the image.
- The Supabase writer and the search look read declared fields directly; a field the Anthropic SDK renames now fails instead of reading as absent.

## [9.45.0] — 2026-11-25

An import whose categories cannot be loaded now fails instead of finishing untagged, and a lost picture or tag write is counted instead of only logged (architecture review 2026-11-25, `soft.fail-loud`).

**For Cayenne:** a job whose categories cannot load ends `error` with "Your categories could not be loaded, so nothing was imported. Try again in a moment." No new columns or fields. The counts of lost pictures and tag writes go into `refused_links`, which no `ingestion_jobs` column holds yet, so the app cannot show them until it gets one.

### 🐛 Fixed
- The Supabase, YAML and Paprika category sources raise `CategoryLoadError` on an HTTP or network failure, an unreadable or unparsable file, or a database error, instead of returning no categories. Empty still means none defined, no credentials or path configured, or no Paprika categories table.
- The pipeline no longer catches a failed load of the category axes or parents and carries on: the run fails before any chunk.
- A Cayenne restore chunk with no recipe data is reported as skipped instead of vanishing from the counts.
- A picture that cannot be stored keeps the recipe and is counted on the job; a recipe whose tag links cannot be written is counted the same way.
- A failed finalize of the job row is tried once more; if that fails too, the row is left to the liveness reaper (about 5 minutes), which ends it as interrupted.
- `cleanup_jobs.py` checks the fetch and every update and exits 1 when Supabase refuses one.

### 🔧 Changed
- `ImageStore.put` raises `ImageStoreError` when storing fails; None now means no bytes or no store configured. `RecipePipeline.run` takes `on_picture_lost`.

## [9.44.1] — 2026-11-25

Readers, writers and the category editor stop losing things quietly, and two pieces of dead code go (architecture review 2026-11-25, `soft.fail-loud`).

### 🐛 Fixed
- A Paprika entry that cannot be read (corrupt or truncated member, not JSON, JSON that is not a recipe, larger than the cap) is reported in the import's "What was missed" list instead of being dropped. JSON that is not an object used to crash the read.
- `--merge` reports the entries and archives it left out (and keeps going past a corrupt member instead of abandoning the rest of that archive); it prints the written and duplicate counts.
- The category editor refuses to load an unreadable or malformed `categories.yaml` and disables Save, so a Save can no longer overwrite the cook's file with an empty tree. A missing file still opens empty on first run.
- A PDF image PyMuPDF cannot extract is logged with its page and xref instead of being dropped silently.
- JSON-LD that breaks the URL import's picture lookup is logged with its traceback instead of being swallowed.

### 🗑️ Removed
- `recipeparser/toc.py` and its tests: nothing in production called it.
- `PaprikaReader.read_entries_with_images`: no callers.

### 🔧 Changed
- `merge_exports()` returns `MergeResult(path, written, duplicates, unreadable)` instead of a path.

## [9.44.0] — 2026-11-25

A used-up daily Gemini quota or a cancel now stops the work instead of being recorded as one more failure. `recipeparser/exceptions.py` has always said every `except Exception` must let `JobCancelledError` and `DailyQuotaExhaustedError` through; about a dozen handlers did not (architecture review 2026-11-25, `soft.fail-loud`).

### 🐛 Fixed
- The recategorise worker ends the job as an error with the quota message instead of skipping every remaining batch; a cancel inside a batch ends it as cancelled.
- The regen and embed workers release the claim and stop the poll on a used-up quota, so no row loses an attempt (embed stops offering a row after three) to a quota outage.
- A photographed recipe whose import hits the quota is no longer reported as a photo that could not be opened.
- TAG stops on a used-up quota instead of failing one axis at a time.
- A malformed `categories.yaml` raises `ConfigurationError` instead of silently becoming no categories; a missing file still loads empty. A failed categorise call is no longer turned into the "EPUB Imports" fallback; only an unusable reply is.
- `--recategorize` copies an entry it cannot parse through unchanged instead of leaving it out, and prints how many recipes were recategorised, kept as they were and copied unparsed. Quota and cancel stop it with nothing written.
- `refine_recipe_for_cayenne` keeps the error's own type instead of returning None for every failure; None now means only an empty reply.
- `estimate_servings` returns None (with a warning) only for an API failure past the retries or an unusable reply; anything else raises.
- Vision OCR: a page that fails for a lasting reason fails the read ("Gemini Vision could not read page N of M"); a page lost to a transient failure is still left out, and a warning names the missing pages.

### 🔧 Changed
- `recategorize()` returns `RecategorizeResult(path, recategorized, kept_original, copied_unparsed)` instead of a path.

## [9.43.1] — 2026-11-24

Released with Cayenne's shopping PR 3 (its migration 20261124120000_shopping_recipe_pins), which makes pins per-person: an owner's own pin no longer shares a recipe, and a member may copy one only while it is on the household's plans or shopping list, or they pinned it.

### 🔧 Changed
- `POST /household/copy`'s 404 now reads "This recipe is no longer shared in your household." (was "no longer pinned"), the same sentence as the app's `NOT_COPYABLE`. A recipe its owner still pins can now be refused, so "no longer pinned" would be false.

## [9.43.0] — 2026-11-20

Fixes for what the 2026-11-18 Christmas walkthrough's recipes lost on the way in (Cayenne Fix Roadmap batch 66). Each fix is in code: no prompt changed, so no golden was re-recorded.

### 🐛 Fixed
- REFINE drops an AI conversion on a count: "2 limes, juiced, to get 2 1/2 tbsp" no longer keeps ≈ 37.5 ml, which the app scaled and showed as "≈ 50 ml limes". A unit that is not a measure in Cayenne's registry is a count, the same as "3 eggs" in the prompt. The unit lists now carry every registry alias ("tbsps", "kilo", "gms", "fl. oz"…), so a measure spelt another way keeps its conversion. A writer's own size on a count ("1 (14-ounce) can") stays (F-684).
- REFINE adds a line's extra amount: "60g coconut cream, plus 100g extra for the coriander cream" is 160 g, so the method's 60 g and 100 g parts add up and aren't taken back. This works only for a second figure in the line's own unit, or a bare number for a count ("2 lemons, plus 1 to serve"). "1 cup sugar plus 2 tablespoons" and "2 eggs, plus 1 yolk" are left as they are. An AI conversion grows with the amount (F-685).
- REFINE keeps a bracketed size after a count: "1 (4- to 5-pound) brisket" gets 5 pound as its second measure (the larger end of a range, the figure written beside the unit), so the list no longer reads "Beef · 1". "2 (14-ounce) cans" and "1 (400g) tin" work the same way. A model's guess at the size gives way to the brackets (F-686).
- The Paprika reader stores a time written as steps as its total. "2 minutes (fry baris) + 4 minutes (fry onions) + …" took the first figure (2 minutes). It is now rewritten "38 minutes (2 minutes (fry baris) + …)", which both duration parsers read as 38 minutes with the steps as the note. Steps can be joined by "+", ";", "," or "then", and ranges add up as ranges. An alternative ("2 hours or 30 minutes"), a qualifier ("10 mins, plus making the sauce"), a multiplier ("per batch") or a step with no figure is left as written. `core.durations.summed_duration_text` is not part of the shared `parse_duration` contract. The backfill of the 95 rows already stored is separate (F-688).
- ASSEMBLE and the regeneration worker wrap a measure the method writes for an ingredient, so it scales: "Heat 2 tbsp {{oil|none}}" becomes "Heat {{oil|2 tbsp|2 tbsp}} {{oil|none}}". A mention holding its quantity and name, "{{water|800ml water|none}}", is split into "{{water|800ml|800 ml}} {{water|water|none}}". This applies only to a `none` mention of an ingredient measured in that same unit, and only where the parts, old and new, still add up to the line. A count, a range or a rate ("per serving") is left alone (F-689, RecipeParser half; F-499).
- A yield in two cuts scales: "Cut into 4 strips, then cut each strip into six bars" (yield 24) wraps the 4 as a yield count, so the bars scale with the servings (F-499).

### ✨ Added
- `tests/fixtures/scaling_text_cases.json`: two cases, the curry's measured parts and the two-cut yield. Cayenne keeps an identical copy and should take them in its F-689 follow-up.

## [9.42.1] — 2026-11-20

A URL import from a Dotdash Meredith site (Serious Eats, Allrecipes…) gets its picture again.

### 🐛 Fixed
- The URL import's page-meta GET (`_fetch_page_meta`, which finds the page's og:image) is now made with `requests` and the page fetch's headers, as UrlReader's own fetch is. It still goes through `PAGE_FETCH_PROXY`, and without it when the proxy can't be reached. Through the proxy, Dotdash Meredith answered the httpx GET 403 and the requests GET 200 (checked from the production container on 2026-11-20). The import read the recipe through UrlReader but found no picture candidate, so the recipe was stored with no picture and no error: Roasted Brussels Sprouts and Best Chocolate Chip Cookies, both imported on 2026-11-15. The picture's own fetch was never the problem: both pictures answer 200 without the proxy (F-687).

## [9.42.0] — 2026-11-19

Shopping labels now tell a made ingredient from a bought one, and a known food is reused only when the ingredient really is that product. A cook's leftover roast turkey, a ganache made in the recipe or homemade pastry is something they make rather than buy, but it still gets a food and an aisle. And "cheese", "flour" or "chicken" no longer take the food of a specific "cheddar", "bread flour" or "turkey" the cook already has. One refused food no longer sinks a whole aisle batch.

**Deploy order:** merge and deploy this before Cayenne's batch-59 PR, which refuses labels that come back without `made`.

### 🔧 Changed
- `POST /shopping/classify`: every item now carries a required `made` flag (an ingredient the cook makes, not buys). A made ingredient still gets its food and aisle. A bought food that is cooked or prepared ("cooked rice", "melted butter", "toasted almonds") or may be shop-bought is not made, and when unsure `made` is false.
- The KNOWN FOODS rule in the classify prompt reuses a known food only when the ingredient is exactly that product, and never answers a generic known food ("cheese", "flour", "chicken") for a specific ingredient ("cheddar", "bread flour", "turkey") (F-272, F-663).
- `POST /shopping/aisles`: a batch the Decisions API refuses (HTTP 400) is split in half and retried, up to `AISLES_SPLIT_BUDGET = 20` extra calls. A lone refused food answers `other` when others in the batch sorted. A batch refused in full, or past the budget, still returns 503, now with the API's text logged (F-665).

### ✨ Added
- `tests/goldens/test_classify_golden.py` recordings re-recorded: the old replies lacked `made`, and the recordings' config blocks were refreshed too because master's Gemini config had moved. The onion whole-and-chopped sample was kept after about a dozen re-records, because "1 cup chopped onion" sometimes answers count null.

---

## [9.41.0] — 2026-11-18

Searching more deeply now starts from the cook's own categories. Every deeper-search request carries the cook's category tree, the search tools show which categories each recipe is filed under, and the agent is told to treat a category as everything inside it and to lead a count with the Library's own filter count.

**Deploy precondition:** Cayenne PR #536 (migration `20261118120000_ai_recipe_filed_under.sql`, the view `ai.recipe_filed_under`) must be live in production before this deploys. The deeper search reads that view before the agent starts, so without it every `/search/ask` request fails. If the migration is applied after this version is already running, restart the API: it reads the list of views the agent may query once at start, and until then it refuses `run_sql` on `ai.recipe_filed_under`.

### ✨ Added
- `POST /search/ask`: the request now includes the cook's category tree (names quoted and escaped; long trees cut to the top two levels), built by `cook_categories` and `categories_block`.
- `semantic_search` and `keyword_search` rows carry each recipe's `categories`; `get_recipe` adds `filed_under` (axis and category, direct filings) read from `ai.recipe_filed_under`.
- The agent's instructions gain a "The cook's categories" section: categories first, a category includes everything below it, and counts lead with the Library filter's count and reconcile the rest. `run_sql` names the new view.

### 🔧 Changed
- `scripts/ask_goldens.py` sends the same category block.

---

## [9.40.2] — 2026-11-18

The golden-question report now shows whether each answer used the cook's categories, and the golden set has new category questions. Nothing the app does changes; only the script the owner runs against the library.

### 🔧 Changed
- `scripts/ask_goldens.yaml`: new optional field `expect_categories`, and a Categories group of five questions (a parent count, two axes, a list, a cuisine and meal type, and a mood question that should not use categories); the soup count now expects categories too.
- `scripts/ask_goldens.py`: the report marks each answer's category use against its expectation and totals it as `Category use: N of M`.

## [9.40.1] — 2026-11-18

One shared Mela recipe keeps the upload ceiling (owner ruling on 9.33.0's open question). Before, a single `.melarecipe` was exempt from the ceiling at upload, then refused by the reader past 50 MB with "is not a Mela recipe: larger than 50 MB."; the cook uploaded the whole file only to be told no. Merge and deploy before the Cayenne client PR, which refuses the same file before upload.

### 🔧 Changed
- `POST /jobs/file`: only a library export (`.paprikarecipes`, `.melarecipes`) is exempt from `MAX_UPLOAD_BYTES`; a `.melarecipe` over it is the usual 413, "Cayenne takes files up to 100 MB."
- `MelaReader` holds a single `.melarecipe` to `MAX_UPLOAD_BYTES` (`_MAX_SINGLE_BYTES`) rather than the 50 MB per-entry cap, which still bounds each member of a library; its refusal names the ceiling ("is not a Mela recipe: larger than 100 MB.").

## [9.40.0] — 2026-11-18

A used-up daily Gemini quota no longer loses a run's work (#161). Gemini's free tier allows 500 requests a day per model; on The Food Lab (280 chunks) the quota ran out at chunk 81, Gemini answered 429 RESOURCE_EXHAUSTED with a retry delay of 4 h 44 min, RecipeParser retried after 2, 4, 8 and 16 seconds, and stopping it lost all 81 chunks because nothing is written until the end. 9.36.5's second pass for rate-limited chunks would have made it worse: every remaining chunk would have spent its retries twice before the run gave up.

### ✨ Added
- `core/quota_wait.py`: `QuotaWait`, the CLI's answer to a used-up daily quota. The call waits until Gemini says the quota resets, plus a minute (or 30 minutes when it gives no time, or the quota is still used up after a wait), and tries again, spending no retry attempt. One pause is shared by every worker that meets the quota, and logs once ("Waiting until 20:31 (4 h 45 min)… Press Ctrl-C to stop and save the recipes made so far"). After 24 hours of waiting without a success it gives up; a success starts the next day's 24 hours afresh, so a book needing several days' quota still finishes.
- The CLI saves what it made when a run stops early: the daily quota's give-up, any error, Ctrl-C, or SIGTERM (`docker stop`, which kills ten seconds later) write `<book>-partial.paprikarecipes` from the recipes finished so far. The pipeline runs off the main thread so a signal is heard at once, even mid-wait; a stop by signal exits 130 (Ctrl-C) or 143 (SIGTERM). In the GUI, which runs the pipeline on its own thread, the error path saves too, but no signal handler is installed.
- `DailyQuotaExhaustedError`, with the reset time in its sentence ("Gemini's daily request quota is used up. It resets in about 4 h 44 min.").

### 🐛 Fixed
- `gemini._with_backoff`: a 429 whose QuotaFailure names a per-day quota, or whose retry delay is over 15 minutes, is the daily quota (`daily_quota_retry_after`, read from the typed error's body: `RetryInfo.retryDelay`, else the message's "Please retry in 4h44m41.2s"). Checked against the reporter's own 429, which is a test fixture. It is never climbed on the 2-to-32-second ladder. With no `QuotaWait` installed — the server, where a job asleep for hours looks hung to the liveness check (Cayenne F-426) — it raises `DailyQuotaExhaustedError` at once, and the job ends with that sentence as its error message, keeping the recipes it already wrote. A per-minute 429 is handled as before.
- The pipeline stops the whole run on `DailyQuotaExhaustedError`: no per-recipe retry, no second pass, and a chunk queued behind it never starts. The recipes handed to `on_result` before it stay.
- `refine_recipe_for_cayenne`, table normalisation, vision OCR, the servings guess, EMBED and EXTRACT's retry no longer swallow `DailyQuotaExhaustedError` (refine, normalisation and the servings guess no longer swallow a cancel either, which is half of Cayenne F-647).

---

## [9.39.0] — 2026-11-17

### ✨ Added
- `POST /shopping/aisles`: sorts typed shopping-list foods into the twelve aisles with the Gemini Decisions API (`gemini-decision-1`) for Cayenne's "typed items get a section". Request `{"foods": [...]}` (1–200 unique, non-blank strings of at most 200 characters; 400 otherwise); response `{"items": [{"food", "aisle"}]}`, one per food in request order. One Decisions request per call, one `choice` question per food over the twelve aisles described by Cayenne's labels; a winner under `AISLE_MIN_CONFIDENCE` (0.5) is answered `other`. Same auth, AI gate and error mapping as `/shopping/classify` (missing key, Gemini refusal → 503; 429 → 503 busy; timeout → 504; an unreadable reply → 502). Recorded in `ai_usage` as action `classify`, step `aisles` (the table's action check admits no new action). New environment overrides `GEMINI_DECISION_MODEL`, `GEMINI_DECISIONS_URL`, `AISLE_MIN_CONFIDENCE`. The endpoint is called over httpx, not google-genai's `client.decisions` (google-genai ≥ 3.2 needs pydantic ≥ 2.12.5 and moves onto httpx2).

---

## [9.38.0] — 2026-11-17

### ✨ Added
- `POST /search/find`: Add Recipe's search turn asks Brave Search directly, on the cook's sites and on the web at once, with no model; its own cap of 30 an hour and 100 a day (Cayenne's Brave search turn design, 2026-11-17)

---

## [9.37.0] — 2026-11-17

### ✨ Added
- Looks also show up to 3 plain links from Brave Search on the sites Anthropic's tools refuse; nothing on those pages is read (Cayenne §11, the owner's rulings of 2026-11-16/17). The web look asks Brave about the whole refused set, the sites look about the cook's own sites in it; one request per look, sent alongside the agent and cut at 5 s, and none when no refused site is in scope. The lane caps its site list to fit Brave's query limits (400 characters, 50 words, the question keeping at least 150 characters), the known refused sites first, and logs one warning when it cuts; link titles are unescaped, and a web look's links show the cook's own name for their site. The links follow the agent's finds in the one `candidates` line, each carrying Brave's description of the page as its note (tags stripped, unescaped, clipped like a find's note; empty when Brave gives none), through the same `yours`/`blocked` classification. Brave failing, timing out or finding nothing adds no links and no error; when the agent fails, its error line stands alone. `no_sites` is sent only when neither the agent nor the links lane has a site. Each request is recorded in `ai_usage` as step `look_links`, model `brave-web-search`. New environment variable `BRAVE_SEARCH_API_KEY`: without it a look shows no plain links (one warning per process) and is otherwise unchanged.

---

## [9.36.5] — 2026-11-17

A failure no longer takes a chunk's other recipes with it. RecipeParser #149: a run of The Curry Guy Bible produced 176 recipes where the run before produced 217, while the log said "23/23 chunks succeeded". Four chunks, about forty recipes, were lost whole: two to EXTRACT still getting Gemini's free-tier 503 "high demand" after its retries, one to a single recipe whose REFINE failed after a 503, one to a single recipe whose Fat Token said `ing_1` for `ing_01`.

The per-call retry budget (Cayenne F-426, 180 s for a transient error) is unchanged: no call waits longer. A failure gets another try later instead, when the spike may have passed.

### 🐛 Fixed
- `_process_chunk`: a recipe whose REFINE, EMBED or ASSEMBLE fails is tried once more after the chunk's other recipes, and failing again is skipped with a reason that names it ("Wings: ValueError: …"); the rest of the chunk is kept, in source order. A cancel still stops the chunk: `refine()` reads one as a failure, so the pipeline checks for it before retrying.
- `run()`: a chunk lost to a transient Gemini failure (503, 500, 504) or a rate limit (429) is tried once more after every other chunk is done. It counts once in the progress and is reported as skipped only if the second try fails too. Any other failure is not retried, so a broken key fails no slower.
- The summary says what was lost: "finished. 21/23 chunks succeeded, 2 failed (3 given a second try) → N recipe(s)." (was "23/23 chunks succeeded" whatever happened).
- `refine()`: a Fat Token whose id differs from one ingredient's only by zero-padding (`ing_1`, `ing_001` for `ing_01`) is read as that ingredient, with a warning, instead of failing the recipe. An id that matches no ingredient still fails it.

---

## [9.36.4] — 2026-11-17

A `.paprikarecipes` archive from an EPUB or PDF imports into Paprika 4. Paprika 4.0.10 for Windows refused every recipe with a photo, logging "Refusing Paprika file with an invalid photo p264.jpg" (RecipeParser #149): the writer named each photo after the book's own image. Measured on Paprika 4 on 2026-11-17: the reporter's entry failed as written and with only `photo_hash` added, and imported once the photo was named `<UUID>.jpg`, with or without the hash. Paprika 3 accepts either name, which is why it went unseen.

### 🐛 Fixed
- `PaprikaWriter`: a photo is named `<UUID>.<ext>` as Paprika names its own, fresh on every export, so two recipes sharing one image, or a book imported twice, never bring Paprika a photo it already has (Paprika 3 crashed on one whose name and bytes it held, 2026-11-16). `photo_hash` is the upper-case SHA-256 of the image, as in Paprika's export (was always `""`). A recipe without a photo still has no `photo` key.

### ⚠️ Changed
- `tests/goldens/test_e2e_golden.py` normalises a photo's name to its extension, since it differs per run; the goldens gain `photo_hash`.

---

## [9.36.3] — 2026-11-16

`POST /jobs/file` takes files up to 100 MB (was 50 MB). An illustrated cookbook EPUB runs past 50 MB, and one was refused (Cayenne bug report 63285b5d). Cayenne's intake carries the same number and must ship with it (Cayenne `domain/ingestion.ts`); until it does, the web app still refuses at 50 MB before uploading.

### ⚠️ Changed
- `config.MAX_UPLOAD_BYTES`: 100,000,000 (was 50,000,000). The 413 sentence follows: "Cayenne takes files up to 100 MB." Paprika and Mela exports stay exempt.

### ✨ Added
- `config.MAX_PICTURE_BYTES` = 50,000,000: `POST /recipes/{id}/image` keeps the old ceiling. That endpoint holds the whole picture in memory on a 2 GB VM (F-426), and no recipe photo needs more. `_too_large_sentence` takes the ceiling that refused the file.

---

## [9.36.2] — 2026-11-16

### 🐛 Fixed
- **A refused web look failed for good.** If the API refused some of the web look's `blocked_domains` (HTTP 400, as it does for a site its crawler may not read), 9.36.0 remembered them and returned `failed`, and every later web look listed them again and failed the same way. The look now drops those domains and runs once more, inside the original time limits; the next web look no longer lists them, and a second refusal is a `failed` line.
- **The blocked list's order.** The list is the cook's own sites, then the sites Anthropic's crawler refuses, then the hosts that refused an import in the last 30 days, cut at 64 (9.36.0's entry said 100; the code already used 64), so the recently refusing hosts are cut first and the cook's sites last.

---

## [9.36.1] — 2026-11-16

Two defects in the local CLI and GUI, reported in issue #149.

### 🐛 Fixed
- **Local imports tagged nothing.** `YamlCategorySource` read only an `axes:` file, but the app writes `categories.yaml` as a `categories:` tree (the bundled default, `--sync-categories`, the GUI editor), so every CLI and GUI run logged "missing top-level 'axes' key" and gave every recipe 0 categories. It now reads the tree too: every name is offered on one axis, `Category`, so TAG makes one call per batch (not one per top-level name, which a 5-RPM free key cannot afford), and a child keeps its parent through `load_parents`. Names are stripped and offered once; the first occurrence decides the parent. An `axes:` file reads as before.
- **`--concurrency` defaulted to 4, not 1.** With no `--concurrency`, `run_cli_pipeline` left the pipeline at its server default (`core/pipeline.py`, 4) while `--help`, the README and `config.MAX_CONCURRENT_API_CALLS` all say 1. The CLI now passes `config.MAX_CONCURRENT_API_CALLS`. The server's default is unchanged, and the GUI always passed its own value.

---

## [9.36.0] — 2026-11-15

`POST /search/look` takes `scope`: `"sites"` (the default, today's look on the cook's own sites) or `"web"`, a look on the open web for the gaps in an answer. The web look sends no `allowed_domains`; it sends `blocked_domains` instead (the cook's own sites first, then hosts that refused an import in the last 30 days, then sites Anthropic's crawler refuses, at most 100), and uses its own prompt. It never returns `no_sites`, and a find on any site is checked as before (fetched in this request, `https`, unambiguous). The response stream is unchanged. Request body: `{"question", "answer", "chosen", "measuring", "exclude", "scope"}`.

---

## [9.35.0] — 2026-11-15

A recipe page is fetched from the owner's home connection, and the image is pinned to Debian bookworm, so Dotdash Meredith's sites (Allrecipes, Serious Eats, EatingWell, Simply Recipes, The Spruce Eats) import again. The owner's decision of 2026-11-15 revisits the 9.21.0 ruling ("no evasion of bot protection") for these two changes only. A browser-fingerprint imitation (curl_cffi and the like) is still out. Every URL import from these sites failed: 18 of the 22 URL jobs since 2026-09-01 (the four that succeeded were all NYT Cooking; the other failures were The Woks of Life, which serves the VM again today).

Root cause, measured from the production VM on 2026-11-15 with the reader's own request (same headers, same `requests`):
- From the VM's Google Cloud address (AS396982) every Dotdash page answers **402** (pay-per-crawl), or a 403 challenge, whatever the client.
- From the home address through the Pi, `python:3.11-slim` (now Debian trixie, OpenSSL 3.5, JA4 `t13d1713h1_…`) is still challenged on four of the five sites (403, `cf-mitigated: challenge`).
- From the home address with `python:3.11-slim-bookworm` (OpenSSL 3.0, JA4 `t13d1812h1_…`, the same handshake as the owner's PC), all six test pages came back 200 with their Recipe JSON-LD.

**Deploy notes:** `PAGE_FETCH_PROXY: http://100.70.123.60:8888` is already in the `api` service's `environment:` in Cayenne's `deploy/docker-compose.yml` (Cayenne #511; it is not a secret). That address is the Pi's tinyproxy, which admits only the VM's tailnet address. Without the setting nothing changes but the base image. Merging deploys (container restart); nothing to migrate.

### ✨ Added
- `PAGE_FETCH_PROXY`: an HTTP proxy the page fetches go through: `UrlReader`'s direct fetch (every redirect hop) and `_fetch_page_meta`. Jina, Gemini, Supabase and picture downloads never use it. A proxy that cannot be reached (a `ProxyError` or connect timeout, with a 10 s connect limit) is logged and skipped, so the Pi being off is no worse than no proxy: a refusing site still ends in "refuses automated readers (HTTP 402). Open it in your browser…".
- `is_cloudflare_challenge(headers)`: a `cf-mitigated: challenge` response header is a challenge page, whatever the page is titled. Dotdash Meredith's challenge is titled "Simple Page", which `looks_like_html_challenge` never knew; a 403 already refused, but a challenge served with a 200 would have been read as the recipe.

### ⚠️ Changed
- `Dockerfile`: `FROM python:3.11-slim-bookworm` (was `python:3.11-slim`, which moved to trixie). Bookworm is Debian oldstable with security support. Cloudflare may start challenging OpenSSL 3.0's handshake too; then these sites fail as they did before this release, with the same message.
- A direct fetch that Cloudflare challenges says "is blocked by the site's bot-protection challenge page." (was "refuses automated readers (HTTP 403).").

### 🧪 Tests
- `tests/unit/readers/test_url_direct_fallback.py`: no proxy, the proxy for the direct fetch and never Jina's, every redirect hop through it, a dead proxy falling back to a plain fetch, a dead proxy and a 402 still naming the site, a blank setting, the challenge header on a 403 and on a 200.
- `tests/test_api.py::TestFetchPageMeta`: the page meta fetched without a proxy, through it, and without it when it fails.

## [9.34.2] — 2026-11-14

### 🐛 Fixed

- **"Look on sites you use" found nothing when the cook's sites had the recipe** (Cayenne bug report 5c6f4904's retry). Asked for tomatoes provençal, the look's searches did find Food52's and Food Network's recipes. But the model ran them from the web tools' dynamic-filtering code, couldn't read what came back, used up its 4 searches and offered nothing. Both web tools now set `allowed_callers: ["direct"]`, so the model calls them itself and reads the results.
- The prompt now says to read each page **with web_fetch** before offering it, and that a find it didn't fetch is thrown away. With direct calls, the model offered three finds from search-result text alone, and the server's fetched check dropped every one.
- The look may search 6 times (was 4), so one empty query doesn't end it.
- `thespruceeats.com` and `bbc.co.uk` join `KNOWN_UNSEARCHABLE` (seen refused 2026-11-14).
- `scripts/look_goldens.yaml` adds `tomatoes-provencal`, with the owner's readable sites.

## [9.34.1] — 2026-11-14

### 🐛 Fixed

- **"Look on sites you use" failed when the retry met another crawler-refused site** (Cayenne bug report 5c6f4904, Cayenne F-626). A request naming a site Anthropic's crawler refuses fails with an unbilled 400. The look dropped the refused sites and retried only once, so when the refill brought in another refused site (`epicurious.com`), the cook saw "Couldn't answer that just now." The look now retries up to `MAX_SITE_ATTEMPTS` (4) requests while each refusal names a site it sent. It also skips the sites known to be refused from the start (`KNOWN_UNSEARCHABLE`): `bbcgoodfood.com`, `bonappetit.com`, `epicurious.com`, `nytimes.com`, `seriouseats.com`, `simplyrecipes.com` and `theguardian.com`. `cooking.nytimes.com` is accepted.
- `scripts/look_goldens.yaml` no longer seeds refused sites (Cayenne F-627).

## [9.34.0] — 2026-11-14

Answers report gaps; `POST /search/look` looks for them on the cook's own sites; refusing sites are recorded; pasted text may carry its web address. Cayenne Library AI search Stage 5 (spec `docs/superpowers/specs/2026-11-13-ai-search-stage-5-sites-you-use-design.md` in IanDBallard/Cayenne). **Deploy order:** Cayenne's `refusing_hosts` migration first, then this release, then the Cayenne client.

### ✨ Added
- Answers report gaps: each alternative of a recipes answer carries `gaps` (`{group, missing}`, at most 4; `group` -1 is the alternative as a whole) and a `none` answer carries `gap`. Out-of-range gaps are dropped; the prompt says never to invent one.
- `history[].added`: recipes the cook added from a site to an earlier answer (`{alternative, group, heading, recipe_ids}`, at most 8 per turn); their ids are admitted and named in the request text.
- `POST /search/look`: a look on the cook's own sites (their top 8 recipe hosts, read through `ai_reader` under the cook's claims) for the chosen alternative's gaps, with Anthropic's web search (at most 4) and web fetch (at most 8), 8 rounds, 60 s and a 40k task budget. Request `{question, answer, chosen, measuring, exclude}`; no gaps → 422. Refusals before the stream as `/search/ask` (401, 422, 503, 429); a look spends one search slot. The stream is progress lines, then one `candidates` line (`{gap, url, title, site, note, status: importable|blocked|yours, recipe_id?}`) or one `error` line (`no_sites`, `timeout`, `failed`). A candidate survives only when it is `https`, on one of the cook's sites, and was read by a successful web fetch in that request; titles are cut to 200 characters and notes to 160, as plain text; at most 3 per gap and 8 in all; excluded addresses are dropped. Usage is recorded as step `look`.
- Sites Anthropic's crawler refuses (the whole request fails with a 400 naming them) are remembered for a day in the process, dropped, and the look retried once; with none left the error is `no_sites` ("None of the sites you use can be searched.").
- `scripts/look_goldens.py` and `look_goldens.yaml`: 10 on-demand golden looks with seeded hosts; the report gives each look's cost and time and checks every candidate's host and that it loads. It spends real money; run it only with the owner's OK.
- `public.refusing_hosts` (Cayenne's table, service role only): a URL import refused by a site records its host; a successful URL import from the host clears it. Best effort; never fails an import.
- `POST /jobs` takes `source_url` with pasted `text` (`https`, at most 2,000 characters, never fetched): it becomes the chunk's source address and web citation.

## [9.33.0] — 2026-11-14

Mela import (Cayenne spec `docs/superpowers/specs/2026-11-13-mela-import-design.md` in IanDBallard/Cayenne). Merging deploys (container restart); deploy before the Cayenne client PR merges, since the client starts offering Mela files at Add Recipe.

### ✨ Added
- `MelaReader` (`io/readers/mela.py`): reads a Mela library export (`.melarecipes`, a zip of `.melarecipe` JSON files) or one shared `.melarecipe`. Each recipe becomes one chunk with its picture, times, yield, notes and source link; `favorite` sets `is_favourite` when the row is created (`Chunk.restore` → `RestoreFields`). Mela categories, Want to Cook, `id` and `date` are dropped, as Paprika import drops categories. `#` lines are headings; `mela://` and web links in text are reduced to their labels.
- `InputType.MELA`; `POST /jobs/file` routes `.melarecipes` and `.melarecipe` to it, and exempts both from `MAX_UPLOAD_BYTES`, as Paprika exports are.
- Refusals: "is not a Mela export: not a ZIP archive.", "is not a Mela recipe: not readable JSON.", "is not a Mela recipe: larger than 50 MB.", "contains no recipes." A recipe that cannot be read, is over 50 MB, or has no recipe text is named under "What was missed"; a notes-only recipe sends its notes as the method.
- Pictures are capped at 15 MB each and 512 MB per read; recipes past the cap import without their picture (logged).
- Golden corpus fixture `banana-bread.melarecipes` and its reader golden.

### 🔧 Changed
- `PaprikaReader` and `MelaReader` share the chunk text and citation builders (`io/readers/_recipe_text.py`); Paprika output is unchanged.

## [9.32.0] — 2026-11-13

Cayenne Library AI search becomes a continuing conversation: the phone sends every earlier turn, and the server reads them all.

### ✨ Added
- `/search/ask` takes `history`, every earlier turn of the conversation (0 to 5 turns of `{ question, answer, chosen }`, oldest first; `chosen` is the alternative the cook was looking at). More than 5 turns, or `history` together with `previous`, is a 422. The request text lists each turn, and the answers' recipe ids are admitted from every turn. `previous` and `filters` are still accepted for one release (an old app's `previous` is served as a one-turn history); the next release drops them.
- `ToolBox.admit_answers` replaces `admit_previous`; `scripts/ask_goldens.py` feeds a refinement the whole chain of entries it refines.

## [9.31.0] — 2026-11-11

Cayenne Library AI search, stage 3: `POST /search/ask`, the agent behind a deeper Library search, answered as a stream. Inert until Cayenne's Stage 4 calls it. **Deploy note:** two new secrets, `ANTHROPIC_API_KEY` and `AI_READER_DATABASE_URL` (a Supavisor pooler DSN that logs in as Cayenne's `ai_reader` role, with its password); a Cayenne deploy PR carries them to the VM. Until both are set the endpoint answers 503 and nothing else changes. The owner sets `ai_reader`'s password and creates both secrets.

### ✨ Added
- `POST /search/ask` (`adapters/search_api.py`): `{ text, today, measuring, filters, previous? }`. Refusals before the agent starts are ordinary JSON errors: 401, 422, 503 when not configured or paused, 429 with `Retry-After` past 10 searches an hour or 30 a day per user (exempt users uncapped). After that the response is `application/x-ndjson`: `progress` lines, then exactly one `answer` or `error` (`timeout` or `failed`) line. A cook who leaves stops the agent before its next model call; the search still counts.
- The search agent (`adapters/ai_search/`): a statement gate that admits one `SELECT` over Cayenne's `ai` views (`sql_gate.py`); the `ai_reader` connection, read-only and carrying the cook's token claims so row-level security shows only their recipes (`ai_db.py`); four tools (`tools.py`); the instructions and request context (`prompt.py`); the answer shape and its checks, which keep only recipes a tool returned and the cook owns (`answer.py`); and the loop, at most 10 rounds, 30 seconds and a 40,000-token budget (`agent.py`). Its Claude calls are recorded in `ai_usage` under `search`, step `agent`.
- `config.AI_SEARCH_MODEL` (`claude-opus-5-5`, overridable) and `config.ai_search_secrets()`.
- Hardening: the answer's strings and lists are bounded, so an oversize `previous` answer is a 422 (`finalize` cuts the agent's own answer to the same limits); the 200-row cap is enforced on the server (a server-side cursor, 201 rows fetched), so one huge join cannot exhaust memory; and the 30 seconds cover tool work too (statement timeouts and pool waits cut to the time left, the embedding call at most 8 s with no retry, a round that ends late is a timeout). `gemini.get_embeddings` takes an optional `timeout_secs`.

### ⚠️ Changed
- New dependencies: `pglast>=8.7,<9`, `psycopg[binary]>=3.2`, `psycopg-pool>=3.2`, `anthropic>=2.8.0,<3`.
- `requires-python` raised to `>=3.10` (pglast 8 needs it).
- The AI gate's `check` takes `search=True` for the search rate limit.

---

## [9.30.0] — 2026-11-10

Cayenne Library AI search, stage 1: a recipe's embedding covers its title, its category names, its ingredient lines and the first 400 characters of its directions, and stays current when any of them change. **Deploy note:** deploy after Cayenne's `20261110120000_embedding_rev` migration is applied; before it, `EmbedWorker` logs a missing-function error on every poll and its `ai_usage` rows are rejected.

### ✨ Added
- `build_embedding_text` in `core/stages/embed.py`: the one embedding text, one part per line, empty parts left out. Category names under the Lists root are excluded by the database.
- `EmbedWorker` (`adapters/embed_worker.py`), started with the other workers: claims rows from `claim_stale_embeddings`, embeds them, and writes `embedding` and `embedded_rev` guarded by `embed_rev`; a failure goes to `embed_failed` (three per revision, then the row waits for a change). Body columns are read through `raw_body_column`, so a double-encoded value is a recorded failure. Its Gemini usage is recorded as `embed`.

### ⚠️ Changed
- `embed()` builds its input with `build_embedding_text` and takes an optional `category_names`. Import and regeneration still write an interim vector without categories; `EmbedWorker` replaces it about half a minute later.

---

## [9.29.1] — 2026-11-08

A site behind a PerimeterX (HUMAN) bot wall is reported as refusing automated readers, not as a page with no recipe in it.

### 🐛 Fixed
- `UrlReader` recognises PerimeterX's "Access to this page has been denied." wall, which Jina renders as a 200, and raises `SiteRefusedError` for it: the job now says the site is blocked by its bot-protection page and suggests pasting the recipe's text. Before, the wall's text went to the model and the job said "no recipes found" (thekitchn.com, 2026-11-08). The direct fetch recognises the same wall by its title or its `captcha.px-cdn.net` script, should a site ever serve it with a 200.

---

## [9.29.0] — 2026-11-07

Yield kinds stage 3 (Cayenne D95–D97): a component is kept as one whether or not it names a tin or an area, a crumb crust is a component like any other, and a crumble's crumbs are its topping. **Deploy note:** deploy after Cayenne's stage 3 PR is live (it fits a component with no tin and no area from its weight); the scoped re-run (`yield-scope-3.txt`) runs on this release.

### ⚠️ Changed
- `check_yield` no longer stores a component with neither a fittable tin nor an `area` as servings: it stays a component with `area` null and its line lists kept (D97); Cayenne fits it from its weight. `has_fittable_tin` and `COMPONENT_TIN_FAMILIES` are removed; `check_yield`'s `pans` argument is no longer read.
- `check_yield` no longer stores a crumb crust as servings (D84 superseded by D95).
- `check_yield` reads a `crumb` container with no base as a crumble top, with its component lines moved to topping, so a crumble is no longer dropped as "no base and no top" (D96).
- The yield rules say a crumble, crisp or streusel over fruit is a topping, never a crumb crust, with an apple crumble and a graham cracker crust as examples, and that a crust or dough with no tin and no size is still a component. The prompt snapshots are updated on purpose.
- The probe set flips six stand-alone pastries, doughs and the graham crust to `component`.

## [9.28.1] — 2026-11-06

Yield kinds (Cayenne F-559): a dough by area always carries its container.

### 🐛 Fixed
- `check_yield` stores the pizza-dough container (flat base, no sides, no top, standard thickness, every ingredient line the component) on a component that keeps its `area` but has no container, whether the model gave none or one was dropped, with the warning "yield component by area with no container stored as pizza dough". The model dropped it between runs on two production doughs; the Cayenne client already read such a row as pizza dough, so parser and client now agree in the data.

---

## [9.28.0] — 2026-11-05

Yield kinds stage 2 (Cayenne): pieces and component areas carry the counts and sizes the recipe states. **Deploy note:** deploy after Cayenne's stage 2 PR is live; the stage 2 backfill runs on this release with `--settle-pieces`.

### ⚠️ Changed
- `check_yield` keeps a component that has an `area` (a count and a diameter) even when the recipe names no tin, so a pizza dough sized "4 × 30 cm" stays a component (P21); a component with neither a tin nor an area is still stored as servings.
- `check_yield` stores a `pieces` kind whose `pieces` group was dropped (no whole count of 1 or more) as `servings`, container kept with its line lists emptied, with the warning "yield pieces with no count stored as servings": the Cayenne client scales a pieces recipe by that count and cannot scale a row without it.
- The yield rules name the piece and its stated size: "noun" is the thing itself in the singular, never "serving", "piece" or "slice" when the recipe names it; a galette or free-form tart is usually 1; a topped pizza's count is the pizzas and its size their diameter; "weight_g" and "diameter_mm" only when the recipe states them, never derived from the ingredients; a dough with no tin gives its count and diameter as "area". The prompt snapshots are updated on purpose.
- The probe set (`tests/goldens/yield_kinds/cases.csv`) scores counts and sizes: two optional columns, `expected_count` and `expected_size` (a piece's weight in grams, else its diameter in millimetres; blank is not scored), filled for the stage 1 pieces recipes whose text states them, plus six new pieces cases (60 in all). `scripts/probe_yield_kinds.py` compares them.
- `scripts/backfill_yields.py` takes its second scope (P22: breads, pizza and untinned pastry without a `yield_spec`, the recipes already tagged `pieces`, and servings recipes that carry an `area` or a pizza `container`; run with `--settle-pieces` and `--ids-file`). A tagged pieces recipe whose spec states neither a weight nor a diameter is noted "pieces: no size stated" in the review, and the summary prints `pieces with no size: N`.

---

## [9.27.0] — 2026-11-04

How a recipe's yield is measured (Cayenne yield kinds): servings, pieces, or a component that goes into or onto another recipe, and the pastry or dough container a pie, tart, quiche or crust makes. **Deploy note:** run this release only after Cayenne's `20261103120000_yield_kinds.sql` is live (it adds `recipes.yield_spec`); the stage 1 backfill runs on this release.

### ✨ Added
- `yield_spec` on REFINE's output (`YieldAnswer`: the kind, the container with its pastry, base, sides, top, thickness and the 1-based `component_lines` and `topping_lines`, the area of a component, the pieces of a pieces recipe), asked by one rule block (`_YIELD_RULES`, templated from `core.yields` so the vocabularies cannot drift) whose first test decides the kind and whose second decides the container. After REFINE, `check_yield` runs against the ingredient list REFINE wrote and the stored dict goes on `CayenneRefinement.yield_checked`; the import writes it to `recipes.yield_spec`.
- A yields-only pass: `build_yields_prompt` and `tag_yield`, for the backfill. The ingredients are numbered 1-based over every line, headings included and marked `(heading)`, so the model's positions are the ones `check_yield` maps to ids; the recipe's tins and bake type are given as stored.
- Regeneration writes `yield_spec` too. A yield the cook edited (`edited: true`) keeps the cook's kind, container pills, area and pieces; only `component_lines` and `topping_lines` are re-derived, because ingredient ids are re-minted by position on every regeneration (Cayenne yield kinds P6). The worker reads the row's `yield_spec` with its tins.
- `scripts/backfill_yields.py`: `yield_spec` for existing recipes through the yields-only pass, dry run by default; `--all-users`, `--ids-file PATH`, `--workers N`, `--live --record PATH` (compare-and-set on the revisions, `yield_spec` and every `cook_*` column as read, the record flushed before each write; `--live` refuses without a fresh `--record` and stops before any work if the new columns are missing) and `--restore PATH` (only rows marked written, and only while their `yield_spec` is still the one that run wrote). Each saved kitchen scale is settled once (Cayenne yield kinds D77, D83); `--settle-pieces` is off until stage 2. The stage 1 live run of 2026-11-05 on the 270-recipe scope: 222 servings, 46 pieces, 2 component, 0 failed, one saved scale cleared, one pieces scale left for stage 2, 35 assemblies with no component lines.
- `scripts/probe_yield_kinds.py` and the 54-recipe probe set (`tests/goldens/yield_kinds/cases.csv`): 52/54 on two live runs with the same answers; the two misses are stage 2 kinds.

### ⚠️ Changed
- `check_yield`: a component demoted to servings (no fittable tin, or a crumb crust) has its `component_lines` and `topping_lines` emptied, so the number moves every line; a container with no rolled piece (no base, crumble or no top) has its `component_lines` moved to `topping_lines`. The yield rules say a crumble, streusel or crisp topping's lines are `topping_lines`, never `component_lines` (prompt snapshots updated).
- `check_yield` stores a `component` whose pastry is `crumb` (a pressed graham or biscuit crust) as `servings`, container kept: a pressed crust's thickness differs from rolled pastry's, and stage 3 models it (D84).
- `check_yield` stores `component` only when the recipe has an unremoved tin in the pie tins families (pie, tart, round-cake, square, traybake); a component with none (rough puff, a suet pastry, a pizza dough sized by count and diameter) is stored as `servings` with its container and area kept, because the stage 1 client can only scale a component through a tin. Stage 2 lifts the rule.
- The yield rules, amended after the 54-recipe probe: a crust that makes a double crust, a lid or two rounds (even only in its yield words) is top full, an open shell top none; cookies, biscuits, muffins, cupcakes, scones, brownies, bars and traybakes are servings; galettes, free-form tarts, crostatas, individual pies, hand pies, pasties, turnovers, sausage rolls and pizza baked on a stone, steel, tray or sheet are pieces, only a deep-pan or skillet pizza is servings.
- REFINE's schema gains `yield_spec`; its prompt gains the yield rules under rule 5. The prompt and schema snapshots and the stage and end-to-end goldens are updated on purpose (a `yield_spec` of null throughout).
- Yield hardening after the whole-branch review: a yield check that raises costs the yield, never the recipe; a huge or non-finite count or position is dropped, not raised; a piece's `diameter_mm` is kept only within 10..1000 mm (an infinity can no longer reach jsonb); regeneration re-checks the fittable-tin rule against the cook's tins when it writes them back; an archive whose `yield_spec` is not an object loses only that field on restore.
- Version 9.27.0.

---

## [9.26.0] — 2026-11-01

The owner's second reading of the pie tins backfill dry run: a pie whose filling never bakes can no longer be a crust, a pavlova is never a sponge, a meat pie bound with egg is savoury, and a Gypsy tart is a custard tart (Cayenne pie tins). **Deploy note:** run the scoped pie tins backfill on this release, not on 9.25.0.

### ✨ Added
- `filling_quote` on REFINE's output and the pans pass's answer, asked before `bake_type`: the words of the step that puts a filling or topping into or onto a crust, or null. `check_bake(..., filling_quote=None)` drops `pie-shell` whenever a quote is given, so step 10 can never catch a recipe that makes a filling (Mango Pie). A yes/no field was tried first, and the model answered it to match its own bake type (Banoffee Pie: "no filling", `pie-shell`).

### ⚠️ Changed
- A meringue, pavlova or dacquoise is never `sponge`, even baked in cake tins as layers (Strawberries and Cream Pavlova).
- A filling that is mostly meat is `pie-savoury`, never `pie-custard`, even when eggs bind it or are baked on top (Sweet and Spicy Beef and Pork Pie); bacon pieces in an egg-and-cream quiche stay `pie-custard`.
- Evaporated or condensed milk whisked with sugar and baked until set is `pie-custard` (Gypsy Tart).
- `pie-savoury` is for savoury fillings only: a sweet suet pond pudding had followed the crust-or-lid rule.
- Step 10 says the recipe's whole result is an empty crust for another recipe to fill.
- The probe set gains Strawberries and Cream Pavlova (null) and Sweet and Spicy Beef and Pork Pie (`pie-savoury`).

---

## [9.25.0] — 2026-11-01

Pie plates and pie dishes are their own tin family, a pie or tart crust alone is a bake type, a tray that only carries another tin is not a tin, and a choice of tins is the first (Cayenne pie tins, D57 and D59; bug report 18274a1a). **Deploy note:** needs Cayenne PR #427 first: its migration admits `pie-shell` to `recipes_bake_type_check`. The scoped backfill runs after this deploys, on the owner's OK.

### ✨ Added
- `PAN_FAMILIES` gains `pie` (a pie plate, pie dish, pie pan or pie tin); `BAKE_TYPES` gains `pie-shell` (a pie or tart crust alone, pastry or crumb, baked empty or not baked). The `RecipePan.family` description and the bake-type count (now 14) follow.
- `scripts/backfill_pans.py --ids-file PATH`: restricts a run to the recipe ids listed one per line (blank lines ignored); it combines with `--force`, `--review`, `--record` and `--live` like `--recipe-id`, and the two cannot be given together. The run says how many listed ids were read and how many were not found (Cayenne pie tins D60).

### ⚠️ Changed
- The tin rules REFINE and the pans-only prompt share: a pie plate, pie dish, pie pan or pie tin is `pie`, a tart, flan or quiche tin is `tart`; a pie dish named only by its capacity is `pie`, `round`, with `capacity_ml`; a baking sheet, tray or roasting tin that only holds another tin or dish (a pie plate on a baking sheet, a water bath, a drip tray) is not listed; a choice of tins or a range of sizes ("8-inch square or round", "9- or 10-inch springform") is one tin, the first size and shape named, for every tin rather than dishes only (F-367's first half).
- Bake types: a recipe for a pie or tart crust alone is `pie-shell`, a new last step 10; other pastry or dough alone stays null. A pie whose filling does not bake in its crust (banana cream, chiffon, banoffee) stays null, and step 3 now says that null "stops here". With `pie-shell` inside step 3, as the plan first worded it, the live probe read Banana Cream, Lime Chiffon, Mango and Banoffee Pie as `pie-shell`; as step 10 with the stop, all ten crust and crust-only-pie cases were right in three runs out of three.
- After the owner read the first dry run: `pie-savoury` needs a crust or lid of pastry, suet or mashed potato, and a filling rolled in tortillas, pasta or leaves, or packed into a loaf tin with no crust or lid, is null (Beef Enchiladas and Perfect Nut Roast had been called `pie-savoury`). In step 10, puff pastry baked flat to be stacked with cream and fruit into a torte is not a pie or tart crust, so it is null (Friesentorte). Step 3's crust-only-bake null also says "never pie-shell". The probe set gains Beef and Chicken Enchiladas, Perfect Nut Roast and Friesentorte, all null.
- The bake-type probe set gains the pie crust and Graham Cracker Crust (`pie-shell`); "Good for Almost Everything Pie Dough" and "Hot Water Crust Pastry" move from null to `pie-shell` under the amended rule.

---

## [9.24.0] — 2026-10-30

Lists are hidden from tagging and re-tagging, and a Cayenne restore files a recipe's lists under the account's own Lists root (Cayenne Lists and bulk categories). **Deploy note:** needs Cayenne's `is_lists_root` column (PR A) applied first; the AI-path selects and the restore read it.

### ✨ Added
- `_cayenne_meta.list_paths` (`CayenneRecipe.list_paths: List[List[str]]`, default `[]`): each path starts below the Lists root, e.g. `[["For Sue"], ["Parties", "Birthday"]]`. An older export without the key reads as no lists.
- `ensure_category_paths(..., list_paths=())`: creates the missing list pills under the account's Lists root, found by `is_lists_root` and never by name (the owner may have renamed it "My lists"). A name that exists anywhere in the account is reused, not duplicated (names are unique per user). The restored recipe is linked to each list's deepest placed pill, never to the root itself. An account with no Lists root skips its `list_paths` with a warning.

### ⚠️ Changed
- Lists never reach the tagging prompt or a re-tag; `/jobs/recategorize` refuses a Lists pill with 422 `Lists can't be tagged automatically.` and the AI-path category selects carry `is_lists_root`.
- A restore's category read selects `is_lists_root`. `category_paths` in a new export no longer holds anything under Lists; they come back through `list_paths`.

---

## [9.23.0] — 2026-10-25

A picture an import would store is shown to the model first, and one that is not food is not stored (Cayenne Fix Roadmap F-209). The trigger: a beta tester's "Roast Duck Breasts with Pomegranate-Chile Sauce" (bug report cc9a6210, 2026-10-25), an Epicurious page saved as a PDF and uploaded (`POST /jobs/file`, a one-chunk book job), was stored with the page's stock photo of a family in an SUV: the model named that photo from the page's image markers, and the pixel checks (`photo_check.py`) cannot tell a car from a dish. Merging deploys (container restart); nothing to migrate. Pictures already stored are untouched (F-210 is a later pass, which can reuse the same check).

### ✨ Added
- `core/picture_check.py`: the policy, with the model call injected. `check_picture` downscales the picture to a 512 px JPEG and asks for a typed verdict, `FoodVerdict(kind=...)`, one of `dish`, `ingredients`, `cooking` (kept) or `people`, `vehicle`, `logo`, `advert`, `text`, `drawing`, `blank`, `decorative`, `other` (refused: the recipe goes without a picture and the app shows its placeholder). A check that cannot run (the call fails or times out, the bytes do not decode) keeps the picture, as before, and logs it; it never fails the import. `first_food_picture` tries a page's candidates in order and keeps the first that is food, asking about at most three.
- `gemini.ask_is_food`: one call on `GEMINI_FOOD_CHECK_MODEL` (default `GEMINI_MODEL`, gemini-3.1-flash-lite) at `MEDIA_RESOLUTION_LOW`, bounded by `FOOD_CHECK_TIMEOUT_SECS` (20 s), one retry. Recorded in `ai_usage` under step `Picture food check`, in the import's attribution, like every other call. Measured on synthetic images: 391 prompt tokens and 11 output tokens a check, about $0.0001 at the model's list price.
- `PageMeta.images`: the pictures a page's JSON-LD Recipe declares (a URL, an ImageObject, or a list of either), the candidates after its og:image.

### ⚠️ Changed
- Every import route asks before it stores. A web link tries its og/twitter image, then its JSON-LD images, then the scraper's markdown images (badges dropped by address first), fetching and checking each in turn. A book page (PDF, EPUB) checks the photo the model named; when the chunk holds that recipe alone, the page's other pictures follow it as candidates. A Paprika entry's embedded photo is checked before it is stored. `RecipePipeline` takes the call as `food_check` (none: every picture kept, the CLI's behaviour); each check takes a rate-limiter slot.
- A Cayenne restore's picture is not checked: it is the cook's own library, and may be a picture the cook chose. A photo of a page (`ImageReader`) and pasted text carry no picture, so neither asks.

### Known gaps
- A Paprika entry with no embedded photo keeps its `image_url` address as Paprika recorded it, unfetched and unchecked, as before.
- No golden set of real photographs: the verdicts on the owner's five calibration samples (F-209) are not recorded here, and third-party photographs are not committed. The tests use stubbed verdicts and synthesised images.

---

## [9.22.0] — 2026-10-24

The counts a method writes scale with the recipe (Cayenne scaling text audit 2026-10-24; Cayenne Fix Roadmap F-491, F-492, F-495). The owner's report: "Rich, Fudgy Brownies" at ×1½ still said "all four of the eggs" and "Cut into 16 squares". No prompt changes and no model call: a deterministic pass after REFINE. Merging deploys (container restart); nothing to migrate. Existing recipes take the same pass through `scripts/backfill_count_tokens.py`, which the owner runs (dry run 2026-10-24 over 2,615 rows: 241 would change).

### ✨ Added
- `core/count_tokens.py`: a count of an ingredient before its mention becomes an amount token, `all {{ing_04|four|4}} of the {{ing_04|eggs|none}}`, when the ingredient is counted by the piece and the count is its whole amount or a share of it ("2 of the eggs"); never a range ("one or two") or a rate ("at a time", "each"). A count of what the recipe makes becomes a yield token, `Cut into {{yield:16|16}} squares`, only after a verb that cuts, divides or makes, before a word for the pieces, and only when the number is the recipe's own yield. ASSEMBLE runs it against the yield it just settled; the regeneration worker against the row's own servings, which it now reads with the tins.
- The `yield:` token in the grammar (`is_yield_id`, `yield_token`); `strip_fat_tokens` gives its words back like any other.
- `scripts/backfill_count_tokens.py`: the same pass over stored rows, dry run by default, `--rows-json` for rows exported read-only, `--live --record` and `--restore` as the other backfills.

### ⚠️ Changed
- `check_mentions` demotes `all` or `rest` on words that name no one ingredient ("Measure all the ingredients", "the mixture", "the dough" for the flour) to `none`, unless the ingredient's own name holds the word: the chip inserted an amount the source never wrote.

---

## [9.21.1] — 2026-10-24

The bake type rules no longer give a pie whose filling never bakes a filling's bake type, and a savoury tart set with eggs and cream is `pie-custard` (Cayenne Fix Roadmap F-487: a custard's set cue under a crumb crust, a pot pie's under an open tart). Merging deploys (container restart). Stored bake types change only when the owner runs the backfill below.

### ⚠️ Changed
- `_TINS_BAKE` (REFINE and the pans prompt): a pie or tart is one only when its filling bakes in it; a filling cooked on the hob or set in the fridge, with only the crust (blind or crumb) baked or only a meringue browned, is null (banana cream, chiffon and banoffee pie, a curd in a baked case); "lemon meringue" leaves the `pie-custard` examples. A no-bake cheesecake is null. "Any savoury tart filled with eggs and cream" is `pie-custard`; `pie-savoury` keeps the savoury tarts and galettes that are not. Proved by `scripts/probe_bake_types.py --runs 2` with 15 new cases (71): 68/71 and 67/71; every new case as expected in both runs but Gypsy Tart (`pie-nut` once). The misses are Moussaka (not read), Turkey Enchiladas (`pie-savoury`; already unstable under the old rules, which gave none then `pie-savoury` on a re-probe) and Black Bun (the recorded F-302 miss). Under the old rules Banana Cream Pie, Mango Pie and Leek and Caerphilly Tart missed in both runs.

### ✨ Added
- `scripts/backfill_pans.py --bake-type TYPE` (repeatable): only recipes stored with that bake type. The summary counts each change of bake type. The F-487 dry run, which writes nothing:
  `python scripts/backfill_pans.py --all-users --force --bake-type pie-custard --bake-type pie-savoury --bake-type pie-nut --bake-type pie-fruit --bake-type cheesecake --review bake-types-review.csv`

---

## [9.21.0] — 2026-10-24

A site that refuses automated readers is a typed reason on the job's row, so Cayenne names the site and offers a screenshot or the recipe's text instead of "The import failed" (Cayenne bug report 37a013af, Allrecipes answering HTTP 403; the owner's ruling of 2026-10-24: no evasion of bot protection; Cayenne Fix Roadmap F-480). Merging deploys (container restart). **Deploy Cayenne's migration `20261024130000_ingestion_job_error_reason.sql` first**: until it lands `ingestion_jobs` has no `error_reason`, PostgREST refuses the finalize that names it, and a refused site's job row would hang at running.

### ✨ Added
- `ingestion_jobs.error_reason` on a failed job: `{"code": "site_blocked", "host": "allrecipes.com"}` when the reader raised `SiteRefusedError` (`_job_error_reason`). Every other failure writes none, and `error_message` stays the job's sentence for older clients.
- `SiteRefusedError.host`: the refusing site, lower-cased without www. (the redirect's final host for the direct fetch).

### ⚠️ Changed
- A bot-protection challenge page that Jina renders in place of the article is a `SiteRefusedError` too, with the same "Open it in your browser, copy the recipe and paste its text in place of the link." as the direct fetch's refusal (was a plain `UrlFetchError` with no advice).

---

## [9.20.0] — 2026-10-22

Every recipe an import writes records the job that wrote it, so Cayenne's Recent imports can open exactly what a job made: the one recipe, or the library filtered to that job (the owner's request of 2026-10-22; Cayenne Fix Roadmap F-473). Merging deploys (container restart). **Deploy Cayenne's migration `20261022120000_recipe_ingestion_job.sql` first**: until it lands the `recipes` table has no `ingestion_job_id`, and PostgREST refuses every recipe insert that names it, so every import would fail.

### ✨ Added
- `write_recipe_to_supabase` takes `ingestion_job_id` and writes it to `recipes.ingestion_job_id`; `JobSink.on_result` passes its job's id for every recipe a job writes. A recipe written outside a job (the CLI) stores null. The column is not exported: `_cayenne_meta` and the zip leave it out, so a restored recipe belongs to the job that restored it.

---

## [9.19.0] — 2026-10-21

A share holds up to 2,000 recipes, so a whole library goes in one share (the owner's ruling of 2026-10-21; Cayenne Fix Roadmap F-461), and the `share_accept` copy job is made to last a job of that size. Merging deploys (container restart). No schema change here: **deploy Cayenne's migration `20261021090000_share_limit_2000.sql` first**, since until it lands the database still refuses a share over 200 (a 422 "That share could not be made…").

### ⚠️ Changed
- `POST /shares` refuses more than 2,000 distinct recipes (was 200) with "A share holds at most 2,000 recipes."; `MAX_RECIPES` stays the one constant, and the parse-time bound on `recipe_ids` and `skip_item_ids` (F-175) follows it at twice that, 4,000 ids, about 160 KB of JSON.
- The copy job counts a share's items with exact counts instead of listing them. PostgREST answers at most 1,000 rows, so a share over 1,000 items held only the first 1,000 and its progress reached 99% half way.
- Every item writes `recipe_count` (accepted so far, as `accepted_count` counts) beside `progress_pct`, and that write is the job's heartbeat.

### ✨ Added
- The job honours `POST /jobs/{id}/cancel` between items: the progress write lands only on a `running` row, so a cancelled row stops the job after the item in hand. The copies made stay, every item not yet copied becomes `skipped`, the share ends `accepted` with the count it reached, and the row keeps `cancelled` with stage `DONE` and that count. A job cancelled before any worker held it is wound down the same way rather than leaving its share `accepting` for good; a picture left by a crashed attempt on a stranded item is removed.

### 🔧 Fixed
- `installer.iss` carries the version again: 9.18.0 left it at 9.17.1, which the installer build's version check refuses.

---

## [9.18.0] — 2026-10-19

A real cancel for imports, bounded Gemini retries, and photos sent at a sane size (Cayenne Fix Roadmap F-426, the first beta bug report: "When I press cancel the process does not stop"). On 2026-10-18 a tester's 4.1 MB, 3000x4000 phone photo was rendered at 2x (6000x8000) and sent as a lossless PNG; Gemini answered 504 DEADLINE_EXCEEDED four times, three cancels answered 200 and did nothing, and the API was OOM-killed (1.48 GB RSS) on the fifth attempt. Merging deploys (container restart). No schema change. Pairs with the Cayenne PR that shows "Cancelling…"; deploy this first.

### ✨ Added
- `recipeparser/core/job_signal.py`: one job's cancel flag and activity clock, carried in a ContextVar to the reader, the chunk workers and the Gemini retry ladder. Outside a job every call is a no-op.
- The job stops at a checkpoint between stages, between chunks, before every Gemini attempt and inside every back-off wait; recipes already written stay, and a cancelled job is not reported as skipped chunks. Tagging the recipes already written is shielded from the cancel (design D1).

### ⚠️ Changed
- `POST /jobs/{id}/cancel` writes `status = 'cancelled'` to the row at once for ingest jobs as well as recategorise, then signals the job. It answers 404 for no such job of the caller's and 409 for a finished one, never a 200 that does nothing; a 200 now says `"status": "cancelled"`. A cancel during a photo's OCR (the job's read, before the controller leaves IDLE) now lands.
- A stage write no longer flips a cancelled row back to `running`, and the final write never overwrites a row already ended as an error.
- A photo is sent upright (EXIF orientation applied), as a JPEG at quality 85 with its long edge at most 2000 px. The tester's photo goes from a 6000x8000 PNG to a 1500x2000 JPEG of 549 KB. PDF pages keep the 2x render, capped at the same 2000 px. Each vision call logs the size it sends.
- The vision call has its own 90 s timeout, and a transient server error is retried only within a 180 s budget per call (rate limits keep their full ladder). A photo or scan that keeps timing out ends with "The AI service took too long to read this photo. Try again in a few minutes." (or "…this document…").
- The liveness tick ends a job this process runs but has heard nothing from for 10 minutes with "The import stopped responding…", and cancels it. "The import stopped when the server restarted…" is kept for a row no live process owns. A row cancelled on another instance stops the job here at the next tick.

---

## [9.17.1] — 2026-10-17

Container and volume yields keep their own count and unit (Cayenne spec `docs/superpowers/specs/2026-10-16-scale-sheet-redesign-design.md` in IanDBallard/Cayenne, the owner's ruling D52). The 9.17.0 backfill's dry run over the live library (1,700 rows: 1,364 set, 336 by the model, 142 flagged for review) gave condiments, spice blends and preserves large model estimates: Garam Masala "1 cup" → 40, Tomato Ketchup "2 cups" → 32, marmalade "3–4 jars" → 40. Merging deploys (container restart). No schema change.

### ⚠️ Changed
- A yield whose unit is a container or a volume (jar, bottle, pot, tub, cup, pint, quart, gallon, litre/liter, l, ml, millilitre/milliliter, fl oz, fluid ounce, singular or plural) is `counted` and never sent to the tin table or the model. `base_servings` is the yield's count: a range's low end ("3–4 jars" is 3), the leading count rather than the size inside ("3 × 1½ lb jars" is 3, "2 × 500 ml jars" is 2), "a jar" is 1, and a volume with only a number keeps it ("750 ml" is 750). A fractional ml or litre count rounds half to even, as `parse_servings` and both twins round: "1.5 litres" is 2 and "2.5 litres" is 2. `servings_source` stays `counted`; the noun in `servings_note` tells a container from an item count, and `Classified.noun` carries it.
- A stated serving still wins ("Serves 4 (makes 2 cups)" is stated 4; "1 jar, about 12 servings" is stated 12), and an item count beats a container count ("2 cups (about 24 truffles)" is counted 24, "4 cups, makes 16 muffins" is 16); the container rule applies only when no serving word or item count is present. Words with no container (loaf, batch, cake, tart, pan, tin) keep the tin or estimate path, and a bake noun beside a volume keeps it too ("1 x 2 litre dish").
- Basins, moulds (molds), pudding basins and dishes are bake vessels: "1 x 1.2 litre pudding basin" and "a 2 litre dish" stay on the tin or estimate path. A stored approximate bare number ("1 jar, about 12") is treated as a lost serving number, so it falls to the bake path. Accepted edge: "makes 4 cups soup" steps in cups.
- Cup, cups, quart(s), litre(s), liter(s), jar(s) and pint(s) are no longer bake nouns. The shared fixture `tests/fixtures/servings_source_cases.json` gains the container rows, and "Makes 4 cups", "3 jars" and "4 cups" move from `bake` to `counted`: Cayenne's TypeScript twin must take the same rule and the fixture copy together.

### 🔧 Operational
- Re-run the backfill's dry run before `--live`. 9.17.0's dry run wrote nothing, so every row is still unsourced and is classified afresh by this version (a re-run skips only rows that already have a source); the container rows now classify `counted` without asking the model, so fewer rows reach Gemini and the `check` list changes.

---

## [9.17.0] — 2026-10-16

Servings at import, with their provenance (Cayenne spec `docs/superpowers/specs/2026-10-16-scale-sheet-redesign-design.md` in IanDBallard/Cayenne, D44, D47, D50 and *Estimating servings*). Every recipe now leaves ingest with a `base_servings` and a `servings_source` saying how it was reached: `stated` (the recipe says so), `counted` (it counts items), `tin` (worked out from the tins it names) or `estimated` (the model's guess, the last resort). Merging deploys (container restart). Deploy order: the Cayenne migration, then this version, then the backfill.

### ⚠️ Requires — apply the Cayenne migration **before** deploying this version
- `servings_source` on `recipes` (Cayenne scale-sheet plan, PR A). Every INSERT now writes the column, so against a schema without it PostgREST rejects the row and every ingest fails.

### ✨ Added
- `core/servings.py`: the deterministic yield rule. It classifies the yield text as stated, counted, the bake itself or none, and composes that with the tin table and the model's estimate. It shares `tests/fixtures/servings_source_cases.json` with the Cayenne TypeScript twin.
- `core/tin_servings.py`: the tin table (`HINT_TINS`, equal to `tests/fixtures/hint_tins.json`, which Cayenne's drift test reads) and `estimate_from_tins`.
- `servings_source` on `CayenneRecipe` and in the Supabase row; `ServingsSource` in the models.
- `gemini.estimate_servings`, and `scripts/backfill_servings.py`, which gives every existing recipe its `servings_source` and corrects a `base_servings` that was a guess. The deterministic rule runs first; the model is asked only when the recipe's words and its tins give nothing.

### ⚠️ Changed
- REFINE's `base_servings` is now the model's estimate of the people one batch serves (never the number of cakes, loaves or tins), used only when the yield gives nothing.
- A Cayenne restore keeps an exported `servings_source`; for an archive that predates it, it derives one without the model, so it still makes no AI call.
- Singular item nouns are recognised at a count of one (cookie, brownie, blondie), after the bake nouns, so "1 swiss roll" and "1 bar cake" are the bake.
- The yield rule takes the number next to a serving word ("Makes 1 loaf (serves 10)" is 10) and the number on an item noun anywhere ("2 loaves (24 slices)" is 24; a dozen is 12); a stored "12 one 9-inch pan" stays 12 servings; a method count needs a portion noun ("cut the butter into 8 pieces" is no count).
- Ingest leaves `base_servings` null when nothing places it, rather than falling back to the yield's first number ("1 cake" was 1 serving).
- The tin table skips a tin counted below one or with a non-finite or non-positive size; only round and square tins stack as layers (two 13x9 traybakes are 28).
- The Paprika contract archive fixture carries `servings_source` (regenerated).

### 🔧 Operational
- Run the backfill in this order. Nothing is written until the third step.
  1. Dry run: `python scripts/backfill_servings.py --all-users --review ../backfill-records/servings-review-<date>.csv`
  2. The dry run still calls Gemini for rows the rule cannot place, so it needs `GOOGLE_API_KEY` (with `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`). Rows that are `stale` (`derived_rev < body_rev`) are skipped, not written, and appear in the review CSV with status `stale` and no moved_pct, for the regen worker to re-derive; once it has, re-run the backfill (it skips sourced rows). Read the CSV and review the `check` rows (those that move more than 25%, and those that read as stated but now fall to the tins or the model, even at the same base).
  3. For real: `python scripts/backfill_servings.py --all-users --live --record ../backfill-records/servings-before-<date>.csv`. Rows the rule cannot place are asked of the model again in the live run, so a model value may differ from the one in the dry run's CSV.
- Without `--force`, a re-run skips rows that already have a source; a `stated` row is never touched, `--force` or not. To undo a run: `python scripts/backfill_servings.py --all-users --restore ../backfill-records/servings-before-<date>.csv --live`.

---

## [9.16.0] — 2026-10-16

Lossless Cayenne restore (Cayenne spec `docs/superpowers/specs/2026-10-16-paprika-export-design.md` in IanDBallard/Cayenne, plan B). A `.paprikarecipes` file exported by Cayenne now restores into a fresh account exactly as it was written, with no AI call and no allowance spent. Merging deploys (container restart); deploy before the owner's restore check (export, then restore into a fresh account, with no AI usage recorded). It can merge before or after the Cayenne client PR: older clients never send version 2.

### ✨ Added
- `_cayenne_meta` version 2 restore fields on `CayenneRecipe`: `is_favourite`, `category_paths`, `source_uom_system`, `image_source`, `stale`; `RecipePan.removed`. Version 1 entries still read.
- Category creation: a restore creates the category paths the new account lacks, level by level, reusing a name that exists anywhere (`restore_paths.py`). A category-creation failure logs and continues; the recipes still import, linked by leaf name.
- Picture copy: a bucket picture is downloaded from the old address and stored in the new account (`picture_copy.py`).

### 🐛 Fixed
- The cook's title is kept as written, not title-cased.
- For version 2 entries, the nine time spans and the base servings are restored as exported, not re-derived. A pre-v2 archive still re-derives them from the recipe text.
- The measuring system (`source_uom_system`) and `image_source` are written to their columns.
- Removed tins are no longer revived: `removed` is kept out of both Gemini schemas (`schema_without_client_fields`; see the Cayenne spec above).
- Stale rows restore stale (`body_rev` 1, `derived_rev` 0).

### ⚠️ Changed
- A corrupt `_cayenne_meta` entry is skipped, not re-extracted: it is one skipped line, never sent to the AI pipeline (see the Cayenne spec above). Embeddings are kept only when exactly 1536 finite numbers.
- A failed picture copy (log warning, old address dropped) and a category path too deep (reported as a refused link) are not skipped lines, so the client does not read them as "a section that produced no recipe". A failed copy also clears `image_source`, so a row is never left with no picture but a `generated` source.
- `category_paths` and `stale` read `null` as `[]` and `false`, so a v2 entry carrying null is restored rather than skipped.
- A restore cleans category path names in one shared helper (strip, drop empty) for both the creation and the link lookup, so a padded name still links.
- The bucket-picture source (a Supabase client) is built only for a job that has a Cayenne restore chunk; URL and EPUB jobs never build it.

---

## [9.15.0] — 2026-10-16

Households (Cayenne spec `2026-10-01-household-shopping-design.md`, Part 3; plan `2026-10-14-household-stage-3b-recipeparser.md`). Six endpoints let a household of Cayenne accounts share one shopping list. Each is one JWT-verified call to one Cayenne database function, which is the transaction.

### ⚠️ Requires — apply the Cayenne migration **before** deploying this version
- `20261016090000_household_membership.sql` (Cayenne Stage 3a, [Cayenne#303](https://github.com/IanDBallard/Cayenne/pull/303)): `household_invites`, `household_links` and the membership functions. It is already live in production (2026-10-15). Order: Cayenne 3a live, then this deploy, then Cayenne 3c (the client).

### ✨ Added
- `recipeparser/adapters/household_api.py`, registered from `api.py` with one `include_router`. It shares the share endpoints' recipient-check limiter (20 lookups an hour) and their idempotency cache. The endpoints:
  - `POST /household/invites`
  - `POST /household/invites/{id}/accept`, `/decline` and `/cancel`
  - `POST /household/leave`
  - `POST /household/members/{user_id}/remove`
  - `POST /household/copy`
- Each Postgres refusal maps to its own status and sentence (409 already a member or already invited; 422 household full; 409 leaving a household of one). Any other database error is a 503, never a misleading 404.
- The share worker's sweep also expires and purges household invites, on the share timers (every 15 minutes, and daily).

---

## [9.14.0] — 2026-10-15

AI caps for the beta (Cayenne spec `2026-10-15-ai-caps-and-invite-gate-design.md`, D2–D10). Every Gemini call is recorded in `ai_usage` against its user and action. The kill switch (`ai_settings.paused`) refuses AI work for everyone but exempt accounts. An import stops at the account's `recipe_cap` and finishes `done` with a note (a Cayenne restore never counts against it). Search embeds are limited to 60 a minute per user.

### ⚠️ Requires — apply the Cayenne migration **before** deploying this version
- `*_ai_allowance.sql` (AI caps plan A): `ai_allowance`, `ai_usage`, `ai_settings`, `consume_recipe_allowance`. Without it every AI request is refused as paused, because the allowance row is missing and the gate fails closed.

### ✨ Added
- `recipeparser/usage.py`: per-request attribution; picture generation now records its tokens too.
- `recipeparser/adapters/ai_gate.py`: 503 when paused, 429 at the cap and over the search rate. Never 403.
- `JobSink`: one allowance spent per AI-made recipe. Cayenne restores spend none.
- The regeneration and re-categorise workers are attributed, and wait out a pause for non-exempt owners.
- A recipe refused at the cap is neither written nor tagged, so no AI is spent on it after the cap.
- A `.paprikarecipes` upload is gated after it is read, and only when some recipe in it needs AI, so a Cayenne restore runs even at the cap, and while paused when it carries its stored embeddings (owner ruling; a restore without them calls EMBED, which the pause covers and the cap does not). Every other upload and `POST /jobs` are still refused at the endpoint; a refused Paprika export finishes `error` with the same sentence.
- While paused, a regeneration row whose owner isn't exempt is left to its claim lease rather than released, so exempt owners' rows keep moving.

## [9.13.2] — 2026-10-15

### 🐛 Fixed
- A baking dish now gets a tin family. The tin rules named tins and pans but never dishes, so 13 dimensioned dishes in the live library ("an ovenproof dish, about 18 x 23cm", "9-by-13-inch baking dish") came back with no family and no recipe baked in one could be fitted in Cayenne. A baking, ovenproof, gratin, casserole or pie dish now takes the family of its shape (rectangular traybake, square square, round round-cake, a pie dish tart), a rectangle is traybake and never square (F-298's 9 x 13 wobble), and a choice of sizes ("8x12 or 9x13-inch dish") takes the first. A dish sized only by capacity ("1.2 litre pie dish") still has no family: fitting by volume is a design of its own. Shared by REFINE and the pans backfill. After deploy, rerun the affected recipes with `backfill_pans.py --recipe-id … --force`.

## [9.13.1] — 2026-10-14

Bake types defined by how the item is made (Cayenne docs/superpowers/specs/2026-10-14-pan-families-savoury-pies-design.md, D29). No schema change; no Cayenne change needed.

### 🐛 Fixed
- The bake type rule gives each of the 13 types a definition by method (whisked foam, creamed or melted batter, yeast or chemical leavening, egg-set, crust or lid) instead of a few examples, and the model takes the first that fits, in a fixed order. `pie-savoury` now needs pastry, a suet crust or a mashed-potato lid: casseroles, gratins, lasagne and moussaka get no bake type, and neither does pastry on its own. A creamed cake is `butter-cake` even when it is called a sponge; yeast cakes are `bread-enriched`; fruit crumbles and cobblers are `pie-fruit`; cookies, granola and meringues get none. Shared by REFINE and the pans backfill.

### ✨ Added
- `scripts/probe_bake_types.py` and `tests/goldens/bake_types/cases.csv`: 56 of the owner's recipes at the boundaries, with the expected type. Read-only; reports matches and run-to-run agreement. On the first 50 cases, 9.13.0's rule matched 34 (46 stable over two runs). This rule matches 55 of all 56 in both runs, stable 56/56; the miss is Black Bun (a sweet filling in a hot-water crust, read as savoury). Over the whole library, two dry runs disagreed on 20 of 1,686 recipes.

## [9.13.0] — 2026-10-14

Stovetop vessels and savoury pies (Cayenne docs/superpowers/specs/2026-10-14-pan-families-savoury-pies-design.md, D25–D28). Needs Cayenne migration 20261014150000_pie_savoury: the database refuses `pie-savoury` until it is applied. Order: Cayenne live, then this deploy, then the pans backfill.

### ✨ Added
- Tin family `stovetop`: frying pans, skillets, sauté pans, woks, paella, omelette and pancake pans, griddles, saucepans, pots and lidded casserole pots, wherever the recipe uses them. A baking dish, even one called a casserole dish, is not stovetop. Cayenne recognises these vessels and never fits them.
- Bake type `pie-savoury` (13 values): savoury fillings under pastry or a topping, including shepherd's, cottage and fish pie. A quiche stays `pie-custard`.

## [9.12.1] — 2026-10-14

### 🐛 Fixed
- A tin the cook removed in Cayenne (a tombstone, `removed: true`) stays removed on regen: the cook's list is written back with it, and its `{{pan_NN|...}}` token is unwrapped to plain words. Pairs with Cayenne's F-291 PR.

## [9.12.0] — 2026-10-14

A recipe's tins, bake type and baking step, and the internal temperature it states, for Cayenne's pan-aware scaling (Cayenne docs/superpowers/specs/2026-10-13-pan-aware-scaling-design.md: parser contract, D9, D10, D21, D22, D24). Needs Cayenne migration 20261014090000_pan_aware_scaling (merged with Cayenne #287). Order: Cayenne #287 live, then this deploy, then the backfill.

### ✨ Added
- REFINE writes `pans` (each tin: id pan_NN, raw words, count, shape, family, dims_mm, capacity_ml, catalog_hint), `bake_type` (12 values) and `bake_step` (0-based) on a new ingest and on regen, and wraps the tin's words in the directions in `{{pan_NN|words}}`.
- A `temp:` value may end `,core`: the food's internal temperature, the source's own doneness figure (`{{temp:93C,core|93°C}}`). Kept only on a °C/°F reading between 30 and 110 °C; a `,core` the model puts on an oven, fan or Gas Mark reading is removed and the token kept.
- `recipeparser/core/pans.py`: the checks; a model slip costs the field, never the recipe. An unknown bake type drops its step too.
- `scripts/backfill_pans.py`: gives existing recipes the same fields, pans-only. Run order: dry run with `--review pans-review.csv`, read the CSV (one line per tagged step with its new text, and one line per skipped recipe with its status), then `--live --record pans-before.csv`. `--restore pans-before.csv` puts the four columns back.

### ⚠️ Operational
- Regen reads the row's current `pans` first: when the cook has edited a tin (`edited: true`), their tins are written back unchanged, and a REFINE tin token is kept only if its id and words still name the cook's tin. Not guarded: a tin edit not yet synced when regen reads it can be overwritten.
- Known limitation: removing a tin in Cayenne does not mark the remaining tins edited, so the next regen restores REFINE's tins. Needs a Cayenne-side follow-up.
- The backfill never touches a recipe with a cook-edited tin, even with `--force`; `--force` unwraps the old tin tokens before placing new ones.
- `scripts/backfill_direction_amounts.py` treats a recipe with a `pan_` token as already tagged, like one with a `temp:` token.
- The Cayenne zip writer and the restore path carry `pans`, `bake_type` and `bake_step` (the writer used to fail on a recipe with a tin).
- The backfill gives a recipe where it finds nothing the status `none-found`: not written, no record line. There is no "looked, found nothing" marker, so a rerun asks again; `--limit` is for trial runs.
- `--restore` re-reads each row and skips one with an edited tin (`edited: true`), with a note.
- Run order: `backfill_direction_amounts` before `backfill_pans`, because a legacy recipe whose only tokens are tin tokens counts as already tagged there.

### 🧪 Tests
- Unit tests for the grammar, the checks, REFINE, regen, the Gemini call and the backfill; the REFINE prompt and schema snapshots are updated.

## [9.11.0] — 2026-10-13

Oven temperatures are marked, so Cayenne can show them in the cook's unit (Cayenne oven temperatures design, D1-D15). Needs no migration. Order: the Cayenne client first, then this deploy, then the backfill.

### ✨ Added
- A `temp:` token in `tokenized_directions`: `{{temp:VALUE|words}}`, where VALUE is `[<n>C|<n>F|gas<mark>][,oven][,fan=<n>C|F]` (for example `180C,oven,fan=160C`, `gas4,oven`, `63C`). It has no `use` field and names no ingredient; `words` are the recipe's own words, unchanged.
- REFINE's TEMPERATURES rule: the model wraps a temperature in a `temp:` token (a figure with its unit, a Gas Mark, a described oven such as "moderate oven"). `_check_temperatures` (D15) then unwraps, with a warning, any token whose value does not parse, whose words the directions do not write, or whose figured words carry neither the value's main nor fan number: a model slip costs the conversion, never the recipe.
- `recipeparser/core/temperatures.py`: the deterministic wrap for the clear forms, including a conventional/fan pair in either order ("180°C (fan 160°C)", "160°C fan-forced (180°C conventional)").
- `scripts/backfill_temperatures.py`: wraps the temperatures of existing recipes (rule first, Gemini only for bare figures and described ovens). Run order: dry run with `--review temperatures-review.csv`, read the CSV, then `--live --record temperatures-before.csv`.

### ⚠️ Operational
- `scripts/backfill_direction_amounts.py` now treats any recipe whose directions carry a `temp:` token as already tagged, with or without `--force`; before this release `--force` re-tagged a recipe from its plain text and would have stripped the temperature tokens.

### 🧪 Tests
- `test_temperatures.py`, `test_refine_temperatures.py` and `test_backfill_direction_amounts.py` cover the above.

## [9.10.0] — 2026-10-12

Ingredient section headings ("For the sauce:") are marked, not dropped (Cayenne ingredient headings, D1-D3). Needs no migration.

### ✨ Added
- REFINE gives every ingredient line exactly one entry and marks each with `kind`: `"heading"` for a line that names a part of the recipe and lists nothing to buy, `"ingredient"` otherwise. A heading has its words as `name`, the line as `fallback_string`, and no amount, unit or state.
- The mentions prompt marks heading lines `(heading)` and forbids naming one as a mention.
- A Cayenne-flavoured Paprika archive exported before 9.10.0 has no `kind`; it no longer restores instantly and goes through REFINE instead (new ids).

## [9.9.1] — 2026-10-11

*Apply to existing recipes* tags in the import's batches (Cayenne Fix Roadmap F-256). Needs no migration; the container restart deploys it.

### 🐛 Fixed — a recategorise asked ten recipes a call
- `RecatWorker` batched by 10 where the import's TAG stage asks five, after ten a call let a batch of vegetarian dishes carry two stock-based soups into `Vegetarian` (9.8.0's measurement). Its default is now `TAG_BATCH_SIZE`, and `scripts/retag_axis.py` reads the same constant instead of its own `5`, so one number governs every tagging call. A recategorise makes about twice the calls it did.
- A recategorise job left running across this deploy resumes at five and estimates its failed-batch tally from the new size (the latent case Cayenne F-130 records).

### 🧪 Tests
- `test_recat_worker.py` and `test_retag_axis.py` hold both to `TAG_BATCH_SIZE`.

## [9.9.0] — 2026-10-08

A retried import or share answers the first one (Cayenne Fix Roadmap F-200). Needs no migration; the container restart deploys it. Ship before the Cayenne half: the browser preflights the new header, and only this release allows it.

### 🐛 Fixed — a retry after a timeout made a second job or share
- `POST /jobs`, `POST /jobs/file` and `POST /shares` take an optional `Idempotency-Key` header (8 to 128 letters, digits or hyphens; otherwise 422). The first request with a key runs; a repeat from the same user within 24 hours gets the same answer and makes nothing; a repeat while the first is still running gets 409 "That request is still being handled. Wait a moment and look again." A request that fails frees its key, so a corrected retry runs.
- Keys live in process memory (`adapters/idempotency.py`), like the recipient-check limit: a restart forgets them.
- A replayed file job answers before the upload is copied anywhere, and a replayed share spends none of the sender's 20 recipient checks.
- CORS allows `Idempotency-Key`.

### 🧪 Tests
- `tests/unit/test_idempotency.py`: replay, per-user scope, 409 while pending, release on failure, expiry, the abandoned-claim timeout, the entry cap, malformed keys.
- `tests/unit/test_idempotent_submits.py`: each endpoint replays and makes one; no key behaves as before; a 400, 413 or 404 frees the key; the concurrent 409; no temp file on a replay; one recipient check per share; the preflight.

## [9.8.0] — 2026-10-05

The import tags one axis per call (Cayenne Fix Roadmap F-246, design `docs/superpowers/specs/2026-10-05-per-axis-tagging-at-import-design.md` in Cayenne). Needs no migration; the container restart deploys it. Recipes imported after the 2026-10-03 clean-up carry the old prompt's tags until the owner re-tags them with `scripts/retag_axis.py --since`.

### 🐛 Fixed — the import tags the way the retag does
- REFINE offered every axis in one request, and on the pinned `gemini-3.1-flash-lite` it filled an axis that should stay empty with the nearest ingredient: potato gnocchi and a Victoria sponge came back `Egg` in every run. `scripts/retag_axis.py`, asking one axis per call, scored 105/105 on the same sample. REFINE no longer tags: its prompt loses the categorisation section and its schema the `grid_categories` field, and a reply that carries tags anyway is cleared.
- A new **TAG** stage (`core/stages/tag.py`) tags finished recipes five at a time, one `categorize_batch` call per axis, with the retag's prompt word for word at temperature 0.0, reading the title and the verbatim ingredient lines and steps. Recipes are queued as they are written and tagged every five, with the rest when the run ends, cancelled or not. A batch is tagged in source order, whatever order its chunks finished in, so the same import asks the same prompt. A Cayenne restore is never queued; a recipe whose write failed is not tagged.
- An axis whose call fails twice leaves only that axis empty for that batch; the job counts each recipe it left untagged with its refused links (`JobSink.on_tag_failed`). Tags are written to rows already in the table by the new `write_recipe_categories`, best-effort like the links written with a recipe.
- The desktop CLI's Paprika export carries TAG's tags: the stage writes them onto the recipes before the run returns.
- The regen worker no longer loads the taxonomy to hand REFINE axes whose answer it then discarded.

### 📏 Measured — why five recipes a call, not ten
- `scripts/measure_per_axis_tagging.py` (new) runs a fourteen-recipe sample rebuilt from F-205's record (the 2026-10-03 harness is lost, so this is not comparable with its 105/105). Three runs each: one recipe per call 99/99; ten per call 95/99, French onion soup and a chicken-stock risotto tagged `Vegetarian` in two runs of three when batched beside five vegetarian dishes; five per call 99/99. At five the two soups also had different neighbours, so the neighbours may account for part of the difference. The spec's ten (D1) becomes five, and `scripts/retag_axis.py` batches by five to match. The egg cases passed in every shape and every run.

### ✨ Added
- `scripts/retag_axis.py --since <ISO timestamp>` plans only recipes created after it (design D3). It needs a zone and narrows only a `--plan`.
- `--record-gemini-missing` for the goldens: replay every recorded reply and call the real API only for one that has none, recording it, so adding a call does not re-pay for the rest.

### 🧪 Tests
- `tests/unit/stages/test_tag.py`: one axis per call, the verbatim rows, answers replacing what a recipe carried, a second try rescuing an axis, a twice-failed axis costing only itself.
- `tests/unit/test_pipeline.py`: five at a time with the rest at the end, source order whatever order chunks finish in, `on_result` before tags, no tags for a failed write or a restore, a cancelled run still tagging what it wrote, no calls without axes.
- `tests/unit/test_job_sink.py`, `tests/unit/writers/test_writers.py`, `tests/unit/scripts/test_retag_axis.py`: the tag links and their refusals, `write_recipe_categories`, `--since`.
- The tagging golden set runs through the TAG stage, each recipe alone as a single-recipe import tags it, and its two strict xfails are gone: recorded on 2026-10-05, gnocchi and the Victoria sponge are no longer tagged `Egg`. The e2e goldens carry the TAG stage's recorded replies, and their expected output gains what one-axis tagging adds: `British` on `legacy-photo`'s boiled custard and `Breakfast` on `dual-units`' buttermilk scones; nothing is lost, and `phases-bakers` is unchanged. The five REFINE recordings only the old tagging set replayed are deleted. REFINE's recordings are kept: their key is the raw recipe, which did not change, and any tags in them are cleared.
- Prompt and stage snapshots lose the refine prompt's categorisation section, the schema field and the stage goldens' categories.

## [9.7.3] — 2026-10-04

A refused document says what to do instead (Cayenne Fix Roadmap F-253). Needs no migration; the container restart deploys it.

### 🐛 Fixed — the document refusal is no longer a dead end
- A `.docx` dropped on Add Recipe was refused with "Cayenne can't read .docx files yet." and nothing more. A word-processor or plain-text document (`.docx`, `.doc`, `.odt`, `.rtf`, `.pages`, `.txt`, or one of their content types when the file has no extension) now adds the two ways in that work today: "Save it as a PDF, or copy the recipe's text and paste it here."
- Any other unreadable type keeps the plain sentence ("Cayenne can't read .zip files yet.").

### 🧪 Tests
- `tests/unit/test_select_reader.py`: each document extension, a document known by its content type alone, and a non-document type keeping the plain sentence; the `.docx` assertions here and in `tests/test_api.py` take the new sentence.

## [9.7.2] — 2026-10-04

`title_case` handles the edge cases that a dry run of the title-case backfill found in the live library. Of the 597 titles that backfill plans to change, 17 now come out differently from 9.7.1, all on purpose; the other 580 are unchanged. Needs no migration; the container restart deploys it. Re-run the backfill plan after the deploy.

### 🐛 Fixed — `title_case` edge cases
- Accented letters count as letters: "éclairs" becomes "Éclairs", not "éClairs".
- French and Italian elisions keep their shape: "à l'Alsacienne" stays as it is, and at the start of a title "l'oignon" becomes "L'Oignon".
- The word after a dash starts a clause, glued or spaced: "Tandoori Chicken—My Version", "Blondies - The Best".
- A deliberate inner capital in a camel-case name is kept: "BraveTart". Each part needs at least two lowercase letters, so "cHoCoLaTe ChIp" still normalises.
- Hindi, Urdu and German particles stay lowercase mid-title: "Khare Masale ka Gosht", "Kala Chana aur Aloo", "Käsekuchen ohne Boden", and inside a hyphenated compound: "Gajar-ka-Halva", "Aloo-ki-Tikiya".
- "GF" and "DF" are preserved acronyms.
- In a mixed-case title, a capitalised stop word straight after "(" is kept: "(The World’s Best Cake)".

### 🧪 Tests
- `tests/test_utils.py::TestTitleCaseLiveLibraryEdges`: one test per example above, taken from the live library.

## [9.7.1] — 2026-10-04

Every stored recipe has unique, non-empty ingredient ids, so the kitchen never refuses to open one over a model slip (Cayenne Fix Roadmap F-194). Needs no migration; the container restart deploys it.

### 🐛 Fixed — ingredient ids are unique at REFINE
- Ids were unique only because the refine prompt asks for them, and Cayenne refuses a recipe whose ids repeat or are blank, since the id is the kitchen's loop key. `refine()` now checks them, before the Fat Token check, for ingest and regeneration alike.
- A blank id, or a repeat that no Fat Token names, takes the next free `ing_NN` with a warning; nothing that is already unique is renumbered, because the id is a key, not a position.
- A repeat that a token names is ambiguous, so it raises into the chunk's error boundary instead of storing a recipe the kitchen cannot open.

### 🧪 Tests
- `tests/unit/stages/test_refine_ingredient_ids.py`: unique and non-sequential ids pass untouched; a repeat and a blank are renumbered without colliding; a referenced repeat is refused.

## [9.7.0] — 2026-10-03

Every recipe title is stored in title case, so a source that prints its titles in capitals no longer shouts in the library. Needs no migration; the container restart deploys it. The library's existing titles are brought into line by `scripts/backfill_title_case.py`, run once after the deploy.

### ✨ Changed — titles are stored in house title case
- REFINE passes every title through `title_case`, before EMBED so the stored and embedded titles agree; a Cayenne-native Paprika restore, which skips REFINE, is title-cased in the same place it is rebuilt. A title the owner types in the app is never recased. This is a deliberate exception to verbatim ingestion's "stored exactly as its writer wrote it" (Cayenne, title-case titles ruling, 2026-10-03).
- `title_case` now holds for any title, not only ALL-CAPS ones: it capitalises a word's first letter past leading punctuation ("(VEGAN)" → "(Vegan)"), capitalises the word after a colon, and in a title that has lowercase letters keeps a capitals word of up to three letters standing on its own as an acronym ("BLT", "XO"); one inside a run of capitals is a shouted name ("CHOLAR DAL" → "Cholar Dal"). "McDonald's", "MacArthur" and "eBay" keep their shape. An ALL-CAPS title carries no such evidence, so only the acronym allowlist survives there.
- Foreign particles ("con", "e", "von", "de", "à la", "mit"…) stay lowercase mid-title, unless the writer capitalised one in a mixed-case title ("Ma La Xiang Guo"). "LA" leaves the acronym allowlist: "à la" turned into "à LA".
- An inner stop word of a hyphenated compound stays lowercase: "Sweet-and-Sour", not "Sweet-And-Sour". The `slow-and-low` test moves with it.

### 🔧 Added — `scripts/backfill_title_case.py`
- Plans the change from the live rows and writes one SQL transaction that disables `recipes_own_body_rev` around the UPDATE, so retitling does not send the library to the regeneration worker; each UPDATE matches the title it was planned from. It ends in ROLLBACK unless `--commit`, with a verification SELECT.

### 🧪 Tests
- `tests/test_utils.py`: the new rules, each from a title in the live library's dry-run plan. `tests/unit/stages/test_refine_title.py`: REFINE and the restore shim. `tests/unit/scripts/test_backfill_title_case.py`: the plan and the transaction. The stage goldens for `text-pages.pdf` and `gutenberg-multi.epub` move: their ALL-CAPS titles are now title-cased.

## [9.6.3] — 2026-10-03

The shopping classifier keeps functionally distinct products apart. Needs no migration; the container restart deploys it.
### 🐛 Fixed — a near-twin known food no longer swallows a distinct product
- "Reuse a food from KNOWN FOODS when the ingredient is the same thing" could tempt Flash-Lite to collapse high-similarity distinct items: baking powder into a known "baking soda", evaporated milk into "sweetened condensed milk", flaky finishing salt into "salt". The classify prompt now carries the negative constraint by name — never equate functionally distinct products, even when a known food is close.
### 🧪 Tests
- `tests/goldens/test_classify_golden.py::test_functionally_distinct_products_never_collapse`: the three pairs, each with its near-twin on KNOWN FOODS. The classify golden set is re-recorded with the new prompt; the prompt snapshot moves.
After the live re-tag of the library (2026-10-03: 1,583 recipes written, 0 failed). Cayenne Fix Roadmap F-225 and F-220 (F-225 was filed as F-219 until that ID turned out taken). Needs no migration.
### 🐛 Fixed — an amount written without a space is an amount (F-225)
- `parse_use` required a space between the number and the unit, so the model's `{{id|50ml|50ml}}` was corrected to `none` and the chip fell back to the source's words, unscaled. A unit glued to the number is now read; a fraction ("1/2 cup") still is not, since a use is a decimal. Cayenne's `parseUse` reads the same.
### 🐛 Fixed — the direction-amounts backfill writes and reports as it goes (F-220)
- `scripts/backfill_direction_amounts.py` sent every recipe to Gemini, then wrote the database, the record and the log in one pass at the end: the live run sat silent for most of an hour, and a crash late in it would have thrown away every call. Each recipe is now written, its record line first and flushed, the moment its call returns; a progress line prints every 50 recipes (`--progress N`, 0 for none); stdout is line-buffered, so a redirected log shows it.
### 🧪 Tests
- `test_fat_tokens.py`: `50ml` and `0.5cup` parse. `test_backfill_direction_amounts.py`: with one worker, the first recipe is in the database and the record before the second is sent, and progress prints.


## [9.6.2] — 2026-10-03

The third dry run of the direction-amounts backfill (after 9.6.1). Needs no migration.

### 🐛 Fixed — no whole amount right after a quantity the text writes
- `Peel approximately 3 {{ing_04|bananas|all}}` printed the line's total beside the source's own number: "Peel approximately 3 4 bananas". `check_mentions` now makes an `all` or `rest` token `none` when the text writes a quantity just before it ("3 ", "two ", "one of the ", "2 cups of the "), and counts it as the ingredient's first whole mention so a later `all` does not print the total instead. The match is tighter than the one for amounts on names: a number ending an earlier clause ("Preheat the oven to 350. Add the flour", "Bake for 20 minutes, then add the flour") does not count.

### 🧪 Tests
- `tests/unit/test_fat_tokens.py::TestTheThirdSample`: the bananas, a remainder after "one of the", and three earlier-clause numbers that must not count.

## [9.6.1] — 2026-10-03

The second dry run of the direction-amounts backfill (after 9.5.1). Needs no migration.

### 🐛 Fixed — an amount on a quantity and the name together is no longer an amount
- The model wrapped both in one amount token, `Add {{ing_01|1 cup freshly shelled (or frozen) peas|1 cup}}`, which the Cayenne kitchen prints as "Add 1 cup to it". `check_mentions` now requires an amount token's words to be a quantity and nothing more (`is_quantity`: numbers, units, number words and a few qualifiers such as "heaped" and "about", the same list Cayenne's client holds); a token with more becomes `none`, and the words show as written. The prompt gives the example.
- The prompt names a rate ("1 teaspoon at a time", "2 per ball") as `none`.

### 🧪 Tests
- `tests/unit/test_fat_tokens.py::TestTheSecondSample`: the peas, and what is and is not a quantity. Prompt snapshots move.

## [9.6.0] — 2026-10-02

Cayenne Fix Roadmap F-205. Needs no migration.

### 🐛 Fixed — a recipe is tagged by what it is, not by everything in it
- Both tagging prompts asked only for tags that "describe" (refine) or "apply to" (bulk recategorise) a recipe, so the model answered by what appeared anywhere in it. Potato gnocchi was tagged Egg for the two eggs binding its dough, and "Perfect Minestrone" Chicken for its stock (the owner's library, 2026-10-02). The same reading overtags any axis: a technique for every step, a course for every way a dish could be served.
- `gemini.TAGGING_RULES` is now shared by both prompts. A tag must be true of the dish as a whole. The test is whether a cook browsing that tag would expect to find the recipe there. Usually one tag per axis, none rather than a doubtful one, and the most specific. The rules name no axis, because the axes are the cook's own. The test cuts both ways: chicken stock makes a soup neither a chicken dish nor meat-free.
- The model now sees the tree: a nested tag is listed as `"Thai" (under "Asian")`. `CategorySource.load_parents` supplies the shape that `load_axes` flattens. The file-based sources stay flat.
- CATEGORIZE enforces what a prompt can only ask for (`categorize.axis_tags`). It drops a tag picked beside its own descendant, since the library filter already finds Thai under Asian, and keeps at most two tags an axis. Pruning runs before the cap. The bulk recategorise applies the same rules per axis.
- A bulk recategorise no longer offers an axis that has children as its own tag. A whole-axis request expands to the axis row and its subtree, and a link to the axis row means only "somewhere in this axis". Ingest never offered it.
- The refine prompt's categorisation section is numbered 5. It was a second "3".
- The bulk recategorise prompt no longer tells the model "most recipes will match nothing". That is false for a whole axis, and the new rule ("prefer no tag to a doubtful one") replaces it.

### ✨ Added — `scripts/retag_axis.py`
- Re-tags one axis of a library under the new rules and replaces the axis's links, because a bulk recategorise only ever adds. Two steps. `--plan` asks the model (one call per 10 recipes) and writes every drop and add to a CSV, without writing any data. `--apply` writes exactly that CSV and asks no model. A recipe whose batch fails twice keeps every link. Links on other axes are never touched. Adds run before drops, so a run cut short leaves a recipe over-tagged, never untagged. The plan's drop lines are the undo. `recipe_categories` records no provenance, so a tag the cook chose is replaced too, and the plan names it.

### 🧪 Tests
- `tests/unit/test_tagging_scope.py`: both prompts carry the same rules, the rules name no axis, nested tags are shown under their parents, pruning before the cap, the batch filter per axis, and no axis row offered. `test_taxonomy.py`: `parents_from_rows` and `most_specific`, including a looped chain. `test_pipeline.py`: the tree reaches REFINE and CATEGORIZE, and a failed parents load keeps the tags. `test_recat_worker.py`: a whole-axis job end to end. `tests/unit/scripts/test_retag_axis.py`: the plan, the CSV, the apply order.
- `tests/goldens/test_tagging_golden.py`: a golden set for the model's judgement across six axes. Gnocchi and a sponge are not Egg. Minestrone with chicken stock is neither Chicken nor Vegetarian. A seared braise is Braising, not Sautéing. A Thai green chicken curry is Chicken and Thai, not Asian. A bulk recategorise of Protein follows the same rules. Every case also asserts a tag the dish should get. **Not recorded yet:** it skips until someone with a real key runs `pytest tests/goldens/test_tagging_golden.py --record-gemini -n0` (six paid calls). The golden client learnt the categorize stage (`RECIPES:` is its key).
- The existing refine recordings replay unchanged. They are keyed by the recipe text after `RAW RECIPE:`, which this does not touch. The prompt snapshots carry the new text.

## [9.5.1] — 2026-10-02

What the first live sample of the direction-amounts backfill (20 recipes, dry run) showed. Needs no migration.

### 🐛 Fixed — an amount on an ingredient's name no longer prints as a bare number
- The model sometimes put an amount on the name instead of on a quantity: `Place the {{ing_01|butter|175 g}}`, which the Cayenne kitchen prints as "Place the 175 g". REFINE has the same prompt, so imports and edits since 9.5.0 could carry it. `check_mentions` now requires an amount token's words to be a quantity (a digit, a fraction, a number word). When they are not, it makes the token `none` if the text before already writes a quantity ("Heat 1 tablespoon of the {{canola oil}}"), `all` if the amount is the line's whole amount ("Place the {{butter}}", 175 g of 175 g), and `none` otherwise ("half the {{buttermilk}}", 200 ml of 400).
- The prompt now says it outright: an amount's words are only the quantity, and a mention with no quantity beside it is never an amount.

### 🐛 Fixed — the whole amount shows once
- The model said `all` at every mention of the same food: wash the 1 ½ cups rice, add the 1 ½ cups rice, drain the 1 ½ cups rice. Only the first `all` of an ingredient keeps it now; later ones become `none`. The prompt says so too.

### 🐛 Fixed — the backfill tags the text the kitchen shows, and places mentions in text order
- It placed tokens in the raw `direction_steps`, which carry the source's OCR noise the import cleaned ("1 In a wok … cook. stirring", "30 40 minutes"). Six of twenty recipes would have swapped cleaned text for noisy text. It now tags the current tokens' words, in the recipe's own step numbering, so no word a cook reads changes.
- Mentions were placed in the order the model listed them, so one listed out of order, or twice, lost the rest of its step (seven in one soda bread). Each is now placed on its own, by its context or an unambiguous quote, then taken in text order; a repeat is skipped. Curly quotes and dashes are folded when matching context ("they’ll" against "they'll").

### 🧪 Tests
- `tests/unit/test_fat_tokens.py::TestTheFirstSample`: each case from the sample. The backfill's tests cover the shown-text base and the numbering. Prompt snapshots move.

## [9.5.0] — 2026-10-02

Cayenne's direction amounts design (`docs/superpowers/specs/2026-10-02-direction-amounts-design.md` in the Cayenne repository). Needs no migration. **Deploy the Cayenne client first.** An older client reads `{{id|words|use}}` with the old two-field regex and fills an amount token with the whole amount ("Add about 0.75 cup flour flour"), so this must not write tagged tokens before the client that reads them is live.

### ✨ Changed — each mention in the directions says how much of the ingredient it uses
- A Fat Token is now `{{id|words|use}}`. `use` is `all`, `rest`, `none`, or the amount the step states (`0.5 cup`, `60 g`, `2`), in which case the token wraps only the quantity the source wrote: `Add about {{ing_02|1/2 cup (60 g)|0.5 cup}} flour`. Before, every mention was `{{id|words}}` and the kitchen filled each with the ingredient's whole amount, so a recipe that adds flour in parts told the cook to add all of it three times.
- REFINE's rule 2 says so, with the rules one shared text (`gemini._MENTION_USES`) so the re-tagging pass below cannot drift from it. When unsure the model is told to say `none`: a missing number is safe, a wrong one is not.
- `core/fat_tokens.py` is the one grammar: `TOKEN_RE`, `strip_fat_tokens`, `parse_use`. `core/regen.py`, `io/writers/paprika_zip.py` (and through it `cayenne_zip.py`) and REFINE's id check use it, so a stripped step is the words alone, never `flour|all`.
- REFINE demotes, and logs, the uses the arithmetic cannot support (`check_mentions`): an amount that does not parse, every part of an ingredient whose parts add up to more than its line (5 % slack, same unit only), and `all` beside a part of the same ingredient. The Cayenne client applies the same checks again.

### ✨ Added — `scripts/backfill_direction_amounts.py`
- Re-tags a library imported before 9.5.0. One Gemini call per recipe (`gemini.tag_direction_mentions`) answers with quotes, each with a few words of context; `splice_mentions` places a token only where its quote is found verbatim in the raw step, so no word of a recipe can change, and a mention it cannot place unambiguously loses its chip, nothing else. Only `tokenized_directions` is written: ingredients, ids, conversions, embedding, categories and the cook's edits are untouched, and the write is conditional on `body_rev` and `derived_rev`, so an edit during the run wins. Stale recipes are left to the regeneration worker. Dry run by default; `--live` requires `--record`, and `--restore <record>` writes the old tokens back. `--user-id` or `--all-users` (a shared recipe is a row of its own).

### 🧪 Tests
- `tests/unit/test_fat_tokens.py`: the grammar, the checks, the splice (the gnocchi, a repeated word, context, what is dropped), REFINE's demotion. `tests/unit/scripts/test_backfill_direction_amounts.py`: what the backfill tags, skips without calling the model, and reports; `main()` end to end against a fake database and model — a dry run writes nothing, a live run records then writes conditionally, a row edited mid-run is left alone, `--restore` puts the record back. The refine prompt and schema snapshots move; the re-tagging prompt and schema join them. **The recorded refine goldens are not re-recorded** (no key in the session that built this): they still replay two-field tokens, which the client reads by its legacy rule. Re-record them with a key.

## [9.4.1] — 2026-10-02

Cayenne Fix Roadmap F-203. Needs no migration.

### 🐛 Fixed — a cookbook's ornaments and blank crops are no longer offered as a recipe's photo
- PDF and EPUB readers offered the model every image on a page over `MIN_PHOTO_BYTES` (20 KB) as a candidate photo. Byte size let through blank white page crops, paper textures and a black spiky ornament ("Perfect Minestrone"), and the model named them as the recipe's photo when they were the only image on the page.
- `io/readers/photo_check.py` now looks at the pixels of every image that clears the byte floor. It refuses one under 100 px on an edge, wider than 4:1, nearly uniform (luminance spread under 6), or ink-on-paper line art: at least 90 % in the darkest or lightest quarter, at least 80 % in two colours, and fewer than 12 colours. Bytes Pillow cannot decode are kept, as before, rather than lose a photograph to a missing codec. Transparency is laid on white, as Cayenne shows it.
- The thresholds were calibrated on real photographs (colour, black-and-white, low-contrast, narrow-range textures, a photo small on a white page) and on blanks, paper textures, ornaments, silhouettes and text. Every one is classified as intended. **Known gap:** a coloured ornament (red on white) is not ink-dark and still passes.

### ✨ Added — `scripts/clear_book_non_photos.py`
- Applies the same test to the pictures already stored on a user's book recipes (`source_kind = 'book'`, not AI-generated) and clears the ones it refuses (`image_url` set to null). Dry run by default. `--live` requires `--record`, a CSV of every cleared row's id, title, URL and reason; the stored file is left in the bucket, so a wrong call is undone by writing the URL back.

### 🧪 Tests
- `tests/unit/readers/test_photo_check.py`: photographs kept (colour, black-and-white, low contrast, small on a white page, 4:1); blanks, a paper texture, an ornament (on white and on transparency), a block of text, a tiny image and a strip refused; a real PDF page with a photo and an ornament marks only the photo. The photograph is `tests/fixtures/coffee_cc0.jpg`, scikit-image's CC0 `coffee` sample. `tests/unit/scripts/test_clear_book_non_photos.py`: what the clean-up looks at, clears, keeps, and never fetches.

## [9.4.0] — 2026-10-02

Shopping list Stage 2a (Cayenne's shopping design, Part 3 §2). Needs no migration.

### ✨ Added — POST /shopping/classify
- One synchronous call at Generate: the device sends up to 400 scaled ingredients and up to 2,000 known foods, and gets each ingredient's `food`, `aisle` (one of the twelve, enumerated in the response schema), `pantry`, `count` and `count_unit` back. Every quantity on the list is computed on the device; the endpoint reads and writes no table. A reply that fails a structural check is asked for once more, then answered 502 with the reason. Lives in `adapters/shopping_api.py`; the classify call in `recipeparser/shopping.py`.

### 🧪 Tests
- `tests/unit/test_shopping_classify.py` and `test_shopping_endpoints.py`, the model scripted. The classify prompt and response schema join the snapshots, and a recorded golden set (`tests/goldens/gemini/shopping-classify/`) holds naming consistency with known foods, the design's *Evidence* cases, and count tolerances against the real model. The first recording caught the model echoing a cooking measure as count_unit, so the prompt now says count_unit names what is counted - and the recordings prove it complies.

## [9.3.4] — 2026-10-02

### 🐛 Fixed — a site that refuses automated readers says so, and what to do instead
- When Jina fails and the site refuses the direct fetch too (HTTP 401, 402, 403 or 451, or a bot-protection challenge page), the job now says "… refuses automated readers (HTTP 402). Open it in your browser, copy the recipe and paste its text in place of the link." Before, it said only "could not be fetched (HTTP 451)." That status was Jina's policy, not the site's. seriouseats.com, 2026-10-02: Jina answered 451, and the site answered the server with a 402 (pay-per-crawl) while serving a browser normally.
- The direct fetch raises the new `SiteRefusedError` (a `UrlFetchError`) for these cases. Any other direct failure still reports Jina's error, as in 9.3.3.

### 🧪 Tests
- `test_url_direct_fallback.py`: each refusing status, a challenge page, and a non-refusing direct failure (a 404) that still reports Jina's error. `test_api.py`: the whole sentence a job ends with, URL included.

## [9.3.3] — 2026-10-02

### 🐛 Fixed — a site Jina will not read is fetched directly
- `UrlReader` now falls back to fetching the page itself when Jina fails: an HTTP error, a timeout, a blank page or a bot-protection challenge. Jina answered HTTP 451 for every seriouseats.com URL, so each one failed as "could not be fetched (HTTP 451).", but the site serves the page to an ordinary browser GET.
- The direct fetch sends the browser user-agent the page-meta fetch already used. It reads the page's schema.org Recipe JSON-LD (title, description, yield, times, ingredients, steps) and falls back to the page's article text if there is none. A bot-protection interstitial is refused, not read as a recipe.
- When both fetches fail, the job still reports Jina's failure, exactly as before. The direct failure, and Jina's own explanation of a refusal (the response body, which was discarded before), are now logged.
- The direct fetch refuses private addresses at every hop. That covers the host, every address its name resolves to, and every redirect, which is followed by hand. `is_unsafe_fetch_target` and the browser user-agent moved from `adapters/api.py` to `io/readers/url.py`, so both direct fetches share them. A DNS-rebinding server can still race the check, because requests resolves the name again when it connects.

### 🧪 Tests
- `tests/unit/readers/test_url_direct_fallback.py`: a Serious Eats-shaped JSON-LD page read after a Jina 451, the article-text fallback, `@graph` and string instructions, a redirect followed by hand, both fetches failing, a bug in the fallback, a direct challenge page, and private addresses (literal, by DNS, and through a redirect).

## [9.3.2] — 2026-10-01

### 🐛 Fixed — a recipe that runs onto the next page of a short PDF
- `PdfReader` now sends a PDF whose whole text fits in one chunk (`MAX_CHUNK_CHARS`, 30,000 characters) as a single chunk. Before, every page was its own chunk, so a recipe photographed or scanned across a page break reached the model as two halves, and came out incomplete or not at all. A longer PDF is still read page by page. The hero-photo pass still runs on the pages before they are joined. Short documents were the target, but this covers any PDF that fits: `text-pages.pdf` (four pages) is now one chunk too.
- A short untitled PDF (one that fits in one chunk) no longer becomes "an unknown book". Its chunk carries no citation, so the model's reading of the page names the source, as it does for a photo. Before, `resolve_citation` treated "unknown book" as settled, so a cookbook named on the scanned page was thrown away. A long untitled PDF is still one unknown book, so its recipes keep a single source. A PDF whose metadata has a title is still that book.

### 🧪 Tests
- `tests/unit/readers/test_pdf_short_document.py` covers the join, the page path for a long PDF, and the three citation cases. The reader golden for `text-pages.pdf` is now the joined chunk.
- The Gemini-backed goldens (stages, image recovery) keep `text-pages.pdf` on the page path through `read_pdf_by_page`. Its recorded replies are keyed to page chunks, and the whole-document replies need a run with a real key (`--record-gemini`).

## [9.3.1] — 2026-10-01

### 🔒 Fixed — a share job copies only a share being accepted
- `ShareWorker` now refuses a `share_accept` job whose share is not `accepting`, and ends the job `error` with "The share is not being accepted". Before this, a job row made any way other than `accept_recipe_share` would copy the share's pending items, even if the share was pending, declined, cancelled by its sender, or expired. Production's `ingestion_jobs` carried a hand-made policy that let users insert their own job rows, which made this reachable. Cayenne removes that policy separately; this guard holds even if a write path ever reopens.

## [9.3.0] — 2026-10-01

Recipe sharing: five endpoints, a copy job and a sweep (#75). Needs one Cayenne migration.

### ✨ Added — recipe sharing (#75)
- `POST /shares/recipient` checks whether a Cayenne account uses an address, at most 20 lookups an hour per sender. `POST /shares` creates a share of up to 200 of the caller's recipes; its lookup of the recipient counts toward the same limit, so a sender can send about ten shares an hour. `POST /shares/{id}/accept` (optionally skipping some items), `/decline` and `/cancel` move a share on. A share the caller is not a party to is a 404; one that has already moved on is a 409. All five live in `adapters/shares_api.py`.
- `ShareWorker` copies an accepted share into the recipient's library, one recipe per poll. Each copy gets its own copy of the picture in storage. The copy's id is derived from the item, so a restart mid-share makes no second copy. It also expires pending shares after 14 days and deletes shares 30 days after they end. It starts with the other workers when `REGEN_WORKER_ENABLED` is set.
- Every change is a Cayenne database function called over `rpc`, so each step is one transaction.

### ⚠️ Requires — apply the Cayenne migration **before** deploying this version
- **`20260930220000_recipe_shares.sql`** — the share tables, `recipes.copied_from_recipe_id`, and the eight functions these endpoints and the worker call. Without it every sharing endpoint answers 503 and the sweep logs an error every 15 minutes.

## [9.2.0] — 2026-09-29

AI recipe picture: a generate endpoint and a generated-picture marker (#68). Also a liveness check for ingest jobs (#63), a read-only similarity script (#64), and Fix Roadmap batches 3 and 4 (#65, #66). Needs one Cayenne migration, already live.

### ✨ Added — AI recipe picture feature (#68)
- `POST /recipes/{id}/image/generate` generates a picture of the recipe's dish using Gemini (`gemini-3.1-flash-image`, override with `GEMINI_IMAGE_MODEL`). The cook previews it in the editor as a pending picture; Save stores it through `POST /recipes/{id}/image` with `source=generated`.
- `POST /recipes/{id}/image` gains an optional `source` form field. `source=generated` marks the picture as AI-generated (written to `recipes.image_source`); omitted, the column is written null. Any other value is a 422.

### ✨ Changed — picture writes set image_source (#68)
- Every `POST` and `DELETE /recipes/{id}/image` writes both `image_url` and `image_source` in a single update, so no device ever syncs a picture carrying the other picture's marker.

### ⚠️ Requires — apply the Cayenne migration **before** deploying this version
- **`recipe_image_source.sql`** — `recipes.image_source` column for the picture marker. Applied by Cayenne's CI when Cayenne#147 merged, before this version deployed.

### ✨ Added — ingest job liveness (#63)
- `adapters/ingest_liveness.py` gives each running ingest job a heartbeat. A job whose process died, for example an OOM or a forced deploy, is ended rather than left `running` for ever. Before this, such a job blocked imports on every device, answered Cancel with a 404, and made `deploy.sh` refuse later deploys (Cayenne F-001). The heartbeat runs whenever the service client exists, whatever `REGEN_WORKER_ENABLED` says.

### ✨ Added — `scripts/measure_similarity_tiers.py` (#64)
- A read-only script that re-derives Cayenne's similar-recipe thresholds (`SAME_RECIPE`, `DIFFERENT_TAKE`) from the library's embeddings.

### 🐛 Fixed — intake guards (Fix Roadmap batch 4, #65)
- Detection evidence must appear as whole words and say something beyond a bare measure (F-010).
- A writer's second measure is checked by its unit as well as its number (F-011).
- Also fixed: the rate limiter's slots, a bad image, and a URL being fetched more than once. Details are in #65.

### 🐛 Fixed — writes and workers (Fix Roadmap batch 3, #66)
- A refused category link is retried row by row, so one bad id no longer loses every link (F-004).
- A null body column fails regeneration instead of regenerating to nothing (F-005).
- Also fixed: claims are released, shutdown is bounded, recategorise does one batch per poll, and worker liveness is tracked. Details are in #66.

### 🔧 CI (#67)
- Every GitHub Action moved to its current major, off the retired Node 20 runtime.

### 🧪 Testing
- 1344 passed, 1 skipped at this version (1272 at 9.1.0).

---

## [9.1.0] — 2026-09-26

A Paprika library of any size imports, and every picture the ingestor stores is scaled to Cayenne's size (#61). No migration; one new dependency.

### ✨ Changed — a Paprika library of any size imports (#61)
- `POST /jobs/file` no longer refuses a `.paprikarecipes` export over 50 MB. The ceiling (`config.MAX_UPLOAD_BYTES`) was sized for one item — a photo, a book, a scan — and a whole Paprika library with its photos runs to hundreds of megabytes. PDFs, EPUBs, photos and recipe pictures keep it. Cayenne's intake drops its matching pre-upload refusal for Paprika in the same change.
- The upload is streamed to its temporary file in 1 MB pieces rather than read into memory, so the job no longer holds the whole body for its lifetime.
- `PaprikaReader.read()` walks the archive one entry at a time (`iter_entries`); each entry's JSON and base64 photo are released once its chunk is built. `read_entries()` keeps its list form for the scripts.
- Each entry is bounded instead of the archive: one that decompresses past 50 MB (`_MAX_ENTRY_BYTES`, photo included) is skipped with a warning, and neither its zip nor its gzip layer is ever inflated past that, so a malformed archive cannot expand without bound.

### ✨ Changed — every stored picture at Cayenne's size (#61)
- `SupabaseImageStore.put()` scales a picture before storing it, to Cayenne's own rule (`io/writers/picture_scale.py`): at most 1600 px on its long edge, upright by its EXIF orientation, flattened onto white and re-encoded as a JPEG at quality 85. A picture already under 1 MB and inside the edge, an animated GIF, and anything Pillow cannot decode are stored as they came. It covers every picture the ingestor keeps: a Paprika library's embedded photos, a photo imported as a recipe, a page's hero image, and a cook's own picture (which Cayenne's editor has already scaled, so it passes through unchanged).
- `POST /recipes/{id}/image` keeps the object `put()` actually wrote when it removes the recipe's older pictures, read from the URL: a large PNG is now stored as a .jpg, and keeping the .png the upload named would have deleted it.
- **New dependency:** `Pillow>=11.0`.

### 🧪 Testing
- 1272 passed at this version (1259 at 9.0.0).

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
