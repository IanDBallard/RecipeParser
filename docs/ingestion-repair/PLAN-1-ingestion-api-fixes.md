# Ingestion API Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the seven ingestion defects that lost twenty recipes and 466 photographs from the 2026-09-04 import, so a partial import is retried, counted, named, and written as it goes.

**Architecture:** All work is in the `RecipeParser` repository, which is a hexagonal layout: `core/` holds the pipeline and its ports and may not import `io/` or `adapters/`; `io/` holds readers and writers; `adapters/` holds the FastAPI app, CLI and GUI. Fixes follow that grain — a new `ImageStore` port for photographs, new optional callbacks on `RecipePipeline.run`, and a new `adapters/job_sink.py` holding the per-job bookkeeping both endpoints need, so `api.py` does not grow further.

**Tech Stack:** Python 3.11+, FastAPI, pydantic v2, `google-genai`, supabase-py, httpx, pytest.

**Spec:** `docs/superpowers/specs/2026-09-05-ingestion-repair-design.md` in the **Cayenne** repository (`IanDBallard/Cayenne`). Read sections 4, 7 and 8 before starting. This plan implements spec section 4 in full.

**Repository:** `c:\Users\iball\Arduino\Projects\RecipeParser\RecipeParser`, branch off `main` at `667f5ef`.

## Global Constraints

- `core/` must not import from `recipeparser.io` or `recipeparser.adapters`. Every stage module states this in its docstring; keep it true.
- Fail-loud is the house rule (§11.4 of `PIPELINE_REFACTOR.md`): a failed stage-change callback re-raises. The two new callbacks are the deliberate exception and must be commented as such — see Task 3.
- `SUPABASE_SERVICE_ROLE_KEY` is the only accepted service-key variable name after Task 8.
- Line length 120 (`ruff`, `pyproject.toml:62`).
- The test suite must not touch the live Supabase project. `_live_writes_blocked()` (`api.py:380`) exists for this; never bypass it in a test.
- Run the whole suite with `pytest tests -q` before every commit; it is fast and there are no live calls in it.
- Existing callers of `RecipePipeline.run` — `adapters/cli.py:136`, `adapters/gui.py`, `tests/unit/test_pipeline.py` — pass no new arguments and must keep working unchanged. Every new parameter is keyword-optional with a `None` default.

---

### Task 1: Retry a reply that will not parse

**Files:**
- Modify: `recipeparser/config.py` (add two constants after `MAX_RETRIES`, line 34)
- Modify: `recipeparser/exceptions.py` (add `ExtractionParseError`)
- Modify: `recipeparser/gemini.py:230`, `:303` (both extraction entry points)
- Test: `tests/unit/test_gemini_parse_retry.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `ExtractionParseError(RecipeParserError)`; `gemini.extract_recipes(...)` and `gemini.extract_recipe_from_text(...)` now raise it instead of returning `None` when every attempt fails. Both still return `RecipeList` on success.

**Why this shape.** `_call_with_retry` (`gemini.py:46`) already retries transport and quota failures, but `json.loads` and `RecipeList.model_validate` sit *outside* it at `gemini.py:303` and `:230`, and the bare `except Exception` on the following line turns a 200 response carrying truncated JSON into `None`. Nothing raised at the transport layer, so no retry ever happened. The parse must move inside a retry loop.

The parse retry uses its own small budget, not `MAX_RETRIES` (5, with a 2→120s backoff ladder). A truncated reply is not a rate-limit signal, and the twenty lost recipes all extracted cleanly on a manual re-run, so two quick retries recover them while the quota ladder would add tens of minutes to a large import for nothing.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_gemini_parse_retry.py`:

```python
"""Truncated-reply retry (spec 4.1). No real API calls — the client is a stub."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from recipeparser.exceptions import ExtractionParseError
from recipeparser.gemini import extract_recipes


VALID = '{"recipes": []}'
TRUNCATED = '{"recipes": [{"title": "Half a rec'


def _client(*replies: str) -> MagicMock:
    """A stub genai client whose generate_content returns each reply in turn."""
    client = MagicMock()
    responses = [
        SimpleNamespace(
            text=r,
            candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
        )
        for r in replies
    ]
    client.models.generate_content.side_effect = responses
    return client


def test_truncated_reply_is_retried_and_the_retry_wins(monkeypatch):
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    client = _client(TRUNCATED, VALID)

    result = extract_recipes("some chunk text", client)

    assert result.recipes == []
    assert client.models.generate_content.call_count == 2


def test_every_attempt_unparseable_raises_with_the_finish_reason(monkeypatch):
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    client = _client(TRUNCATED, TRUNCATED, TRUNCATED)

    with pytest.raises(ExtractionParseError) as excinfo:
        extract_recipes("some chunk text", client)

    assert "MAX_TOKENS" in str(excinfo.value)
    assert client.models.generate_content.call_count == 3


def test_an_empty_reply_is_retried_too(monkeypatch):
    monkeypatch.setattr("recipeparser.gemini.time.sleep", lambda _s: None)
    client = _client("", VALID)

    result = extract_recipes("some chunk text", client)

    assert result.recipes == []
    assert client.models.generate_content.call_count == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/test_gemini_parse_retry.py -v`
Expected: FAIL — `ImportError: cannot import name 'ExtractionParseError'`.

- [ ] **Step 3: Add the constants and the exception**

In `recipeparser/config.py`, immediately after `MAX_RETRIES: int = 5` (line 34):

```python
# A reply that will not parse is not a rate-limit signal: the transport
# succeeded and the model simply returned truncated or malformed JSON. The
# 2026-09-04 import showed such replies parse cleanly when asked again, so a
# short, flat retry recovers them without adding the quota ladder's minutes to
# every bad chunk of a large import.
MAX_PARSE_RETRIES: int = 2
PARSE_RETRY_DELAY_SECS: float = 1.0
```

In `recipeparser/exceptions.py`, at the end of the file:

```python
class ExtractionParseError(RecipeParserError):
    """Raised when Gemini's extraction reply could not be parsed on any attempt.

    Distinct from a chunk that genuinely contains no recipe: that is an empty
    result, not an error. This means the reply itself was unusable — usually
    truncated JSON — and the chunk's recipes are lost unless something upstream
    counts it. The message carries the reply's finish reason and first line,
    which is what separates truncation from malformed content.
    """
```

- [ ] **Step 4: Add the retrying parse helper**

In `recipeparser/gemini.py`, update the config import at line 10:

```python
from recipeparser.config import (
    BACKOFF_BASE_SECS,
    BACKOFF_MAX_SECS,
    MAX_PARSE_RETRIES,
    MAX_RETRIES,
    PARSE_RETRY_DELAY_SECS,
)
```

Add these imports near the top of the file, beside the existing ones:

```python
from pydantic import ValidationError

from recipeparser.exceptions import ExtractionParseError
```

Then add, immediately after `_call_with_retry` ends (before `def verify_connectivity`):

```python
def _finish_reason(response: object) -> str:
    """Best-effort finish reason from a genai response; empty when absent."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    return str(getattr(candidates[0], "finish_reason", "") or "")


def _generate_and_parse(client, model: str, contents: str, config: dict, *, what: str) -> RecipeList:
    """Call Gemini and parse the reply, retrying a reply that will not parse.

    ``_call_with_retry`` covers transport and quota failures. It cannot cover a
    200 response whose body is truncated JSON, because nothing raised — which is
    why such a reply was previously swallowed and its recipes lost. The parse
    therefore happens inside this loop, not after it.

    Raises:
        ExtractionParseError: every attempt returned something unparseable.
    """
    last_error = "no attempt made"
    for attempt in range(1, MAX_PARSE_RETRIES + 2):
        response = _call_with_retry(client, model=model, contents=contents, config=config)
        text = (getattr(response, "text", "") or "").strip()
        if text:
            try:
                return RecipeList.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        else:
            last_error = "empty response"
        log.warning(
            "%s: unparseable reply (attempt %d/%d) — finish_reason=%s, first line=%r",
            what, attempt, MAX_PARSE_RETRIES + 1, _finish_reason(response),
            text.splitlines()[0][:200] if text else "",
        )
        if attempt <= MAX_PARSE_RETRIES:
            time.sleep(PARSE_RETRY_DELAY_SECS)
    raise ExtractionParseError(
        f"{what}: {MAX_PARSE_RETRIES + 1} attempts all unparseable "
        f"(finish_reason={_finish_reason(response)}); last error: {last_error}"
    )
```

- [ ] **Step 5: Use it from both entry points**

In `extract_recipes`, replace the whole `try:` block that currently spans `gemini.py:289-305` with:

```python
    return _generate_and_parse(
        client,
        model="gemini-2.5-flash",
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "response_json_schema": _schema_for_gemini(RecipeList),
            "temperature": 0.1,
        },
        what="Gemini extraction",
    )
```

In `extract_recipe_from_text`, replace the `try:` block spanning `gemini.py:215-232` with:

```python
    return _generate_and_parse(
        client,
        model="gemini-2.5-flash",
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "response_json_schema": _schema_for_gemini(RecipeList),
            "temperature": 0.1,
        },
        what="Gemini plain-text extraction",
    )
```

Update both function signatures' return annotations from `Optional[RecipeList]` to `RecipeList`, and their docstrings to say they raise `ExtractionParseError`.

- [ ] **Step 6: Stop the extract stage treating failure as an empty chunk**

In `recipeparser/core/stages/extract.py`, delete these lines (currently `:63-65`):

```python
    if result is None:
        log.warning("extract(): Gemini returned None — treating as empty chunk.")
        return []
```

and update the docstring's `Returns:` paragraph to:

```
    Returns:
        A list of ``RecipeExtraction`` objects.  Returns ``[]`` when the chunk
        contains no recognisable recipes — this is NOT an error condition.

    Raises:
        ValueError: If ``chunk_text`` is empty or whitespace-only.
        ExtractionParseError: If Gemini's reply could not be parsed on any
            attempt.  Distinct from an empty chunk: the recipes existed and
            were lost, so the caller must count this rather than ignore it.
```

- [ ] **Step 7: Run the new tests**

Run: `pytest tests/unit/test_gemini_parse_retry.py -v`
Expected: PASS, all three.

- [ ] **Step 8: Run the whole suite**

Run: `pytest tests -q`
Expected: PASS. If a stage test asserted `extract()` returns `[]` for a `None` reply, update it to assert `ExtractionParseError` is raised — that behaviour change is the point of this task, not a regression.

- [ ] **Step 9: Commit**

```bash
git add recipeparser/config.py recipeparser/exceptions.py recipeparser/gemini.py recipeparser/core/stages/extract.py tests/unit/test_gemini_parse_retry.py
git commit -m "fix(extract): retry a reply that will not parse instead of dropping the chunk

The retry wrapper covered transport and quota failures, but the parse sat
outside it, so a 200 response carrying truncated JSON was caught by a bare
except and returned as None. The extract stage read that None as an empty
chunk. Twenty recipes left the 2026-09-04 import that way, with no retry, no
count and a job that still reported success.

The parse now happens inside its own short retry loop and raises
ExtractionParseError when every attempt fails, carrying the reply's finish
reason and first line - what distinguishes truncation from content the model
could not parse. The retry budget is deliberately small and flat: a bad reply
is not a rate-limit signal, and these replies parse on the next attempt.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Give a chunk a label

**Files:**
- Modify: `recipeparser/core/models.py:74-80` (the `Chunk` dataclass and its field docs)
- Modify: `recipeparser/io/readers/paprika.py:107-130`
- Modify: `recipeparser/io/readers/epub.py`, `recipeparser/io/readers/pdf.py`
- Test: `tests/unit/readers/test_readers.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: `Chunk.label: Optional[str]`, a defaulted field. Task 3's `on_skip` callback reads it; Task 5 writes it into the job row.

**Why this shape.** A count says twenty recipes were lost without saying which. The Paprika reader already holds each entry's name at `paprika.py:119`, folds it into the chunk text and discards it as a value. A defaulted dataclass field no stage reads costs nothing and makes the loss nameable. For book chunks the label names a region, not a recipe — what a chunk contained is unknowable until extraction succeeds, and extraction is what failed — so the field is null there rather than guessed.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/readers/test_readers.py`:

```python
def test_paprika_reader_labels_each_chunk_with_the_entry_name(tmp_path):
    """Spec 4.2: a dropped chunk must be nameable, and Paprika supplies real names."""
    archive = _write_paprika_archive(
        tmp_path,
        [{"name": "Sticky Toffee Pudding", "ingredients": "1 cup dates", "directions": "Bake."}],
    )

    chunks = PaprikaReader().read(str(archive))

    assert [c.label for c in chunks] == ["Sticky Toffee Pudding"]
```

Reuse whichever archive-building helper already exists in this module; if there is none, write `_write_paprika_archive(tmp_path, entries)` producing a `.paprikarecipes` zip of gzipped JSON entries, matching what `PaprikaReader.read` expects at `paprika.py:231-270`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/readers/test_readers.py -k label -v`
Expected: FAIL — `AttributeError: 'Chunk' object has no attribute 'label'`.

- [ ] **Step 3: Add the field**

In `recipeparser/core/models.py`, add to the `Chunk` dataclass after `image_bytes`:

```python
    label: Optional[str] = None
```

and to the docstring's field list, after the `image_bytes` entry:

```
    label:
        Human-readable identifier for this chunk, used to name it if it is
        dropped.  A Paprika entry's name, an EPUB chapter title, a PDF page
        range — whatever the source actually provides.  None when the source
        names nothing: what a book chunk contained is unknown until extraction
        succeeds, and a dropped chunk is one where it did not.  No stage reads
        this field; it exists for reporting.
```

- [ ] **Step 4: Fill it in the readers**

`recipeparser/io/readers/paprika.py` — both `Chunk(...)` constructions (the `PAPRIKA_CAYENNE` branch at `:107-115` and the `PAPRIKA_LEGACY` branch at `:124-130`) gain `label=entry.get("name") or None`. In the legacy branch the local `name` is already in scope, so use `label=name or None`.

`recipeparser/io/readers/epub.py` — where each chapter chunk is constructed, pass the chapter title the reader already has.

`recipeparser/io/readers/pdf.py` — where each page-range chunk is constructed, pass `f"pages {first}-{last}"`.

For both book readers: if the value is not already to hand at the construction site, pass `label=None` rather than restructuring the reader. A null label is honest; an invented one is not.

- [ ] **Step 5: Run the tests**

Run: `pytest tests/unit/readers -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add recipeparser/core/models.py recipeparser/io/readers/ tests/unit/readers/test_readers.py
git commit -m "feat(core): let a chunk carry the name it will be reported under

A count of dropped chunks says twenty recipes were lost without saying which.
The Paprika reader already reads each entry's name and folds it into the chunk
text, then discards it; keeping it costs a defaulted field no stage reads.

Book readers label a region rather than a recipe, because what a chunk held is
unknown until extraction succeeds and a dropped chunk is one where it did not.
The field is null there rather than inferred from the text.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Report results and skips as they happen

**Files:**
- Modify: `recipeparser/core/pipeline.py:101-180` (the `run` signature, docstring and chunk loop)
- Test: `tests/unit/test_pipeline.py` (append)

**Interfaces:**
- Consumes: `Chunk.label` from Task 2.
- Produces: two new keyword-optional parameters on `RecipePipeline.run`:
  - `on_result: Optional[Callable[[IngestResponse], None]] = None` — fired once per assembled recipe, from the collecting loop in the calling thread, in completion order.
  - `on_skip: Optional[Callable[[Chunk, str], None]] = None` — fired once per chunk that produced no result because of a failure. The second argument is a short reason string.
  - `run` still returns `List[IngestResponse]`, unchanged.

**Why this shape.** The existing loop already isolates per-chunk failures and logs them (`pipeline.py:158-165`), so nothing downstream can distinguish a barren chunk from a lost one. These callbacks mirror the `on_progress` parameter the method already takes, which keeps one convention in the file.

**The fail-loud exception, deliberately.** `on_progress` re-raises when it fails (`pipeline.py:171-175`) because a stale stage label is indistinguishable from a zombie job. The two new callbacks must **not** re-raise: `on_result` is a single recipe's write, and killing an 827-recipe import because row 400 failed to insert is strictly worse than losing row 400 and saying so. Comment this at the call site so the next reader does not "fix" the inconsistency.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_pipeline.py`:

```python
def test_on_result_fires_per_recipe_and_on_skip_names_the_failed_chunk():
    """Spec 4.2, 4.3: results stream out as they finish and failures are reported, not swallowed."""
    good = Chunk(text="good", input_type=InputType.URL, label="Good One")
    bad = Chunk(text="bad", input_type=InputType.URL, label="Bad One")
    results_seen: List[IngestResponse] = []
    skips_seen: List[tuple] = []

    pipeline = _make_pipeline()
    with patch.object(
        RecipePipeline,
        "_process_chunk",
        side_effect=lambda chunk, stages, axes: (
            [_make_ingest_response("Good One")] if chunk.text == "good" else _raise(RuntimeError("boom"))
        ),
    ):
        returned = pipeline.run(
            [good, bad],
            on_result=results_seen.append,
            on_skip=lambda chunk, reason: skips_seen.append((chunk.label, reason)),
        )

    assert [r.title for r in results_seen] == ["Good One"]
    assert [r.title for r in returned] == ["Good One"]
    assert len(skips_seen) == 1
    assert skips_seen[0][0] == "Bad One"
    assert "boom" in skips_seen[0][1]


def test_a_raising_on_result_does_not_abort_the_run():
    """Unlike on_progress: one failed write must not discard the rest of an 827-recipe import."""
    chunks = [Chunk(text=f"c{i}", input_type=InputType.URL) for i in range(3)]

    pipeline = _make_pipeline()
    with patch.object(
        RecipePipeline,
        "_process_chunk",
        side_effect=lambda chunk, stages, axes: [_make_ingest_response(chunk.text)],
    ):
        returned = pipeline.run(chunks, on_result=lambda _r: (_ for _ in ()).throw(RuntimeError("write failed")))

    assert len(returned) == 3


def _raise(exc: Exception):
    raise exc
```

`_make_pipeline()` is a helper: if `test_pipeline.py` already has one, use it; otherwise add

```python
def _make_pipeline(**kwargs) -> RecipePipeline:
    return RecipePipeline(
        client=MagicMock(),
        controller=PipelineController(),
        category_source=_FakeCategorySource(),
        **kwargs,
    )
```

using whatever fake category source the module already defines.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/test_pipeline.py -k "on_result or on_skip" -v`
Expected: FAIL — `TypeError: run() got an unexpected keyword argument 'on_result'`.

- [ ] **Step 3: Widen the signature**

In `recipeparser/core/pipeline.py`, change `run`:

```python
    def run(
        self,
        chunks: List[Chunk],
        on_progress: Optional[Callable[[str, int, int], None]] = None,
        user_id: str = "",
        *,
        on_result: Optional[Callable[[IngestResponse], None]] = None,
        on_skip: Optional[Callable[[Chunk, str], None]] = None,
    ) -> List[IngestResponse]:
```

`on_progress` and `user_id` keep their positions, because `api.py` passes them positionally today. Add to the docstring's `Args:`:

```
            on_result:   Optional callback fired once per assembled recipe, in
                         completion order, from this thread.  Lets a caller
                         persist results as they arrive instead of after the
                         whole batch.
            on_skip:     Optional callback ``(chunk, reason) -> None`` fired for
                         every chunk that produced no result because of a
                         failure.  A chunk that simply contained no recipe is
                         not a skip.
```

- [ ] **Step 4: Fire them in the loop**

Replace the body of the `for future in as_completed(future_to_chunk):` loop's `try/except/finally` (`pipeline.py:158-176`) with:

```python
                chunk = future_to_chunk[future]
                try:
                    results = future.result(timeout=SEGMENT_TIMEOUT_SECS)
                    all_results.extend(results)
                    if on_result is not None:
                        for result in results:
                            try:
                                on_result(result)
                            except Exception:
                                # Deliberately NOT re-raised, unlike on_progress below. That
                                # callback guards a stale stage label, which is indistinguishable
                                # from a zombie job. This one is a single recipe's write, and
                                # aborting an 827-recipe import because one row failed is worse
                                # than losing that row and saying so — which _report_skip does.
                                log.exception("RecipePipeline: on_result callback failed for one recipe.")
                                _report_skip(chunk, "result callback failed")
                except TimeoutError:
                    log.warning("RecipePipeline: chunk timed out after %ds — skipping.", SEGMENT_TIMEOUT_SECS)
                    _report_skip(chunk, f"timed out after {SEGMENT_TIMEOUT_SECS}s")
                except Exception as exc:
                    log.error("RecipePipeline: chunk worker raised unexpectedly — skipping. Error: %s", exc)
                    _report_skip(chunk, f"{type(exc).__name__}: {exc}")
                finally:
                    completed += 1
                    if on_progress is not None:
                        try:
                            on_progress("PROCESSING", completed, total)
                        except Exception:
                            log.exception("RecipePipeline: on_progress callback failed — re-raising (§11.4).")
                            raise
```

and define the reporter just above the `with ThreadPoolExecutor(...)` block:

```python
        def _report_skip(chunk: Chunk, reason: str) -> None:
            """Tell the caller a chunk produced nothing, and never let that itself fail the run."""
            if on_skip is None:
                return
            try:
                on_skip(chunk, reason)
            except Exception:
                log.exception("RecipePipeline: on_skip callback failed — continuing.")
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/unit/test_pipeline.py -v`
Expected: PASS, including the pre-existing tests — they pass neither callback and take the `None` paths.

- [ ] **Step 6: Commit**

```bash
git add recipeparser/core/pipeline.py tests/unit/test_pipeline.py
git commit -m "feat(pipeline): stream results out and report the chunks that produced none

The loop already isolated per-chunk failures and logged them, so nothing
downstream could tell a chunk that held no recipe from one whose recipes were
lost, and no caller could persist anything until every chunk had finished.

Two optional callbacks in the style of the on_progress the method already
takes. on_result fires per assembled recipe so a caller can write as it goes;
on_skip fires for every chunk dropped by a failure, with the chunk and a
reason. Both are keyword-only with None defaults, so the CLI, the GUI and the
existing tests are untouched.

Neither re-raises, unlike on_progress. That callback guards a stale stage
label, indistinguishable from a zombie job; these guard one recipe, and
killing an 827-recipe import over one bad row is worse than reporting it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Store the photographs the reader already extracts

**Files:**
- Modify: `recipeparser/core/ports.py` (add `ImageStore`)
- Create: `recipeparser/io/writers/image_store.py`
- Modify: `recipeparser/io/readers/paprika.py:113`, `:128` (decode `photo_data`)
- Modify: `recipeparser/core/pipeline.py` (constructor, and the three `image_url=` sites at `:241`, `:264`, `:316`)
- Test: `tests/unit/test_pipeline.py` (append), `tests/unit/writers/test_writers.py` (append), `tests/unit/readers/test_readers.py` (append)

**Interfaces:**
- Consumes: `RecipePipeline.run` from Task 3 (unchanged by this task).
- Produces:
  - `recipeparser.core.ports.ImageStore`, an ABC with `put(self, image_bytes: bytes, recipe_id: str, content_type: str = "image/jpeg") -> Optional[str]`.
  - `recipeparser.io.writers.image_store.SupabaseImageStore`, implementing it.
  - `RecipePipeline.__init__` gains `image_store: Optional[ImageStore] = None`, keyword-optional and last.
  - `Chunk.image_content_type: str = "image/jpeg"`, and `Chunk.image_bytes` now genuinely holds bytes.
  - `recipeparser.io.readers.paprika._decode_photo(entry) -> tuple[Optional[bytes], str]` — the repair scripts in the third plan reuse this rather than re-implementing the decode.

**Why this shape.** `PaprikaReader` attaches each entry's photo to the chunk (`paprika.py:113`, `:128`) and nothing reads the field. The one upload helper, `_upload_image_to_storage` (`api.py:294`), takes a web address and is called from one place Paprika entries never reach. 466 of 827 entries carried a photo and none arrived. `core/` may not import `io/`, so the capability has to enter as a port, exactly as `CategorySource` does.

**A second defect in the same path, found while planning.** `Chunk.image_bytes` is typed `Optional[bytes]`, but Paprika's `photo_data` is a **base64 string**, and the reader assigns it straight across. Verified against the actual archive: `photo_data` is a `str` of 1.5 MB for the first entry, exactly 466 of the 827 entries carry one, and the archive holds no loose image files — so `photo_data` is the entire photo path and `read_entries_with_images` would find nothing. Uploading the field unchanged would store base64 text as a JPEG. The reader must decode, because that is where the archive's encoding is known.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_pipeline.py`:

```python
class _FakeImageStore(ImageStore):
    """Records what it was asked to store; returns a predictable URL."""

    def __init__(self, url: Optional[str] = "https://example.test/stored.jpg") -> None:
        self.url = url
        self.calls: List[bytes] = []

    def put(self, image_bytes: bytes, recipe_id: str, content_type: str = "image/jpeg") -> Optional[str]:
        self.calls.append(image_bytes)
        return self.url


def test_chunk_image_bytes_are_stored_and_the_url_reaches_the_recipe():
    """Spec 4.5: 466 photographs were read and dropped; they must reach the assembled recipe."""
    store = _FakeImageStore()
    chunk = Chunk(text="a recipe", input_type=InputType.URL, image_bytes=b"\xff\xd8jpegbytes")

    pipeline = _make_pipeline(image_store=store)
    with patch.object(
        RecipePipeline, "_process_chunk", side_effect=lambda c, s, a: [_make_ingest_response("R")]
    ):
        pipeline.run([chunk])

    assert store.calls == [b"\xff\xd8jpegbytes"]
    assert chunk.image_url == "https://example.test/stored.jpg"


def test_a_failed_upload_still_yields_the_recipe():
    """A missing photograph is not a reason to lose a recipe."""
    store = _FakeImageStore(url=None)
    chunk = Chunk(text="a recipe", input_type=InputType.URL, image_bytes=b"bytes")

    pipeline = _make_pipeline(image_store=store)
    with patch.object(
        RecipePipeline, "_process_chunk", side_effect=lambda c, s, a: [_make_ingest_response("R")]
    ):
        results = pipeline.run([chunk])

    assert len(results) == 1
    assert chunk.image_url is None
```

Add `from recipeparser.core.ports import CategorySource, ImageStore` to the module's imports.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/test_pipeline.py -k image -v`
Expected: FAIL — `ImportError: cannot import name 'ImageStore'`.

- [ ] **Step 3: Add the port**

In `recipeparser/core/ports.py`, after the `CategorySource` class:

```python
class ImageStore(ABC):
    """Somewhere a recipe's hero image can be put, addressed by a public URL.

    A port so the pipeline can store the photograph a reader pulled out of an
    archive without importing recipeparser.io — the same reason CategorySource
    exists.  The Supabase implementation lives in recipeparser.io.writers.
    """

    @abstractmethod
    def put(self, image_bytes: bytes, recipe_id: str, content_type: str = "image/jpeg") -> Optional[str]:
        """Store *image_bytes* and return its public URL.

        Returns None on any failure.  Implementations must not raise: a
        photograph that cannot be stored is a recipe without a picture, never a
        recipe that fails to import.
        """
```

Add `Optional` to the module's `typing` import if it is not already there.

- [ ] **Step 4: Implement it**

Create `recipeparser/io/writers/image_store.py`:

```python
"""Supabase Storage implementation of the ImageStore port.

Holds the bucket logic that was previously inline in adapters/api.py, so both
the pipeline's byte uploads and the API's URL uploads go through one path.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from recipeparser.core.ports import ImageStore

log = logging.getLogger(__name__)

BUCKET = "recipe-images"

_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


class SupabaseImageStore(ImageStore):
    """Uploads to the public ``recipe-images`` bucket using the service-role key."""

    def __init__(self, url: Optional[str] = None, service_key: Optional[str] = None) -> None:
        self._url = url or os.environ.get("SUPABASE_URL", "")
        self._key = service_key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    def put(self, image_bytes: bytes, recipe_id: str, content_type: str = "image/jpeg") -> Optional[str]:
        if not image_bytes:
            return None
        if not self._url or not self._key:
            log.warning("SupabaseImageStore: credentials not set — cannot store the image for %s.", recipe_id)
            return None
        ext = _EXTENSIONS.get(content_type.lower(), "jpg")
        path = f"{BUCKET}/{recipe_id}.{ext}"
        try:
            from supabase import create_client  # noqa: PLC0415

            sb = create_client(self._url, self._key)
            sb.storage.from_(BUCKET).upload(
                path,
                image_bytes,
                {"content-type": content_type, "upsert": "true"},
            )
            public_url: str = sb.storage.from_(BUCKET).get_public_url(path)
            return public_url
        except Exception:
            log.exception("SupabaseImageStore: failed to store the image for %s — continuing without it.", recipe_id)
            return None
```

- [ ] **Step 5: Decode the archive's photos into actual bytes**

First the test — append to `tests/unit/readers/test_readers.py`:

```python
def test_paprika_photo_data_is_decoded_to_bytes(tmp_path):
    """photo_data is base64 text; image_bytes is typed bytes. Uploading the string
    unchanged would store base64 as a JPEG."""
    import base64

    jpeg = b"\xff\xd8\xff\xe0 not really a jpeg but it is bytes"
    archive = _write_paprika_archive(
        tmp_path,
        [{
            "name": "Photographed Thing",
            "ingredients": "1 cup x",
            "directions": "Cook.",
            "photo": "pg_1.jpg",
            "photo_data": base64.b64encode(jpeg).decode("ascii"),
        }],
    )

    chunks = PaprikaReader().read(str(archive))

    assert chunks[0].image_bytes == jpeg
    assert chunks[0].image_content_type == "image/jpeg"


def test_unreadable_photo_data_is_dropped_not_raised(tmp_path):
    archive = _write_paprika_archive(
        tmp_path,
        [{"name": "Bad Photo", "ingredients": "x", "directions": "y", "photo_data": "!!!not base64!!!"}],
    )

    chunks = PaprikaReader().read(str(archive))

    assert chunks[0].image_bytes is None
```

Run it and watch it fail, then implement. In `recipeparser/core/models.py`, add one more field to `Chunk` beside `image_bytes`:

```python
    image_content_type: str = "image/jpeg"
```

documented as "MIME type of ``image_bytes``, taken from the source's own filename where it gives one."

In `recipeparser/io/readers/paprika.py`, add near the top:

```python
import base64

_PHOTO_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def _decode_photo(entry: Dict[str, Any]) -> tuple[Optional[bytes], str]:
    """Decode a Paprika entry's embedded photo.

    ``photo_data`` is base64 text, not bytes — assigning it straight to
    Chunk.image_bytes (typed bytes) is how 466 photographs became unusable. A
    photo that will not decode is dropped: a recipe without a picture, never a
    failed import.
    """
    raw = entry.get("photo_data")
    if not raw:
        return None, "image/jpeg"
    ext = str(entry.get("photo") or "").rsplit(".", 1)[-1].lower()
    content_type = _PHOTO_TYPES.get(ext, "image/jpeg")
    if isinstance(raw, bytes):
        return raw, content_type
    try:
        return base64.b64decode(raw, validate=True), content_type
    except Exception:
        log.warning("PaprikaReader: could not decode photo_data for %r — continuing without it.", entry.get("name"))
        return None, content_type
```

Then in both `Chunk(...)` constructions replace `image_bytes=entry.get("photo_data")` with the decoded pair:

```python
                photo_bytes, photo_type = _decode_photo(entry)
                ...
                        image_bytes=photo_bytes,
                        image_content_type=photo_type,
```

- [ ] **Step 6: Use it from the pipeline**

In `RecipePipeline.__init__`, add the parameter last, before the closing paren:

```python
        image_store: Optional[ImageStore] = None,
```

document it in the `Args:` block as

```
            image_store:        Optional ImageStore for chunks that carry raw
                                image bytes.  Without one those bytes are
                                dropped, which is what shipped before.
```

and assign `self._image_store = image_store`.

In `_process_chunk`, at the very top of the method (before the stage fast-paths), add:

```python
        # A reader may have pulled a photograph out of the archive; store it before
        # ASSEMBLE so the assembled recipe carries a URL rather than raw bytes.
        if chunk.image_bytes and not chunk.image_url and self._image_store is not None:
            chunk.image_url = self._image_store.put(
                chunk.image_bytes, str(uuid.uuid4()), chunk.image_content_type
            )
```

Import `uuid` at the top of `pipeline.py` if it is not already imported. The three existing `image_url=chunk.image_url ...` call sites (`:241`, `:264`, `:316`) then need no change — they read the field this sets.

- [ ] **Step 7: Test the store itself**

Append to `tests/unit/writers/test_writers.py`:

```python
def test_image_store_returns_none_without_credentials(monkeypatch):
    """No credentials is a recipe without a picture, never a raised exception."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    assert SupabaseImageStore().put(b"bytes", "some-id") is None


def test_image_store_returns_none_for_empty_bytes():
    assert SupabaseImageStore(url="https://x.test", service_key="k").put(b"", "some-id") is None
```

with `from recipeparser.io.writers.image_store import SupabaseImageStore` added to the imports.

- [ ] **Step 8: Leave one upload path, not two**

Spec 4.5 requires the orphaned helper to become a wrapper over the same store, so the codebase does not keep one live upload path and one dead one. Replace the body of `_upload_image_to_storage` (`api.py:294-330`) with:

```python
async def _upload_image_to_storage(image_url: str, recipe_id: str) -> Optional[str]:
    """Download *image_url* and store it, returning the public URL or None.

    A thin wrapper over the same ImageStore the pipeline uses: this path exists
    for URL submissions, which arrive with an address rather than bytes. Failures
    are logged but never raised — a recipe without a picture, not a failed job.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.get(image_url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            data = response.content
    except Exception:
        logger.exception("Could not fetch %s for recipe %s — continuing without an image.", image_url, recipe_id)
        return None
    return await asyncio.to_thread(SupabaseImageStore().put, data, recipe_id, content_type)
```

Add `from recipeparser.io.writers.image_store import SupabaseImageStore` to `api.py`'s imports if Task 6 has not already added it. The bucket constants and the extension map that were inline here now live in `image_store.py` and can be deleted from `api.py`.

- [ ] **Step 9: Run the tests**

Run: `pytest tests/unit/test_pipeline.py tests/unit/writers tests/unit/readers -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add recipeparser/core/ports.py recipeparser/core/models.py recipeparser/core/pipeline.py recipeparser/io/readers/paprika.py recipeparser/io/writers/image_store.py recipeparser/adapters/api.py tests/unit/test_pipeline.py tests/unit/writers/test_writers.py tests/unit/readers/test_readers.py
git commit -m "feat(pipeline): store the photographs the Paprika reader already extracts

The reader pulls each entry's embedded photo out of the archive and attaches
the bytes to the chunk. Nothing read that field. The only upload helper takes
a web address and is called from one place Paprika entries never reach, so of
827 entries in the 2026-09-04 import, 466 carried a photograph and none
arrived - while the bucket sat there, public, holding images the retired
mobile client had uploaded.

core may not import io, so the capability enters as an ImageStore port beside
CategorySource, implemented against Supabase Storage in io/writers. The
pipeline uploads before ASSEMBLE. put() returns None rather than raising: a
photograph that will not store is a recipe without a picture, not a lost one.

The reader also has to decode. photo_data is base64 text while image_bytes is
typed bytes, so even a working upload path would have stored base64 as a JPEG.
The archive carries no loose image files, so this field is the whole photo
path, and its filename is where the content type comes from.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: One place that knows what a job has done

**Files:**
- Create: `recipeparser/adapters/job_sink.py`
- Test: `tests/unit/test_job_sink.py` (create)

**Interfaces:**
- Consumes: `Chunk.label` (Task 2); the callback signatures from Task 3.
- Produces: `recipeparser.adapters.job_sink.JobSink`, with:
  - `JobSink(job_id: str, user_id: str, category_ids: dict[str, str], write=write_recipe_to_supabase, now=...)` — the injected `write` is called as `write(recipe, user_id, category_ids=...)`. Note the real function's signature is `write_recipe_to_supabase(recipe, user_id, recipe_id=None, category_ids=None)`, so `category_ids` **must** be passed by keyword; three positional arguments would file the category map as the row id.
  - `.on_result(recipe: IngestResponse) -> None`
  - `.on_skip(chunk: Chunk, reason: str) -> None`
  - `.on_progress(stage: str, completed: int, total: int) -> None`
  - `.recipe_count: int`, `.skipped_count: int`, `.skipped: list[dict]`
  - `.progress_updates: list[int]` — the percentages it decided to emit, for tests
  - `.finalize_payload(success: bool, error_message: Optional[str]) -> dict[str, Any]`
- Task 6 wires this into both endpoints.

**Why a separate module.** Both `_run()` closures in `api.py` need identical bookkeeping, `api.py` is already over 800 lines, and this logic is worth testing without standing up FastAPI. Constants: `SKIPPED_LIST_CAP = 50`.

**Behaviour it must have** (spec 5.1): the skipped list is capped at 50 entries while `skipped_count` stays truthful; progress is emitted only when the whole-number percentage changes, so an 827-chunk import writes at most 100 updates rather than 827; and `finalize_payload` omits `progress_pct` entirely on failure, rather than writing 0 over a job that died at 60%.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_job_sink.py`:

```python
"""JobSink bookkeeping (spec 4.2, 4.3, 4.4, 5.1). No network, no FastAPI."""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from recipeparser.adapters.job_sink import SKIPPED_LIST_CAP, JobSink
from recipeparser.core.models import Chunk, InputType


def _sink(**kwargs: Any) -> JobSink:
    written: List[Any] = []
    sink = JobSink(
        job_id="job-1",
        user_id="user-1",
        category_ids={},
        write=lambda recipe, user_id, category_ids: written.append(recipe),
        **kwargs,
    )
    sink.written = written  # type: ignore[attr-defined]
    return sink


class _Recipe:
    def __init__(self, title: str = "R") -> None:
        self.title = title


def test_each_result_is_written_immediately_and_counted():
    sink = _sink()

    sink.on_result(_Recipe("A"))
    sink.on_result(_Recipe("B"))

    assert [r.title for r in sink.written] == ["A", "B"]
    assert sink.recipe_count == 2
    assert sink.skipped_count == 0


def test_a_failed_write_is_counted_as_a_skip_not_raised():
    def _boom(recipe, user_id, category_ids):
        raise RuntimeError("insert rejected")

    sink = JobSink(job_id="j", user_id="u", category_ids={}, write=_boom)

    sink.on_result(_Recipe("A"))

    assert sink.recipe_count == 0
    assert sink.skipped_count == 1
    assert "insert rejected" in sink.skipped[0]["reason"]


def test_a_skipped_chunk_is_named_by_its_label():
    sink = _sink()

    sink.on_skip(Chunk(text="t", input_type=InputType.URL, label="Sticky Toffee Pudding"), "MAX_TOKENS")

    assert sink.skipped == [{"label": "Sticky Toffee Pudding", "index": 0, "reason": "MAX_TOKENS"}]
    assert sink.skipped_count == 1


def test_a_chunk_with_no_label_is_reported_by_index():
    sink = _sink()

    sink.on_skip(Chunk(text="t", input_type=InputType.PDF), "boom")

    assert sink.skipped[0]["label"] is None
    assert sink.skipped[0]["index"] == 0


def test_the_list_is_capped_but_the_count_is_not():
    sink = _sink()

    for _ in range(SKIPPED_LIST_CAP + 10):
        sink.on_skip(Chunk(text="t", input_type=InputType.URL), "boom")

    assert len(sink.skipped) == SKIPPED_LIST_CAP
    assert sink.skipped_count == SKIPPED_LIST_CAP + 10


def test_progress_is_emitted_once_per_whole_percent():
    sink = _sink()

    for completed in range(1, 828):
        sink.on_progress("PROCESSING", completed, 827)

    assert sink.progress_updates == sorted(set(sink.progress_updates))
    assert len(sink.progress_updates) <= 100
    assert sink.progress_updates[-1] == 100


def test_finalize_omits_progress_on_failure():
    """A job that died at 60% must not claim it never started."""
    sink = _sink()

    payload = sink.finalize_payload(success=False, error_message="reader exploded")

    assert "progress_pct" not in payload
    assert payload["status"] == "error"
    assert payload["error_message"] == "reader exploded"


def test_finalize_reports_a_hundred_and_the_counts_on_success():
    sink = _sink()
    sink.on_result(_Recipe("A"))
    sink.on_skip(Chunk(text="t", input_type=InputType.URL, label="Lost"), "MAX_TOKENS")

    payload = sink.finalize_payload(success=True, error_message=None)

    assert payload["progress_pct"] == 100
    assert payload["recipe_count"] == 1
    assert payload["skipped_count"] == 1
    assert payload["skipped"] == [{"label": "Lost", "index": 0, "reason": "MAX_TOKENS"}]
    assert payload["status"] == "done"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/test_job_sink.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'recipeparser.adapters.job_sink'`.

- [ ] **Step 3: Write the module**

Create `recipeparser/adapters/job_sink.py`:

```python
"""Per-job bookkeeping shared by both ingestion endpoints.

Both /jobs and /jobs/file need the same three callbacks and the same terminal
payload, and api.py is long enough already. Keeping it here also means the
counting rules can be tested without standing up FastAPI or Supabase.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any, Callable, Dict, List, Optional

from recipeparser.core.models import Chunk
from recipeparser.io.writers.supabase import write_recipe_to_supabase
from recipeparser.models import IngestResponse

log = logging.getLogger(__name__)

# The list rides a row that re-syncs on every stage change; the count stays
# truthful, so a pathological job cannot inflate what every client downloads.
SKIPPED_LIST_CAP = 50

# Keyword, not positional: write_recipe_to_supabase's third parameter is
# recipe_id, and category_ids is fourth. Passing three positionally would file
# every recipe's category map as its row id.
WriteFn = Callable[..., Any]


def _utc_now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


class JobSink:
    """Collects what one ingestion job did, and writes each recipe as it arrives."""

    def __init__(
        self,
        job_id: str,
        user_id: str,
        category_ids: Dict[str, str],
        write: WriteFn = write_recipe_to_supabase,
        now: Callable[[], str] = _utc_now,
    ) -> None:
        self._job_id = job_id
        self._user_id = user_id
        self._category_ids = category_ids
        self._write = write
        self._now = now
        self.recipe_count = 0
        self.skipped_count = 0
        self.skipped: List[Dict[str, Any]] = []
        self.progress_updates: List[int] = []
        self._last_pct = -1
        self._chunk_index = 0

    # ── callbacks handed to RecipePipeline.run ────────────────────────────────

    def on_result(self, recipe: IngestResponse) -> None:
        """Persist one finished recipe. A failure here costs that recipe, not the job."""
        try:
            self._write(recipe, self._user_id, category_ids=self._category_ids)
        except Exception as exc:
            log.exception("Job %s: failed to write recipe %r — counting it as skipped.", self._job_id, getattr(recipe, "title", "?"))
            self._record_skip(getattr(recipe, "title", None), f"write failed: {exc}")
            return
        self.recipe_count += 1

    def on_skip(self, chunk: Chunk, reason: str) -> None:
        """Record a chunk that produced nothing because something failed."""
        self._record_skip(chunk.label, reason)

    def on_progress(self, stage: str, completed: int, total: int) -> None:
        """Note a whole-percent change. Sub-percent ticks are dropped, not written."""
        if total <= 0:
            return
        pct = round(100 * completed / total)
        if pct == self._last_pct:
            return
        self._last_pct = pct
        self.progress_updates.append(pct)

    # ── terminal state ────────────────────────────────────────────────────────

    def finalize_payload(self, success: bool, error_message: Optional[str] = None) -> Dict[str, Any]:
        """The ingestion_jobs UPDATE for a finished job.

        progress_pct is present only on success. Writing 0 on failure told the
        client a job that died at 60% had never started.
        """
        payload: Dict[str, Any] = {
            "status": "done" if success else "error",
            "stage": "DONE" if success else "ERROR",
            "recipe_count": self.recipe_count,
            "skipped_count": self.skipped_count,
            "skipped": self.skipped,
            "updated_at": self._now(),
        }
        if success:
            payload["progress_pct"] = 100
        if error_message:
            payload["error_message"] = error_message
        return payload

    # ── internals ─────────────────────────────────────────────────────────────

    def _record_skip(self, label: Optional[str], reason: str) -> None:
        index = self._chunk_index
        self._chunk_index += 1
        self.skipped_count += 1
        if len(self.skipped) < SKIPPED_LIST_CAP:
            self.skipped.append({"label": label, "index": index, "reason": reason})
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_job_sink.py -v`
Expected: PASS, all eight.

- [ ] **Step 5: Commit**

```bash
git add recipeparser/adapters/job_sink.py tests/unit/test_job_sink.py
git commit -m "feat(api): one place that knows what a job has done

Both ingestion endpoints need the same bookkeeping - write each recipe as it
lands, count what was dropped and name it, decide when progress is worth
writing, and build the terminal row. api.py is past 800 lines and none of that
is testable through it without FastAPI and Supabase, so it lives in its own
module.

The rules that matter: a write that fails costs that recipe and is counted,
not raised; the skipped list is capped at fifty while the count stays true,
because the list rides a row every client re-downloads on each stage change;
progress is emitted once per whole percent, turning 827 updates into at most a
hundred; and the terminal payload omits progress_pct on failure rather than
writing 0 over a job that died at 60%.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Wire the sink into both endpoints

**Files:**
- Modify: `recipeparser/adapters/api.py:412-483` (`_create_ingestion_job`, `_finalize_ingestion_job`)
- Modify: `recipeparser/adapters/api.py:574-662` (`submit_job`) and `:664-760` (`submit_file_job`)
- Test: `tests/unit/test_ingestion_endpoints.py` (create)

**Interfaces:**
- Consumes: `JobSink` (Task 5); `RecipePipeline.run(..., on_result=, on_skip=)` (Task 3); `SupabaseImageStore` (Task 4).
- Produces: `_finalize_ingestion_job(job_id: str, payload: dict[str, Any]) -> None` — signature **changed**; it now takes the payload the sink built rather than assembling one from four arguments.

**Why.** `submit_file_job` runs the whole pipeline and then writes once (`api.py:738-739`): a long import shows an empty library for hours and a restart loses the run. Both endpoints also pass `None` where `on_progress` belongs (`:644`, `:738`), which is why the client's progress bar has never moved.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ingestion_endpoints.py`:

```python
"""Both endpoints must hand the pipeline a sink (spec 4.3, 4.4). No live writes."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import recipeparser.adapters.api as api


def test_finalize_takes_a_payload(monkeypatch):
    """The sink builds the row; finalize only sends it."""
    sent = {}

    class _Table:
        def update(self, payload):
            sent.update(payload)
            return self

        def eq(self, *_a):
            return self

        def execute(self):
            return None

    client = MagicMock()
    client.table.return_value = _Table()
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: client)
    monkeypatch.setattr(api, "_live_writes_blocked", lambda: False)

    api._finalize_ingestion_job("job-1", {"status": "done", "recipe_count": 3, "skipped_count": 0})

    assert sent["status"] == "done"
    assert sent["recipe_count"] == 3


def test_the_pipeline_is_given_result_and_skip_callbacks():
    """Regression: passing None here is why nothing was written until the end."""
    import inspect

    source = inspect.getsource(api.submit_file_job)
    assert "on_result=" in source
    assert "on_skip=" in source
    assert "on_progress=" in source
    assert "writer.write(results)" not in source
```

The second test is a source assertion rather than a behavioural one on purpose: exercising the endpoint's background task end to end needs a live pipeline, and the behaviour it guards — that the callbacks are wired at all — is exactly what regressed. The behaviour behind the callbacks is covered by Tasks 3 and 5.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/test_ingestion_endpoints.py -v`
Expected: FAIL — `_finalize_ingestion_job` takes the old arguments; the source assertions fail.

- [ ] **Step 3: Change the finalizer**

Replace `_finalize_ingestion_job` (`api.py:449-483`) with:

```python
def _finalize_ingestion_job(job_id: str, payload: dict[str, Any]) -> None:
    """UPDATE ingestion_jobs to the terminal state the JobSink built.

    The payload comes from JobSink.finalize_payload, which is where the rules
    about counts and progress live.  Failures are logged but NOT re-raised.
    """
    if _live_writes_blocked():
        logger.warning("Test run: skipping the finalize for job %s.", job_id)
        return
    sb = _get_supabase_service_client()
    if sb is None:
        logger.warning("Job %s: Supabase credentials not set — skipping ingestion_jobs finalize.", job_id)
        return
    try:
        sb.table("ingestion_jobs").update(payload).eq("id", job_id).execute()
        logger.info(
            "Job %s: finalized — status=%s, recipes=%s, skipped=%s.",
            job_id, payload.get("status"), payload.get("recipe_count"), payload.get("skipped_count"),
        )
    except Exception:
        logger.exception("Job %s: failed to finalize ingestion_jobs row.", job_id)
```

In `_create_ingestion_job` (`api.py:433-443`), add the two new columns to the INSERT so a freshly created row is complete:

```python
            "skipped_count": 0,
            "skipped": [],
```

- [ ] **Step 4: Add a progress writer**

Add beside `_make_stage_callback`:

```python
def _make_progress_writer(job_id: str) -> Callable[[int], None]:
    """Write one whole-percent progress update to the job row.

    Unlike the stage callback this does not re-raise: a missed percentage is a
    bar that lags, not a job whose state is unknowable.
    """
    def _write(pct: int) -> None:
        if _live_writes_blocked():
            return
        sb = _get_supabase_service_client()
        if sb is None:
            return
        try:
            import datetime  # noqa: PLC0415

            sb.table("ingestion_jobs").update({
                "progress_pct": pct,
                "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
            }).eq("id", job_id).execute()
        except Exception:
            logger.exception("Job %s: failed to write progress %d%%.", job_id, pct)

    return _write
```

- [ ] **Step 5: Rewrite the two run bodies**

In **both** `submit_job._run()` and `submit_file_job._run()`, replace the block that begins `# Wire category source + writer` and ends at the `_finalize_ingestion_job(...)` call with:

```python
            category_source = SupabaseCategorySource()
            category_ids = category_source.load_category_ids(user_id)
            sink = JobSink(job_id=job_id, user_id=user_id, category_ids=category_ids)
            write_progress = _make_progress_writer(job_id)

            def _on_progress(stage: str, completed: int, total: int) -> None:
                before = len(sink.progress_updates)
                sink.on_progress(stage, completed, total)
                if len(sink.progress_updates) > before:
                    write_progress(sink.progress_updates[-1])

            pipeline = RecipePipeline(
                client=client,
                controller=controller,
                category_source=category_source,
                uom_system=<uom>,
                measure_preference=<measure>,
                image_store=SupabaseImageStore(),
            )
            await asyncio.to_thread(
                lambda: pipeline.run(
                    <chunks>,
                    _on_progress,
                    user_id,
                    on_result=sink.on_result,
                    on_skip=sink.on_skip,
                )
            )
            logger.info(
                "Job %s completed — %d recipe(s), %d skipped.",
                job_id, sink.recipe_count, sink.skipped_count,
            )
            await asyncio.to_thread(_finalize_ingestion_job, job_id, sink.finalize_payload(True))
```

substituting per endpoint: `submit_job` uses `body.uom_system`, `body.measure_preference` and `[chunk]`; `submit_file_job` uses the `uom_system` and `measure_preference` parameters and `chunks`.

In both `except Exception as exc:` handlers, replace the finalize call with:

```python
            await asyncio.to_thread(
                _finalize_ingestion_job, job_id, sink.finalize_payload(False, str(exc))
            )
```

and hoist `sink = None` before the `try:` with a guard in the handler, because the reader can raise before the sink exists:

```python
        except Exception as exc:
            logger.error("Job %s failed: %s", job_id, exc, exc_info=True)
            controller.transition("error")
            payload = (
                sink.finalize_payload(False, str(exc))
                if sink is not None
                else {"status": "error", "stage": "ERROR", "error_message": str(exc)}
            )
            await asyncio.to_thread(_finalize_ingestion_job, job_id, payload)
```

Delete the now-unused `writer = SupabaseWriter(...)` lines and the `writer.write(results)` calls in both endpoints. Add the imports:

```python
from recipeparser.adapters.job_sink import JobSink
from recipeparser.io.writers.image_store import SupabaseImageStore
```

and remove `from recipeparser.io.writers.supabase import SupabaseWriter` if nothing else in the file uses it.

- [ ] **Step 6: Run the tests**

Run: `pytest tests -q`
Expected: PASS. Any existing test calling `_finalize_ingestion_job` with the old four arguments must be updated to pass a payload.

- [ ] **Step 7: Commit**

```bash
git add recipeparser/adapters/api.py tests/unit/test_ingestion_endpoints.py
git commit -m "fix(api): write recipes as they finish, and report real progress

Both endpoints ran the entire pipeline and then wrote once, so an 827-recipe
import showed an empty library for hours and a restart in that window lost the
whole run. Both also passed None where the progress callback belongs, which is
why the client's bar has only ever shown 0 or 100 while the parser knew
exactly how many chunks it had finished.

Each endpoint now hands the pipeline a JobSink: recipes are written one at a
time as they assemble, drops are counted and named, and progress reaches the
job row once per whole percent. Photographs travel through a SupabaseImageStore
handed to the pipeline. The finalizer takes the payload the sink built rather
than assembling one from four positional arguments, which is what let the old
version write progress_pct 0 over a job that died at 60%.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Authenticate the job endpoints

**Files:**
- Modify: `recipeparser/adapters/api.py:532` (registry), `:594`, `:690` (writers), `:765-825` (the four endpoints)
- Test: `tests/unit/test_job_endpoint_auth.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_active_jobs: Dict[str, tuple[str, PipelineController]]` — the registry now keys job id to `(user_id, controller)`. Any code reading it must unpack.

**Why 404 and not 403.** A 403 confirms the job exists, which turns the endpoint into an oracle for valid job ids. A caller who does not own a job should not be able to tell it apart from one that never existed.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_job_endpoint_auth.py`:

```python
"""The four job endpoints must verify the token and the owner (spec 4.6)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import recipeparser.adapters.api as api
from recipeparser.core.fsm import PipelineController


OWNER = "11111111-1111-1111-1111-111111111111"
STRANGER = "22222222-2222-2222-2222-222222222222"

PATHS = [
    ("get", "/jobs/job-1"),
    ("post", "/jobs/job-1/pause"),
    ("post", "/jobs/job-1/resume"),
    ("post", "/jobs/job-1/cancel"),
]


@pytest.fixture
def client(monkeypatch):
    api._active_jobs.clear()
    api._active_jobs["job-1"] = (OWNER, PipelineController())
    yield TestClient(api.app)
    api._active_jobs.clear()


def _as(user_id: str):
    return lambda: {"sub": user_id}


@pytest.mark.parametrize("method,path", PATHS)
def test_no_token_is_rejected(client, method, path, monkeypatch):
    monkeypatch.setattr(api, "_DISABLE_AUTH", False)
    api.app.dependency_overrides.clear()

    response = getattr(client, method)(path)

    assert response.status_code == 401


@pytest.mark.parametrize("method,path", PATHS)
def test_a_stranger_gets_404_not_403(client, method, path):
    """403 would confirm the job exists, turning these into an id oracle."""
    api.app.dependency_overrides[api._verify_supabase_jwt] = _as(STRANGER)
    try:
        response = getattr(client, method)(path)
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 404


@pytest.mark.parametrize("method,path", PATHS)
def test_the_owner_is_served(client, method, path):
    api.app.dependency_overrides[api._verify_supabase_jwt] = _as(OWNER)
    try:
        response = getattr(client, method)(path)
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 200
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/test_job_endpoint_auth.py -v`
Expected: FAIL — the endpoints answer 200 without a token, and the registry holds a bare controller so the fixture's tuple breaks the handlers.

- [ ] **Step 3: Change the registry**

At `api.py:530-532`:

```python
# Process-level registry: job_id → (user_id, PipelineController)
# The user id is kept so the control endpoints can refuse a job the caller does
# not own; without it any caller who reached the API could cancel any import.
_active_jobs: Dict[str, tuple[str, PipelineController]] = {}
```

At `api.py:594` and `:690`, change `_active_jobs[job_id] = controller` to `_active_jobs[job_id] = (user_id, controller)`.

- [ ] **Step 4: Add a lookup helper and use it**

Above the endpoints:

```python
def _owned_controller(job_id: str, user: dict[str, Any]) -> PipelineController:
    """Return the caller's own job, or 404.

    Not 403 for someone else's job: that would confirm the id exists and make
    the registry enumerable.
    """
    entry = _active_jobs.get(job_id)
    if entry is None or entry[0] != user.get("sub", ""):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )
    return entry[1]
```

Rewrite the four endpoints to use it:

```python
@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(
    job_id: str,
    user: dict[str, Any] = Depends(_verify_supabase_jwt),
) -> JobStatusResponse:
    """Return the current FSM status of the caller's running job."""
    controller = _owned_controller(job_id, user)
    return JobStatusResponse(job_id=job_id, status=controller.status.value)


@app.post("/jobs/{job_id}/pause", status_code=200)
def pause_job(job_id: str, user: dict[str, Any] = Depends(_verify_supabase_jwt)) -> dict[str, str]:
    """Request a pause on the caller's running job."""
    controller = _owned_controller(job_id, user)
    controller.request_pause()
    return {"job_id": job_id, "status": controller.status.value}
```

and the same shape for `resume_job` (`controller.request_resume()`) and `cancel_job` (`controller.request_cancel()`) — keep whatever method each currently calls.

- [ ] **Step 5: Run the tests**

Run: `pytest tests/unit/test_job_endpoint_auth.py -v`
Expected: PASS, all twelve parametrised cases.

- [ ] **Step 6: Run the whole suite**

Run: `pytest tests -q`
Expected: PASS. Update any test that put a bare controller into `_active_jobs`.

- [ ] **Step 7: Commit**

```bash
git add recipeparser/adapters/api.py tests/unit/test_job_endpoint_auth.py
git commit -m "fix(api): require a token and ownership on the four job endpoints

The three ingestion endpoints verify a bearer token. Reading a job's status and
pausing, resuming or cancelling it took a job id and nothing else, so anyone
who could reach the API could cancel someone else's import - confirmed by an
unauthenticated request returning 404 for a missing job rather than 401.

All four now depend on the same verifier, and the registry keys job id to
(user_id, controller) so a caller who does not own the job is refused. The
refusal is 404, not 403: 403 confirms the id exists and makes the registry
enumerable.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: One name for the service key

**Files:**
- Modify: `recipeparser/io/category_sources/supabase_source.py:25-30`, `:54`, `:63`
- Modify: `cleanup_jobs.py:7-10`
- Modify: `recipeparser/adapters/api.py` (startup check)
- Modify: `.env` (local, untracked — change the variable name, keep the value)
- Test: `tests/unit/test_service_key_name.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: no new symbols. After this task `SUPABASE_SERVICE_KEY` is read nowhere.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_service_key_name.py`:

```python
"""One service-key variable name, and a loud failure for the old one (spec 4.7)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from recipeparser.adapters.api import check_service_key_name
from recipeparser.io.category_sources.supabase_source import SupabaseCategorySource

ROOT = Path(__file__).resolve().parents[2]


def test_no_module_reads_the_legacy_name():
    hits = subprocess.run(
        [sys.executable, "-c",
         "import pathlib,sys;"
         "p=pathlib.Path(sys.argv[1]);"
         "print('\\n'.join(str(f) for f in p.rglob('*.py')"
         " if 'SUPABASE_SERVICE_KEY' in f.read_text(encoding='utf-8')))",
         str(ROOT)],
        capture_output=True, text=True,
    ).stdout.strip()

    assert hits == "", f"legacy SUPABASE_SERVICE_KEY still read in:\n{hits}"


def test_category_source_reads_the_canonical_name(monkeypatch):
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "canonical-value")

    assert SupabaseCategorySource()._key == "canonical-value"


def test_only_the_legacy_name_set_is_a_loud_failure(monkeypatch):
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "legacy-value")

    with pytest.raises(RuntimeError, match="SUPABASE_SERVICE_ROLE_KEY"):
        check_service_key_name()


def test_neither_set_is_not_an_error(monkeypatch):
    """A machine with no Supabase config at all is a valid dev setup; only the
    half-configured case is fatal, because that is the one that half works."""
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)

    check_service_key_name()
```

Note the first test excludes itself by construction only if it lives outside the scanned tree — it does not, so write the literal in that test as `"SUPABASE_SERVICE" + "_KEY"` to keep the scan honest.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/test_service_key_name.py -v`
Expected: FAIL — `ImportError: cannot import name 'check_service_key_name'`, and the scan finds two modules.

- [ ] **Step 3: Rename the reads**

`recipeparser/io/category_sources/supabase_source.py`: line 63 becomes

```python
        self._key = service_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
```

and the docstring at `:25-30` and the `Args:` note at `:54` change to name `SUPABASE_SERVICE_ROLE_KEY`.

`cleanup_jobs.py:7-10`:

```python
service_key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
...
    print("Error: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not found in environment.")
```

- [ ] **Step 4: Add the startup check**

In `recipeparser/adapters/api.py`, after `_resolve_auth_mode` (around `:155`):

```python
def check_service_key_name() -> None:
    """Refuse to boot half-configured.

    The service key was read under two names in different modules, so setting
    only one disabled a subset of writes with a warning rather than an error —
    the worst failure mode available, because most of the app kept working.
    """
    if os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        return
    if os.environ.get("SUPABASE_SERVICE_KEY"):
        raise RuntimeError(
            "SUPABASE_SERVICE_KEY is set but SUPABASE_SERVICE_ROLE_KEY is not. "
            "The latter is now the only name read. Rename the variable."
        )
```

and call it once at import time, immediately after `app = FastAPI(...)` is constructed:

```python
check_service_key_name()
```

- [ ] **Step 5: Rename it in your local .env**

`.env` is untracked. Change the key's name there, keeping the value, or the server will now refuse to start. Check for other consumers first:

Run: `grep -rn "SUPABASE_SERVICE_KEY" --include="*.py" --include="*.ps1" --include="*.yml" --include="*.yaml" .`
Expected: no hits outside `.env` once Step 3 is done.

- [ ] **Step 6: Run the tests**

Run: `pytest tests -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add recipeparser/io/category_sources/supabase_source.py cleanup_jobs.py recipeparser/adapters/api.py tests/unit/test_service_key_name.py
git commit -m "fix: read the service key under one name, and refuse to boot half-configured

api.py read SUPABASE_SERVICE_ROLE_KEY in four places while the category source
and the cleanup script read SUPABASE_SERVICE_KEY. Both were set locally, so
nothing showed; setting only one would have disabled a subset of writes with a
warning rather than an error, which is the worst available failure mode
because most of the application keeps working.

SUPABASE_SERVICE_ROLE_KEY is now the only name read, and the app refuses to
start when only the old one is present. A machine with neither is still a
valid development setup and boots fine - it is the half-configured case that
is fatal.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: An unquantified ingredient is null

**Files:**
- Modify: `recipeparser/models.py:99`
- Modify: `recipeparser/gemini.py` (both extraction prompts, and the refinement prompt if it restates the rule)
- Test: `tests/unit/writers/test_writers.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: `StructuredIngredient.amount: Optional[float]`. The Supabase writer passes it through unchanged (`io/writers/supabase.py:187` dumps the model), so a null reaches the jsonb as JSON `null`.

**Why.** `amount: float = Field(description="Numeric quantity, e.g., 1.5. 0 if none.")` makes an absent quantity indistinguishable from a real one. The web client had to paper over it by refusing to print zeros (Cayenne `0231c4f`); that is a decision about display standing in for a missing representation.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/writers/test_writers.py`:

```python
def test_an_unquantified_ingredient_serialises_as_null():
    """Spec 4.8: 'to taste' must not be indistinguishable from a real zero."""
    ingredient = StructuredIngredient(
        id="ing_01",
        amount=None,
        unit=None,
        name="Kosher salt",
        fallback_string="Kosher salt",
        converted_amount=None,
        converted_unit=None,
        is_ai_converted=False,
    )

    dumped = ingredient.model_dump()

    assert dumped["amount"] is None
    assert json.dumps([dumped]).count("null") >= 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/writers/test_writers.py -k unquantified -v`
Expected: FAIL — pydantic `ValidationError`, `amount` is not optional.

- [ ] **Step 3: Make the field optional**

`recipeparser/models.py:99`:

```python
    amount: Optional[float] = Field(
        default=None,
        description="Numeric quantity, e.g. 1.5. null when the source states no amount ('salt to taste').",
    )
```

Confirm `Optional` is imported in that module.

- [ ] **Step 4: Tell the model**

In `recipeparser/gemini.py`, in the ingredient rules of both extraction prompts, replace any instruction implying a zero with:

```
- amount: the numeric quantity. Use null - never 0 - when the source states no
  amount ("salt to taste", "a pinch of nutmeg"). A zero would be read as a real
  measurement of nothing.
```

Search the file for other prompt text that mentions the amount and make it consistent; leaving one prompt saying "0 if none" would keep producing zeros from that path.

Run: `grep -n "0 if none" recipeparser/gemini.py`
Expected: no hits when you are done.

- [ ] **Step 5: Run the tests**

Run: `pytest tests -q`
Expected: PASS. Snapshot tests under `tests/snapshots/` may hold serialised ingredients; if a snapshot legitimately changes, re-record with `pytest --snapshot-update` and read the diff before accepting it.

- [ ] **Step 6: Commit**

```bash
git add recipeparser/models.py recipeparser/gemini.py tests/unit/writers/test_writers.py
git commit -m "fix(model): store no quantity as null rather than zero

The extraction schema had no way to say an ingredient came without an amount,
so 'salt to taste' was stored as a quantity of zero and no client could tell
that from a real measurement. The web client had to compensate by refusing to
print any zero - a display decision standing in for a missing representation.

amount is now Optional[float], the prompts say null and never 0, and the
writer passes it through to the jsonb unchanged.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Open the pull request

- [ ] **Step 1: Run the full suite one more time**

Run: `pytest tests -q`
Expected: PASS, no skips you did not expect.

- [ ] **Step 2: Lint and type-check**

Run: `ruff check recipeparser tests` then `mypy recipeparser`
Expected: clean, or no new findings against `baseline_mypy.txt`.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin <branch>
gh pr create --repo IanDBallard/RecipeParser --title "Ingestion fixes: retry, count, name, store, authenticate" --body "$(cat <<'BODY'
Closes the seven findings recorded in the Cayenne deferred ledger after the
2026-09-04 import of an 827-recipe Paprika export lost twenty recipes and all
466 photographs while reporting success.

- Retry a reply that will not parse; raise rather than returning None (4.1)
- Count and name every dropped chunk (4.2)
- Write recipes as they assemble instead of all at the end (4.3)
- Report real progress (4.4)
- Store the photographs the reader already extracts (4.5)
- Require a token and ownership on the four job endpoints (4.6)
- One service-key name, and refuse to boot half-configured (4.7)
- Store an unquantified ingredient as null, not zero (4.8)

Spec: `docs/superpowers/specs/2026-09-05-ingestion-repair-design.md` in IanDBallard/Cayenne.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```

- [ ] **Step 4: Link the issues**

Once the seven issues exist (they are filed by the third plan), edit the PR body to reference each by number so they close on merge.
