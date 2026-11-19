# Architecture review

- Commit: 85ec090994887ecbd96492837ee29e5e095efaf7
- Invocation: /architecture-review --update-baseline
- Auditor model: claude-opus-5-5

## Groups

| Group | Status | Files examined | Findings (after exemptions) |
| --- | --- | --- | --- |
| fail-loud | OK | 53 | 21 |
| no-shims | OK | 33 | 38 |
| config/architecture/auditors/layer-imports.md | OK | 136 | 0 |
| state-machine-discipline | OK | 3 | 0 |
| class-cohesion | OK | 36 | 6 |

### Auditor error notes

- fail-loud: check off: exception_base absent (flag 4 not applied). Seen for the record, classes whose bases are outside RecipeParserError: recipeparser/adapters/api.py:120 AuthConfigurationError(RuntimeError), recipeparser/adapters/api.py:733 _AiRefusal(Exception), recipeparser/adapters/brave.py:19 BraveError(Exception), recipeparser/adapters/ai_search/ai_db.py:91 NotAiReader(RuntimeError), recipeparser/adapters/ai_search/ai_db.py:95 QueryFailed(Exception), recipeparser/adapters/ai_search/agent.py:40/44/48 AgentTimeout/AgentFailed/AgentCancelled(Exception), recipeparser/adapters/ai_search/look.py:107 UnsearchableSites(Exception), recipeparser/adapters/ai_search/sql_gate.py:79 StatementRefused(Exception), recipeparser/core/picture_gen.py:32 NoPictureError(RuntimeError), recipeparser/exceptions.py:158 AIServiceTimeoutError(RuntimeError), recipeparser/shopping.py:63 ClassifyCheckError(RuntimeError), recipeparser/shopping_aisles.py:74 DecisionsAPIError(RuntimeError), recipeparser/shopping_aisles.py:82 AislesCheckError(RuntimeError)
- fail-loud: examined by repository-wide Grep for this rule's patterns (bare/broad except, narrow except returning a default/pass/continue, hasattr, getattr with default, exception class definitions) with context, not read in full: about 90 surface files under recipeparser/, scripts/, tools/ and the repository root (cleanup_jobs.py, kill_server.py, start_server.py). Every pre-pass candidate in these files was read in context and classified.
- no-shims: method note: the 33 files counted in files_examined were read (whole or the relevant ranges). Every other surface file was examined only by repository-wide Grep sweeps for the rule's tells (shim/legacy/back-compat/deprecated/re-export wording, hasattr and defaulted getattr, type: ignore/cast/Any, sys.version_info/__version__/sys.platform, retry loops and retry wording, except-then-return-sentinel, .get() fallbacks, version switches, except ImportError/AttributeError), with no Read of the file. The auditor's list names about 100 such files under recipeparser/, scripts/ and the repository root.
- no-shims: partially read (relevant sections only, rest Grep-swept): recipeparser/core/pipeline.py (490-949), recipeparser/adapters/api.py (118-199, 1210-1289), recipeparser/gemini.py (170-230, 1430-1449), recipeparser/adapters/search_api.py (340-400), recipeparser/io/readers/url.py (80-99), recipeparser/adapters/ai_search/look.py (200-220), recipeparser/adapters/job_sink.py (85-139), recipeparser/adapters/regen_worker.py (195-238), recipeparser/io/writers/supabase.py (385-424), recipeparser/core/fat_tokens.py (1-160, 280-319), recipeparser/core/fsm.py (1-40, 370-468), recipeparser/core/yields.py (1-70), recipeparser/toc.py (1-80), recipeparser/models.py (15-609)
- no-shims: no candidates were supplied (candidates field empty); classification came from the auditor's own sweeps
- state-machine-discipline: check off (check 2, declared failure not raised): base_classes empty for soft.state-machines; only check 1 was judged, against files that use the validator `transition`, which are recipeparser/core/fsm.py, recipeparser/core/pipeline.py and recipeparser/adapters/api.py
- class-cohesion: coverage: every class declaration in the surface was enumerated with Grep (about 198 sites). Behavioural classes were deep-read in their files; there was no triage cutoff.
- class-cohesion: classified from declaration lines only, not deep-read (every class in them is a data class, NamedTuple, Pydantic model, Enum, exception, Protocol or ABC port, which the rule exempts): recipeparser/adapters/api.py, recipeparser/exceptions.py, recipeparser/models.py, recipeparser/core/citation.py, recipeparser/core/durations.py, recipeparser/core/fat_tokens.py, recipeparser/core/models.py, recipeparser/core/picture_check.py, recipeparser/core/picture_gen.py, recipeparser/core/ports.py, recipeparser/core/temperatures.py, recipeparser/core/stages/extract.py, recipeparser/core/stages/tag.py, recipeparser/gemini.py, recipeparser/shopping.py, recipeparser/adapters/ai_search/agent.py, recipeparser/adapters/ai_search/answer.py, recipeparser/adapters/cli.py, recipeparser/adapters/household_api.py, recipeparser/adapters/shopping_api.py, recipeparser/io/readers/__init__.py, recipeparser/io/writers/__init__.py
- class-cohesion: check not applicable (Grep found no class declaration): every other surface file not counted in files_examined or listed above, including recipeparser/categories.yaml

## Findings

### blocking

- `recipeparser/adapters/gui.py:160` — fail-loud: bare or broad handler that swallows — soft.fail-loud
  - Why: Any failure to read or parse the categories YAML, including a malformed file, silently becomes an empty category list with no log, and the editor can then save that empty list over the user's file.
  - Snippet:
    ```
            except Exception:
                entries = []
    ```
  - Suggested fix: Catch only FileNotFoundError for the first-run case, and for any other failure show the error and abort the load (or raise a ConfigurationError) so the editor never starts from a false empty state.
- `recipeparser/io/readers/paprika.py:366` — fail-loud: bare or broad handler that swallows — soft.fail-loud
  - Why: In read_entries_with_images an entry that is neither gzip nor plain JSON is dropped with no log and no record, so the caller cannot tell a recipe was lost.
  - Snippet:
    ```
                        except Exception:
                            continue
    ```
  - Suggested fix: Catch the specific decode errors, log the entry name, and either raise PaprikaExtractionError or return a skipped record as read() does with skip_reason; or delete this method if nothing uses it.
- `recipeparser/io/readers/paprika.py:374` — fail-loud: bare or broad handler that swallows — soft.fail-loud
  - Why: An unreadable _cayenne_meta string is silently left as a string with no log, unlike iter_entries, which logs it and turns it into a skipped line.
  - Snippet:
    ```
                        except Exception:
                            pass
    ```
  - Suggested fix: Catch json.JSONDecodeError only, log it, and handle it the way iter_entries does, so the entry becomes a skipped line.
- `recipeparser/io/writers/paprika_zip.py:92` — fail-loud: bare or broad handler that swallows — soft.fail-loud
  - Why: A catch-all with a bare pass around the total-time arithmetic hides any failure and leaves total_time empty, with no log.
  - Snippet:
    ```
                except Exception:
                    pass
    ```
  - Suggested fix: Remove the try/except: the regex and integer sum cannot fail on strings. If one case can fail, catch that one exception and log it.
- `recipeparser/io/readers/pdf.py:262` — fail-loud: bare or broad handler that swallows — soft.fail-loud
  - Why: Any failure extracting a page image is skipped silently, with no log and no stated reason, so a book's photographs can disappear without trace.
  - Snippet:
    ```
            except Exception:
                continue
    ```
  - Suggested fix: Catch PyMuPDF's own error type, log the page and xref at warning, and add a comment giving the reason, or let unexpected errors propagate.
- `recipeparser/adapters/regen_worker.py:226` — fail-loud: interface probing instead of typed dispatch — soft.fail-loud
  - Why: Whether the poll loop sleeps depends on probing workers typed Sequence[Any] for an optional attribute, which RegenWorker does not declare, rather than on a declared worker interface.
  - Snippet:
    ```
    busy = busy or (did_work and getattr(w, "keeps_loop_busy", False))
    ```
  - Suggested fix: Declare a Worker Protocol (or base class) with run_once() and a keeps_loop_busy class attribute that defaults to False, type workers as Sequence[Worker], and read the attribute directly.
- `recipeparser/toc.py:45` — fail-loud: interface probing instead of typed dispatch — soft.fail-loud
  - Why: The kind of ebooklib TOC node is decided by duck-typing with hasattr (and getattr title/label at line 39) rather than isinstance on ebooklib's Link/Section types, so an unexpected node is silently skipped.
  - Snippet:
    ```
    elif hasattr(item, "title") and hasattr(item, "href"):
    ```
  - Suggested fix: Dispatch with isinstance(item, epub.Link) and isinstance(section, epub.Section), and raise or log on any other node type.
- `recipeparser/toc.py:68` — fail-loud: interface probing instead of typed dispatch — soft.fail-loud
  - Why: Same hasattr dispatch on TOC node type in _flatten_epub_toc_leaves_only (getattr title/label probing at line 61).
  - Snippet:
    ```
    elif hasattr(item, "title") and hasattr(item, "href"):
    ```
  - Suggested fix: Use isinstance on ebooklib's typed nodes, shared with _flatten_epub_toc, and report unknown node types.

### important

- `recipeparser/adapters/api.py:915` — fail-loud: broad handler with weak handling — soft.fail-loud
  - Why: The terminal job-row write logs and returns. Unlike the progress, total_chunks and create-row writes, which the module names as §11.4's deliberate exceptions, it gives no reason, and a failed finalize leaves the client's only view of the job stale (the zombie-job symptom §11.4 exists to prevent).
  - Snippet:
    ```
        except Exception:
            logger.exception("Job %s: failed to finalize ingestion_jobs row.", job_id)
    ```
  - Suggested fix: Re-raise (or raise a typed error) so the job's outer handler and the liveness reaper act on it, or document it as a deliberate §11.4 exception and say what recovers the row.
- `recipeparser/adapters/job_sink.py:158` — fail-loud: broad handler with weak handling — soft.fail-loud
  - Why: A failure writing a recipe's TAG links is only logged: unlike on_tag_failed and _link_refused, it is not added to refused_link_count or refused_links, so the job row says nothing was refused while the recipe lost all its tags.
  - Snippet:
    ```
            except Exception:
                log.exception("Job %s: the tags for recipe %r could not be written.", self._job_id, title)
    ```
  - Suggested fix: In the handler, record the failure as refused links for that recipe's tags (count and list), as on_tag_failed does.
- `recipeparser/categories.py:174` — fail-loud: broad handler with weak handling — soft.fail-loud
  - Why: The catch-all also swallows DailyQuotaExhaustedError and JobCancelledError, which exceptions.py says every skip-reading handler must let through, so a used-up daily quota silently files every remaining recipe under 'EPUB Imports' and the --recategorize run reports success.
  - Snippet:
    ```
        except Exception as e:
            log.warning("  -> Category assignment failed for '%s': %s", recipe.name, e)
    ```
  - Suggested fix: Add `except (JobCancelledError, DailyQuotaExhaustedError): raise` before the broad handler, as gemini.refine_recipe_for_cayenne does.
- `recipeparser/core/stages/tag.py:87` — fail-loud: broad handler with weak handling — soft.fail-loud
  - Why: DailyQuotaExhaustedError, which exceptions.py says must stop the whole run and never be read as a skip, is caught here and recorded as a per-axis TagFailure, then retried on every remaining axis.
  - Snippet:
    ```
                except Exception as exc:  # noqa: BLE001 -- one axis must not cost the others
    ```
  - Suggested fix: Let DailyQuotaExhaustedError (and JobCancelledError) propagate before the broad handler, as extract.py and embed.py do.
- `recipeparser/adapters/recat_worker.py:338` — fail-loud: broad handler with weak handling — soft.fail-loud
  - Why: A batch that fails with DailyQuotaExhaustedError (raised at once on the server) is retried and then written to `skipped` while the cursor moves on, so a used-up quota marks every remaining recipe skipped instead of stopping the job.
  - Snippet:
    ```
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
    ```
  - Suggested fix: Re-raise DailyQuotaExhaustedError before the broad handler so run_once ends or holds the job, rather than reading quota exhaustion as a per-batch skip.
- `recipeparser/gemini.py:1650` — fail-loud: silent null success — soft.fail-loud
  - Why: estimate_servings returns None for every failure, with no log. That is wider than the doctrine's provider sanction, which covers transient API failures only, so a bad key, schema drift or a programming error all look like 'no estimate'.
  - Snippet:
    ```
        except Exception:  # noqa: BLE001
            return None
    ```
  - Suggested fix: Log the failure, and limit the None return to transient or parse failures (for example _is_transient_server_error, json/KeyError), letting other errors raise.
- `recipeparser/recategorize.py:91` — fail-loud: broad handler with weak handling — soft.fail-loud
  - Why: An entry that will not decompress or parse is only logged and left out of the rewritten archive, so the recategorized export silently holds fewer recipes than the input.
  - Snippet:
    ```
                except Exception as exc:
                    log.warning(
                        "recategorize: could not parse '%s' — skipping. (%s)", member, exc
    ```
  - Suggested fix: Copy the unparseable member through unchanged, or collect the failures and raise RecategorizationError (or report them in the result) so the user knows entries were dropped.
- `recipeparser/io/writers/paprika_zip.py:196` — fail-loud: broad handler with weak handling — soft.fail-loud
  - Why: merge_exports drops any unparseable entry with only a log line, and its declared contract (ExportError) never reports the loss, so the merged archive can silently lack recipes.
  - Snippet:
    ```
                        except Exception:
                            log.warning(
                                "merge_exports: could not parse entry '%s' in '%s' — skipping.",
    ```
  - Suggested fix: Catch only the decode errors, count the dropped entries, and raise ExportError or return the skipped names so the CLI can report them.
- `recipeparser/api.py:1` — no-shims: explicit shims — soft.no-shims
  - Why: The whole module is a compatibility alias (sys.modules[__name__] = recipeparser.adapters.api) kept so old import paths and patch() targets keep working, past its stated Phase 7 removal.
  - Snippet:
    ```
    # recipeparser/api.py  ← SHIM — will be deleted in Phase 7
    ```
  - Suggested fix: Move every importer and patch target to recipeparser.adapters.api and delete this module.
- `recipeparser/epub.py:2` — no-shims: explicit shims — soft.no-shims
  - Why: A backward-compat re-export, the exact shape the doctrine names, and recipeparser/toc.py still imports through it.
  - Snippet:
    ```
    # Backward-compat re-export. Import from recipeparser.io.readers.epub directly.
    ```
  - Suggested fix: Point toc.py (and any test) at recipeparser.io.readers.epub and delete the module.
- `recipeparser/export.py:1` — no-shims: explicit shims — soft.no-shims
  - Why: A re-export of io.writers.paprika_zip (private _normalise_recipe_name included) kept for old importers; __main__.py still imports merge_exports through it.
  - Snippet:
    ```
    # recipeparser/export.py  ← SHIM — will be deleted in Phase 7
    ```
  - Suggested fix: Import from recipeparser.io.writers.paprika_zip in __main__.py and delete this module.
- `recipeparser/pdf.py:2` — no-shims: explicit shims — soft.no-shims
  - Why: A backward-compat re-export of io.readers.pdf kept alive for old importers.
  - Snippet:
    ```
    # Backward-compat re-export. Import from recipeparser.io.readers.pdf directly.
    ```
  - Suggested fix: Move any remaining importers to recipeparser.io.readers.pdf and delete the module.
- `recipeparser/supabase_writer.py:1` — no-shims: explicit shims — soft.no-shims
  - Why: A re-export of io.writers.supabase kept for old import paths past its stated removal phase.
  - Snippet:
    ```
    # recipeparser/supabase_writer.py  ← SHIM — will be deleted in Phase 7
    ```
  - Suggested fix: Move any importers to recipeparser.io.writers.supabase and delete the module.
- `recipeparser/io/category_sources/base.py:16` — no-shims: explicit shims — soft.no-shims
  - Why: A backward-compatibility re-export of CategorySource that the docstring says will be removed in Phase 8; four sibling modules still import through it instead of from core.ports.
  - Snippet:
    ```
    from recipeparser.core.ports import CategorySource  # noqa: F401 — re-export
    ```
  - Suggested fix: Change the importers to `from recipeparser.core.ports import CategorySource` and delete base.py.
- `recipeparser/io/category_sources/__init__.py:2` — no-shims: explicit shims — soft.no-shims
  - Why: This caller still goes through the base.py compatibility re-export, which keeps the shim alive.
  - Snippet:
    ```
    from recipeparser.io.category_sources.base import CategorySource
    ```
  - Suggested fix: Import CategorySource from recipeparser.core.ports.
- `recipeparser/io/category_sources/yaml_source.py:42` — no-shims: explicit shims — soft.no-shims
  - Why: This caller still goes through the base.py compatibility re-export, which keeps the shim alive.
  - Snippet:
    ```
    from recipeparser.io.category_sources.base import CategorySource
    ```
  - Suggested fix: Import CategorySource from recipeparser.core.ports.
- `recipeparser/io/category_sources/paprika_db.py:29` — no-shims: explicit shims — soft.no-shims
  - Why: This caller still goes through the base.py compatibility re-export, which keeps the shim alive.
  - Snippet:
    ```
    from recipeparser.io.category_sources.base import CategorySource
    ```
  - Suggested fix: Import CategorySource from recipeparser.core.ports.
- `recipeparser/io/category_sources/supabase_source.py:40` — no-shims: explicit shims — soft.no-shims
  - Why: This caller still goes through the base.py compatibility re-export, which keeps the shim alive.
  - Snippet:
    ```
    from recipeparser.io.category_sources.base import CategorySource
    ```
  - Suggested fix: Import CategorySource from recipeparser.core.ports.
- `recipeparser/toc.py:21` — no-shims: explicit shims — soft.no-shims
  - Why: Production code imports through the recipeparser.epub back-compat shim, so the caller never moved and the shim cannot be deleted.
  - Snippet:
    ```
    from recipeparser.epub import read_epub, split_large_chunk
    ```
  - Suggested fix: Import read_epub and split_large_chunk from recipeparser.io.readers.epub.
- `recipeparser/__main__.py:68` — no-shims: explicit shims — soft.no-shims
  - Why: A deprecated alias kept under the comment 'Backward compatibility for callers (e.g. tests) that use the old name'.
  - Snippet:
    ```
    _resolve_epub = _resolve_book
    ```
  - Suggested fix: Rename the remaining callers to _resolve_book and delete the alias.
- `recipeparser/__main__.py:106` — no-shims: explicit shims — soft.no-shims
  - Why: The CLI imports through the recipeparser.export shim instead of its canonical module.
  - Snippet:
    ```
    from recipeparser.export import merge_exports
    ```
  - Suggested fix: Import merge_exports from recipeparser.io.writers.paprika_zip.
- `recipeparser/core/pipeline.py:877` — no-shims: explicit shims — soft.no-shims
  - Why: This is a self-described shim that accepts a 'legacy IngestResponse' shape. PaprikaReader only ever builds CayenneRecipe (and IngestResponse subclasses it), so the dual-shape contract (and Chunk.pre_parsed's Union) is dead compatibility surface.
  - Snippet:
    ```
    Shim: convert a pre-parsed object (either a ``CayenneRecipe`` stored by ``PaprikaReader`` or a legacy ``IngestResponse`` from ``_cayenne_meta``)
    ```
  - Suggested fix: Type pre_parsed and these helpers as CayenneRecipe only, and drop the shim wording and the legacy-type branch.
- `recipeparser/core/pipeline.py:896` — no-shims: compatibility branches — soft.no-shims
  - Why: getattr probes for pans, bake_type, bake_step, yield_spec and source_uom_* are kept 'for an archive that predates them', but every one is a declared CayenneRecipe field with a Pydantic default, so the probes only paper over a typed interface.
  - Snippet:
    ```
    pans=list(getattr(pr, "pans", None) or []),
    ```
  - Suggested fix: Read pr.pans, pr.bake_type and the rest directly, since the model already supplies defaults for old archives.
- `recipeparser/core/pipeline.py:616` — no-shims: compatibility branches — soft.no-shims
  - Why: Optional-attribute probes on CayenneRecipe.ingredient_lines and direction_steps, which are always present; line 647 repeats the same pair.
  - Snippet:
    ```
    ingredient_lines=list(getattr(pr, "ingredient_lines", []) or []),
    ```
  - Suggested fix: Use pr.ingredient_lines and pr.direction_steps directly in both fast paths.
- `recipeparser/core/pipeline.py:514` — no-shims: compatibility branches — soft.no-shims
  - Why: A getattr probe on a declared CayenneRecipe field, already narrowed to non-None above.
  - Snippet:
    ```
    old_url = getattr(pr, "image_url", None)
    ```
  - Suggested fix: Read pr.image_url directly.
- `recipeparser/core/pipeline.py:717` — no-shims: compatibility branches — soft.no-shims
  - Why: raw is a RecipeExtraction that always declares prep_time, cook_time, servings, stated_source, byline, total_time, description, nutritional_info, notes and photo_filename, yet `raw: Any` plus hasattr/getattr probes (lines 698-731) hide that contract.
  - Snippet:
    ```
    prep_time=raw.prep_time if hasattr(raw, "prep_time") else None,
    ```
  - Suggested fix: Type raw as RecipeExtraction and read its fields directly.
- `recipeparser/core/pipeline.py:851` — no-shims: compatibility branches — soft.no-shims
  - Why: getattr probes for source_kind/key/title/author, all declared Optional fields on CayenneRecipe, papering over a typed interface.
  - Snippet:
    ```
    kind = getattr(pr, "source_kind", None)
    ```
  - Suggested fix: Read pr.source_kind and the other three fields directly.
- `recipeparser/core/pipeline.py:933` — no-shims: compatibility branches — soft.no-shims
  - Why: More optional-attribute probes for declared CayenneRecipe fields (direction_steps, pans, bake_type) in _restore_spans.
  - Snippet:
    ```
    list(getattr(pr, "direction_steps", None) or []),
    ```
  - Suggested fix: Use the typed attributes directly.
- `recipeparser/gemini.py:200` — no-shims: soft fallbacks — soft.no-shims
  - Why: _finalize_config silently strips 'deprecated parameters' instead of making callers move. Callers at lines 1441, 1520, 1597 and 1639 still pass temperature 0 and believe it takes effect.
  - Snippet:
    ```
    if k not in ("temperature", "top_p", "top_k", "thinking_budget")
    ```
  - Suggested fix: Raise on a deprecated key, or remove it from every caller, so no config is silently rewritten.
- `recipeparser/adapters/regen_worker.py:226` — no-shims: compatibility branches — soft.no-shims
  - Why: With workers typed Sequence[Any], an optional-attribute probe stands in for a missing worker interface, the hasattr-style probe the doctrine forbids.
  - Snippet:
    ```
    busy = busy or (did_work and getattr(w, "keeps_loop_busy", False))
    ```
  - Suggested fix: Define a Worker Protocol with run_once() and keeps_loop_busy, and type workers with it.
- `recipeparser/adapters/job_sink.py:103` — no-shims: compatibility branches — soft.no-shims
  - Why: recipe is typed IngestResponse, which always has title and source_key, yet lines 103, 124, 128 and 133 probe them with getattr.
  - Snippet:
    ```
    title = getattr(recipe, "title", None)
    ```
  - Suggested fix: Read recipe.title and recipe.source_key directly.
- `recipeparser/io/writers/supabase.py:403` — no-shims: compatibility branches — soft.no-shims
  - Why: write_recipe_to_supabase takes recipe: IngestResponse, whose grid_categories is a declared dict, so the getattr probe papers over the typed interface.
  - Snippet:
    ```
    grid = getattr(recipe, "grid_categories", None) or {}
    ```
  - Suggested fix: Use recipe.grid_categories directly.
- `recipeparser/categories.py:82` — no-shims: soft fallbacks — soft.no-shims
  - Why: A malformed taxonomy file degrades to an empty tree 'so the script still runs', so the run continues in a wrong state (every recipe filed under the default bucket) instead of failing.
  - Snippet:
    ```
    except Exception as e:
            log.warning("Failed to load categories.yaml (%s) — no categories will be assigned.", e)
            return []
    ```
  - Suggested fix: Let a malformed categories.yaml raise a RecipeParserError subclass. Keep only the missing-file case if a default is genuinely intended.
- `recipeparser/core/fsm.py:113` — class-cohesion: methods fall into disjoint clusters sharing no instance state — soft.class-cohesion
  - Why: The checkpoint cluster (save_checkpoint, load_checkpoint, delete_checkpoint, _checkpoint_path, _book_hash) uses only _output_dir to do JSON file I/O. It shares no state with the FSM and pause/cancel cluster (status, _lock, _resume_event, _auto_resume_timer, _consecutive_429s), so the class mixes state-transition policy with file persistence. No code under recipeparser/ calls the checkpoint methods, record_429 or trigger_rate_limit_pause.
  - Snippet:
    ```
    class PipelineController:
    ```
  - Suggested fix: Move checkpoint persistence (and _output_dir) into its own checkpoint store class, so PipelineController keeps only the transition table, pause/resume/cancel and the callbacks. If the 429 auto-pause path really has no callers, decide whether it stays.
- `recipeparser/adapters/share_worker.py:96` — class-cohesion: job description needs 'and' between unrelated responsibilities; disjoint clusters — soft.class-cohesion
  - Why: The class copies accepted shares and also runs a timed expiry/purge sweep, which covers household invites too. The sweep (_sweep, _sweep_call) uses only _clock, _next_expire and _next_purge. The copy job (_claim, _start, _step, _copy, _picture, _wind_down, _finish) uses _held, _images and _supabase_url. The two share only the Supabase handle.
  - Snippet:
    ```
    class ShareWorker:
    ```
  - Suggested fix: Move _sweep and _sweep_call, with their timers, into their own worker (for example a ShareSweeper) that run_workers polls beside ShareWorker. ShareWorker then holds only the share_accept job lifecycle.

### nit

- `recipeparser/core/pipeline.py:717` — fail-loud: interface probing instead of typed dispatch — soft.fail-loud
  - Why: RecipeExtraction declares prep_time and cook_time (line 718 likewise), and the other fields read with getattr defaults at lines 722-731, so these probes only hide a wrong type passed in as `raw: Any`.
  - Snippet:
    ```
    prep_time=raw.prep_time if hasattr(raw, "prep_time") else None,
    ```
  - Suggested fix: Type `raw` as RecipeExtraction and read the declared fields directly.
- `recipeparser/core/pipeline.py:851` — fail-loud: interface probing instead of typed dispatch — soft.fail-loud
  - Why: CayenneRecipe (and IngestResponse, its subclass) declares source_kind, source_key, source_title and source_author, so getattr with a default probes an interface the type already guarantees.
  - Snippet:
    ```
        kind = getattr(pr, "source_kind", None)
    ```
  - Suggested fix: Read pr.source_kind and the other declared fields directly.
- `recipeparser/adapters/api.py:1225` — fail-loud: interface probing instead of typed dispatch — soft.fail-loud
  - Why: pre_parsed is typed Optional[CayenneRecipe | IngestResponse], and CayenneRecipe declares category_paths and list_paths, so the getattr plus isinstance(list) probing is redundant once input_type is checked.
  - Snippet:
    ```
            raw = getattr(chunk.pre_parsed, "category_paths", None)
    ```
  - Suggested fix: Check input_type and that pre_parsed is not None, then read chunk.pre_parsed.category_paths and list_paths directly.
- `scripts/backfill_count_tokens.py:215` — no-shims: version sniffing — soft.no-shims
  - Why: A runtime feature probe that selects behaviour in a tooling-only script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure unconditionally (Python 3.7+), or set PYTHONIOENCODING in the runner.
- `scripts/backfill_direction_amounts.py:200` — no-shims: version sniffing — soft.no-shims
  - Why: A runtime feature probe that selects behaviour in a tooling-only script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure unconditionally, or set PYTHONIOENCODING in the runner.
- `scripts/backfill_pans.py:348` — no-shims: version sniffing — soft.no-shims
  - Why: A runtime feature probe that selects behaviour in a tooling-only script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure unconditionally, or set PYTHONIOENCODING in the runner.
- `scripts/backfill_servings.py:283` — no-shims: version sniffing — soft.no-shims
  - Why: A runtime feature probe that selects behaviour in a tooling-only script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure unconditionally, or set PYTHONIOENCODING in the runner.
- `scripts/backfill_temperatures.py:242` — no-shims: version sniffing — soft.no-shims
  - Why: A runtime feature probe that selects behaviour in a tooling-only script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure unconditionally, or set PYTHONIOENCODING in the runner.
- `scripts/backfill_yields.py:447` — no-shims: version sniffing — soft.no-shims
  - Why: A runtime feature probe that selects behaviour in a tooling-only script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure unconditionally, or set PYTHONIOENCODING in the runner.
- `scripts/probe_bake_types.py:66` — no-shims: version sniffing — soft.no-shims
  - Why: A runtime feature probe that selects behaviour in a tooling-only script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure unconditionally, or set PYTHONIOENCODING in the runner.
- `scripts/probe_yield_kinds.py:123` — no-shims: version sniffing — soft.no-shims
  - Why: A runtime feature probe that selects behaviour in a tooling-only script.
  - Snippet:
    ```
    if hasattr(sys.stdout, "reconfigure"):
    ```
  - Suggested fix: Call sys.stdout.reconfigure unconditionally, or set PYTHONIOENCODING in the runner.

### suggestion

- `recipeparser/io/readers/paprika.py:306` — fail-loud: broad handler with weak handling — soft.fail-loud
  - Why: Ambiguous: the docstring declares skip-and-continue, but an entry dropped here only reaches the log, never the job's skipped list (unlike a corrupt _cayenne_meta, which becomes a skip_reason chunk), and the docstring's stated RuntimeError is never raised.
  - Snippet:
    ```
                    except Exception as exc:
                        log.warning("Skipping entry %r — unexpected error: %s", name, exc)
                        continue
    ```
  - Suggested fix: Consider yielding a marker that read() turns into a skipped Chunk (as _unreadable_cayenne_chunk does) so the cook sees the lost entry, and correct the docstring.
- `recipeparser/io/readers/photo_check.py:40` — fail-loud: interface probing instead of typed dispatch — soft.fail-loud
  - Why: Ambiguous: which Pillow method gets called is chosen by probing for an attribute. That is a library-version shim with no typed alternative, but it stays alive for an old interface.
  - Snippet:
    ```
        getter = getattr(img, "get_flattened_data", None) or img.getdata
    ```
  - Suggested fix: Pin the Pillow version range in the project's dependencies and call the one supported method, or gate on a parsed PIL.__version__ with a comment saying when the fallback is removed.
- `recipeparser/adapters/api.py:1225` — no-shims: compatibility branches — soft.no-shims
  - Why: category_paths and list_paths are declared list fields on CayenneRecipe (null-coerced by validators), so these getattr probes and isinstance checks only guard pre_parsed being None and a 'v1 archive' shape. Whether user-held old archives warrant that needs human judgment.
  - Snippet:
    ```
    raw = getattr(chunk.pre_parsed, "category_paths", None)
    ```
  - Suggested fix: Narrow on chunk.pre_parsed is not None and read the typed fields directly, dropping the isinstance(raw, list) guards.
- `recipeparser/core/stages/assemble.py:98` — no-shims: explicit shims — soft.no-shims
  - Why: The flat categories list is labelled legacy and kept 'for backward compatibility with consumers' (docstring line 52). Paprika's external export format does need it, so whether this is a shim or a format requirement needs judgment.
  - Snippet:
    ```
    # legacy `categories` field (e.g. ["Italian", "Chicken"])
    ```
  - Suggested fix: If only the Paprika writers need it, derive it in those writers and drop the field. Otherwise reword the comment to name the Paprika format as the reason.
- `recipeparser/core/fat_tokens.py:146` — no-shims: compatibility branches — soft.no-shims
  - Why: The Fat Token grammar keeps two live shapes (tokens without `use`, written before 9.5.0) although scripts/backfill_direction_amounts.py exists to migrate them. Whether stored rows still need the legacy branch is a data question.
  - Snippet:
    ```
    return Use("legacy")
    ```
  - Suggested fix: Once the backfill has run over every row, remove the legacy kind (here and in count_tokens.py) so a missing use is invalid.
- `recipeparser/io/readers/photo_check.py:40` — no-shims: version sniffing — soft.no-shims
  - Why: A runtime feature probe that picks between Pillow 12's new API and the deprecated getdata in a production path. Its subject is an external dependency, so whether pinning Pillow is preferable needs judgment.
  - Snippet:
    ```
    getter = getattr(img, "get_flattened_data", None) or img.getdata
    ```
  - Suggested fix: Pin Pillow >= 12 and call get_flattened_data directly, or pin below 12 and call getdata, rather than probing at runtime.
- `recipeparser/io/readers/url.py:95` — no-shims: type escape hatches — soft.no-shims
  - Why: headers is typed Any and an AttributeError catch accepts anything without .get(), which papers over an undeclared interface (possibly test doubles).
  - Snippet:
    ```
    except AttributeError:
            return False
    ```
  - Suggested fix: Type headers as a Mapping[str, str] (or the requests/httpx headers type) and drop the AttributeError catch.
- `recipeparser/adapters/gui.py:66` — class-cohesion: presentation mixed with I/O — soft.class-cohesion
  - Why: The widget builds and redraws the two-panel editor. The same class also reads and writes categories.yaml (_load, _save, _to_yaml_structure) and syncs from the Paprika SQLite database (_sync_from_paprika). These share _data and _order, so whether to split a GUI frame like this needs human judgment.
  - Snippet:
    ```
    class CategoryEditorFrame(ctk.CTkFrame):
    ```
  - Suggested fix: Consider moving the YAML load/save and Paprika sync into a small category-tree model or repository that the frame calls, so the frame keeps only rendering and user-event handling.
- `recipeparser/adapters/gui.py:502` — class-cohesion: presentation mixed with orchestration — soft.class-cohesion
  - Why: Besides building the view and handling its widgets, _start_parse sets up and runs the pipeline itself: it resolves the book path, creates the output directory, builds the PipelineController and genai.Client, sets GOOGLE_API_KEY in os.environ, attaches a root-logger handler and starts the worker thread. Whether that belongs in the view needs judgment.
  - Snippet:
    ```
    class ParseFrame(ctk.CTkFrame):
    ```
  - Suggested fix: Consider moving the run setup (client, controller, env, log routing, thread) into a small run-controller object that the frame starts and polls, so the frame stays a view over controller status.
- `recipeparser/adapters/ai_gate.py:50` — class-cohesion: two clusters with little shared state — soft.class-cohesion
  - Why: The database-backed kill switch and recipe cap (paused, allowance, ai_allowed, consume_recipe, using _get_client and the paused cache) sit beside in-memory per-user sliding-window rate limiters (_embed_slot, _window_slot, _search_slot, _find_slot, search_retry_after, using _embeds, _search_hour/_day and _find_hour/_day). The two share only _lock and _clock and meet only in check(). The module documents them as one gate, so this needs judgment.
  - Snippet:
    ```
    class AiGate:
    ```
  - Suggested fix: Consider moving the sliding-window limiter into its own class (one instance per window pair), which AiGate.check calls, so AiGate keeps only the database-backed allowance and pause policy.
- `recipeparser/core/pipeline.py:102` — class-cohesion: picture-selection policy and storage I/O inside the stage orchestrator — soft.class-cohesion
  - Why: Beside orchestrating chunks (run, _process_chunk, _get_stages, _enter_stage), the class holds a picture cluster: _restore_bucket_picture downloads and re-stores pictures, and _is_food, _food_book_photo and _ask_food decide which picture to keep. That cluster is the only user of _picture_source and _food_check, and shares only _image_store and _limiter with the orchestration.
  - Snippet:
    ```
    class RecipePipeline:
    ```
  - Suggested fix: Consider moving the picture handling into a collaborator (for example a picture-keeper built from image_store, picture_source, food_check and the limiter) that the pipeline calls per chunk and per recipe.

0 findings exempted by inline tokens.

## Roll-up

- Groups: OK: 5, FAILED: 0
- Findings by severity: blocking: 8, important: 35, nit: 11, suggestion: 11
- Auditor error notes: 9

no baseline (run --update-baseline to set one)

