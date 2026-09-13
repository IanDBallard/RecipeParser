# Intake Bulk Fix Implementation Plan (RecipeParser half)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the RecipeParser residuals the Add Recipe workstream left after #41 (2026-09-12) in one pull request: the page-meta fetch that can fail a job on a malformed URL, the extracted `notes` that are read and then dropped, an upload ceiling with a 413 whose sentence the client shows verbatim, and a script that writes the Paprika archive's 68 lost entries into a small archive the new intake can take in one drop.

**Architecture:** Four self-contained changes. One `if` moves inside a `try`; `assemble()` gains a fifth extracted field with the same "the source's own statement wins" rule as `description`; the file endpoint checks the upload's size after the reader is chosen and before the body is used, against one constant in `config.py`; a new script under `scripts/` reuses the restore script's matching rule and writes the unmatched members, bytes untouched, into a new ZIP. No migration, no prompt change, no golden change, no new dependency.

**Tech Stack:** Python 3.11/3.12, FastAPI + Starlette `UploadFile`, httpx, PyMuPDF (untouched), pytest (`-n0`), ruff.

**Spec:** Cayenne `SpecificationDocumentation/INGESTION_API.md` (*Input media* 2 — the 422 sentence the client shows verbatim is the shape the 413 copies; *The client side* — the Cayenne half's guard) and the residuals recorded on `docs/superpowers/plans/2026-09-12-intake-changes.md` under *Rulings made during execution* ("Parked from the fix wave's re-review" and "Follow-ups this execution recorded and did not take"). The Cayenne half is `Cayenne/docs/superpowers/plans/2026-09-13-add-recipe-bulk-fix.md`; the two share one number, the 50 MB ceiling, and one sentence for it.

## Global Constraints

- **The ceiling is one constant:** `MAX_UPLOAD_BYTES: int = 50_000_000` in `recipeparser/config.py`; Cayenne's `MAX_UPLOAD_BYTES` in `cayenne-web/src/lib/domain/ingestion.ts` is the same number. The 413 `detail` is exactly `This file is {size/1_000_000:.1f} MB. Cayenne takes files up to {MAX_UPLOAD_BYTES // 1_000_000} MB.` — the client shows it verbatim, as it shows the 422.
- **A refusal is a sentence, never a stack:** every `HTTPException` detail added here names the file's size or type in words and nothing internal (the 422 rule, INGESTION_API.md *Input media* 2).
- **`_fetch_page_meta` never raises** (its docstring): any failure is `PageMeta(None, None)`.
- **`RecipeExtraction` is not widened and its `repr` does not move:** `notes` is an existing field already in the schema and the repr; no prompt text changes; the golden corpus is untouched.
- **Tests:** `pytest -n0` always (never `-p no:xdist`); `tests/test_api.py` has an import-order auth quirk — run it first or alone. No `.env` is created in the worktree; the unit suite runs with none.
- **Lint:** `ruff check <touched files>` reports no new finding (the tree has ~130 pre-existing; the gate is no new ones in touched files).
- **Commits:** one per task, message ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. `CHANGELOG.md` `[Unreleased]` gains a line per task.
- **The gate** (Task 5): `pytest -n0 tests/test_api.py` green, then `pytest -n0 tests/unit` green; ruff clean on touched files.

---

### Task 1: A malformed URL is a page without meta, not a failed job

`_is_unsafe_fetch_target(url)` calls `urlparse` outside `_fetch_page_meta`'s `try`; `urlparse("http://[::1")` raises `ValueError: Invalid IPv6 URL`, and the job fails where the docstring promises degradation to no meta.

**Files:**
- Modify: `recipeparser/adapters/api.py` (`_fetch_page_meta`)
- Test: `tests/test_api.py` (the class holding `test_a_cloud_metadata_ip_is_refused_before_any_get`)

- [ ] **Step 1: Write the failing test**

In `tests/test_api.py`, in the same class as `test_localhost_is_refused_before_any_get`, add:

```python
    def test_a_malformed_url_is_no_meta_not_a_failed_job(self) -> None:
        # urlparse raises ValueError on an unbalanced bracket; the docstring promises degradation.
        calls: list = []
        with patch("recipeparser.adapters.api.httpx.AsyncClient", self._refusing_http(calls)):
            result = asyncio.run(_fetch_page_meta("http://[::1"))
        assert result == PageMeta(None, None)
        assert calls == []
```

- [ ] **Step 2: Run it to see it fail**

Run: `pytest -n0 tests/test_api.py -k malformed_url -v`
Expected: FAIL with `ValueError: Invalid IPv6 URL`.

- [ ] **Step 3: Move the check inside the try**

In `_fetch_page_meta`, replace

```python
    if _is_unsafe_fetch_target(url):
        return PageMeta(None, None)
    try:
        async with httpx.AsyncClient(
```

with

```python
    try:
        # Inside the try: urlparse raises on a malformed address ("http://[::1"), and a malformed
        # address is a page without meta, not a failed job.
        if _is_unsafe_fetch_target(url):
            return PageMeta(None, None)
        async with httpx.AsyncClient(
```

(The `except Exception` at the bottom already logs and returns `PageMeta(None, None)`.)

- [ ] **Step 4: Run the API tests**

Run: `pytest -n0 tests/test_api.py -v`
Expected: PASS, the whole file (the three existing refusal tests still see no GET).

- [ ] **Step 5: Changelog and commit**

`CHANGELOG.md`, under `## [Unreleased]`, add a heading `### 🐛 Fixed — the bulk fix after Stage E (2026-09-13)` (once; Tasks 2–4 add bullets under their own headings or this one as fits) with:

```
- A malformed page address (`http://[::1`) no longer fails a URL job: the private-host check runs inside `_fetch_page_meta`'s `try`, so it degrades to no meta as the docstring promises.
```

```bash
git add recipeparser/adapters/api.py tests/test_api.py CHANGELOG.md
git commit -m "A malformed page address is a page without meta, not a failed job" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: The extracted notes reach the row

`RecipeExtraction.notes` ("Any additional notes or headnotes from the author") is extracted on every path and then dropped: `assemble()` writes `notes=meta.notes if meta else None`, so only a Paprika entry's notes survive. The same rule `description` got in #41 applies: the source's own statement wins, else the extractor's reading, else null.

**Files:**
- Modify: `recipeparser/core/stages/assemble.py` (a `notes` parameter)
- Modify: `recipeparser/core/pipeline.py` (`_process_chunk`'s `assemble(...)` call)
- Test: `tests/unit/stages/test_assemble.py`

**Interfaces:**
- Produces: `assemble(..., notes: Optional[str] = None)`; `IngestResponse.notes` is `meta.notes or notes`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/stages/test_assemble.py`, after `test_nothing_stated_anywhere_stays_null`:

```python
def test_the_extracted_notes_are_kept_when_no_source_states_them():
    r = _assemble(notes="Best the next day.")
    assert r.notes == "Best the next day."


def test_a_source_statement_beats_the_extracted_notes():
    r = _assemble(notes="model's note", meta=SourceMeta(notes="The cook's own note."))
    assert r.notes == "The cook's own note."


def test_a_source_with_no_notes_does_not_blank_the_extracted_ones():
    r = _assemble(notes="Best the next day.", meta=SourceMeta(description="A blurb."))
    assert r.notes == "Best the next day."
```

And in `test_the_full_pipeline_hands_assemble_the_three_extracted_fields`, widen the tuple and the name:

```python
def test_the_full_pipeline_hands_assemble_the_four_extracted_fields(monkeypatch):
    """The call site in RecipePipeline._process_chunk passes total_time, description, nutritional_info and notes."""
    import inspect

    from recipeparser.core import pipeline as pipeline_mod

    src = inspect.getsource(pipeline_mod.RecipePipeline._process_chunk)
    for name in ("total_time", "description", "nutritional_info", "notes"):
        assert f'{name}=getattr(raw, "{name}", None)' in src, name
```

- [ ] **Step 2: Run them to see them fail**

Run: `pytest -n0 tests/unit/stages/test_assemble.py -v`
Expected: FAIL — `assemble()` has no `notes` parameter; the call site lacks the line.

- [ ] **Step 3: Implement**

`recipeparser/core/stages/assemble.py`: add `notes: Optional[str] = None,` to the signature after `nutritional_info` (match the file's parameter style), add to the docstring's Args:

```
        notes:            The author's notes or headnote the extractor read. A source's own
                          statement (meta) wins over it, as for description.
```

In the `if meta is not None:` block add `notes = meta.notes or notes`, and in the `IngestResponse(...)` construction replace `notes=meta.notes if meta else None,` with `notes=notes,`. Amend the comment above it: `# rating and difficulty come only from a Paprika entry: nothing infers them from a book or a web page. description, nutritional_info and notes are the source's statement where it made one, else the extractor's.`

`recipeparser/core/pipeline.py`, in `_process_chunk`'s `assemble(` call, after `nutritional_info=getattr(raw, "nutritional_info", None),`:

```python
                notes=getattr(raw, "notes", None),
```

- [ ] **Step 4: Run the stage tests and the pipeline tests**

Run: `pytest -n0 tests/unit/stages/test_assemble.py tests/unit/test_pipeline.py -v`
Expected: PASS. (If `test_pipeline.py`'s stand-in `assemble` signature rejects the new keyword — Stage D's Task 6 hit exactly this — add `notes=None` to that stand-in.)

- [ ] **Step 5: Changelog and commit**

`CHANGELOG.md` `[Unreleased]`:

```
- The author's notes the extractor reads reach the row: `assemble()` takes `notes` with the rule `description` has — a Paprika entry's own notes win, else the extracted ones, else null. Since 2026-09-07 they were extracted and dropped on every path but Paprika.
```

```bash
git add recipeparser/core/stages/assemble.py recipeparser/core/pipeline.py tests/unit/stages/test_assemble.py tests/unit/test_pipeline.py CHANGELOG.md
git commit -m "The extracted notes reach the row, a source's own statement winning" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: A 413 at 50 MB, with the sentence the client shows

Nothing limits an upload: the Caddyfile sets no `request_body`, and `submit_file_job` reads the whole body. The type check stays first (a 17-byte `.docx` is still a 422); then the size, from Starlette's `UploadFile.size` when the parser set it, else the body's length.

**Files:**
- Modify: `recipeparser/config.py` (`MAX_UPLOAD_BYTES`)
- Modify: `recipeparser/adapters/api.py` (`_too_large_sentence`, the check in `submit_file_job`)
- Test: `tests/test_api.py` (`TestPostJobsFile`)

**Interfaces:**
- Produces: `MAX_UPLOAD_BYTES = 50_000_000`; `_too_large_sentence(size: int) -> str`; `POST /jobs/file` answers 413 with that sentence as `detail`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_api.py`, class `TestPostJobsFile`:

```python
    def test_a_body_over_the_ceiling_is_a_413_with_the_sentence(self, client: TestClient) -> None:
        # The ceiling is patched down so the test does not build fifty megabytes; the sentence
        # is built from the same constant, so it names the patched number.
        with patch("recipeparser.adapters.api.MAX_UPLOAD_BYTES", 16):
            resp = self._upload(client, "big.pdf", b"%PDF-1.4" + b"\x00" * 9, "application/pdf")
        assert resp.status_code == 413
        assert resp.json()["detail"] == "This file is 0.0 MB. Cayenne takes files up to 0 MB."

    def test_the_type_is_refused_before_the_size(self, client: TestClient) -> None:
        with patch("recipeparser.adapters.api.MAX_UPLOAD_BYTES", 16):
            resp = self._upload(client, "menu.docx", b"\x00" * 17, "application/octet-stream")
        assert resp.status_code == 422
        assert resp.json()["detail"] == "Cayenne can't read .docx files yet."

    def test_a_body_at_the_ceiling_is_accepted(self, client: TestClient) -> None:
        with _patch_pipeline_and_writer()[0], \
             patch("recipeparser.io.readers.pdf.extract_text_from_pdf", return_value="pasta"), \
             patch("recipeparser.adapters.api.MAX_UPLOAD_BYTES", 16):
            resp = self._upload(client, "recipe.pdf", b"%PDF-1.4" + b"\x00" * 8, "application/pdf")
        assert resp.status_code == 202
```

- [ ] **Step 2: Run them to see them fail**

Run: `pytest -n0 tests/test_api.py -k "ceiling or before_the_size" -v`
Expected: FAIL — `MAX_UPLOAD_BYTES` does not exist on the module (patch raises `AttributeError`).

- [ ] **Step 3: Implement**

`recipeparser/config.py`, after `PDF_OCR_MAX_PAGES`:

```python
# The largest upload POST /jobs/file takes, refused with a 413 and a sentence. The same number
# sits in Cayenne's domain/ingestion.ts (MAX_UPLOAD_BYTES), where the intake refuses a file before
# the upload with the same sentence; change one, change both. Fifty megabytes is a phone photo
# (3–8), a cookbook EPUB with images (2–20) or a multi-page scan (10–20) with room, and the OCR
# cap above already bounds what a scan can cost.
MAX_UPLOAD_BYTES: int = 50_000_000
```

`recipeparser/adapters/api.py`: import it — change the config import line to

```python
from recipeparser.config import MAX_UPLOAD_BYTES, live_writes_blocked as _live_writes_blocked
```

Add, after `_WEBP_SENTENCE`:

```python
def _too_large_sentence(size: int) -> str:
    """The 413 detail: the client shows it verbatim, like the 422 sentences above."""
    return (
        f"This file is {size / 1_000_000:.1f} MB. "
        f"Cayenne takes files up to {MAX_UPLOAD_BYTES // 1_000_000} MB."
    )
```

In `submit_file_job`, replace `file_bytes = await file.read()` with:

```python
    # The ceiling (config.MAX_UPLOAD_BYTES), after the type check so a small .docx is still a 422.
    # Starlette's multipart parser sets `size`; when it did not, the body's length is the size.
    size = file.size
    file_bytes: Optional[bytes] = None
    if size is None:
        file_bytes = await file.read()
        size = len(file_bytes)
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=_too_large_sentence(size),
        )
    if file_bytes is None:
        file_bytes = await file.read()
```

- [ ] **Step 4: Run the API tests**

Run: `pytest -n0 tests/test_api.py -v`
Expected: PASS, the whole file.

- [ ] **Step 5: Changelog and commit**

`CHANGELOG.md` `[Unreleased]`:

```
- `POST /jobs/file` refuses a body over 50 MB (`config.MAX_UPLOAD_BYTES`) with a 413 whose `detail` is a sentence the client shows verbatim: "This file is 120.3 MB. Cayenne takes files up to 50 MB." The type check still runs first. Cayenne refuses the same size before the upload with the same sentence.
```

```bash
git add recipeparser/config.py recipeparser/adapters/api.py tests/test_api.py CHANGELOG.md
git commit -m "POST /jobs/file refuses a body over 50 MB with a 413 and a sentence" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: The lost Paprika entries, written into their own archive

Stage C's restore found 68 archive entries with no recipe row (the bulk import lost them) and only counted them; a re-import of the full 827-entry export would duplicate the other 759. This script writes the unmatched members — original bytes, so nothing is re-encoded — into `<archive stem>-unmatched.paprikarecipes` beside the archive, and prints their titles. The cook then drops that one file on Add Recipe. The matching rule is the restore script's (`normalise_title`, letters and digits only); an entry whose title matches no row is unmatched, whatever else shares its title.

**Files:**
- Create: `scripts/extract_unmatched_paprika.py`
- Create: `tests/unit/scripts/test_extract_unmatched_paprika.py`
- Modify: `docs/ingestion-repair/2026-09-12-stage-c/README.md` (how the 68 are recovered)

**Interfaces:**
- Consumes: `scripts.backfill_paprika_metadata.normalise_title(text) -> str`; `PaprikaReader().read_entries(path)` (the decoding rule this script mirrors, not calls — it needs the member names).
- Produces: `read_members(path) -> List[Tuple[str, bytes, Dict[str, Any]]]` (member name, raw bytes, decoded entry); `select_unmatched(members, rows) -> List[Tuple[str, bytes, Dict[str, Any]]]`; `write_archive(out_path, members) -> int` (members written).

- [ ] **Step 1: Write the failing tests**

`tests/unit/scripts/test_extract_unmatched_paprika.py`:

```python
import gzip
import json
import zipfile
from pathlib import Path

from recipeparser.io.readers.paprika import PaprikaReader
from scripts.extract_unmatched_paprika import read_members, select_unmatched, write_archive


def _archive(path: Path, entries: dict[str, dict]) -> Path:
    """A .paprikarecipes: a ZIP of gzip-compressed JSON members, as Paprika exports one."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, entry in entries.items():
            zf.writestr(name, gzip.compress(json.dumps(entry).encode("utf-8")))
    return path


def _row(i: int, title: str) -> dict:
    return {"id": f"r{i}", "title": title}


def test_read_members_keeps_the_name_and_the_bytes_beside_the_entry(tmp_path):
    src = _archive(tmp_path / "e.paprikarecipes", {"a.paprikarecipe": {"name": "Tomato Soup"}, "notes.txt": {}})
    members = read_members(src)
    assert [m[0] for m in members] == ["a.paprikarecipe"]
    assert members[0][2] == {"name": "Tomato Soup"}
    assert gzip.decompress(members[0][1]) == json.dumps({"name": "Tomato Soup"}).encode("utf-8")


def test_select_unmatched_uses_the_restore_rule():
    members = [
        ("a.paprikarecipe", b"a", {"name": "Tomato Soup"}),
        ("b.paprikarecipe", b"b", {"name": "Brown Butter—Braised Leeks"}),
        ("c.paprikarecipe", b"c", {"name": "Lost Cake"}),
        ("d.paprikarecipe", b"d", {"name": "Lost Cake"}),
        ("e.paprikarecipe", b"e", {"name": ""}),
    ]
    rows = [_row(0, "Tomato Soup"), _row(1, "brown butter braised leeks")]
    unmatched = select_unmatched(members, rows)
    # Both Lost Cake members are unmatched (no row shares the title); the blank-named one is skipped.
    assert [m[0] for m in unmatched] == ["c.paprikarecipe", "d.paprikarecipe"]


def test_write_archive_produces_a_paprikarecipes_the_reader_opens(tmp_path):
    src = _archive(tmp_path / "e.paprikarecipes", {"a.paprikarecipe": {"name": "Kept"}, "c.paprikarecipe": {"name": "Lost Cake"}})
    members = select_unmatched(read_members(src), [_row(0, "Kept")])
    out = tmp_path / "e-unmatched.paprikarecipes"
    assert write_archive(out, members) == 1
    assert [e["name"] for e in PaprikaReader().read_entries(out)] == ["Lost Cake"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `pytest -n0 tests/unit/scripts/test_extract_unmatched_paprika.py -v`
Expected: FAIL — no module `scripts.extract_unmatched_paprika`.

- [ ] **Step 3: Write the script**

`scripts/extract_unmatched_paprika.py`:

```python
"""
scripts/extract_unmatched_paprika.py — write the Paprika archive entries the bulk
import lost into a smaller archive, for one re-import through Add Recipe.

Stage C (2026-09-12) found 68 archive entries with no recipe row and only
counted them: `restore_source_urls.py` restores URLs and never creates a recipe.
Re-importing the whole export would duplicate the other 759. This writes the
unmatched members — the original gzip bytes, untouched — into
`<archive stem>-unmatched.paprikarecipes` beside the archive and prints their
titles; the cook drops that one file on Add Recipe.

    # dry run -- lists the titles, writes nothing
    python scripts/extract_unmatched_paprika.py --archive "/path/to/Export.paprikarecipes" --user-id <uuid>

    # write the archive
    python scripts/extract_unmatched_paprika.py --archive "/path/to/Export.paprikarecipes" --user-id <uuid> --write

Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the environment. The
matching rule is the restore script's: a title reduced to letters and digits.
An entry whose title matches no row is unmatched, whatever else shares its title.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backfill_paprika_metadata import normalise_title  # noqa: E402

PAGE = 500
Member = Tuple[str, bytes, Dict[str, Any]]


def _creds() -> Tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must both be set.")
    return url, key


def read_members(path: Path | str) -> List[Member]:
    """Every .paprikarecipe member: its name, its raw bytes, and the entry it decodes to.

    The decoding is PaprikaReader.read_entries's (gzip then JSON; plain JSON for the
    exporters that skip gzip); the reader itself drops the member name, which the
    output archive needs.
    """
    path = Path(path)
    if not zipfile.is_zipfile(path):
        raise ValueError(f"Not a valid ZIP archive: {path}")
    members: List[Member] = []
    with zipfile.ZipFile(path, "r") as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".paprikarecipe"):
                continue
            raw = zf.read(name)
            try:
                entry = json.loads(gzip.decompress(raw))
            except gzip.BadGzipFile:
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    print(f"  skipping {name!r}: not gzip or JSON")
                    continue
            except json.JSONDecodeError:
                print(f"  skipping {name!r}: JSON decode error")
                continue
            members.append((name, raw, entry))
    return members


def select_unmatched(members: Sequence[Member], rows: Sequence[Dict[str, Any]]) -> List[Member]:
    """The members whose title matches no row. A blank title matches nothing and is skipped."""
    rows_by_title: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_title[normalise_title(row.get("title"))].append(row)
    unmatched: List[Member] = []
    for name, raw, entry in members:
        key = normalise_title(entry.get("name"))
        if key == "":
            continue
        if not rows_by_title.get(key):
            unmatched.append((name, raw, entry))
    return unmatched


def write_archive(out_path: Path | str, members: Sequence[Member]) -> int:
    """A .paprikarecipes holding the members, bytes as they were. Returns how many."""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, raw, _entry in members:
            zf.writestr(name, raw)
    return len(members)


def main() -> int:
    ap = argparse.ArgumentParser(description="Write a Paprika archive's unmatched entries into their own archive.")
    ap.add_argument("--archive", required=True, help="The .paprikarecipes export.")
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--out", help="Output path; default <archive stem>-unmatched.paprikarecipes beside the archive.")
    ap.add_argument("--write", action="store_true", help="Actually write the archive. Omit to list only.")
    args = ap.parse_args()
    from supabase import create_client  # noqa: PLC0415
    sb = create_client(*_creds())

    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = (sb.table("recipes").select("id,title").eq("user_id", args.user_id)
                .order("id").range(offset, offset + PAGE - 1).execute().data or [])
        if not page:
            break
        rows.extend(page)
        offset += PAGE

    archive = Path(args.archive)
    members = read_members(archive)
    unmatched = select_unmatched(members, rows)
    for _name, _raw, entry in unmatched:
        print(f"  {entry.get('name')!r}")
    print(f"{len(members)} archive entries, {len(rows)} rows; {len(unmatched)} unmatched")
    if not args.write:
        print("LISTED ONLY — nothing was written. Re-run with --write to produce the archive.")
        return 0
    out = Path(args.out) if args.out else archive.with_name(f"{archive.stem}-unmatched.paprikarecipes")
    written = write_archive(out, unmatched)
    print(f"wrote {written} entries to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the script tests and the lint**

Run: `pytest -n0 tests/unit/scripts/test_extract_unmatched_paprika.py -v && ruff check scripts/extract_unmatched_paprika.py tests/unit/scripts/test_extract_unmatched_paprika.py`
Expected: PASS; no ruff finding. (`from __future__ import annotations` makes `Path | str` legal on 3.9+ signatures; the tree runs 3.11 and 3.12.)

- [ ] **Step 5: The repair README and the changelog**

`docs/ingestion-repair/2026-09-12-stage-c/README.md`: after the table row that mentions "68 unmatched (the parked lost entries)", add a paragraph:

```
**Recovering the 68 (2026-09-13).** `scripts/extract_unmatched_paprika.py --archive <export> --user-id <uuid> --write` writes those entries, bytes untouched, into `<export stem>-unmatched.paprikarecipes` beside the export; that one file goes through Add Recipe, and *Set source* afterwards if a site needs naming. Run it after the restore, against the same archive; the run's own output (titles and counts) is recorded here as `extract-unmatched.txt` when it is done.
```

`CHANGELOG.md` `[Unreleased]`, under a `### ✨ Added` heading for this pull request:

```
- `scripts/extract_unmatched_paprika.py`: the Paprika export's entries with no recipe row (Stage C counted 68), written into a small `.paprikarecipes` for one re-import through Add Recipe. The restore script's matching rule; the original member bytes.
```

- [ ] **Step 6: Commit**

```bash
git add scripts/extract_unmatched_paprika.py tests/unit/scripts/test_extract_unmatched_paprika.py docs/ingestion-repair/2026-09-12-stage-c/README.md CHANGELOG.md
git commit -m "A script writes the Paprika export's lost entries into their own archive for the intake" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: The gate

- [ ] **Step 1: Run the suites in the order the auth quirk needs**

```bash
pytest -n0 tests/test_api.py
pytest -n0 tests/unit
ruff check recipeparser/adapters/api.py recipeparser/config.py recipeparser/core/stages/assemble.py recipeparser/core/pipeline.py scripts/extract_unmatched_paprika.py tests/test_api.py tests/unit/stages/test_assemble.py tests/unit/scripts/test_extract_unmatched_paprika.py
```

Expected: both green (1055 was the Stage D count; expect that plus the eleven added here); ruff reports nothing new in the touched files (compare against `git stash`-free baseline by running the same command on `master` if any finding appears — the tree has pre-existing ones elsewhere).

- [ ] **Step 2: Nothing to commit**

The gate adds no file. Push once; open the pull request with the four changes, the 50 MB ruling, and the after-merge line: rebuild the container, then run the extract script against the live project and record its output in the repair folder.

---

## Rulings made in this plan (flag in review)

1. **50 MB, not 10** (the user proposed 10 on 2026-09-13). Measured on the development machine: cookbook EPUBs 2–4.3 MB, a single scanned PDF 11.4 MB, phone photos 3–8 MB; a multi-page scan at 300 dpi lands at 10–20. Ten would refuse a real scan. Fifty keeps the whole range and the OCR page cap bounds the cost. One constant per repo; the number moves together.
2. **The size is checked after the type**, so the 422 a cook already knows keeps its precedence and a small `.docx` is not told it is too large.
3. **`UploadFile.size` first, the body's length second.** Starlette sets `size` from the multipart parser; when it is `None` the body is read once and measured — the same read the endpoint made anyway.
4. **`notes` is not made `repr=False`.** It has been in the extraction schema and the repr from the start; the golden recordings already contain it. Only its destination changes.
5. **The unmatched selection ignores ambiguity.** `plan_restore` reports a title as ambiguous when it matches several rows or several entries; here a title with no row at all is unmatched however many members carry it, because every such member is a lost recipe. The two Lost Cake members in the test both come out.
6. **Not taken, recorded:** the CLI's second copy of the scanned-PDF detection and the serial jina/page-meta fetches (both parked on the Stage D plan) stay parked — neither changes a result, and each is a refactor with its own review surface.
