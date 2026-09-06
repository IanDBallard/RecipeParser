# Library Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the twenty recipes and 466 photographs the 2026-09-04 import lost, without disturbing anything edited since, and close the ledger.

**Architecture:** Three one-off scripts in a new `scripts/` directory in the `RecipeParser` repository — snapshot, diff, backfill — plus a build step that produces a trimmed archive for re-import through the fixed API. Nothing here ships as a product feature; the scripts exist to be run once, reviewed, and kept for the next time an import goes wrong.

**Tech Stack:** Python 3.11+, httpx against the Supabase REST and Storage APIs, the repository's own `PaprikaReader` and `SupabaseImageStore`, pytest.

**Spec:** `docs/superpowers/specs/2026-09-05-ingestion-repair-design.md` in the **Cayenne** repository. Read section 6 before starting.

**Prerequisites — both must be true before Task 4 runs:**
1. `docs/superpowers/plans/2026-09-05-ingestion-api-fixes.md` is merged and the fixed API is deployed and reachable.
2. `docs/superpowers/plans/2026-09-05-cayenne-skip-reporting.md`'s migrations 009 and 010 are applied to the Supabase project.

**Source of truth:** `C:\Users\iball\My Drive\Cooking Stuff and Restaurants\Export 2026-03-23 20.31.13 Mains, Simple Dinner.paprikarecipes` — 827 entries, 466 carrying `photo_data`, no loose image files. Verified 2026-09-05.

## Global Constraints

- **Every script defaults to a dry run.** Writing requires `--live` on the command line. There is one database and these operations are irreversible; the repository's own test suite has written phantom rows into this project before (fixed in `667f5ef`), so the guard belongs in the code, not in the operator's care.
- **No script infers its target.** Archive path and user id are arguments. A script that guesses which library to rewrite is a script that rewrites the wrong one.
- Scripts read `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` from the environment — the canonical name after Task 8 of the fixes plan.
- Scripts live in `RecipeParser/scripts/`, a new directory. They import from `recipeparser` rather than re-implementing archive parsing or uploads.
- Every script prints a summary line whether or not `--live` was passed, so a dry run and a real run are comparable.

---

### Task 1: Snapshot before anything is written

**Files:**
- Create: `scripts/__init__.py` (empty, so the scripts are importable by tests)
- Create: `scripts/supabase_rest.py`
- Create: `scripts/snapshot_recipes.py`
- Test: `tests/unit/scripts/test_supabase_rest.py` (create), plus `tests/unit/scripts/__init__.py`

**Interfaces:**
- Produces:
  - `scripts.supabase_rest.RestClient(url, key)` with `.select(table, params) -> list[dict]` (paged), `.patch(table, match, payload) -> int`, `.insert(table, rows) -> int`.
  - `scripts.snapshot_recipes.main(argv) -> int`, writing `recipes-<user_id>-<UTC timestamp>.json`.

**Why a REST client and not supabase-py.** These scripts page through a few thousand rows and patch single columns; PostgREST does that directly, and a thin client is far easier to fake in a test than the supabase SDK's fluent chain. The writer at `recipeparser/io/writers/supabase.py:146` already talks to PostgREST with httpx, so this follows the house pattern.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/scripts/__init__.py` (empty) and `tests/unit/scripts/test_supabase_rest.py`:

```python
"""The thin PostgREST client the repair scripts share. No network."""
from __future__ import annotations

import httpx
import pytest

from scripts.supabase_rest import RestClient


def _client(handler) -> RestClient:
    transport = httpx.MockTransport(handler)
    client = RestClient("https://project.test", "service-key")
    client._http = httpx.Client(transport=transport)
    return client


def test_select_pages_until_a_short_page():
    pages = [
        [{"id": str(i)} for i in range(1000)],
        [{"id": "1000"}],
    ]
    seen_ranges = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_ranges.append(request.headers.get("Range"))
        return httpx.Response(200, json=pages[len(seen_ranges) - 1])

    rows = _client(handler).select("recipes", {"select": "id"})

    assert len(rows) == 1001
    assert seen_ranges == ["0-999", "1000-1999"]


def test_select_sends_the_service_key_as_both_headers():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json=[])

    _client(handler).select("recipes", {"select": "id"})

    assert captured["apikey"] == "service-key"
    assert captured["authorization"] == "Bearer service-key"


def test_patch_raises_on_an_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    with pytest.raises(RuntimeError, match="bad request"):
        _client(handler).patch("recipes", {"id": "eq.abc"}, {"image_url": "x"})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/scripts -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts'`.

- [ ] **Step 3: Write the REST client**

Create `scripts/__init__.py` (empty) and `scripts/supabase_rest.py`:

```python
"""A thin PostgREST client for the one-off repair scripts.

Not the supabase SDK: these scripts page through thousands of rows and patch
single columns, which PostgREST does directly, and a small client is far easier
to fake in a test than a fluent chain. Follows the pattern the Supabase writer
already uses at recipeparser/io/writers/supabase.py.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx

PAGE = 1000


class RestClient:
    def __init__(self, url: Optional[str] = None, key: Optional[str] = None) -> None:
        self.url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self.key = key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not self.url or not self.key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")
        self._http = httpx.Client(timeout=60.0)

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    def select(self, table: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
        """Read every matching row, a page at a time."""
        rows: List[Dict[str, Any]] = []
        offset = 0
        while True:
            headers = dict(self._headers)
            headers["Range"] = f"{offset}-{offset + PAGE - 1}"
            response = self._http.get(f"{self.url}/rest/v1/{table}", params=params, headers=headers)
            self._raise_for_status(response)
            page = response.json()
            rows.extend(page)
            if len(page) < PAGE:
                return rows
            offset += PAGE

    def patch(self, table: str, match: Dict[str, str], payload: Dict[str, Any]) -> int:
        headers = dict(self._headers)
        headers["Prefer"] = "return=minimal"
        response = self._http.patch(
            f"{self.url}/rest/v1/{table}", params=match, json=payload, headers=headers
        )
        self._raise_for_status(response)
        return 1

    def insert(self, table: str, rows: List[Dict[str, Any]]) -> int:
        headers = dict(self._headers)
        headers["Prefer"] = "return=minimal"
        response = self._http.post(f"{self.url}/rest/v1/{table}", json=rows, headers=headers)
        self._raise_for_status(response)
        return len(rows)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise RuntimeError(f"{response.status_code}: {response.text}")
```

- [ ] **Step 4: Write the snapshot script**

Create `scripts/snapshot_recipes.py`:

```python
"""Dump a user's recipes to a JSON file before any repair writes to them.

The repair is irreversible and there is one database. This runs first, always,
and writes outside both repositories so a stray `git clean` cannot take it.

    python -m scripts.snapshot_recipes --user <uuid> [--out DIR]
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import List, Optional

from scripts.supabase_rest import RestClient


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Snapshot a user's recipes before a repair.")
    parser.add_argument("--user", required=True, help="The owning user's UUID.")
    parser.add_argument("--out", default=str(Path.home() / "cayenne-snapshots"), help="Directory for the dump.")
    args = parser.parse_args(argv)

    client = RestClient()
    rows = client.select("recipes", {"select": "*", "user_id": f"eq.{args.user}"})

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"recipes-{args.user}-{stamp}.json"
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    print(f"Snapshot: {len(rows)} recipe row(s) → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

There is no `--live` here: the script only reads.

- [ ] **Step 5: Run the tests**

Run: `pytest tests/unit/scripts -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/ tests/unit/scripts/
git commit -m "feat(scripts): a PostgREST client and a snapshot, before any repair writes

The repair rewrites live rows in the one database this project has, so the
first thing that runs is a dump of what is there. It writes outside both
repositories, where a stray clean cannot take it.

The client is deliberately not the supabase SDK: the scripts page through
thousands of rows and patch single columns, which PostgREST does directly, and
a small client is far easier to fake in a test than a fluent chain.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Find what is missing, and say what is ambiguous

**Files:**
- Create: `scripts/archive_diff.py`
- Test: `tests/unit/scripts/test_archive_diff.py` (create)

**Interfaces:**
- Consumes: `RestClient` (Task 1); `PaprikaReader.read_entries` from the repository.
- Produces:
  - `scripts.archive_diff.normalise_title(title: str) -> str`
  - `scripts.archive_diff.diff(archive_titles: list[str], row_titles: list[str]) -> DiffResult`, a dataclass with `.matched: list[str]`, `.missing: list[str]`, `.ambiguous: list[tuple[str, str]]` (title, why)
  - `scripts.archive_diff.main(argv) -> int`, which prints the three lists and writes `missing.json`
- Task 3 consumes `missing.json`.

**Why ambiguity is reported and never resolved.** A recipe renamed in the app since the import reads as missing and would be imported a second time. There is no way for a script to tell a rename from a loss. So the script reports and the human decides; that is the approval point in spec 6.3 step 4.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/scripts/test_archive_diff.py`:

```python
"""Title matching for the repair diff (spec 6.2). Pure functions, no network."""
from __future__ import annotations

from scripts.archive_diff import diff, normalise_title


def test_normalisation_folds_case_whitespace_and_punctuation():
    assert normalise_title("  Sticky   Toffee Pudding!  ") == "sticky toffee pudding"
    assert normalise_title("Mum's Pie") == normalise_title("Mums Pie")
    assert normalise_title("Soup — Tomato") == normalise_title("soup tomato")


def test_a_title_present_on_both_sides_is_matched():
    result = diff(["Sticky Toffee Pudding"], ["sticky toffee pudding"])

    assert result.matched == ["Sticky Toffee Pudding"]
    assert result.missing == []
    assert result.ambiguous == []


def test_a_title_absent_from_the_library_is_missing():
    result = diff(["Lost Recipe"], ["Something Else"])

    assert result.missing == ["Lost Recipe"]


def test_a_title_duplicated_in_the_archive_is_ambiguous_not_missing():
    result = diff(["Pancakes", "Pancakes"], [])

    assert result.missing == []
    assert [t for t, _why in result.ambiguous] == ["Pancakes"]
    assert "twice in the archive" in result.ambiguous[0][1]


def test_a_title_matching_several_rows_is_ambiguous():
    result = diff(["Pancakes"], ["Pancakes", "pancakes"])

    assert result.missing == []
    assert "2 rows" in result.ambiguous[0][1]


def test_an_untitled_archive_entry_is_ambiguous():
    result = diff([""], [])

    assert result.missing == []
    assert result.ambiguous[0][1].startswith("no title")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/scripts/test_archive_diff.py -v`
Expected: FAIL — no module `scripts.archive_diff`.

- [ ] **Step 3: Write the diff**

Create `scripts/archive_diff.py`:

```python
"""Match an archive's entries against a live library, and report what does not match.

Nothing here guesses. A recipe renamed in the app since the import is
indistinguishable from one that was lost, so anything less than a clean
one-to-one match is reported for a human to rule on.

    python -m scripts.archive_diff --archive PATH --user <uuid> [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from recipeparser.io.readers.paprika import PaprikaReader
from scripts.supabase_rest import RestClient

_PUNCTUATION = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalise_title(title: str) -> str:
    """Case-folded, punctuation-stripped, single-spaced.

    Unicode is normalised first so a curly apostrophe and a straight one, or a
    combining accent and a precomposed one, compare equal — Paprika exports and
    hand-typed edits differ in exactly those ways.
    """
    decomposed = unicodedata.normalize("NFKD", title or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    no_punctuation = _PUNCTUATION.sub(" ", stripped)
    return _WHITESPACE.sub(" ", no_punctuation).strip().lower()


@dataclass
class DiffResult:
    matched: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    ambiguous: List[Tuple[str, str]] = field(default_factory=list)


def diff(archive_titles: List[str], row_titles: List[str]) -> DiffResult:
    """Compare by normalised title. Anything not one-to-one is ambiguous."""
    result = DiffResult()
    rows: Counter = Counter(normalise_title(t) for t in row_titles)
    archive_counts: Counter = Counter(normalise_title(t) for t in archive_titles)
    reported_ambiguous: set = set()

    for title in archive_titles:
        key = normalise_title(title)
        if not key:
            result.ambiguous.append((title, "no title in the archive entry"))
            continue
        if archive_counts[key] > 1:
            if key not in reported_ambiguous:
                reported_ambiguous.add(key)
                result.ambiguous.append((title, f"appears {archive_counts[key]} times in the archive"))
            continue
        found = rows[key]
        if found == 0:
            result.missing.append(title)
        elif found == 1:
            result.matched.append(title)
        else:
            result.ambiguous.append((title, f"matches {found} rows in the library"))
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Diff a Paprika archive against a live library.")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--out", default=".")
    args = parser.parse_args(argv)

    entries = PaprikaReader().read_entries(args.archive)
    archive_titles = [str(e.get("name") or "") for e in entries]

    rows = RestClient().select("recipes", {"select": "title", "user_id": f"eq.{args.user}"})
    row_titles = [str(r.get("title") or "") for r in rows]

    result = diff(archive_titles, row_titles)

    print(f"Archive: {len(archive_titles)} entries.  Library: {len(row_titles)} rows.")
    print(f"Matched:   {len(result.matched)}")
    print(f"Missing:   {len(result.missing)}")
    for title in result.missing:
        print(f"  - {title}")
    print(f"Ambiguous: {len(result.ambiguous)}  (reported, never guessed at)")
    for title, why in result.ambiguous:
        print(f"  ? {title}  — {why}")

    out = Path(args.out) / "missing.json"
    out.write_text(json.dumps(result.missing, indent=2), encoding="utf-8")
    print(f"\nWrote {out}. Review it before Task 3 turns it into an import.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/scripts/test_archive_diff.py -v`
Expected: PASS, all six.

- [ ] **Step 5: Commit**

```bash
git add scripts/archive_diff.py tests/unit/scripts/test_archive_diff.py
git commit -m "feat(scripts): diff an archive against a live library without guessing

Finding the recipes an import lost is title matching, and title matching is
where a repair goes wrong: a recipe renamed in the app since the import looks
exactly like one that was lost, and importing it again would duplicate it.

So the script reports three lists rather than two. Anything that is not a clean
one-to-one match - duplicated in the archive, matching several rows, or
untitled - lands in the ambiguous list for a person to rule on. Normalisation
folds case, punctuation and unicode form, because Paprika exports and hand
edits differ in exactly those ways.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Build the trimmed archive

**Files:**
- Create: `scripts/build_trimmed_archive.py`
- Test: `tests/unit/scripts/test_build_trimmed_archive.py` (create)

**Interfaces:**
- Consumes: `normalise_title` (Task 2); `missing.json`.
- Produces: `scripts.build_trimmed_archive.main(argv) -> int`, writing a `.paprikarecipes` archive containing only the named entries.

**Why an archive rather than direct writes.** The twenty lost recipes go back in through the fixed API as a real import, which is what makes the repair double as the acceptance test for the retry, the photo path, incremental writes, progress and the skip count. That needs a file the API's `/jobs/file` endpoint will accept: a zip of gzipped `.paprikarecipe` members, the format `PaprikaReader.read_entries` reads at `paprika.py:161-175`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/scripts/test_build_trimmed_archive.py`:

```python
"""The trimmed archive must be readable by the same reader the API uses."""
from __future__ import annotations

import gzip
import json
import zipfile
from pathlib import Path

from recipeparser.io.readers.paprika import PaprikaReader
from scripts.build_trimmed_archive import build


def _archive(path: Path, entries: list[dict]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for i, entry in enumerate(entries):
            zf.writestr(f"entry-{i}.paprikarecipe", gzip.compress(json.dumps(entry).encode("utf-8")))
    return path


def test_only_the_named_entries_survive_and_stay_readable(tmp_path):
    source = _archive(tmp_path / "all.paprikarecipes", [
        {"name": "Keep Me", "ingredients": "1 cup x", "directions": "Cook."},
        {"name": "Leave Me", "ingredients": "2 cups y", "directions": "Wait."},
    ])
    out = tmp_path / "trimmed.paprikarecipes"

    count = build(str(source), ["Keep Me"], str(out))

    assert count == 1
    entries = PaprikaReader().read_entries(str(out))
    assert [e["name"] for e in entries] == ["Keep Me"]


def test_a_name_that_matches_nothing_is_reported(tmp_path):
    source = _archive(tmp_path / "all.paprikarecipes", [{"name": "Only One", "ingredients": "x", "directions": "y"}])
    out = tmp_path / "trimmed.paprikarecipes"

    count = build(str(source), ["Only One", "Not Here"], str(out))

    assert count == 1  # the missing name is skipped, and the caller is told


def test_matching_is_normalised_like_the_diff(tmp_path):
    source = _archive(tmp_path / "all.paprikarecipes", [{"name": "Mum's  Pie!", "ingredients": "x", "directions": "y"}])
    out = tmp_path / "trimmed.paprikarecipes"

    assert build(str(source), ["mums pie"], str(out)) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/scripts/test_build_trimmed_archive.py -v`
Expected: FAIL — no module `scripts.build_trimmed_archive`.

- [ ] **Step 3: Write it**

Create `scripts/build_trimmed_archive.py`:

```python
"""Produce a .paprikarecipes archive holding only the named entries.

The lost recipes go back in through the fixed API as a real /jobs/file import,
so the repair also serves as the acceptance test for the retry, the photo path,
incremental writes, progress and the skip count. That needs a file in the same
format the reader consumes.

    python -m scripts.build_trimmed_archive --archive PATH --names missing.json --out trimmed.paprikarecipes
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import zipfile
from typing import List, Optional

from scripts.archive_diff import normalise_title


def build(archive: str, names: List[str], out: str) -> int:
    """Copy the entries whose names normalise to one of *names*. Returns the count."""
    wanted = {normalise_title(n) for n in names}
    written = 0
    with zipfile.ZipFile(archive, "r") as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for member in src.namelist():
            if not member.lower().endswith(".paprikarecipe"):
                continue
            raw = src.read(member)
            try:
                entry = json.loads(gzip.decompress(raw))
            except Exception:
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
            if normalise_title(str(entry.get("name") or "")) in wanted:
                # Re-compress rather than copying the member: the source may or may
                # not be gzipped, and the reader tries gzip first.
                dst.writestr(member, gzip.compress(json.dumps(entry).encode("utf-8")))
                written += 1
    return written


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Trim a Paprika archive to a named subset.")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--names", required=True, help="JSON file holding a list of titles (missing.json).")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    names = json.loads(open(args.names, encoding="utf-8").read())
    written = build(args.archive, names, args.out)

    print(f"Asked for {len(names)} entr{'y' if len(names) == 1 else 'ies'}; wrote {written} to {args.out}.")
    if written != len(names):
        print("WARNING: the counts differ. Some requested titles were not found in the archive.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/scripts -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/build_trimmed_archive.py tests/unit/scripts/test_build_trimmed_archive.py
git commit -m "feat(scripts): trim an archive to the entries a repair needs to re-import

The lost recipes go back in through the fixed API as a real file import rather
than being written directly, so the repair doubles as the end-to-end test of
the retry, the photo path, incremental writes, progress and the skip count.
That needs a file in the format the reader consumes, holding only the entries a
human approved.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Backfill the photographs

**Files:**
- Create: `scripts/backfill_photos.py`
- Test: `tests/unit/scripts/test_backfill_photos.py` (create)

**Interfaces:**
- Consumes: `RestClient` (Task 1), `normalise_title` (Task 2), `_decode_photo` and `SupabaseImageStore` from the fixes plan (Tasks 4 of that plan).
- Produces: `scripts.backfill_photos.plan_backfill(entries, rows) -> list[Backfill]` and `main(argv) -> int`.

**Why only null `image_url` rows.** The recipes restored in step 5 of the runbook arrive through the fixed pipeline with their photographs already attached. Filtering on null makes the script idempotent and keeps it from overwriting anything — including a picture the user set themselves.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/scripts/test_backfill_photos.py`:

```python
"""Which rows the photo backfill would touch (spec 6.3 step 6). Pure planning, no uploads."""
from __future__ import annotations

import base64

from scripts.backfill_photos import plan_backfill

JPEG = b"\xff\xd8\xff\xe0 bytes"
B64 = base64.b64encode(JPEG).decode("ascii")


def _entry(name: str, with_photo: bool = True) -> dict:
    entry = {"name": name, "photo": "pg_1.jpg"}
    if with_photo:
        entry["photo_data"] = B64
    return entry


def test_a_photographed_entry_whose_row_has_no_image_is_backfilled():
    plans = plan_backfill([_entry("Radish Slivers")], [{"id": "r1", "title": "Radish Slivers", "image_url": None}])

    assert len(plans) == 1
    assert plans[0].recipe_id == "r1"
    assert plans[0].image_bytes == JPEG
    assert plans[0].content_type == "image/jpeg"


def test_a_row_that_already_has_an_image_is_left_alone():
    """Idempotence, and it must never overwrite a picture the user chose."""
    plans = plan_backfill(
        [_entry("Radish Slivers")],
        [{"id": "r1", "title": "Radish Slivers", "image_url": "https://already/there.jpg"}],
    )

    assert plans == []


def test_an_entry_with_no_photo_is_skipped():
    plans = plan_backfill([_entry("Plain", with_photo=False)], [{"id": "r1", "title": "Plain", "image_url": None}])

    assert plans == []


def test_an_entry_with_no_matching_row_is_skipped():
    plans = plan_backfill([_entry("Orphan")], [{"id": "r1", "title": "Something Else", "image_url": None}])

    assert plans == []


def test_an_ambiguous_title_is_skipped_rather_than_guessed():
    plans = plan_backfill(
        [_entry("Pancakes")],
        [{"id": "r1", "title": "Pancakes", "image_url": None}, {"id": "r2", "title": "pancakes", "image_url": None}],
    )

    assert plans == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/scripts/test_backfill_photos.py -v`
Expected: FAIL — no module `scripts.backfill_photos`.

- [ ] **Step 3: Write it**

Create `scripts/backfill_photos.py`:

```python
"""Upload the photographs the 2026-09-04 import read and dropped.

466 of the archive's 827 entries carry an embedded photo; none reached the
library, because the pipeline had no path from a chunk's bytes to storage.
That path now exists, so this walks the archive, matches each photographed
entry to its row, and fills in the picture.

Only rows whose image_url is null are touched. That makes the script
idempotent, leaves the recipes re-imported through the fixed API alone - they
arrive with their photographs already - and cannot overwrite a picture the user
chose.

    python -m scripts.backfill_photos --archive PATH --user <uuid> [--live]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from recipeparser.io.readers.paprika import PaprikaReader, _decode_photo
from recipeparser.io.writers.image_store import SupabaseImageStore
from scripts.archive_diff import normalise_title
from scripts.supabase_rest import RestClient


@dataclass
class Backfill:
    recipe_id: str
    title: str
    image_bytes: bytes
    content_type: str


def plan_backfill(entries: List[Dict[str, Any]], rows: List[Dict[str, Any]]) -> List[Backfill]:
    """Decide which rows to patch. Pure: no network, no uploads."""
    by_title: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_title.setdefault(normalise_title(str(row.get("title") or "")), []).append(row)

    plans: List[Backfill] = []
    for entry in entries:
        image_bytes, content_type = _decode_photo(entry)
        if not image_bytes:
            continue
        candidates = by_title.get(normalise_title(str(entry.get("name") or "")), [])
        if len(candidates) != 1:
            continue  # no match, or ambiguous — never guessed at
        row = candidates[0]
        if row.get("image_url"):
            continue
        plans.append(Backfill(str(row["id"]), str(row.get("title") or ""), image_bytes, content_type))
    return plans


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill recipe photographs from a Paprika archive.")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--live", action="store_true", help="Actually upload and patch. Without it, nothing is written.")
    args = parser.parse_args(argv)

    client = RestClient()
    entries = PaprikaReader().read_entries(args.archive)
    rows = client.select("recipes", {"select": "id,title,image_url", "user_id": f"eq.{args.user}"})
    plans = plan_backfill(entries, rows)

    photographed = sum(1 for e in entries if _decode_photo(e)[0])
    print(f"Archive: {len(entries)} entries, {photographed} with a photograph.")
    print(f"Library: {len(rows)} rows.")
    print(f"Would backfill: {len(plans)}.")

    if not args.live:
        for plan in plans[:20]:
            print(f"  dry-run: {plan.title} → {len(plan.image_bytes)} bytes")
        if len(plans) > 20:
            print(f"  … and {len(plans) - 20} more")
        print("\nDry run. Pass --live to upload and patch.")
        return 0

    store = SupabaseImageStore()
    outcomes: Counter = Counter()
    for plan in plans:
        url = store.put(plan.image_bytes, plan.recipe_id, plan.content_type)
        if url is None:
            outcomes["upload failed"] += 1
            print(f"  FAILED upload: {plan.title}")
            continue
        try:
            client.patch("recipes", {"id": f"eq.{plan.recipe_id}"}, {"image_url": url})
        except Exception as exc:
            outcomes["patch failed"] += 1
            print(f"  FAILED patch: {plan.title} — {exc}")
            continue
        outcomes["done"] += 1

    print(f"\nBackfilled {outcomes['done']} of {len(plans)}. "
          f"Upload failures: {outcomes['upload failed']}. Patch failures: {outcomes['patch failed']}.")
    return 0 if outcomes["done"] == len(plans) else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/scripts -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_photos.py tests/unit/scripts/test_backfill_photos.py
git commit -m "feat(scripts): backfill the photographs the import read and dropped

466 of the archive's 827 entries carry an embedded photo and none reached the
library: the reader attached the bytes to each chunk and nothing downstream
ever read the field. With that path built, this fills in what was lost.

Only rows whose image_url is null are touched, which makes the script
idempotent, leaves the recipes re-imported through the fixed API alone since
they arrive with their pictures, and means it can never overwrite one the user
chose. A title that matches no row, or more than one, is skipped rather than
guessed at - the same rule the diff follows.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Run the repair

Not a code task. Work through it in order, and stop at step 4 for the user's ruling.

- [ ] **Step 1: Confirm the prerequisites**

The fixes PR is merged and the API is running with that code; migrations 009 and 010 are applied. Check the API is the fixed one:

Run: `curl -s http://localhost:8000/health`
Expected: a 200 naming the auth mode. Then confirm an unauthenticated `GET /jobs/does-not-exist` returns **401**, not 404 — that is the signature of Task 7 of the fixes plan being live.

- [ ] **Step 2: Snapshot**

```bash
python -m scripts.snapshot_recipes --user <uuid>
```

Note the row count it prints. Keep the path.

- [ ] **Step 3: Diff**

```bash
python -m scripts.archive_diff \
  --archive "C:\Users\iball\My Drive\Cooking Stuff and Restaurants\Export 2026-03-23 20.31.13 Mains, Simple Dinner.paprikarecipes" \
  --user <uuid>
```

Expected shape, from the 2026-09-04 audit: roughly 807 matched, roughly 20 missing, a small ambiguous list. A missing count far from twenty means something else is going on — stop and work out what before writing anything.

- [ ] **Step 4: The user rules on the lists**

Present `missing.json` and the ambiguous list. The user confirms which titles are genuinely lost and which are renames. Edit `missing.json` to match their ruling. **Do not proceed without this.**

- [ ] **Step 5: Re-import the approved entries**

```bash
python -m scripts.build_trimmed_archive \
  --archive "…Mains, Simple Dinner.paprikarecipes" \
  --names missing.json --out trimmed.paprikarecipes

curl -X POST http://localhost:8000/jobs/file \
  -H "Authorization: Bearer <a real user JWT>" \
  -F "file=@trimmed.paprikarecipes"
```

Then watch the job row. This is the acceptance test, so check all of it:

- `progress_pct` moves through values other than 0 and 100.
- `recipe_count` matches the approved list, or `skipped_count` is non-zero and `skipped` names what did not make it.
- The recipes appear in the app **while the job is still running**, not only at the end — that is incremental writes working.
- Each restored recipe has an `image_url`.
- The app's banner reads as a clean import if nothing was skipped, and as a warning naming the entries if something was.

- [ ] **Step 6: Backfill the photographs, dry run first**

```bash
python -m scripts.backfill_photos --archive "…Mains, Simple Dinner.paprikarecipes" --user <uuid>
```

Expected: "Would backfill" close to 466 minus however many the re-import already covered. Read the sample lines. Then:

```bash
python -m scripts.backfill_photos --archive "…" --user <uuid> --live
```

- [ ] **Step 7: Verify**

Run these three checks in the Supabase SQL editor:

```sql
-- No archive recipe still missing: expect 0 after the user's ruling is applied.
select count(*) from recipes where user_id = '<uuid>';

-- No photographed entry left without a picture: expect a number you can account for.
select count(*) from recipes where user_id = '<uuid>' and image_url is null;

-- No zero amounts anywhere: expect 0.
select count(*) from recipes r
where exists (
  select 1 from jsonb_array_elements(r.structured_ingredients) e
  where e->>'amount' is not null and (e->>'amount')::numeric = 0
);
```

Open the app, confirm the restored recipes have pictures, and open one of the previously "0 Kosher salt" recipes to confirm the line now reads as a name alone.

---

### Task 6: File the issues and close the ledger

**Files:**
- Modify: `SpecificationDocumentation/EXECUTION_PLAN.md:30-35` (the checklist) and the "The ingestion API" bullets under "What is deferred"

- [ ] **Step 1: File the seven issues**

One per finding, on `IanDBallard/RecipeParser`. Titles:

1. Silent chunk drops: a truncated extraction reply loses its recipes
2. Nothing is written until every chunk finishes
3. Job progress is only ever 0 or 100
4. Embedded photographs are read and dropped
5. Job status and control endpoints are unauthenticated
6. The service key is read under two different variable names
7. An unquantified ingredient is stored as a zero

Each body: the finding as recorded in the ledger, the fix as built, and a link to the fixes PR.

```bash
gh issue create --repo IanDBallard/RecipeParser --title "<title>" --body "<body>"
```

- [ ] **Step 2: Link them from the ledger**

In `SpecificationDocumentation/EXECUTION_PLAN.md`, append ` ([#N](https://github.com/IanDBallard/RecipeParser/issues/N))` to each of the seven bullets under **The ingestion API**.

- [ ] **Step 3: Tick what is done**

- The cut-over checklist item for RecipeParser CORS and bearer verification: closed by `667f5ef`, never ticked.
- The two client-side import items — visibility outside `/import`, and cancellation — stay on the list, annotated as phase 9b inputs.
- Mark the seven ingestion bullets done as their issues close.

- [ ] **Step 4: Commit**

```bash
git add SpecificationDocumentation/EXECUTION_PLAN.md
git commit -m "docs: link the ingestion findings to their issues and close what is done

The seven findings were filed into this ledger rather than a tracker, so
nothing linked out and nothing marked them closed. Each bullet now names its
issue. The CORS and bearer-verification checklist item is ticked - 667f5ef
closed it and the box was never marked - and the two client-side import items
stay open, annotated as phase 9b inputs.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```
