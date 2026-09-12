# Intake Changes Implementation Plan (RecipeParser, Stage D)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Merged:** PR #41 (https://github.com/IanDBallard/RecipeParser/pull/41), merged 2026-09-12 (`19d95f3`), onto `master`; post-merge run green on Python 3.11 and 3.12; container rebuilt from `master` the same day. No Cayenne twin: no migration, no sync-rule change.

**Status: executed and landed, by subagent-driven development with a task review per task, a whole-branch review, one fix wave and one scoped re-review. The checkboxes below were never ticked; that is bookkeeping, not an unfinished plan.** Do not read status from this file. The record is Stage 6, row D, of Cayenne's `SpecificationDocumentation/ROADMAP.md`; the decisions taken during execution are under *Rulings made during execution* at the end of this file.

**Goal:** The API reads a photo and a scanned PDF through the vision OCR it already owns, refuses what it cannot read in a sentence the client can show, files every job under the source key its recipes carry, and stops losing three things a page states — its total time, its description and its nutrition — and one thing a page's own `<meta>` tags carry, its hero image.

**Architecture:** One new reader, `ImageReader`, opens a photo with PyMuPDF (an image is a one-page document to it) and hands the document to `extract_text_via_vision`, exactly as the command-line PDF path does; `load_pdf` gains the same fallback behind a `client` parameter so the job path stops refusing a scan. The URL path fetches the page itself once, with a browser user-agent, and reads `og:image` / `twitter:image` and the meta description from its HTML before consulting the scraper's markdown; the markdown fallback refuses badges and logos. `RecipeExtraction` gains three `repr=False` fields — `total_time`, `description`, `nutritional_info` — that `assemble()` uses only where a Paprika entry states nothing. The job row's `source_hint` becomes the recipes' `source_key`: at read time from the chunks' citations (books, sites), and at finalize from the results (pasted text, photos).

**Tech Stack:** Python 3.9 syntax (the container runs 3.11), Pydantic v2, PyMuPDF (`fitz`), `httpx`, `google-genai` via `_call_with_retry`, pytest with `-n auto`, syrupy snapshots.

**Spec:** `docs/superpowers/specs/2026-09-11-recipe-source-citation-design.md` (*What the API writes, per medium*, and the `source_hint` paragraph under it); Cayenne's `SpecificationDocumentation/INGESTION_API.md` (*Provenance*, *Input media*); Cayenne's `docs/superpowers/plans/2026-09-11-add-recipe-workstream.md`, *Stage D*, including its three carry-forward bullets; Cayenne's `SpecificationDocumentation/ROADMAP.md`, Stage 6 row D.

## Global Constraints

- **Branch:** `feat/intake`, worktree `.claude/worktrees/feat+intake`, off `master` at `d2cba49` (v8.0.0). One pull request; no Cayenne twin (no migration, no sync-rule change: every column written here already exists). After the merge the container is rebuilt from `master` (`docker compose up -d --build`, `/health` ok).
- Python 3.9 syntax only: `Optional[X]`, `List[X]`, `Dict[K, V]`, `Tuple[...]` from `typing`; no `X | Y` in annotations, no `match`.
- `recipeparser/core/**` must not import from `recipeparser.io` or `recipeparser.adapters` (ruff TID rule). `ruff check recipeparser scripts tests` before every commit.
- Tests never reach a real Supabase project or the Gemini API: `recipeparser.config.live_writes_blocked()` refuses writes under pytest; the Gemini client is a `MagicMock` (see `make_mock_client` in `tests/conftest.py`) or the goldens' `GoldenClient`. **Never** create a `.env` in this worktree.
- **The extraction schema is Gemini-facing.** Adding a field to `RecipeExtraction` changes `response_json_schema` and both extract prompts, so `tests/goldens/test_prompts_snapshot.py` changes. Re-approve with `pytest tests/goldens tests/snapshots --snapshot-update -q -p no:xdist`, review the `.ambr` diff (only the three new fields and the three new prompt lines may appear), and stage `tests/goldens/__snapshots__` alongside the code. Recorded Gemini replies are keyed by the *body* of a prompt (the text after `Text chunk:` / `Text:` / `RAW RECIPE:`), which these changes leave untouched; a changed prompt scaffold warns (`prompt_sha256 mismatch`) and still serves. **Every new `RecipeExtraction` field is `repr=False`**, like `stated_source` and `byline`, so `str(raw_recipe)` — the refine prompt's body — does not move.
- Every prompt is a `build_*_prompt(...)` function, never an f-string at the call site.
- **Rulings made in this plan (flag in review):**
  1. The image reader's chunk is `InputType.IMAGE`, a new enum value that takes the default route through `_get_stages` (the full pipeline) and the book-mode extract prompt, since a photographed page may hold more than one recipe. It carries no citation; `resolve_citation` takes the model's `stated_source`, as for pasted text (design table, row *Photo*).
  2. The photo itself is never the recipe's `image_url`: a photograph of a page is not a photograph of the dish.
  3. A scanned PDF read through OCR extracts no images (its page images are the scans themselves) and yields one chunk for the whole document, because `extract_text_via_vision` returns one transcript.
  4. The page's meta description reaches `assemble()` as `SourceMeta(description=...)` on the URL chunk, so it wins over the model's extracted description the way a Paprika entry's does: it is the page's own statement, deterministic, and it is present when the scraper's markdown drops the headnote (NYT, 2026-09-12).
  5. `total_time` fills the **structured cook span only** when `cook_time` is absent: `duration_columns(prep_time, cook_time or total_time, ...)`. The `cook_time` text column stays what the page stated for cook (null), and `durations.py` itself does not change, so the Python parser stays byte-equivalent in rules to the Cayenne TypeScript twin and the shared fixture is untouched.
  6. `RecipeExtraction.notes` stays as it is: extracted today and dropped by `assemble()` (which takes only `meta.notes`). Out of scope here; recorded so it is a known gap.
  7. `source_hint` is written twice, and both are the key: once with `total_chunks` from the chunks' citations (books and sites know their key before extraction), once in the finalize payload from the written recipes' `source_key` (pasted text and photos only know it after). The finalize write is skipped when no recipe carried a key, so a hint set at read time is never blanked.
  8. The 422 sentences, shown verbatim by the client: `Cayenne can't read .docx files yet.` (the extension, lower-cased, with its dot); `Cayenne can't read this file yet.` when there is no extension; `Cayenne can't read HEIC photos yet. Share it as a JPEG instead.` for `.heic`/`.heif` or an `image/heic`/`image/heif` content type.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `recipeparser/adapters/api.py` | `_select_reader` (image, HEIC, the sentence); `_IMAGE_SUFFIX_BY_TYPE`; `_fetch_page_meta`; `_extract_image_url_from_markdown` refuses badges; the image branch of `submit_file_job`; `_PdfReader(client=...)`; `_update_total_chunks(..., source_hint)`; `_source_key_of(chunks)` | Modify |
| `recipeparser/io/readers/image.py` | `ImageReader(client)`: one photo → one `InputType.IMAGE` chunk through vision OCR | **Create** |
| `recipeparser/io/readers/pdf.py` | `PdfReader(client=None)`, `load_pdf(path, output_dir, client=None)` with the OCR fallback; `_check_document` and `_text_density` split out of `_preflight` | Modify |
| `recipeparser/io/readers/url.py` | `PageMeta`, `page_meta_from_html(html)`, `looks_like_badge(url, alt)` — pure | Modify |
| `recipeparser/core/models.py` | `InputType.IMAGE` | Modify |
| `recipeparser/exceptions.py` | `ImageExtractionError` | Modify |
| `recipeparser/models.py` | `RecipeExtraction` gains `total_time`, `description`, `nutritional_info` (`repr=False`) | Modify |
| `recipeparser/gemini.py` | `build_plain_text_prompt` and `build_extract_prompt` name the three fields | Modify |
| `recipeparser/core/stages/assemble.py` | `total_time`, `description`, `nutritional_info` parameters; the fallback rule | Modify |
| `recipeparser/core/pipeline.py` | Passes the three at the full-pipeline call site | Modify |
| `recipeparser/adapters/job_sink.py` | Counts `source_key` per written recipe; `finalize_payload` carries `source_hint` | Modify |
| `tests/unit/test_select_reader.py` | The four tags and the three sentences | Modify |
| `tests/unit/readers/test_image_reader.py` | The reader over a PNG PyMuPDF renders, with a mock client | **Create** |
| `tests/unit/readers/test_pdf_ocr_fallback.py` | A blank-page PDF: refused without a client, read with one; a text PDF never calls the client | **Create** |
| `tests/unit/readers/test_page_meta.py` | `page_meta_from_html` and `looks_like_badge` | **Create** |
| `tests/test_api.py` | Badge refusal in `TestExtractImageUrl`; image 202 and HEIC/`.docx` 422 on `/jobs/file`; the URL path takes the page's meta image and description; `total_chunks` and `source_hint` land before the pipeline runs | Modify |
| `tests/unit/test_models.py` | The three fields in the schema and out of the repr | Modify |
| `tests/unit/stages/test_assemble.py` | The fallback rule for the three | Modify |
| `tests/unit/test_job_sink.py` | `source_hint` in the finalize payload | Modify |
| `tests/unit/test_ingestion_endpoints.py` | `_update_total_chunks` sends `source_hint`; `_source_key_of` | Modify |
| `tests/goldens/__snapshots__/` | Re-approved prompt and schema snapshots | Modify |
| `CHANGELOG.md` | An `[Unreleased]` section in the house style | Modify |

Test directory: `tests/unit/readers/` may not exist yet; create it with an empty `__init__.py` only if the other `tests/unit/*` subpackages carry one (check `tests/unit/stages/`); otherwise plain files.

---

### Task 1: `_select_reader` accepts photos and refuses the rest in a sentence

**Files:**
- Modify: `recipeparser/adapters/api.py:711-731`
- Test: `tests/unit/test_select_reader.py`

**Interfaces:**
- Produces: `_select_reader(filename: str, content_type: str) -> str` returning one of `"pdf"`, `"epub"`, `"paprika"`, `"image"`; raises `ValueError` whose message is the sentence the endpoint sends as the 422 `detail`. Module constants `_IMAGE_EXTENSIONS`, `_IMAGE_CONTENT_TYPES`, `_HEIC_EXTENSIONS`, `_HEIC_CONTENT_TYPES`, `_IMAGE_SUFFIX_BY_TYPE` (Task 2 uses the last).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_select_reader.py`, and change the one existing assertion that matches the old message:

```python
# In TestSelectReaderFallbackContentType.test_non_paprika_zip_not_misrouted, replace
#     with pytest.raises(ValueError, match="Unsupported file type"):
# with
#     with pytest.raises(ValueError, match=r"Cayenne can't read \.zip files yet\."):


class TestSelectReaderImages:
    """Stage D (INGESTION_API.md, *Input media* 1): photos route to the image reader."""

    @pytest.mark.parametrize(
        "filename,content_type",
        [
            ("IMG_4021.jpg", "image/jpeg"),
            ("page.jpeg", "image/jpeg"),
            ("page.png", "image/png"),
            ("page.webp", "image/webp"),
            ("PAGE.JPG", "application/octet-stream"),   # extension wins
            ("blob", "image/jpeg"),                      # no extension: content type decides
            ("blob", "image/jpg"),                       # the non-standard spelling some browsers send
        ],
    )
    def test_photo_routes_to_image(self, filename, content_type):
        assert _select_reader(filename, content_type) == "image"

    @pytest.mark.parametrize(
        "filename,content_type",
        [("IMG_1.heic", "image/heic"), ("IMG_1.HEIF", "application/octet-stream"), ("blob", "image/heif")],
    )
    def test_heic_is_refused_plainly(self, filename, content_type):
        with pytest.raises(ValueError) as excinfo:
            _select_reader(filename, content_type)
        assert str(excinfo.value) == "Cayenne can't read HEIC photos yet. Share it as a JPEG instead."


class TestSelectReaderSentence:
    """Input media 2: the refusal names the type in a sentence the client shows verbatim."""

    def test_names_the_extension(self):
        with pytest.raises(ValueError) as excinfo:
            _select_reader("menu.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        assert str(excinfo.value) == "Cayenne can't read .docx files yet."

    def test_lower_cases_the_extension(self):
        with pytest.raises(ValueError) as excinfo:
            _select_reader("MENU.DOCX", "application/octet-stream")
        assert str(excinfo.value) == "Cayenne can't read .docx files yet."

    def test_no_extension(self):
        with pytest.raises(ValueError) as excinfo:
            _select_reader("blob", "application/octet-stream")
        assert str(excinfo.value) == "Cayenne can't read this file yet."
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_select_reader.py -q -p no:xdist`
Expected: the new image cases fail with `ValueError: Unsupported file type…`, the sentence cases fail on the message, the `.zip` case fails on the new `match`.

- [ ] **Step 3: Implement**

Replace `_select_reader` in `recipeparser/adapters/api.py` (lines 711-731) with:

```python
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
_IMAGE_CONTENT_TYPES = ("image/jpeg", "image/jpg", "image/png", "image/webp")
_HEIC_EXTENSIONS = (".heic", ".heif")
_HEIC_CONTENT_TYPES = ("image/heic", "image/heif")
# When a photo arrives with no extension, the temp file the reader opens needs
# one PyMuPDF recognises; the content type is the only clue left.
_IMAGE_SUFFIX_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _select_reader(filename: str, content_type: str) -> str:
    """Return a reader tag from the filename extension (primary) or the content type.

    Returns one of: 'pdf', 'epub', 'paprika', 'image'.
    Raises ValueError for anything else; its message is the sentence the endpoint
    sends as the 422 ``detail`` and the client shows verbatim (INGESTION_API.md,
    *Input media* 2), so it names the type and nothing internal.
    """
    ext = Path(filename).suffix.lower()
    if ext == ".pdf" or content_type == "application/pdf":
        return "pdf"
    if ext == ".epub" or content_type == "application/epub+zip":
        return "epub"
    # .paprikarecipes files are ZIP archives; browsers/Node may send them as
    # application/zip or application/octet-stream — match by extension first,
    # then fall back to content-type + filename suffix check.
    if ext == ".paprikarecipes":
        return "paprika"
    if content_type in ("application/zip", "application/octet-stream") and filename.lower().endswith(".paprikarecipes"):
        return "paprika"
    if ext in _HEIC_EXTENSIONS or content_type in _HEIC_CONTENT_TYPES:
        # The iOS picker hands the browser a JPEG anyway; a HEIC only arrives
        # through a share or a desktop drop, and there is no decoder here yet.
        raise ValueError("Cayenne can't read HEIC photos yet. Share it as a JPEG instead.")
    if ext in _IMAGE_EXTENSIONS or content_type in _IMAGE_CONTENT_TYPES:
        return "image"
    if ext:
        raise ValueError(f"Cayenne can't read {ext} files yet.")
    raise ValueError("Cayenne can't read this file yet.")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_select_reader.py tests/test_api.py -q -p no:xdist -k "select_reader or unsupported"`
Expected: PASS. `tests/test_api.py::…::test_unsupported_type_returns_422` still passes (a `.txt` upload is still a 422).

- [ ] **Step 5: Lint and commit**

```bash
ruff check recipeparser tests
git add recipeparser/adapters/api.py tests/unit/test_select_reader.py
git commit -m "feat(api): _select_reader routes photos to an image reader and refuses the rest in a sentence the client can show"
```

---

### Task 2: `ImageReader` and `InputType.IMAGE`, wired into `/jobs/file`

**Files:**
- Create: `recipeparser/io/readers/image.py`
- Modify: `recipeparser/core/models.py:24-40` (the enum), `recipeparser/exceptions.py`, `recipeparser/adapters/api.py` (imports at 50-52; `submit_file_job` at 879-1008)
- Test: `tests/unit/readers/test_image_reader.py`, `tests/test_api.py` (the `/jobs/file` class around line 245)

**Interfaces:**
- Consumes: `_select_reader` returning `"image"` and `_IMAGE_SUFFIX_BY_TYPE` (Task 1); `recipeparser.gemini.extract_text_via_vision(doc, client) -> str` (exists, `gemini.py:515`); `RecipeReader.read(source: str) -> List[Chunk]` (`io/readers/__init__.py`).
- Produces: `ImageReader(client).read(path) -> List[Chunk]` — exactly one chunk, `input_type=InputType.IMAGE`, `source_url=None`, `citation=None`, `label=<the file's basename>`; raises `ImageExtractionError` when PyMuPDF cannot open the file, lets `extract_text_via_vision`'s `RuntimeError` propagate when the model reads nothing. `InputType.IMAGE = "IMAGE"`. The pipeline needs no change: `_get_stages` routes every non-Paprika type through the full pipeline, and `plain_text` is `False` for it.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/readers/test_image_reader.py`:

```python
"""ImageReader — one photo, one chunk, through the vision OCR the PDF path already owns."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import fitz  # PyMuPDF
import pytest

from recipeparser.core.models import InputType
from recipeparser.exceptions import ImageExtractionError
from recipeparser.io.readers.image import ImageReader


def _png(tmp_path: Path, name: str = "IMG_4021.png") -> str:
    """A small real PNG, rendered by PyMuPDF so no binary fixture is committed."""
    doc = fitz.open()
    page = doc.new_page(width=200, height=100)
    page.insert_text((20, 50), "Cake: 1 cup flour. Mix.")
    path = tmp_path / name
    path.write_bytes(page.get_pixmap().tobytes("png"))
    doc.close()
    return str(path)


def _client(text: str) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=text)
    return client


def test_a_photo_becomes_one_image_chunk_from_the_transcript(tmp_path):
    client = _client("Cake\n\n1 cup flour\n\nMix and bake.")
    chunks = ImageReader(client).read(_png(tmp_path))
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.text == "Cake\n\n1 cup flour\n\nMix and bake."
    assert chunk.input_type is InputType.IMAGE
    assert chunk.source_url is None
    assert chunk.citation is None            # ruling 1: the transcript says where it came from
    assert chunk.image_url is None and chunk.image_bytes is None   # ruling 2
    assert chunk.label == "IMG_4021.png"


def test_the_vision_call_carries_the_photo_as_png_bytes(tmp_path):
    client = _client("something")
    ImageReader(client).read(_png(tmp_path))
    client.models.generate_content.assert_called_once()
    contents = client.models.generate_content.call_args.kwargs["contents"]
    assert len(contents) == 2                # the image part, then the OCR prompt
    assert getattr(contents[0], "inline_data", None) is not None
    assert contents[0].inline_data.mime_type == "image/png"


def test_a_file_pymupdf_cannot_open_is_an_image_extraction_error(tmp_path):
    bad = tmp_path / "not-a-photo.png"
    bad.write_bytes(b"this is not an image")
    with pytest.raises(ImageExtractionError, match="not-a-photo.png"):
        ImageReader(_client("x")).read(str(bad))


def test_a_photo_the_model_cannot_read_raises_the_vision_error(tmp_path):
    with pytest.raises(RuntimeError, match="no text"):
        ImageReader(_client("")).read(_png(tmp_path))


def test_input_type_image_routes_through_the_full_pipeline():
    from recipeparser.core.models import Chunk
    from recipeparser.core.pipeline import RecipePipeline

    stages = RecipePipeline._get_stages(MagicMock(), Chunk(text="x", input_type=InputType.IMAGE))
    assert stages == ["EXTRACT", "REFINE", "CATEGORIZE", "EMBED", "ASSEMBLE"]
```

Note for the implementer: `extract_text_via_vision` builds `genai_types.Part.from_bytes(...)`, whose `inline_data.mime_type` is what the second test reads; if the installed `google-genai` exposes it under a different attribute, assert on `contents[0]` being a `genai_types.Part` with `mime_type == "image/png"` reachable through whatever attribute the real object carries — do not weaken the test to `len(contents) == 2` alone.

Add to `tests/test_api.py`, inside the `/jobs/file` test class (the one holding `test_pdf_returns_202`):

```python
    def test_photo_returns_202_through_the_image_reader(self, client: TestClient) -> None:
        mock_chunk = MagicMock()
        mock_chunk.text = "Cake\n1 cup flour\nMix."
        with _patch_pipeline_and_writer()[0], \
             patch("recipeparser.adapters.api._ImageReader") as mock_reader_cls:
            mock_reader_cls.return_value.read.return_value = [mock_chunk]
            resp = self._upload(client, "IMG_4021.jpg", b"\xff\xd8\xff\xe0", "image/jpeg")
        assert resp.status_code == 202
        mock_reader_cls.assert_called_once()          # built with the Gemini client
        assert mock_reader_cls.call_args.args or mock_reader_cls.call_args.kwargs

    def test_heic_is_a_422_with_the_sentence(self, client: TestClient) -> None:
        resp = self._upload(client, "IMG_1.heic", b"\x00\x00\x00\x18ftypheic", "image/heic")
        assert resp.status_code == 422
        assert resp.json()["detail"] == "Cayenne can't read HEIC photos yet. Share it as a JPEG instead."

    def test_docx_is_a_422_naming_the_extension(self, client: TestClient) -> None:
        resp = self._upload(client, "menu.docx", b"PK\x03\x04", "application/octet-stream")
        assert resp.status_code == 422
        assert resp.json()["detail"] == "Cayenne can't read .docx files yet."
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/readers/test_image_reader.py tests/test_api.py -q -p no:xdist -k "image or heic or docx"`
Expected: `ImportError` on `recipeparser.io.readers.image` / `InputType.IMAGE`; the API tests fail on `_ImageReader` not existing.

- [ ] **Step 3: Implement**

`recipeparser/core/models.py`, after `EPUB` in the enum:

```python
    IMAGE = "IMAGE"
    """One photograph of a recipe, transcribed by vision OCR; routed like a book chunk."""
```

`recipeparser/exceptions.py`, after `PdfExtractionError`:

```python
class ImageExtractionError(RecipeParserError):
    """Raised when a photo cannot be opened as an image."""
```

Create `recipeparser/io/readers/image.py`:

```python
"""A photograph of a recipe, read through the vision OCR the scanned-PDF path owns."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List

import fitz  # type: ignore[import-untyped]  # PyMuPDF

from recipeparser.core.models import Chunk, InputType
from recipeparser.exceptions import ImageExtractionError
from recipeparser.io.readers import RecipeReader

log = logging.getLogger(__name__)


class ImageReader(RecipeReader):
    """
    Reads one photo and returns one Chunk holding its transcript.

    PyMuPDF opens a JPEG, PNG or WebP as a one-page document, which is exactly
    what ``extract_text_via_vision`` takes — the same call that reads a scanned
    PDF. The chunk carries no citation and no image: the transcript says where
    the recipe came from (``resolve_citation`` reads the model's stated source),
    and a photograph of a page is not a photograph of the dish.

    Args:
        client: An initialised ``google.genai.Client``; the OCR is a model call.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def read(self, source: str) -> List[Chunk]:
        """
        Transcribe the photo at ``source`` and return a single-element list.

        Raises:
            ImageExtractionError: PyMuPDF could not open the file as an image.
            RuntimeError: the model returned no text for it (from
                ``extract_text_via_vision``; the job fails with that message).
        """
        # Imported here, as pdf.py does, so this module stays importable without
        # the Gemini SDK on the path.
        from recipeparser.gemini import extract_text_via_vision  # noqa: PLC0415

        try:
            doc = fitz.open(source)
        except Exception as exc:
            raise ImageExtractionError(f"Could not open '{Path(source).name}' as an image: {exc}") from exc
        try:
            if doc.page_count == 0:
                raise ImageExtractionError(f"Could not open '{Path(source).name}' as an image: no pages.")
            text = extract_text_via_vision(doc, self._client)
        finally:
            doc.close()

        log.info("ImageReader: %s transcribed to %d chars.", Path(source).name, len(text))
        return [
            Chunk(
                text=text,
                input_type=InputType.IMAGE,
                source_url=None,
                citation=None,
                label=Path(source).name,
            )
        ]
```

`recipeparser/adapters/api.py`:

Imports (beside the other readers, line 50-52):

```python
from recipeparser.io.readers.image import ImageReader as _ImageReader
```

In `submit_file_job`, the docstring's first line becomes `Accepts PDF, EPUB, .paprikarecipes or a photo (JPEG, PNG, WebP).`, the temp-file suffix line (917) becomes:

```python
            suffix = Path(filename).suffix or _IMAGE_SUFFIX_BY_TYPE.get(content_type, ".bin")
```

and the reader dispatch (930-935) becomes:

```python
                if reader_tag == "pdf":
                    chunks = await asyncio.to_thread(_PdfReader().read, tmp_path)
                elif reader_tag == "epub":
                    chunks = await asyncio.to_thread(_EpubReader().read, tmp_path)
                elif reader_tag == "image":
                    # A model call inside the reader: the OCR is the read.
                    chunks = await asyncio.to_thread(_ImageReader(client).read, tmp_path)
                else:  # paprika
                    chunks = await asyncio.to_thread(_PaprikaReader().read, tmp_path)
```

(`client` is already bound at the top of `_run` by `client = _get_client()`; Task 3 changes the `pdf` line again.)

Also extend the routing comment above the dispatch with a line `#   IMAGE               → full pipeline (EXTRACT→…→ASSEMBLE)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/readers/test_image_reader.py tests/test_api.py tests/unit/test_models.py -q -p no:xdist`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff check recipeparser tests
git add recipeparser/io/readers/image.py recipeparser/core/models.py recipeparser/exceptions.py recipeparser/adapters/api.py tests/unit/readers/test_image_reader.py tests/test_api.py
git commit -m "feat(readers): an ImageReader transcribes a photo through vision OCR, and /jobs/file routes image/* to it"
```

---

### Task 3: `load_pdf` reads a scan through the same OCR on the job path

**Files:**
- Modify: `recipeparser/io/readers/pdf.py:26-147`, `recipeparser/adapters/api.py` (the `pdf` dispatch line from Task 2)
- Test: `tests/unit/readers/test_pdf_ocr_fallback.py`

**Interfaces:**
- Consumes: `extract_text_via_vision(doc, client)`.
- Produces: `PdfReader(client: Any = None)`; `load_pdf(path: str, output_dir: str, client: Any = None) -> Tuple[Citation, str, Set[str], List[str]]`. Behaviour: a text-poor document (average chars per sampled page below `PDF_PREFLIGHT_MIN_CHARS_PER_PAGE`) raises `PdfExtractionError` exactly as today when `client` is `None`, and is transcribed by vision into **one** raw chunk with **no** images when a client is given. `_preflight` is split into `_check_document(doc, path)` (pages, encryption, page cap, the few-pages warning) and `_text_density(doc) -> float`. `extract_text_from_pdf` (the CLI helper at 185-223) is untouched.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/readers/test_pdf_ocr_fallback.py`:

```python
"""load_pdf on the job path: a scan is read through vision OCR, not refused (Input media 1)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import fitz  # PyMuPDF
import pytest

from recipeparser.core.models import InputType
from recipeparser.exceptions import PdfExtractionError
from recipeparser.io.readers.pdf import PdfReader, load_pdf


def _pdf(tmp_path: Path, text: str, name: str = "scan.pdf") -> str:
    doc = fitz.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text)
    doc.set_metadata({"title": "Scanned Book", "author": "A. Cook"})
    path = tmp_path / name
    doc.save(str(path))
    doc.close()
    return str(path)


def _client(text: str) -> MagicMock:
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=text)
    return client


def test_a_scan_without_a_client_is_still_refused(tmp_path):
    with pytest.raises(PdfExtractionError, match="little or no extractable text"):
        load_pdf(_pdf(tmp_path, ""), str(tmp_path / "out"))


def test_a_scan_with_a_client_is_transcribed_into_one_chunk(tmp_path):
    client = _client("Soup\n\n2 onions\n\nSimmer.")
    citation, _image_dir, images, raw_chunks = load_pdf(_pdf(tmp_path, ""), str(tmp_path / "out"), client=client)
    assert raw_chunks == ["Soup\n\n2 onions\n\nSimmer."]     # ruling 3: one transcript, one chunk
    assert images == set()                                    # the page images are the scan itself
    assert (citation.kind, citation.title, citation.author) == ("book", "Scanned Book", "A. Cook")
    client.models.generate_content.assert_called_once()


def test_a_text_pdf_never_calls_the_client(tmp_path):
    client = _client("must not be read")
    long_text = ("Roast chicken. " * 40).strip()              # well over 100 chars on the page
    _c, _d, _i, raw_chunks = load_pdf(_pdf(tmp_path, long_text), str(tmp_path / "out"), client=client)
    assert raw_chunks and "Roast chicken." in raw_chunks[0]
    client.models.generate_content.assert_not_called()


def test_the_reader_passes_its_client_through(tmp_path):
    client = _client("Soup\n\n2 onions\n\nSimmer.")
    chunks = PdfReader(client=client).read(_pdf(tmp_path, ""))
    assert len(chunks) == 1
    assert chunks[0].input_type is InputType.PDF
    assert chunks[0].text.startswith("Soup")
    assert chunks[0].citation.title == "Scanned Book"


def test_the_reader_without_a_client_keeps_the_refusal(tmp_path):
    with pytest.raises(PdfExtractionError):
        PdfReader().read(_pdf(tmp_path, ""))


def test_a_password_protected_document_is_refused_before_any_ocr(tmp_path):
    # PyMuPDF will not save a zero-page document, so the "no pages" branch has
    # no fixture; the password branch of _check_document proves the order.
    client = _client("x")
    doc = fitz.open()
    doc.new_page()
    path = tmp_path / "locked.pdf"
    doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret")
    doc.close()
    with pytest.raises(PdfExtractionError, match="password-protected"):
        load_pdf(str(path), str(tmp_path / "out"), client=client)
    client.models.generate_content.assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/readers/test_pdf_ocr_fallback.py -q -p no:xdist`
Expected: `TypeError: load_pdf() got an unexpected keyword argument 'client'` and `PdfReader.__init__() got an unexpected keyword argument 'client'`.

- [ ] **Step 3: Implement**

In `recipeparser/io/readers/pdf.py`:

`PdfReader` gains a constructor and threads the client (replace lines 40-59's `read` / `_read_in_dir` signatures accordingly):

```python
    def __init__(self, client: Any = None) -> None:
        # With a client, a scan is transcribed by vision OCR instead of refused
        # (Input media 1). Without one — the CLI's text-only paths — the
        # pre-flight refusal stands.
        self._client = client

    def read(self, source: str) -> List[Chunk]:
        ...  # docstring unchanged, plus: "A scan is transcribed by vision OCR when a client was given."
        with tempfile.TemporaryDirectory(prefix="cayenne_pdf_") as output_dir:
            return self._read_in_dir(source, output_dir)

    def _read_in_dir(self, source: str, output_dir: str) -> List[Chunk]:
        """Internal helper — called with a managed temp directory."""
        citation, _image_dir, _qualifying, raw_chunks = load_pdf(source, output_dir, client=self._client)
        ...  # the loop is unchanged
```

`load_pdf` and the pre-flight (replace lines 80-147):

```python
def load_pdf(path: str, output_dir: str, client: Any = None) -> Tuple[Citation, str, Set[str], List[str]]:
    """
    Load a PDF and return the standard book-loader tuple.

    Runs pre-flight (page count, password, page cap), then either extracts
    images and page-based text chunks with [IMAGE: filename] markers, or — for
    a document with little or no text layer — transcribes every page through
    Gemini Vision when ``client`` is given. A scan read that way yields one
    chunk for the whole document and no images: its page images are the scan
    itself, not photographs of dishes. Without a client a scan is refused, as
    it always was.

    Returns:
        (citation, image_dir, qualifying_images, raw_chunks)
    """
    try:
        doc = fitz.open(path)
    except Exception as e:
        raise PdfExtractionError(f"Failed to open PDF '{path}': {e}") from e

    try:
        _check_document(doc, path)
        citation = _get_book_citation(doc)
        image_dir = os.path.join(output_dir, "images")
        os.makedirs(image_dir, exist_ok=True)

        avg_chars, sample_pages = _text_density(doc)
        if avg_chars < PDF_PREFLIGHT_MIN_CHARS_PER_PAGE:
            if client is None:
                raise PdfExtractionError(
                    f"PDF has little or no extractable text (avg {avg_chars:.0f} chars/page over first {sample_pages} pages). "
                    f"It may be a scan without OCR: '{path}'"
                )
            log.info("Scanned PDF detected (avg %.0f chars/page) — transcribing through Gemini Vision.", avg_chars)
            from recipeparser.gemini import extract_text_via_vision  # noqa: PLC0415

            return citation, image_dir, set(), [extract_text_via_vision(doc, client)]

        qualifying_images: Set[str] = set()
        page_image_lists: List[List[str]] = []  # per-page list of qualifying image filenames

        for page_num in range(len(doc)):
            page = doc[page_num]
            filenames = _extract_page_images(doc, page, page_num, image_dir)
            qualifying_images.update(filenames)
            page_image_lists.append(filenames)

        raw_chunks = []
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text()
            markers = "".join(f"\n[IMAGE: {f}]\n" for f in page_image_lists[page_num])
            chunk = (markers + text).strip() if (markers or text.strip()) else ""
            if chunk:
                raw_chunks.append(chunk)

        return citation, image_dir, qualifying_images, raw_chunks
    finally:
        doc.close()


def _check_document(doc: "fitz.Document", path: str) -> None:
    """Raise PdfExtractionError for a document nothing can read: no pages, a password, too many pages."""
    if doc.page_count == 0:
        raise PdfExtractionError(f"PDF has no pages: '{path}'")
    if doc.is_encrypted:
        raise PdfExtractionError(f"PDF is password-protected: '{path}'")
    if doc.page_count < PDF_PREFLIGHT_MIN_PAGES:
        log.warning("PDF has very few pages (%d): %s", doc.page_count, path)
    if PDF_PREFLIGHT_MAX_PAGES is not None and doc.page_count > PDF_PREFLIGHT_MAX_PAGES:
        raise PdfExtractionError(
            f"PDF has too many pages ({doc.page_count}; max {PDF_PREFLIGHT_MAX_PAGES}): '{path}'"
        )


def _text_density(doc: "fitz.Document") -> Tuple[float, int]:
    """Average extractable characters per page over the first sampled pages, and how many were sampled."""
    sample_pages = min(PDF_PREFLIGHT_SAMPLE_PAGES, doc.page_count)
    total_chars = sum(len(doc[i].get_text()) for i in range(sample_pages))
    return (total_chars / sample_pages if sample_pages else 0.0), sample_pages
```

Delete the old `_preflight`. If anything else in the repo imports `_preflight` (grep `_preflight` under `recipeparser/` and `tests/`), keep a one-line alias `_preflight = _check_document` is **not** acceptable — update the caller to the two new names instead.

`recipeparser/adapters/api.py`, the `pdf` dispatch line from Task 2:

```python
                if reader_tag == "pdf":
                    # With the client, a scan is transcribed rather than refused (Input media 1).
                    chunks = await asyncio.to_thread(_PdfReader(client=client).read, tmp_path)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/readers tests/test_api.py tests/goldens/test_readers_golden.py -q -p no:xdist`
Expected: PASS, and the reader goldens unchanged (a text PDF takes the same route as before).

- [ ] **Step 5: Lint and commit**

```bash
ruff check recipeparser tests
git add recipeparser/io/readers/pdf.py recipeparser/adapters/api.py tests/unit/readers/test_pdf_ocr_fallback.py
git commit -m "feat(readers): load_pdf transcribes a scan through vision OCR on the job path instead of refusing it"
```

---

### Task 4: The hero image from the page's own meta, and no more badges

**Files:**
- Modify: `recipeparser/io/readers/url.py` (pure helpers), `recipeparser/adapters/api.py:357-382` (`_extract_image_url_from_markdown`), the URL branch of `submit_job` (777-808), a new `_fetch_page_meta`
- Test: `tests/unit/readers/test_page_meta.py`, `tests/test_api.py` (`TestExtractImageUrl` at 498; the `/jobs` class)

**Interfaces:**
- Produces, in `recipeparser/io/readers/url.py`: `PageMeta` (frozen dataclass: `image_url: Optional[str]`, `description: Optional[str]`); `page_meta_from_html(html: str) -> PageMeta`; `looks_like_badge(url: str, alt: str = "") -> bool`.
- Produces, in `api.py`: `async def _fetch_page_meta(url: str) -> PageMeta` — never raises; `_extract_image_url_from_markdown(md)` unchanged in signature, rule 3 now skips badges. The URL chunk gains `meta=SourceMeta(description=...)` when the page has a description (ruling 4).
- Consumes: `SourceMeta` (`core/models.py:44`); `_upload_image_to_storage` (exists).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/readers/test_page_meta.py`:

```python
"""The page's own <meta> tags, and what an image URL says about itself."""
from __future__ import annotations

from recipeparser.io.readers.url import PageMeta, looks_like_badge, page_meta_from_html

NYT = """<html><head>
<meta name="description" content="In this quick and spicy weeknight noodle dish, sizzling hot oil is poured over red-pepper flakes." data-next-head=""/>
<meta property="og:image" content="https://static01.nyt.com/images/2019/12/18/dining/as-sesame-noodles/merlin_165769617-facebookJumbo.jpg" data-next-head=""/>
<meta property="og:description" content="Should not win over name=description."/>
<meta name="twitter:image" content="https://static01.nyt.com/other.jpg"/>
</head><body>...</body></html>"""


def test_og_image_and_the_description_are_read():
    meta = page_meta_from_html(NYT)
    assert meta == PageMeta(
        image_url="https://static01.nyt.com/images/2019/12/18/dining/as-sesame-noodles/merlin_165769617-facebookJumbo.jpg",
        description="In this quick and spicy weeknight noodle dish, sizzling hot oil is poured over red-pepper flakes.",
    )


def test_twitter_image_and_og_description_are_the_fallbacks():
    html = '<meta name="twitter:image" content="https://x.test/t.jpg"><meta property="og:description" content="Blurb &amp; more">'
    assert page_meta_from_html(html) == PageMeta(image_url="https://x.test/t.jpg", description="Blurb & more")


def test_single_quotes_and_attribute_order_do_not_matter():
    html = "<META content='https://x.test/p.png' property='og:image'>"
    assert page_meta_from_html(html).image_url == "https://x.test/p.png"


def test_a_relative_or_empty_image_is_no_image():
    assert page_meta_from_html('<meta property="og:image" content="/assets/hero.jpg">').image_url is None
    assert page_meta_from_html('<meta property="og:image" content="">').image_url is None
    assert page_meta_from_html("<html><body>no head</body></html>") == PageMeta(None, None)


def test_the_first_occurrence_of_a_tag_wins():
    html = '<meta property="og:image" content="https://x.test/1.jpg"><meta property="og:image" content="https://x.test/2.jpg">'
    assert page_meta_from_html(html).image_url == "https://x.test/1.jpg"


class TestLooksLikeBadge:
    def test_the_edamam_badge_through_a_next_image_wrapper(self):
        assert looks_like_badge("https://cooking.nytimes.com/_next/image?url=%2Fassets%2Fedamam-logo.png&w=768&q=75", "Image 1")

    def test_logo_in_the_path(self):
        assert looks_like_badge("https://cdn.site.test/img/site-logo.png")

    def test_logo_in_the_alt_only(self):
        assert looks_like_badge("https://cdn.site.test/img/a1b2c3.png", "Site logo")

    def test_svg_is_furniture(self):
        assert looks_like_badge("https://cdn.site.test/icons/share.svg")

    def test_a_photograph_is_not(self):
        assert not looks_like_badge("https://static01.nyt.com/images/2019/12/18/dining/as-sesame-noodles/merlin_165769617-facebookJumbo.jpg", "Spicy sesame noodles")
        assert not looks_like_badge("https://cdn.site.test/uploads/2024/dish.jpg", "")
```

In `tests/test_api.py`, `TestExtractImageUrl` gains:

```python
    def test_a_badge_is_not_the_hero(self) -> None:
        md = (
            "# Recipe\n"
            "[Powered by ![Image 1](https://cooking.nytimes.com/_next/image?url=%2Fassets%2Fedamam-logo.png&w=768&q=75)](https://www.edamam.com/)\n"
            "Boil water."
        )
        assert _extract_image_url_from_markdown(md) is None

    def test_the_first_non_badge_markdown_image_wins(self) -> None:
        md = (
            "![Site logo](https://cdn.site.test/logo.png)\n"
            "![Spicy sesame noodles](https://cdn.site.test/uploads/dish.jpg)\n"
        )
        assert _extract_image_url_from_markdown(md) == "https://cdn.site.test/uploads/dish.jpg"

    def test_og_meta_line_still_beats_everything(self) -> None:
        md = "og:image: https://example.com/photo.jpg\n![Dish](https://example.com/other.jpg)"
        assert _extract_image_url_from_markdown(md) == "https://example.com/photo.jpg"
```

And in the `/jobs` test class, a test that the URL path takes the page's meta first. Wait for the background task the way `test_the_patched_write_receives_the_recipe_end_to_end` does (poll `_active_jobs` for the job id, 5 s deadline):

```python
    def test_a_url_job_takes_the_hero_and_the_description_from_the_page_meta(self) -> None:
        from unittest.mock import AsyncMock

        from recipeparser.io.readers.url import PageMeta

        markdown = "Title: Noodles\n\n![Image 1](https://cooking.nytimes.com/_next/image?url=%2Fassets%2Fedamam-logo.png)\n\n1 cup noodles"

        class _Resp:
            text = markdown

            def raise_for_status(self) -> None:
                return None

        class _Http:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def __aenter__(self) -> "_Http":
                return self

            async def __aexit__(self, *a: Any) -> bool:
                return False

            async def get(self, url: str, **kw: Any) -> _Resp:
                return _Resp()

        stack, _mock_client, mock_pipeline_cls = _patch_pipeline_and_writer()
        with stack, \
             patch("recipeparser.adapters.api.httpx.AsyncClient", _Http), \
             patch("recipeparser.adapters.api._fetch_page_meta",
                   new=AsyncMock(return_value=PageMeta("https://static01.nyt.com/hero.jpg", "A weeknight noodle dish."))) as meta, \
             patch("recipeparser.adapters.api._upload_image_to_storage",
                   new=AsyncMock(return_value="https://storage.test/hero.jpg")) as upload, \
             TestClient(app, raise_server_exceptions=False) as tc:
            resp = tc.post("/jobs", json={"url": "https://cooking.nytimes.com/recipes/1020732-noodles"})
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]
            deadline = time.monotonic() + 5.0
            while job_id in _active_jobs and time.monotonic() < deadline:
                time.sleep(0.05)

        meta.assert_awaited_once_with("https://cooking.nytimes.com/recipes/1020732-noodles")
        upload.assert_awaited_once()
        assert upload.await_args.args[0] == "https://static01.nyt.com/hero.jpg"   # the meta image, not the badge
        chunks = mock_pipeline_cls.return_value.run.call_args.args[0]
        assert chunks[0].image_url == "https://storage.test/hero.jpg"
        assert chunks[0].meta is not None and chunks[0].meta.description == "A weeknight noodle dish."
```

If `_patch_pipeline_and_writer` already patches `httpx.AsyncClient` for the jina fetch, drop the `_Http` patch here and reuse what it provides — read the helper first.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/readers/test_page_meta.py tests/test_api.py -q -p no:xdist -k "page_meta or badge or ExtractImageUrl or hero"`
Expected: `ImportError` on `PageMeta`; the badge tests return the logo; the URL-job test fails on `_fetch_page_meta`.

- [ ] **Step 3: Implement**

`recipeparser/io/readers/url.py` — add after the imports (keep `requests` and `UrlReader` as they are):

```python
import html as html_mod
import re
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse


@dataclass(frozen=True)
class PageMeta:
    """What a page says about itself in its <head>: the hero image and the description."""

    image_url: Optional[str]
    description: Optional[str]


_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_IMAGE_KEYS = ("og:image", "og:image:secure_url", "og:image:url", "twitter:image", "twitter:image:src")
_DESCRIPTION_KEYS = ("description", "og:description", "twitter:description")


def _meta_tags(html: str) -> Dict[str, str]:
    """property/name → content for every <meta> tag, first occurrence winning."""
    found: Dict[str, str] = {}
    for tag in _META_TAG_RE.findall(html):
        attrs = {}
        for m in _ATTR_RE.finditer(tag):
            attrs[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = html_mod.unescape(attrs.get("content") or "").strip()
        if key and content and key not in found:
            found[key] = content
    return found


def page_meta_from_html(html: str) -> PageMeta:
    """
    The hero image and description a page declares in its own <meta> tags.

    The scraper's markdown carries neither reliably — on 2026-09-12 an NYT page
    came back with no og:image line and, as its only image, the Edamam
    "Powered by" logo, while the page's own head named the real photograph.
    An image that is not an absolute http(s) URL is treated as none.
    """
    tags = _meta_tags(html)
    image = next((tags[k] for k in _IMAGE_KEYS if k in tags), None)
    if image is not None and not image.lower().startswith(("http://", "https://")):
        image = None
    description = next((tags[k] for k in _DESCRIPTION_KEYS if k in tags), None)
    return PageMeta(image_url=image, description=description)


_BADGE_WORDS = ("logo", "badge", "icon", "sprite", "avatar", "powered", "button", "pixel", "spacer", "placeholder")


def looks_like_badge(url: str, alt: str = "") -> bool:
    """
    True for an image that is a site's furniture rather than a photograph.

    Judged from the URL's path (with a Next.js ``/_next/image?url=…`` wrapper
    unwrapped, and percent-encoding undone) and the alt text: a badge word in
    either, an SVG, or anything the wrapper serves from ``/assets/``.
    """
    parsed = urlparse(unquote(url))
    inner = parse_qs(parsed.query).get("url", [""])[0].lower()
    path = parsed.path.lower()
    if path.endswith(".svg") or inner.endswith(".svg"):
        return True
    if inner.startswith("/assets/") or "/assets/" in inner:
        return True
    haystack = " ".join((path, inner, alt.lower()))
    return any(word in haystack for word in _BADGE_WORDS)
```

`recipeparser/adapters/api.py`:

Import (beside the reader imports):

```python
from recipeparser.core.models import Chunk, InputType, SourceMeta
from recipeparser.io.readers.url import PageMeta, looks_like_badge, page_meta_from_html
```

(`SourceMeta` joins the existing `Chunk, InputType` import on line 47.)

Replace `_extract_image_url_from_markdown` (357-382):

```python
def _extract_image_url_from_markdown(md: str) -> Optional[str]:
    """Extract the best image URL from Jina-flavoured markdown.

    Priority:
      1. ``og:image: <url>`` meta line
      2. ``twitter:image: <url>`` meta line
      3. The first Markdown image ``![alt](url)`` that is not a badge or a logo
         (``looks_like_badge``): on an NYT page the only markdown image is the
         Edamam "Powered by" logo, and it was the hero for a day.

    A trailing ``))`` is cleaned to a single ``)``.
    """
    # 1 & 2 — og/twitter meta lines
    meta_match = re.search(
        r"(?:og|twitter):image:\s*(https?://\S+)", md
    )
    if meta_match:
        url = meta_match.group(1)
        if url.endswith("))"):
            url = url[:-1]
        return url

    # 3 — first Markdown image tag that is a photograph
    for md_match in re.finditer(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)", md):
        alt, url = md_match.group(1), md_match.group(2)
        if not looks_like_badge(url, alt):
            return url

    return None
```

Add, directly after `_upload_image_to_storage`:

```python
_PAGE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


async def _fetch_page_meta(url: str) -> PageMeta:
    """The page's own <meta> tags — its hero image and its description.

    One GET of the page itself, with a browser user-agent (a bare client is
    served a consent wall or a 403 by the sites that matter), read before the
    scraper's markdown is consulted. Any failure — unreachable, not HTML, a
    timeout — is a page without meta, never a failed job.
    """
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": _PAGE_USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"},
        ) as http:
            resp = await http.get(url)
            resp.raise_for_status()
            if "html" not in resp.headers.get("content-type", "").lower():
                return PageMeta(None, None)
            # The head is at the top; a megabyte is more than any head needs.
            return page_meta_from_html(resp.text[:1_000_000])
    except Exception:
        logger.info("Page meta unavailable for %s — falling back to the scraper's markdown.", url)
        return PageMeta(None, None)
```

In `submit_job._run`, the URL branch (777-808) becomes:

```python
            page_meta = PageMeta(None, None)
            if body.url:
                source_url = body.url
                jina_url = f"https://r.jina.ai/{body.url}"
                async with httpx.AsyncClient(timeout=30) as http:
                    resp = await http.get(jina_url)
                    resp.raise_for_status()
                    markdown_text = resp.text
                # The page's own head first (og:image, the description), the
                # scraper's markdown second: the markdown dropped both on the
                # NYT page of 2026-09-12 and offered a logo instead.
                page_meta = await _fetch_page_meta(body.url)
                image_url_candidate = page_meta.image_url or _extract_image_url_from_markdown(markdown_text)
                recipe_id_for_img = str(uuid.uuid4())
                if image_url_candidate:
                    stored_image_url = await _upload_image_to_storage(
                        image_url_candidate, recipe_id_for_img
                    )
                source_text = html_to_text(markdown_text)
            else:
                source_text = (body.text or "").strip()

            # Build a single URL/text chunk for the pipeline.
            # Both URL-scraped and raw-text paths use InputType.URL so the
            # pipeline routes them through the full EXTRACT→REFINE→…→ASSEMBLE
            # sequence.  source_url is None for raw-text submissions.
            # A list of one, so the total_chunks write below and the run() call
            # take the same shape here as they do in the file endpoint.
            # The page's description is the page's own statement, so it rides
            # SourceMeta and beats the model's reading, as a Paprika entry's does.
            chunks = [
                Chunk(
                    text=source_text,
                    input_type=InputType.URL,
                    source_url=source_url,
                    image_url=stored_image_url,
                    citation=web_citation(body.url) if body.url else None,
                    meta=SourceMeta(description=page_meta.description) if page_meta.description else None,
                )
            ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/readers/test_page_meta.py tests/test_api.py -q -p no:xdist`
Expected: PASS, including the five pre-existing `TestExtractImageUrl` tests.

- [ ] **Step 5: Lint and commit**

```bash
ruff check recipeparser tests
git add recipeparser/io/readers/url.py recipeparser/adapters/api.py tests/unit/readers/test_page_meta.py tests/test_api.py
git commit -m "feat(api): the hero image comes from the page's own og:image before the scraper's markdown, and a badge is never the hero"
```

---

### Task 5: `RecipeExtraction` states a total time, a description and nutrition

**Files:**
- Modify: `recipeparser/models.py:7-79`, `recipeparser/gemini.py:391-409` and `438-478`
- Test: `tests/unit/test_models.py`, `tests/goldens/test_prompts_snapshot.py` (snapshots re-approved)

**Interfaces:**
- Produces: `RecipeExtraction.total_time: Optional[str]`, `.description: Optional[str]`, `.nutritional_info: Optional[str]`, each `default=None, repr=False`. Both extract prompts carry three new rule lines, verbatim below.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_models.py`:

```python
def test_extraction_asks_for_a_total_time_a_description_and_nutrition():
    from recipeparser.models import RecipeExtraction

    props = RecipeExtraction.model_json_schema()["properties"]
    assert "total_time" in props and "description" in props and "nutritional_info" in props
    # The one instruction the pulled-pork ingest needed: a stated total is never invented.
    assert "Never the sum" in props["total_time"]["description"]
    assert "verbatim" in props["nutritional_info"]["description"]
    r = RecipeExtraction(name="x", ingredients=[], directions=[])
    assert (r.total_time, r.description, r.nutritional_info) == (None, None, None)


def test_the_three_new_extraction_fields_stay_out_of_repr():
    # Same invariant as stated_source/byline: the refine prompt's body is
    # str(raw_recipe), and the golden recordings are keyed by it.
    from recipeparser.models import RecipeExtraction

    text = str(RecipeExtraction(
        name="x", ingredients=[], directions=[],
        total_time="8 to 10 hours", description="A weeknight dish.", nutritional_info="572 calories",
    ))
    for needle in ("total_time", "description", "nutritional_info", "8 to 10 hours", "weeknight", "572"):
        assert needle not in text


def test_both_extract_prompts_name_the_three_fields():
    from recipeparser import gemini

    for prompt in (gemini.build_plain_text_prompt("x"), gemini.build_extract_prompt("x")):
        assert "total_time:" in prompt and "description:" in prompt and "nutritional_info:" in prompt
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_models.py -q -p no:xdist`
Expected: the three new tests fail (`total_time` not in properties; prompts lack the lines).

- [ ] **Step 3: Implement**

`recipeparser/models.py` — after `byline` (line 70), before the `categories` field:

```python
    total_time: Optional[str] = Field(
        default=None,
        repr=False,
        description=(
            "The total or overall time when the text states one ('Total Time 8 to 10 hours, plus "
            "refrigeration'). Never the sum of prep and cook. null when the text states no total."
        ),
    )
    description: Optional[str] = Field(
        default=None,
        repr=False,
        description=(
            "The headnote or introduction printed with the recipe, in the source's own words, at most "
            "one paragraph. null when there is none."
        ),
    )
    nutritional_info: Optional[str] = Field(
        default=None,
        repr=False,
        description=(
            "The recipe's nutrition statement, verbatim, as one line ('572 calories; 19 grams fat; "
            "35 grams protein'). null when the text carries none."
        ),
    )
```

`recipeparser/gemini.py` — in `build_plain_text_prompt`, after the `stated_source` rule (line 403) and before `- Do not invent`:

```
- total_time: the recipe's stated total or overall time, only when the text states one.
  Never add prep and cook together.
- description: the headnote or introduction printed with the recipe, in its own words, at
  most one paragraph. null when there is none.
- nutritional_info: the recipe's nutrition statement, verbatim, as one line. null when the
  text carries none.
```

In `build_extract_prompt`, the same three rule lines after the `stated_source` rule (line 473) and before `- Do not invent`. Copy them exactly; the two prompts share the wording.

- [ ] **Step 4: Run the tests, then re-approve the snapshots**

Run: `python -m pytest tests/unit/test_models.py -q -p no:xdist`
Expected: PASS.

Run: `python -m pytest tests/goldens tests/snapshots -q -p no:xdist`
Expected: the prompt snapshots (`extract-*`, `plain_text_prompt`) and the `recipe_list_schema` snapshot FAIL with a diff; every other golden PASSES (with `prompt_sha256 mismatch` warnings on extract replays, which is the drift detector working).

Run: `python -m pytest tests/goldens tests/snapshots --snapshot-update -q -p no:xdist`, then `git diff --stat tests/goldens/__snapshots__` and read the diff: only the three prompt lines (×5 prompts) and the three schema properties may appear. Anything else is a defect — stop and report.

- [ ] **Step 5: Lint and commit**

```bash
ruff check recipeparser tests
git add recipeparser/models.py recipeparser/gemini.py tests/unit/test_models.py tests/goldens/__snapshots__
git commit -m "feat(extract): the model states a total time, a description and nutrition, out of the repr so the goldens hold"
```

---

### Task 6: `assemble()` uses the three where a Paprika entry states nothing

**Files:**
- Modify: `recipeparser/core/stages/assemble.py:21-121`, `recipeparser/core/pipeline.py:384-401`
- Test: `tests/unit/stages/test_assemble.py`

**Interfaces:**
- Consumes: the three fields (Task 5); `duration_columns` (`core/durations.py:152`, unchanged).
- Produces: `assemble(..., total_time: Optional[str] = None, description: Optional[str] = None, nutritional_info: Optional[str] = None)`. Rules: `description` and `nutritional_info` are `meta`'s when `meta` states them, else the extracted ones, else null; `total_time` feeds `duration_columns` as the cook text **only** when `cook_time` (after `meta`) is absent, and the `cook_time` text column is not changed by it (ruling 5). The pipeline passes `getattr(raw, "total_time", None)` etc. at the full-pipeline call site; the two Paprika fast paths pass nothing new (a Paprika entry carries its own).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/stages/test_assemble.py`:

```python
def test_a_stated_total_fills_the_cook_span_when_cook_is_absent():
    r = _assemble(total_time="8 to 10 hours, plus refrigeration")
    assert (r.cook_min_minutes, r.cook_max_minutes, r.cook_note) == (480, 600, "plus refrigeration")
    assert r.cook_time is None            # the text column is what the page said for cook: nothing


def test_a_stated_cook_time_beats_the_total():
    r = _assemble(cook_time="30 mins", total_time="8 hours")
    assert (r.cook_min_minutes, r.cook_max_minutes) == (30, 30)
    assert r.cook_time == "30 mins"


def test_a_paprika_cook_time_still_beats_both():
    r = _assemble(cook_time="30 mins", total_time="8 hours", meta=SourceMeta(cook_time="45 mins"))
    assert (r.cook_min_minutes, r.cook_max_minutes, r.cook_time) == (45, 45, "45 mins")


def test_the_extracted_description_and_nutrition_are_kept_when_no_source_states_them():
    r = _assemble(description="A weeknight noodle dish.", nutritional_info="572 calories; 19 grams fat")
    assert r.description == "A weeknight noodle dish."
    assert r.nutritional_info == "572 calories; 19 grams fat"


def test_a_source_statement_beats_the_extracted_description_and_nutrition():
    meta = SourceMeta(description="The page's own blurb.", nutritional_info="Per serving: 600 kcal")
    r = _assemble(description="model's blurb", nutritional_info="model's nutrition", meta=meta)
    assert r.description == "The page's own blurb."
    assert r.nutritional_info == "Per serving: 600 kcal"


def test_a_source_that_states_only_one_of_them_does_not_blank_the_other():
    meta = SourceMeta(description="The page's own blurb.")
    r = _assemble(description="model's blurb", nutritional_info="572 calories", meta=meta)
    assert r.description == "The page's own blurb."
    assert r.nutritional_info == "572 calories"


def test_nothing_stated_anywhere_stays_null():
    r = _assemble()
    assert (r.description, r.nutritional_info, r.cook_min_minutes) == (None, None, None)
```

And a pipeline wiring test — append to `tests/unit/stages/test_assemble.py` (it is the nearest home; the pipeline test files exercise the FSM):

```python
def test_the_full_pipeline_hands_assemble_the_three_extracted_fields(monkeypatch):
    """The call site in RecipePipeline._process_chunk passes total_time, description and nutritional_info."""
    import inspect

    from recipeparser.core import pipeline as pipeline_mod

    src = inspect.getsource(pipeline_mod.RecipePipeline._process_chunk)
    for name in ("total_time", "description", "nutritional_info"):
        assert f'{name}=getattr(raw, "{name}", None)' in src, name
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/stages/test_assemble.py -q -p no:xdist`
Expected: `TypeError: assemble() got an unexpected keyword argument 'total_time'`; the wiring test fails on the source text.

- [ ] **Step 3: Implement**

`recipeparser/core/stages/assemble.py` — the signature (21-34) gains, after `citation`:

```python
    citation: Optional[Citation] = None,
    total_time: Optional[str] = None,
    description: Optional[str] = None,
    nutritional_info: Optional[str] = None,
) -> IngestResponse:
```

The docstring's `Args` gains:

```
        total_time:      The total the text stated, when it stated only a total.
                         Feeds the structured cook span when no cook time is
                         known; never the cook_time text column.
        description:     The headnote the extractor read. A source's own
                         statement (meta) wins over it.
        nutritional_info: The nutrition line the extractor read. As above.
```

Replace lines 84-93 (the meta block through `cols = ...`):

```python
    if meta is not None:
        prep_time = meta.prep_time or prep_time
        cook_time = meta.cook_time or cook_time
        description = meta.description or description
        nutritional_info = meta.nutritional_info or nutritional_info

    derived_lines, derived_steps = raw_lines_from_derived(
        recipe.structured_ingredients, recipe.tokenized_directions
    )
    lines = list(ingredient_lines) if ingredient_lines else derived_lines
    steps = list(direction_steps) if direction_steps else derived_steps
    # A page that states only a total ("Total Time 8 to 10 hours") gets that
    # as its cook span; the cook_time text stays what the page said for cook.
    # durations.py is untouched, so its rules stay those of the Cayenne twin.
    cols = duration_columns(prep_time, cook_time or total_time, servings_text, recipe.base_servings)
```

And in the `IngestResponse(...)` construction, replace the comment at 107-108 and the two lines at 119-120:

```python
        # notes, rating and difficulty come only from a Paprika entry: nothing
        # infers them from a book or a web page. description and nutritional_info
        # are the source's statement where it made one, else the extractor's.
        ...
        nutritional_info=nutritional_info,
        description=description,
```

(`notes`, `rating`, `difficulty` lines are unchanged.)

`recipeparser/core/pipeline.py`, the full-pipeline `assemble(` call (384-401): after the `citation=resolve_citation(...)` argument add:

```python
                total_time=getattr(raw, "total_time", None),
                description=getattr(raw, "description", None),
                nutritional_info=getattr(raw, "nutritional_info", None),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/stages tests/unit/test_durations.py tests/goldens -q -p no:xdist`
Expected: PASS; the stage and e2e goldens are unchanged in outcome (recorded replies carry none of the three fields, so every assembled value is null exactly as before).

- [ ] **Step 5: Lint and commit**

```bash
ruff check recipeparser tests
git add recipeparser/core/stages/assemble.py recipeparser/core/pipeline.py tests/unit/stages/test_assemble.py
git commit -m "feat(assemble): a stated total fills the cook span, and the page's description and nutrition are kept where no source states them"
```

---

### Task 7: The job row's `source_hint` becomes the recipes' source key

**Files:**
- Modify: `recipeparser/adapters/job_sink.py`, `recipeparser/adapters/api.py:668-698` (`_update_total_chunks`), the two `_update_total_chunks` call sites (837, 969)
- Test: `tests/unit/test_job_sink.py`, `tests/unit/test_ingestion_endpoints.py`, `tests/test_api.py`

**Interfaces:**
- Produces: `_source_key_of(chunks: List[Chunk]) -> Optional[str]` (the most common non-null `chunk.citation.key`, ties broken by first seen); `_update_total_chunks(job_id: str, total: int, source_hint: Optional[str] = None)` — the UPDATE carries `source_hint` only when given; `JobSink.finalize_payload(...)` carries `source_hint` (the most common `source_key` among the recipes it wrote) only when at least one recipe carried a key (ruling 7).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_job_sink.py`:

```python
class _CitedRecipe(_Recipe):
    def __init__(self, title: str = "R", source_key: "str | None" = None) -> None:
        super().__init__(title)
        self.source_key = source_key


def test_finalize_carries_the_recipes_source_key_as_the_hint():
    sink = _sink()
    for key in ("cooking.nytimes.com", "cooking.nytimes.com", "seriouseats.com"):
        sink.on_result(_CitedRecipe(source_key=key))
    assert sink.finalize_payload(True)["source_hint"] == "cooking.nytimes.com"


def test_finalize_omits_the_hint_when_no_recipe_carried_a_key():
    sink = _sink()
    sink.on_result(_CitedRecipe(source_key=None))
    sink.on_result(_Recipe())          # no source_key attribute at all
    assert "source_hint" not in sink.finalize_payload(True)


def test_a_recipe_whose_write_failed_does_not_vote():
    def _boom(recipe, user_id, recipe_id=None, category_ids=None):
        raise RuntimeError("no")

    sink = JobSink(job_id="job-1", user_id="user-1", category_ids={}, write=_boom, now=lambda: "t")
    sink.on_result(_CitedRecipe(source_key="the woks of life"))
    assert "source_hint" not in sink.finalize_payload(True)
```

(Use `Optional[str]` from `typing` rather than the string annotation if the file already imports it.)

Append to `tests/unit/test_ingestion_endpoints.py`:

```python
def _capturing_client(sent: dict):
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
    return client


def test_total_chunks_update_carries_the_source_hint_when_known(monkeypatch):
    sent = {}
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: _capturing_client(sent))
    monkeypatch.setattr(api, "_live_writes_blocked", lambda: False)
    api._update_total_chunks("job-1", 12, source_hint="the best of jane grigson")
    assert sent["total_chunks"] == 12
    assert sent["source_hint"] == "the best of jane grigson"


def test_total_chunks_update_leaves_the_hint_alone_when_unknown(monkeypatch):
    sent = {}
    monkeypatch.setattr(api, "_get_supabase_service_client", lambda: _capturing_client(sent))
    monkeypatch.setattr(api, "_live_writes_blocked", lambda: False)
    api._update_total_chunks("job-1", 1)
    assert sent["total_chunks"] == 1
    assert "source_hint" not in sent


def test_source_key_of_takes_the_majority_key_and_ignores_uncited_chunks():
    from recipeparser.core.citation import book_citation, web_citation
    from recipeparser.core.models import Chunk, InputType

    book = book_citation("Italian Food", "Elizabeth David")
    chunks = [
        Chunk(text="a", input_type=InputType.EPUB, citation=book),
        Chunk(text="b", input_type=InputType.EPUB, citation=book),
        Chunk(text="c", input_type=InputType.URL, citation=web_citation("https://x.test/r")),
        Chunk(text="d", input_type=InputType.IMAGE),
    ]
    assert api._source_key_of(chunks) == "italian food"
    assert api._source_key_of([Chunk(text="d", input_type=InputType.IMAGE)]) is None
    assert api._source_key_of([]) is None
```

In `tests/test_api.py`, in the `/jobs` class, the ordering proof (the roadmap's "written before the first EXTRACTING stage"): 

```python
    def test_total_chunks_and_the_hint_land_before_the_pipeline_runs(self) -> None:
        order: list[str] = []
        stack, _mock_client, mock_pipeline_cls = _patch_pipeline_and_writer()
        mock_pipeline_cls.return_value.run.side_effect = lambda *a, **kw: order.append("run") or []

        def _record(job_id: str, total: int, source_hint: "str | None" = None) -> None:
            order.append(f"total_chunks={total} hint={source_hint}")

        with stack, patch("recipeparser.adapters.api._update_total_chunks", side_effect=_record), \
             TestClient(app, raise_server_exceptions=False) as tc:
            resp = tc.post("/jobs", json={"text": "Boil water. Add pasta."})
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]
            deadline = time.monotonic() + 5.0
            while job_id in _active_jobs and time.monotonic() < deadline:
                time.sleep(0.05)

        assert order == ["total_chunks=1 hint=None", "run"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_job_sink.py tests/unit/test_ingestion_endpoints.py tests/test_api.py -q -p no:xdist -k "hint or source_key_of or before_the_pipeline"`
Expected: `KeyError: 'source_hint'`; `TypeError` on the `source_hint` keyword; `AttributeError: _source_key_of`.

- [ ] **Step 3: Implement**

`recipeparser/adapters/job_sink.py`:

```python
from collections import Counter
...
        self._last_pct = 0
        # Which source the written recipes carry, so the job row can point the
        # library at it (design 2026-09-11: source_hint becomes the source_key).
        self._source_keys: Counter = Counter()
```

In `on_result`, after `self.recipe_count += 1`:

```python
        key = getattr(recipe, "source_key", None)
        if key:
            self._source_keys[key] += 1
```

In `finalize_payload`, after the `progress_pct` line and before `error_message`:

```python
        if self._source_keys:
            # Pasted text and photos only learn their source from the model, so
            # the read-time hint (the chunks' citation) was never set for them;
            # a job that wrote nothing keyed keeps whatever hint it had.
            payload["source_hint"] = self._source_keys.most_common(1)[0][0]
```

`recipeparser/adapters/api.py`:

```python
from collections import Counter
...
def _source_key_of(chunks: List[Chunk]) -> Optional[str]:
    """The source key the job's chunks carry, when a reader knew it.

    A book's chunks all carry the book; a URL's one chunk carries its host.
    The most common key wins on a mixed batch; pasted text and photos carry
    none and answer None, and learn theirs at finalize from the written rows.
    """
    keys: Counter = Counter(
        chunk.citation.key for chunk in chunks
        if chunk.citation is not None and chunk.citation.key
    )
    if not keys:
        return None
    return keys.most_common(1)[0][0]


def _update_total_chunks(job_id: str, total: int, source_hint: Optional[str] = None) -> None:
    """Record how many chunks this job will attempt, and which source they carry.

    ... (existing docstring paragraphs unchanged) ...

    ``source_hint`` rides the same UPDATE (design 2026-09-11): once the reader
    has returned, the job's hint becomes the recipes' source_key, so a
    Recent-imports row opens the library on exactly what the job wrote. It is
    sent only when known; None leaves the row's hint as the endpoint set it.
    """
    if _live_writes_blocked():
        logger.warning("Test run: skipping the total_chunks update for job %s.", job_id)
        return
    try:
        import datetime  # noqa: PLC0415

        sb = _get_supabase_service_client()
        if sb is None:
            return
        payload: Dict[str, Any] = {
            "total_chunks": total,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        }
        if source_hint is not None:
            payload["source_hint"] = source_hint
        sb.table("ingestion_jobs").update(payload).eq("id", job_id).execute()
        logger.info("Job %s: total_chunks = %d, source_hint = %r.", job_id, total, source_hint)
    except Exception:
        logger.exception("Job %s: failed to write total_chunks=%d.", job_id, total)
```

`List` must be in the `typing` import at line 35. Both call sites (837 and 969) become:

```python
            await asyncio.to_thread(_update_total_chunks, job_id, len(chunks), _source_key_of(chunks))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_job_sink.py tests/unit/test_ingestion_endpoints.py tests/test_api.py -q -p no:xdist`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff check recipeparser tests
git add recipeparser/adapters/job_sink.py recipeparser/adapters/api.py tests/unit/test_job_sink.py tests/unit/test_ingestion_endpoints.py tests/test_api.py
git commit -m "feat(jobs): source_hint becomes the recipes' source key — from the chunks at read time, from the rows at finalize"
```

---

### Task 8: The changelog, and the full gate

**Files:**
- Modify: `CHANGELOG.md` (a new section above `## [8.0.0]`)

- [ ] **Step 1: Write the entry**

Insert after the `---` on line 6 and before `## [8.0.0] — 2026-09-12`:

```markdown
## [Unreleased]

Stage D of the Add Recipe workstream (Cayenne `docs/superpowers/plans/2026-09-11-add-recipe-workstream.md`; roadmap Stage 6 row D). No migration: every column written here already exists.

### ✨ Added — the intake reads photos and scans
- `POST /jobs/file` accepts `image/jpeg`, `image/png` and `image/webp`. A new `ImageReader` opens the photo with PyMuPDF and transcribes it through the vision OCR the command-line PDF path already owned; one chunk, routed like a book chunk, no citation of its own (the transcript's stated source is used).
- `load_pdf` gains the same fallback behind a `client` parameter, so a scanned PDF is transcribed on the job path instead of refused by the pre-flight. Without a client the refusal stands.
- The 422 for a file the API cannot read is a sentence the client shows verbatim: `Cayenne can't read .docx files yet.`; HEIC is refused plainly: `Cayenne can't read HEIC photos yet. Share it as a JPEG instead.`

### ✨ Added — what a page states is no longer lost
- The URL path fetches the page itself once, with a browser user-agent, and takes `og:image` / `twitter:image` and the meta description from its `<head>` before consulting the scraper's markdown; the markdown fallback refuses badges and logos. On 2026-09-12 an NYT recipe stored the Edamam "Powered by" logo as its hero because the scraper's markdown carried no `og:image` line and that logo was its only image.
- `RecipeExtraction` gains `total_time`, `description` and `nutritional_info` (all `repr=False`, so the refine prompt body and the golden recordings do not move). `assemble()` fills the structured cook span from a stated total when no cook time is known, and keeps the extracted description and nutrition where no source (a Paprika entry, the page's meta) states them — since `607671d` both columns were Paprika-only, so a URL ingest had neither.

### ✨ Added — the job row points at its source
- `ingestion_jobs.source_hint` becomes the recipes' `source_key`: written with `total_chunks` from the chunks' citations (books and sites), and again at finalize from the written rows (pasted text and photos). The Add Recipe screen's Recent-imports rows open the library on it.
```

- [ ] **Step 2: Run the whole gate**

```bash
ruff check recipeparser scripts tests
python -m pytest -q
```

Expected: ruff clean; the full suite green (the baseline on `d2cba49` is recorded in the SDD ledger; the count rises by the tests this plan adds). No `.env` in the worktree, so nothing reaches a live project.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for the Stage D intake changes"
```

---

## Rulings made during execution (2026-09-12)

Executed by subagent-driven development: a fresh implementer per task, a task review per task, one fix round where a review asked for it, a whole-branch review, one fix wave and one scoped re-review. What the execution decided that this plan did not, each with what it costs if wrong:

- **The lint gate is "no new findings in a touched file".** The repository carries about 130 pre-existing ruff findings, so the plan's "ruff clean before every commit" had no clean baseline. Cost: a pre-existing finding in a touched file could mask a new one of the same code.
- **A present extension decides the reader on its own; the content type only when there is none.** The plan's `ext in X or content_type in Y` per category let `image/heic` refuse a `.jpg`. The old paprika content-type branch, unreachable by the test file's own account, went. Cost: a file with a lying extension is routed by its name, as pdf/epub always were.
- **WebP is refused plainly, like HEIC.** PyMuPDF 1.27 fails at `fitz.open()` on WebP and Pillow is not a dependency; the sentence is `Cayenne can't read WebP photos yet. Share it as a JPEG instead.` Cost: Android Chrome shares WebP, so those users get a sentence until a decoder lands. `INGESTION_API.md`'s "image/webp" is amended at housekeeping.
- **`PDF_OCR_MAX_PAGES = 40` caps the OCR path**, refused before any vision call; the 2000-page read cap was the only bound. Cost: a 41-page scan is refused with a sentence; one constant to raise.
- **Ruling 3 amended: the OCR transcript is split to `MAX_CHUNK_CHARS`** with the EPUB reader's `split_large_chunk`, still no images, no page attribution. One 40-page transcript in one chunk was two to four times the cap on exactly the path this plan opens. Cost: a recipe straddling a split boundary is lost, the risk every EPUB chapter already carries.
- **`og:image` is badge-checked too**, else the markdown fallback runs; **badge words match whole tokens** (`iconic-lasagna.jpg` is a photograph), and `button`/`avatar` left the list; the wrapper is parsed before it is unquoted; an absent Content-Type is parsed anyway; `_fetch_page_meta` refuses non-http schemes and private, loopback, link-local and reserved IP-literal hosts without DNS. Cost: a decimal-encoded loopback or a DNS-rebinding host still passes; recorded.
- **Ruling 4's rationale corrected:** until Task 6 landed, `assemble()` read `description` only from `meta`, so the page's description was additive then and beats the model's now.
- **Ruling 7 amended twice: the hint is written only when the batch carries exactly one distinct key** (a Paprika archive carries many and keeps the filename the endpoint set), **and at read time only for a web citation's key**; a book's key arrives at finalize, because Cayenne's import banner shows `source_hint` verbatim while a job runs and a running `Italian Food.epub` must not read "italian food". Cost: a Recent-imports row for a running book job opens on nothing until the job is done, which Stage E handles regardless; a two-source photo job keeps its filename.
- **The stage snapshots gained three `None` keys per entry** because they record `raw.model_dump()`, which lists every field regardless of `repr=False`. Accepted; cost none.
- **Parked from the fix wave's re-review:** `urlparse` in the private-host check sits outside `_fetch_page_meta`'s `try`, so an unbalanced-bracket URL raises `ValueError` and fails the job instead of degrading to no meta. A one-line fix, carried on the pull request as a residual. Deferred minors from every task review are on the pull request too; none blocks the merge.

Follow-ups this execution recorded and did not take: the CLI's `extract_text_from_pdf` still holds a second copy of the scanned-PDF detection; the jina fetch and the page-meta fetch could run concurrently; `RecipeExtraction.notes` is still extracted and dropped; Gate D's eyes-on items gain a multi-page scan.

## Notes for the whole-branch review

- **Ruling 4 in practice:** a site whose `<meta name="description">` is SEO boilerplate will put that boilerplate in `description`. Accepted: it is the page's own statement, and the editor can change it; the alternative — the model's reading of markdown that may not carry the headnote at all — was null on the case that prompted this.
- **What Stage E inherits:** `source_hint` is a *normalised key* (`the best of jane grigson`, `cooking.nytimes.com`), never a display title. The Recent-imports row titles itself through the library's source facets for that key, not by printing the hint.
- **Not in this plan, recorded so they are known gaps:** `RecipeExtraction.notes` is still extracted and dropped (ruling 6); the Paprika reader still does not read a `total_time` (Paprika has none); the page-meta fetch is one extra GET per URL job, uncached, and a site that refuses even a browser user-agent simply falls back to the markdown.
- **Post-merge housekeeping (Cayenne repository, not this pull request):** `INGESTION_API.md`'s *Provenance* and *Input media* paragraphs say "not yet built" — amend to built with this PR's number; `ROADMAP.md` row D and the workstream plan's Stage D get their state; this plan gets its `**Merged:**` header; the container is rebuilt; Gate D's eyes-on items (a photo of a recipe page ingests end to end; a scanned PDF fixture ingests; a `.docx` is refused with the sentence) are run on the device and recorded on row D.
