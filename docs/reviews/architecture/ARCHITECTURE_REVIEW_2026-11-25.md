# Architecture review

- Commit: 4971ffb5fe48a3355237fd4b83e7d7f4422b17dc
- Invocation: /architecture-review --update-baseline
- Auditor model: claude-opus-5-5

## Groups

| Group | Status | Files examined | Findings (after exemptions) |
| --- | --- | --- | --- |
| fail-loud | OK | 135 | 15 |
| no-shims | OK | 139 | 50 |
| config/architecture/auditors/layer-imports.md | OK | 33 | 0 |
| state-machine-discipline | OK | 3 | 0 |
| class-cohesion | OK | 59 | 7 |

### Auditor error notes

- fail-loud: check off: exception_base absent (flag 4, exception classes outside the declared base, was not applied; candidates such as recipeparser/exceptions.py:178 AIServiceTimeoutError(RuntimeError), recipeparser/adapters/api.py:124, recipeparser/adapters/brave.py:19, recipeparser/core/picture_gen.py:32, recipeparser/shopping.py:63 and recipeparser/shopping_aisles.py:74,82 were read but not judged against a base)
- no-shims: check off: exempt_paths empty (no file is exempt from version-sniffing check 4)
- state-machine-discipline: check 2 off: base_classes empty (check 1 judged only files that use validator_name 'transition': recipeparser/core/fsm.py, recipeparser/core/pipeline.py, recipeparser/adapters/api.py)
- state-machine-discipline: check does not apply: recipeparser/adapters/gui.py (never calls transition; it only reads controller.status and calls the request_* wrappers)
- state-machine-discipline: check does not apply: recipeparser/adapters/cli.py (never calls transition; it only constructs a PipelineController and passes it to RecipePipeline)
- class-cohesion: check does not apply (no class declared): recipeparser/__init__.py, recipeparser/__main__.py, recipeparser/adapters/__init__.py, recipeparser/adapters/ai_search/__init__.py, recipeparser/adapters/ai_search/find.py, recipeparser/adapters/ai_search/links.py, recipeparser/adapters/ai_search/look_prompt.py, recipeparser/adapters/ai_search/prompt.py, recipeparser/adapters/ingest_liveness.py, recipeparser/adapters/refusing_hosts.py, recipeparser/api.py, recipeparser/categories.py, recipeparser/categories.yaml, recipeparser/config.py, recipeparser/core/__init__.py, recipeparser/core/clock.py, recipeparser/core/count_tokens.py, recipeparser/core/engine.py, recipeparser/core/headings.py, recipeparser/core/numbers.py, recipeparser/core/pans.py, recipeparser/core/regen.py, recipeparser/core/providers/__init__.py, recipeparser/core/stages/__init__.py, recipeparser/core/stages/assemble.py, recipeparser/core/stages/categorize.py, recipeparser/core/stages/embed.py, recipeparser/core/stages/refine.py, recipeparser/core/taxonomy.py, recipeparser/core/tin_servings.py, recipeparser/core/yields.py, recipeparser/epub.py, recipeparser/export.py, recipeparser/io/__init__.py, recipeparser/io/category_sources/__init__.py, recipeparser/io/category_sources/base.py, recipeparser/io/category_sources/restore_paths.py, recipeparser/io/readers/_recipe_text.py, recipeparser/io/readers/book_images.py, recipeparser/io/readers/photo_check.py, recipeparser/io/writers/picture_scale.py, recipeparser/io/writers/supabase.py, recipeparser/logging_setup.py, recipeparser/paprika_db.py, recipeparser/paths.py, recipeparser/pdf.py, recipeparser/supabase_writer.py, recipeparser/utils.py
- class-cohesion: examined: every class body read (module-level functions are outside this rule): recipeparser/adapters/api.py, recipeparser/gemini.py, recipeparser/models.py, recipeparser/core/fat_tokens.py, recipeparser/core/servings.py, recipeparser/adapters/search_api.py

## Findings

### blocking

- `recipeparser/adapters/regen_worker.py:233` — fail-loud: interface probing in a dispatch position — soft.fail-loud
  - Why: run_workers decides whether to skip its sleep by probing each worker (typed Sequence[Any]) for an optional attribute, instead of using a declared worker interface.
  - Snippet:
    ```
    busy = busy or (did_work and getattr(w, "keeps_loop_busy", False))
    ```
  - Suggested fix: Declare a Worker Protocol (or ABC) with run_once() and keeps_loop_busy, have RegenWorker and EmbedWorker declare keeps_loop_busy = False, and read the attribute directly.
- `recipeparser/gemini.py:200` — no-shims: escape hatch hiding a broken contract at a shared boundary — soft.no-shims
  - Why: _call_with_retry silently strips 'deprecated' parameters, so tag_temperatures, tag_pans, tag_yield and estimate_servings (gemini.py:1477,1556,1633,1682) pass temperature 0 for deterministic re-runs and it never reaches the model.
  - Snippet:
    ```
    if k not in ("temperature", "top_p", "top_k", "thinking_budget")
    ```
  - Suggested fix: Remove the silent strip: either make the callers stop passing temperature (and drop their determinism claim) or raise on a parameter the SDK no longer accepts, so the contract break is visible.
- `recipeparser/core/fsm.py:113` — class-cohesion: orchestration, I/O and policy in one class with disjoint clusters — soft.class-cohesion
  - Why: PipelineController holds four clusters: the state machine and cooperative pause (status, _lock, _resume_event), adapter notifications (_on_progress, _on_stage_change), a policy that auto-pauses after repeated 429s (_consecutive_429s, _auto_resume_timer), and JSON checkpoint file I/O (_book_hash, _checkpoint_path, save/load/delete_checkpoint, which touch only _output_dir and share no state with the rest); its own docstring needs 'and' three times, and nothing in the package calls the checkpoint or 429 methods.
  - Snippet:
    ```
    class PipelineController:
    ```
  - Suggested fix: Move checkpoint persistence into its own class outside core/ (it does file I/O) and the 429 counter and auto-resume timer into a separate rate-limit policy object, or delete both if nothing outside the package uses them, leaving PipelineController as the transition table plus pause and cancel signalling.

### important

- `recipeparser/io/writers/picture_copy.py:25` — fail-loud: silent null success / broad handler that logs and continues — soft.fail-loud
  - Why: A failed bucket download becomes None, which is the same as 'no picture'. The pipeline's restore path only calls on_picture_lost when download() raises (pipeline.py:568-573), so this failure never reaches the job's refused-link count, despite the exemption comment there.
  - Snippet:
    ```
    except Exception as exc:  # noqa: BLE001
            log.warning("could not download the picture %s (%s).", key, exc)
            return None
    ```
  - Suggested fix: Let the storage error propagate, or wrap it in a typed error such as ImageStoreError, so _restore_bucket_picture's handler reports the loss through on_picture_lost. Keep None only for a URL that is not a bucket picture.
- `recipeparser/core/fsm.py:438` — fail-loud: silent null success on a load path — soft.fail-loud
  - Why: A checkpoint file that exists but has the wrong version is invalid, yet load_checkpoint returns the same None as 'no checkpoint', even though its docstring says a malformed file raises CheckpointError.
  - Snippet:
    ```
    if data.get("version") != _CHECKPOINT_VERSION:
                log.warning("Checkpoint version mismatch ... — ignoring." ...)
                return None
    ```
  - Suggested fix: Raise CheckpointError for an unsupported version, as the method already does for unreadable JSON. If a hash mismatch is meant to be a valid 'not this book' answer, say so in the docstring.
- `recipeparser/api.py:1` — no-shims: explicit shim / re-export kept for old importers — soft.no-shims
  - Why: A sys.modules alias module kept only so old import and patch paths keep working.
  - Snippet:
    ```
    # recipeparser/api.py  ← SHIM — will be deleted in Phase 7
    ```
  - Suggested fix: Move the remaining patch targets and importers to recipeparser.adapters.api and delete this module.
- `recipeparser/epub.py:2` — no-shims: explicit shim / compatibility re-export — soft.no-shims
  - Why: The doctrine names exactly this 'Backward-compat re-export' pattern as forbidden.
  - Snippet:
    ```
    # Backward-compat re-export. Import from recipeparser.io.readers.epub directly.
    ```
  - Suggested fix: Point the remaining importers (tests) at recipeparser.io.readers.epub and delete the module.
- `recipeparser/pdf.py:2` — no-shims: explicit shim / compatibility re-export — soft.no-shims
  - Why: A compatibility re-export kept for old importers.
  - Snippet:
    ```
    # Backward-compat re-export. Import from recipeparser.io.readers.pdf directly.
    ```
  - Suggested fix: Delete the module and import from recipeparser.io.readers.pdf.
- `recipeparser/export.py:1` — no-shims: explicit shim / compatibility re-export — soft.no-shims
  - Why: A re-export of paprika_zip, including a private name, kept so old importers keep working.
  - Snippet:
    ```
    # recipeparser/export.py  ← SHIM — will be deleted in Phase 7
    ```
  - Suggested fix: Move __main__.py and the tests to recipeparser.io.writers.paprika_zip and delete this module.
- `recipeparser/supabase_writer.py:1` — no-shims: explicit shim / compatibility re-export — soft.no-shims
  - Why: A re-export kept only for the old import path (used by tests/live_api_test.py).
  - Snippet:
    ```
    # recipeparser/supabase_writer.py  ← SHIM — will be deleted in Phase 7
    ```
  - Suggested fix: Import from recipeparser.io.writers.supabase and delete the module.
- `recipeparser/io/category_sources/base.py:16` — no-shims: compatibility re-export — soft.no-shims
  - Why: The docstring calls it a 'backward-compatibility re-export' and says the shim 'will be removed in Phase 8'.
  - Snippet:
    ```
    from recipeparser.core.ports import CategorySource  # noqa: F401 — re-export
    ```
  - Suggested fix: Import CategorySource from recipeparser.core.ports everywhere and delete base.py.
- `recipeparser/io/category_sources/__init__.py:2` — no-shims: caller kept on a compatibility re-export — soft.no-shims
  - Why: The package entry point still imports through the base.py shim, which keeps it alive.
  - Snippet:
    ```
    from recipeparser.io.category_sources.base import CategorySource
    ```
  - Suggested fix: Import CategorySource from recipeparser.core.ports here.
- `recipeparser/__main__.py:68` — no-shims: deprecated alias kept for old callers — soft.no-shims
  - Why: The comment says it exists for 'callers (e.g. tests) that use the old name'.
  - Snippet:
    ```
    _resolve_epub = _resolve_book
    ```
  - Suggested fix: Rename the test usages to _resolve_book and delete the alias.
- `recipeparser/__main__.py:106` — no-shims: production caller importing through a shim — soft.no-shims
  - Why: The CLI's --merge path still goes through the export.py shim rather than its canonical module.
  - Snippet:
    ```
    from recipeparser.export import merge_exports
    ```
  - Suggested fix: Import merge_exports from recipeparser.io.writers.paprika_zip.
- `recipeparser/adapters/search_api.py:117` — no-shims: compatibility branch for an old payload shape — soft.no-shims
  - Why: The endpoint accepts and converts the previous release's request shape alongside history.
  - Snippet:
    ```
    previous: Previous | None = None  # the previous release's one turn; accepted for one release
    ```
  - Suggested fix: Once the client release has shipped, remove previous and its validator so history is the only shape.
- `recipeparser/adapters/search_api.py:115` — no-shims: compatibility branch for an old payload shape — soft.no-shims
  - Why: README and ARCHITECTURE §15 say filters is 'accepted transitionally' for the previous app release only.
  - Snippet:
    ```
    filters: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=12)  # D1
    ```
  - Suggested fix: Drop the filters field together with previous.
- `recipeparser/adapters/ai_search/prompt.py:274` — no-shims: compatibility branch for an old payload shape — soft.no-shims
  - Why: The docstring says filters 'is only ever non-empty for the previous release's app', so this branch is a legacy path.
  - Snippet:
    ```
    if filters:
    ```
  - Suggested fix: Remove the filters parameter and the Active filters block when the transitional field is dropped.
- `recipeparser/adapters/ai_search/answer.py:125` — no-shims: explicit shim kept for one release — soft.no-shims
  - Why: The docstring says 'Kept for one release' and 'round is accepted but ignored (transitional)'.
  - Snippet:
    ```
    class Previous(BaseModel):
    ```
  - Suggested fix: Delete Previous once the old client is gone; Turn is the only shape.
- `recipeparser/gemini.py:1355` — no-shims: check for a field only an old payload shape carries — soft.no-shims
  - Why: It strips grid_categories so 'a recording made before the change' keeps working instead of re-recording the goldens.
  - Snippet:
    ```
    return schema.model_validate(raw_data).model_copy(update={"grid_categories": {}})
    ```
  - Suggested fix: Re-record the golden replies and drop grid_categories from the refinement model, rather than scrubbing the old shape at runtime.
- `recipeparser/adapters/regen_worker.py:233` — no-shims: optional-attribute probe papering over a missing interface — soft.no-shims
  - Why: workers is Sequence[Any] and the loop probes for an optional attribute instead of using a typed worker protocol.
  - Snippet:
    ```
    busy = busy or (did_work and getattr(w, "keeps_loop_busy", False))
    ```
  - Suggested fix: Define a Worker Protocol with run_once() and keeps_loop_busy, declare the attribute on every worker, and read it directly.
- `recipeparser/core/pipeline.py:945` — no-shims: explicit shim with optional-attribute probes — soft.no-shims
  - Why: _pre_parsed_to_refinement is documented as a 'Shim' for a 'legacy IngestResponse' and probes fields CayenneRecipe always declares (pans, bake_type, bake_step, yield_spec, source_uom_*).
  - Snippet:
    ```
    pans=list(getattr(pr, "pans", None) or []),
    ```
  - Suggested fix: Type pr as CayenneRecipe (IngestResponse already subclasses it) and read the attributes directly; drop the shim wording and the getattr defaults.
- `recipeparser/core/pipeline.py:667` — no-shims: optional-attribute probe papering over a typed interface — soft.no-shims
  - Why: CayenneRecipe always has ingredient_lines and direction_steps (also probed at 668, 698 and 699), so the probe hides the typed contract.
  - Snippet:
    ```
    ingredient_lines=list(getattr(pr, "ingredient_lines", []) or []),
    ```
  - Suggested fix: Use pr.ingredient_lines and pr.direction_steps directly.
- `recipeparser/core/pipeline.py:900` — no-shims: optional-attribute probe papering over a typed interface — soft.no-shims
  - Why: _citation_from probes four citation fields that the CayenneRecipe model always defines, citing exports that 'predate the columns'.
  - Snippet:
    ```
    kind = getattr(pr, "source_kind", None)
    ```
  - Suggested fix: Read pr.source_kind and the other fields directly; the model's None default already covers old archives.
- `recipeparser/core/pipeline.py:982` — no-shims: optional-attribute probe papering over a typed interface — soft.no-shims
  - Why: _restore_spans probes direction_steps, pans and bake_type on a model that always declares them.
  - Snippet:
    ```
    list(getattr(pr, "direction_steps", None) or []),
    ```
  - Suggested fix: Use the typed attributes directly.
- `recipeparser/core/yields.py:146` — no-shims: dead parameter kept for old callers — soft.no-shims
  - Why: The docstring says '`pans` is not read since D97 (kept for the four callers)', so the signature preserves an old contract instead of moving the callers.
  - Snippet:
    ```
    pans: Sequence[Any],
    ```
  - Suggested fix: Remove the pans parameter from check_yield and update its four callers.
- `recipeparser/core/regen.py:21` — no-shims: re-export kept for old importers — soft.no-shims
  - Why: strip_fat_tokens lives in core.fat_tokens and is re-exported here only so old importers (tests/unit/test_regen.py) keep working.
  - Snippet:
    ```
    __all__ = ["strip_fat_tokens", "raw_lines_from_derived", "raw_body_column", "build_extraction", "build_update"]
    ```
  - Suggested fix: Drop strip_fat_tokens from regen.__all__ and import it from core.fat_tokens.
- `recipeparser/io/readers/photo_check.py:42` — no-shims: version sniffing / explicit shim with a removal condition — soft.no-shims
  - Why: A package __version__ switch keeps two live code paths in production, documented as 'Drop the getdata branch once the pin is Pillow>=12.1'.
  - Snippet:
    ```
    _PILLOW_VERSION = tuple(int(part) for part in PIL.__version__.split(".")[:2])
    ```
  - Suggested fix: Pin Pillow>=12.1 and call get_flattened_data unconditionally.
- `recipeparser/io/readers/epub.py:341` — no-shims: code kept alive for an old shape — soft.no-shims
  - Why: The old 'Title — Author' / 'EPUB Auto-Import' source string, superseded by get_book_citation, is reachable only through the epub.py shim re-export.
  - Snippet:
    ```
    def get_book_source(book: epub.EpubBook) -> str:
    ```
  - Suggested fix: Delete get_book_source together with the shim.
- `recipeparser/io/writers/paprika_zip.py:42` — no-shims: code kept alive for an old shape — soft.no-shims
  - Why: The pre-pipeline monolith exporter (RecipeExtraction plus a book_source string) is reachable only through the export.py shim, since PaprikaWriter replaced it.
  - Snippet:
    ```
    def create_paprika_export(
    ```
  - Suggested fix: Delete create_paprika_export and port its tests to PaprikaWriter.
- `recipeparser/adapters/share_worker.py:96` — class-cohesion: disjoint method clusters sharing no instance state — soft.class-cohesion
  - Why: The sweep cluster (_sweep, _sweep_call over _clock, _next_expire, _next_purge) shares nothing but the database client with the share-copy job cluster (_claim, _start, _step, _copy, _picture, _wind_down, _finish over _held, _images, _supabase_url), and the sweep also expires and purges household invites, which has nothing to do with copying shares; the copy cluster itself mixes job orchestration, database and storage I/O, and share-state policy.
  - Snippet:
    ```
    class ShareWorker:
    ```
  - Suggested fix: Move _sweep and _sweep_call into a separate periodic-maintenance worker registered with run_workers (or a sweeper object the API lifespan owns), leaving ShareWorker to claim and run share_accept jobs only.
- `recipeparser/core/pipeline.py:112` — class-cohesion: orchestration mixed with policy and I/O; job description needs 'and' — soft.class-cohesion
  - Why: Besides routing chunks through the stages (run, _process_chunk, _get_stages), the class owns a picture cluster: the food check (_is_food, _ask_food), choosing among book-photo candidates (_food_book_photo), storing and restore-copying pictures through the ports (_store_picture, _restore_bucket_picture) and recording lost pictures; that cluster works on _image_store, _picture_source, _food_check and _pictures_lost and shares only _limiter with the orchestration.
  - Snippet:
    ```
    class RecipePipeline:
    ```
  - Suggested fix: Extract the picture methods and their state into a collaborator (for example a picture keeper built from image_store, picture_source, food_check and the limiter) that RecipePipeline calls once per recipe, so the pipeline only orchestrates stages.
- `recipeparser/adapters/ai_gate.py:50` — class-cohesion: I/O and policy mixed; clusters share only a lock and clock — soft.class-cohesion
  - Why: The class reads and spends the kill switch and recipe cap in Postgres (paused, allowance, ai_allowed, consume_recipe over _get_client, _paused_at, _paused_value, _warned_unconfigured) and also keeps in-process sliding-window rate limits (_embed_slot, _search_slot, _find_slot, _window_slot, _window_wait, search_retry_after over the _embeds, _search_* and _find_* deques); the two share only _lock and _clock and meet only in check(), which also turns the policy into HTTPException responses.
  - Snippet:
    ```
    class AiGate:
    ```
  - Suggested fix: Move the per-user window rate limiting into its own limiter class (like RecipientCheckLimiter), keep the database-backed pause and allowance reads in another, and let check() combine the two.

### nit

- `recipeparser/core/pipeline.py:900` — fail-loud: interface probing — soft.fail-loud
  - Why: pr is CayenneRecipe | IngestResponse and both declare source_kind (models.py:490), so the getattr default can never fire and the probe hides the typed field.
  - Snippet:
    ```
    kind = getattr(pr, "source_kind", None)
        if not kind:
            return None
    ```
  - Suggested fix: Read pr.source_kind, pr.source_key, pr.source_title and pr.source_author directly.
- `recipeparser/core/pipeline.py:945` — fail-loud: interface probing — soft.fail-loud
  - Why: The comment says an older archive has no tins, but CayenneRecipe declares pans, bake_type, bake_step, yield_spec and source_uom_system_* with defaults. These getattr probes are dead and should be typed attribute reads.
  - Snippet:
    ```
    pans=list(getattr(pr, "pans", None) or []),
            bake_type=getattr(pr, "bake_type", None),
    ```
  - Suggested fix: Use pr.pans, pr.bake_type, pr.bake_step, pr.yield_spec and pr.source_uom_system_detected/evidence directly. The same applies at lines 982-984.
- `recipeparser/core/pipeline.py:667` — fail-loud: interface probing — soft.fail-loud
  - Why: ingredient_lines and direction_steps are declared on CayenneRecipe (models.py:539-540), so probing for them on the typed union is unnecessary. The same pattern repeats at lines 698-699.
  - Snippet:
    ```
    ingredient_lines=list(getattr(pr, "ingredient_lines", []) or []),
                    direction_steps=list(getattr(pr, "direction_steps", []) or []),
    ```
  - Suggested fix: Read pr.ingredient_lines and pr.direction_steps directly.
- `recipeparser/adapters/cli.py:43` — no-shims: runtime feature probe — soft.no-shims
  - Why: SIGTERM is defined on every supported platform, so this probe (and the one at line 80) guards a missing attribute that cannot occur.
  - Snippet:
    ```
    _EXIT_STATUS = {signal.SIGINT: 130, getattr(signal, "SIGTERM", 15): 143}
    ```
  - Suggested fix: Use signal.SIGTERM directly.
- `scripts/backfill_count_tokens.py:215` — no-shims: hasattr probe — soft.no-shims
  - Why: A hasattr feature probe in a tooling script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure directly (Python 3.7+ always provides it on text streams).
- `scripts/backfill_direction_amounts.py:200` — no-shims: hasattr probe — soft.no-shims
  - Why: A hasattr feature probe in a tooling script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure directly.
- `scripts/backfill_pans.py:348` — no-shims: hasattr probe — soft.no-shims
  - Why: A hasattr feature probe in a tooling script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure directly.
- `scripts/backfill_servings.py:283` — no-shims: hasattr probe — soft.no-shims
  - Why: A hasattr feature probe in a tooling script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure directly.
- `scripts/backfill_temperatures.py:242` — no-shims: hasattr probe — soft.no-shims
  - Why: A hasattr feature probe in a tooling script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure directly.
- `scripts/backfill_yields.py:447` — no-shims: hasattr probe — soft.no-shims
  - Why: A hasattr feature probe in a tooling script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure directly.
- `scripts/probe_bake_types.py:66` — no-shims: hasattr probe — soft.no-shims
  - Why: A hasattr feature probe in a tooling script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure directly.
- `scripts/probe_yield_kinds.py:123` — no-shims: hasattr probe — soft.no-shims
  - Why: A hasattr feature probe in a tooling script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure directly.
- `scripts/backfill_yields.py:352` — no-shims: schema-version switch keeping two live shapes — soft.no-shims
  - Why: read_rows probes for a missing column and falls back to the pre-migration schema, a migration shim in a tooling script.
  - Snippet:
    ```
    rows = _read_rows(sb, user_id, recipe_id, BASE_COLUMNS)
    ```
  - Suggested fix: Once the Cayenne migration is live everywhere, drop the BASE_COLUMNS fallback and _is_missing_column.

### suggestion

- `recipeparser/core/pipeline.py:281` — fail-loud: silent success default on a failure path — soft.fail-loud
  - Why: An invalid transition returns False and only logs a warning, and run() ignores the result here and at line 506, so a controller that is not IDLE goes on silently. Ambiguity: api.py:1467 relies on 'error' being a no-op from IDLE, so a strict raise may only be wanted for 'start' and 'done'.
  - Snippet:
    ```
    self._controller.transition("start")
    ```
  - Suggested fix: Use transition_or_raise('start') and transition_or_raise('done') where an invalid state means a bug. Keep transition() only where a no-op is the intended answer.
- `scripts/backfill_count_tokens.py:215` — fail-loud: interface probing — soft.fail-loud
  - Why: Whether stdout is reconfigured depends on a hasattr probe. Ambiguity: it only gates a console setting, and sys.stdout can legitimately be any TextIO.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Dispatch on the type with isinstance(sys.stdout, io.TextIOWrapper), or drop the guard if the script only runs on a real console.
- `scripts/backfill_direction_amounts.py:200` — fail-loud: interface probing — soft.fail-loud
  - Why: Same hasattr probe on stdout to choose line buffering. Ambiguity: console-only nicety.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Use isinstance(sys.stdout, io.TextIOWrapper).
- `scripts/backfill_pans.py:348` — fail-loud: interface probing — soft.fail-loud
  - Why: Same hasattr probe on stdout. Ambiguity: console-only nicety.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Use isinstance(sys.stdout, io.TextIOWrapper).
- `scripts/backfill_servings.py:283` — fail-loud: interface probing — soft.fail-loud
  - Why: Same hasattr probe on stdout. Ambiguity: console-only nicety.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Use isinstance(sys.stdout, io.TextIOWrapper).
- `scripts/backfill_temperatures.py:242` — fail-loud: interface probing — soft.fail-loud
  - Why: Same hasattr probe on stdout. Ambiguity: console-only nicety.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Use isinstance(sys.stdout, io.TextIOWrapper).
- `scripts/backfill_yields.py:447` — fail-loud: interface probing — soft.fail-loud
  - Why: Same hasattr probe on stdout. Ambiguity: console-only nicety.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Use isinstance(sys.stdout, io.TextIOWrapper).
- `scripts/probe_bake_types.py:66` — fail-loud: interface probing — soft.fail-loud
  - Why: Same hasattr probe on stdout. Ambiguity: console-only nicety.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Use isinstance(sys.stdout, io.TextIOWrapper).
- `scripts/probe_yield_kinds.py:123` — fail-loud: interface probing — soft.fail-loud
  - Why: Same hasattr probe on stdout. Ambiguity: console-only nicety.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Use isinstance(sys.stdout, io.TextIOWrapper).
- `recipeparser/io/readers/epub.py:366` — no-shims: code possibly kept alive for an old shape — soft.no-shims
  - Why: The only non-test reference is the epub.py shim re-export; it needs a human decision whether any external library user still relies on it.
  - Snippet:
    ```
    def extract_text_from_epub(epub_path: str) -> str:
    ```
  - Suggested fix: If no supported caller remains, delete it with the shim.
- `recipeparser/io/readers/pdf.py:285` — no-shims: code possibly kept alive for an old shape — soft.no-shims
  - Why: It is referenced only through the pdf.py shim re-export, so it may survive only for old importers.
  - Snippet:
    ```
    def extract_text_from_pdf(pdf_path: str, client: Any = None) -> str:
    ```
  - Suggested fix: Confirm there is no caller and delete it with the shim.
- `recipeparser/io/writers/paprika_zip.py:271` — no-shims: alias wrapper kept for an old importer — soft.no-shims
  - Why: A private pass-through to core.fat_tokens.strip_fat_tokens that survives only because cayenne_zip.py imports the old name.
  - Snippet:
    ```
    def _strip_fat_tokens(text: str) -> str:
    ```
  - Suggested fix: Import strip_fat_tokens from core.fat_tokens in both writers and delete the wrapper.
- `recipeparser/models.py:595` — no-shims: model kept for removed endpoints — soft.no-shims
  - Why: It describes the removed /ingest* endpoints and nothing in the surface references it; the ambiguity is whether Cayenne still imports it.
  - Snippet:
    ```
    class JobResponse(BaseModel):
    ```
  - Suggested fix: Delete it if no consumer remains.
- `recipeparser/models.py:537` — no-shims: legacy fallback — soft.no-shims
  - Why: Empty raw body columns on 'legacy objects' are filled from derived data (assemble.py:119-120); it needs judgment whether archives without these columns must still be accepted.
  - Snippet:
    ```
    # Raw, user-owned body (spec 3.2). Empty lists on legacy objects; the writer
    ```
  - Suggested fix: If old archives no longer need support, make the fields required and remove the derive-on-empty fallback.
- `recipeparser/core/stages/assemble.py:99` — no-shims: field kept 'for backward compatibility' — soft.no-shims
  - Why: The docstring and comment call the flat categories list a 'legacy' field for backward compatibility, but the Paprika export format may genuinely need it.
  - Snippet:
    ```
    flat_categories: List[str] = [
    ```
  - Suggested fix: Either document it as Paprika's format requirement or derive it only in the Paprika writers.
- `recipeparser/core/fat_tokens.py:146` — no-shims: compatibility branch for an old data shape — soft.no-shims
  - Why: Tokens without a use (written before 9.5.0) keep a live 'legacy' branch here and in count_tokens.py:271; whether backfill_direction_amounts has migrated every row needs checking.
  - Snippet:
    ```
    return Use("legacy")
    ```
  - Suggested fix: If the backfill is complete, treat a missing use as invalid and remove the legacy kind.
- `recipeparser/exceptions.py:178` — no-shims: type chosen to fit old catch sites — soft.no-shims
  - Why: It subclasses RuntimeError, outside the RecipeParserError hierarchy, 'so the readers that already let RuntimeError through' need not change.
  - Snippet:
    ```
    class AIServiceTimeoutError(RuntimeError):
    ```
  - Suggested fix: Derive it from RecipeParserError and update the except clauses that rely on RuntimeError.
- `recipeparser/adapters/api.py:1277` — no-shims: soft fallback on a failure path — soft.no-shims
  - Why: A failed category creation degrades to linking leaves by name and the restore continues; whether a partial restore is acceptable is a product judgment.
  - Snippet:
    ```
    placed = {p: p[-1] for _, ps, ls in wanted for p in ps + ls}
    ```
  - Suggested fix: Report the lost category creation on the job (as refused links) or fail the restore instead of silently degrading.
- `recipeparser/adapters/api.py:1308` — no-shims: optional-attribute probe — soft.no-shims
  - Why: The wrapped writer's recipe is always an IngestResponse with a title, so the probe hides an untyped Any.
  - Snippet:
    ```
    for leaf, reason in pending.pop(getattr(recipe, "title", None), []):
    ```
  - Suggested fix: Type the wrapper's recipe parameter as IngestResponse and read recipe.title.
- `recipeparser/adapters/ingest_liveness.py:125` — no-shims: dispatch keeping two input shapes — soft.no-shims
  - Why: tick accepts both bare job ids and an id-to-signal mapping, yet the only production caller (api._liveness_view) passes a mapping, so the ids form may be an old shape.
  - Snippet:
    ```
    dict(live_jobs) if isinstance(live_jobs, Mapping) else {job_id: None for job_id in live_jobs}
    ```
  - Suggested fix: If no caller passes bare ids, narrow LiveJobs to the Mapping form.
- `recipeparser/io/readers/paprika.py:365` — no-shims: compatibility branch for an old payload shape — soft.no-shims
  - Why: _cayenne_meta is accepted as either a JSON string or an object; it needs judgment whether string-encoded archives from older exporters must still be read.
  - Snippet:
    ```
    if isinstance(meta, str):
    ```
  - Suggested fix: If no supported exporter writes a string, drop the branch; otherwise document the older format it serves.
- `recipeparser/io/writers/supabase.py:493` — no-shims: compatibility branch for an old data shape — soft.no-shims
  - Why: verify_recipe_in_supabase still decodes the old double-encoded jsonb shape that the writer stopped producing.
  - Snippet:
    ```
    if isinstance(ings, str):
    ```
  - Suggested fix: Treat a string here as a verification failure rather than decoding it.
- `recipeparser/core/stages/refine.py:672` — no-shims: optional-attribute probe — soft.no-shims
  - Why: raw is typed RecipeExtraction, which always has name (also probed at line 309).
  - Snippet:
    ```
    f"refine(): Gemini returned None for recipe '{getattr(raw, 'name', '?')}'. "
    ```
  - Suggested fix: Use raw.name directly.
- `recipeparser/gemini.py:604` — no-shims: soft fallback returning a degraded default — soft.no-shims
  - Why: normalise_baker_table returns the unnormalised chunk on any failure, which may be the sanctioned provider fallback or a silent degradation.
  - Snippet:
    ```
    return text_chunk
    ```
  - Suggested fix: Decide whether this is a transient-provider None case; if so, return None and let extract() decide, otherwise raise.
- `recipeparser/adapters/gui.py:86` — class-cohesion: presentation mixed with persistence I/O — soft.class-cohesion
  - Why: This needs human judgment, since the clusters share _data and _order as a Tk frame usually does: the class combines building and rendering widgets, editing the category tree, loading and saving the YAML file, and reading the live Paprika SQLite database (_sync_from_paprika).
  - Snippet:
    ```
    class CategoryEditorFrame(ctk.CTkFrame):
    ```
  - Suggested fix: Consider moving the tree model (_data, _order, the add/rename/move/delete operations and the YAML structure) and its file and Paprika I/O into a plain model class that the frame renders, so the frame keeps only widgets and dialogs.
- `recipeparser/adapters/job_sink.py:30` — class-cohesion: job description needs 'and' (I/O plus policy plus bookkeeping) — soft.class-cohesion
  - Why: This needs human judgment, since the state is shared: the docstring says 'Collects what one ingestion job did, and writes each recipe as it arrives', and the class also enforces the beta-cap policy (cap_reached and _consume in on_result, the cap sentence in finalize_payload) alongside the recipe and link writes and the skip and progress bookkeeping.
  - Snippet:
    ```
    class JobSink:
    ```
  - Suggested fix: Consider moving the cap decision (consume, on_cap, the closing sentence) into the caller or a small policy object passed in, leaving JobSink to record results and build the terminal payload.
- `recipeparser/adapters/recat_worker.py:133` — class-cohesion: queue-lease mechanics mixed with job business logic — soft.class-cohesion
  - Why: This needs human judgment, since _held is shared: a generic ingestion_jobs lease cluster (_claim with its compare-and-swap, _stale_running_jobs, _finish, _terminal_payload) sits beside the recategorise batch logic and the done-or-error verdict, and ShareWorker repeats the same lease cluster, which suggests it is a responsibility of its own.
  - Snippet:
    ```
    class RecatWorker:
    ```
  - Suggested fix: Consider extracting a shared job-lease helper (claim pending or stale-running jobs by kind, finish only out of running) for RecatWorker and ShareWorker to use, leaving each worker with its own batch or item step.

0 findings exempted by inline tokens.

## Roll-up

- Groups: OK: 5, FAILED: 0
- Findings by severity: blocking: 3, important: 29, nit: 13, suggestion: 27
- Auditor error notes: 7

## Drift vs baseline

_Baseline 85ec090994887ecbd96492837ee29e5e095efaf7 @ 2026-11-19T22:08:15.004284+00:00. Counts may be LLM-derived and non-deterministic — small deltas are noise. Report-only; never fails the run._

| Mandate | Subsystem | Baseline | Current | Δ |
|---|---|---|---|---|
| soft.fail-loud | scripts/ | 0 | 8 | +8 |
| soft.no-shims | recipeparser/adapters/ | 3 | 9 | +6 |
| soft.no-shims | recipeparser/ | 10 | 13 | +3 |
| soft.fail-loud | recipeparser/core/ | 3 | 5 | +2 |
| soft.no-shims | recipeparser/io/ | 8 | 10 | +2 |
| soft.class-cohesion | recipeparser/adapters/ | 4 | 5 | +1 |
| soft.no-shims | scripts/ | 8 | 9 | +1 |
| soft.class-cohesion | recipeparser/core/ | 2 | 2 | 0 |
| soft.no-shims | recipeparser/core/ | 9 | 9 | 0 |
| soft.fail-loud | recipeparser/ | 5 | 0 | -5 |
| soft.fail-loud | recipeparser/adapters/ | 6 | 1 | -5 |
| soft.fail-loud | recipeparser/io/ | 7 | 1 | -6 |

## Staleness

fresh

## Provenance

- pre-baseline: 70
- post-baseline: 2
- uncommitted: 0
- unresolved: 0

