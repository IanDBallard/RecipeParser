# RecipeParser v2.2.0 — Phase 3 Release

## What's New

### 🗂️ Folder Processing & Archive Merging (Phase 3a)

You can now point RecipeParser at an entire folder of EPUB/PDF files and it will process them all in one go:

```
recipeparser --folder /path/to/cookbooks/
```

Each book is processed independently and produces its own `.paprikarecipes` archive. When the folder run completes, all archives are automatically merged into a single `merged_<timestamp>.paprikarecipes` file — deduplicated by recipe name (accent-insensitive, punctuation-insensitive) so duplicates across books are silently dropped.

You can also merge existing archives manually:

```
recipeparser --merge archive1.paprikarecipes archive2.paprikarecipes
```

---

### ⏸️ PipelineController FSM — Pause, Resume & Cancel (Phase 3b)

A new `PipelineController` class wraps every pipeline run with a proper finite-state machine:

| State | Meaning |
|---|---|
| `IDLE` | No run in progress |
| `RUNNING` | Actively processing |
| `PAUSING` | Pause requested; waiting for next safe checkpoint |
| `PAUSED` | Fully paused; worker is blocked |
| `RESUMING` | Resume requested; worker unblocking |
| `CANCELLING` | Cancel requested; worker will exit cleanly |

The GUI and CLI can call `request_pause()`, `request_resume()`, and `request_cancel()` from any thread. The worker thread cooperatively checks `check_pause_point()` between segments — no forced thread kills.

**Checkpoint persistence:** Progress is saved to a JSON checkpoint file (keyed by a SHA-256 hash of the first 64 KB of the book) so an interrupted run can be resumed from where it left off.

---

### 🚦 Automatic Rate-Limit Pause on 429 Errors (Phase 3c)

When Gemini returns repeated HTTP 429 (Too Many Requests) responses, the pipeline now auto-pauses rather than crashing:

- A configurable threshold (`RATE_LIMIT_PAUSE_THRESHOLD`, default: 3 consecutive 429s) triggers an automatic pause.
- The controller schedules an auto-resume after a configurable cooldown (`RATE_LIMIT_AUTO_RESUME_SECS`, default: 1 hour).
- A `RateLimitPauseError` exception is raised to signal the pause; the worker catches it and calls `trigger_rate_limit_pause()`.
- The auto-resume timer is cancelled immediately if the user manually resumes or cancels.

---

### 🔄 Recategorize Existing Archives (Phase 3d)

Already have a `.paprikarecipes` archive but want to re-run categorisation against an updated `categories.yaml`? Use the new `--recategorize` flag:

```
recipeparser --recategorize cookbook.paprikarecipes
```

This reads every recipe from the archive, re-runs the Gemini category-assignment against the current taxonomy, and writes a new `cookbook_recategorized.paprikarecipes` alongside the original. Recipes that fail categorisation keep their existing categories.

---

## Bug Fixes & Internal Improvements

- **Module-level imports for patchability:** `process_epub` and `get_env_file` are now module-level imports in `__main__.py`, making them correctly patchable in tests (`recipeparser.__main__.process_epub`).
- **Deadlock fix in `check_pause_point()`:** The `transition("running")` call after a resume is now made *outside* the internal lock, eliminating a potential deadlock when the GUI thread and worker thread both tried to acquire the lock simultaneously.
- **`.gitignore` updated:** The stray Windows reserved device name `nul` in the repo root is now excluded.

---

## Test Coverage

288 tests pass across all modules (excluding `test_gui.py` which requires a display server):

| New test file | What it covers |
|---|---|
| `tests/test_pipeline_controller.py` | FSM transitions, pause/resume/cancel, checkpoint save/load/delete, 429 auto-pause |
| `tests/test_merge_exports.py` | Archive merging, deduplication, bad-ZIP tolerance, empty-input error |
| `tests/test_recategorize.py` | Full recategorize flow, missing-file error, empty-archive error, categorisation failure fallback |

---

## Upgrade Notes

- No breaking changes to the existing single-file CLI interface.
- The `--folder`, `--merge`, and `--recategorize` flags are all additive.
- Checkpoint files are stored in `<output_dir>/.checkpoints/` and are safe to delete manually.
- Requires an authenticated `GEMINI_API_KEY` in your `.env` file (unchanged from v2.1.x).
