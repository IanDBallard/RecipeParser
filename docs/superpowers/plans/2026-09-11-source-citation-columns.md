# Source Citation Columns Implementation Plan (RecipeParser)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** RecipeParser writes four citation columns (`source_kind`, `source_key`, `source_title`, `source_author`) on every recipe it inserts, derived deterministically wherever a reader knows the answer, and ships the two read-only-by-default scripts that repair the 788 recipes already in the library.

**Architecture:** One pure module, `recipeparser/core/citation.py`, owns the `Citation` value, the key normalisation, the per-medium constructors and the seven ordered classification rules; nothing else derives a citation. Readers attach a `Citation` to the `Chunk` when they know it (a book's metadata, a URL's host, a Paprika entry's source); the extraction model is asked only for what the text states (a publication name, a byline); `assemble()` resolves the two, reader first. The writer sends the four columns; two scripts under `scripts/` apply the same rules to the live rows, dry run by default.

**Tech Stack:** Python 3.9, Pydantic v2, `google-genai` via `_call_with_retry`, `httpx`, pytest, syrupy snapshots, `supabase-py` in the scripts.

**Spec:** `docs/superpowers/specs/2026-09-11-recipe-source-citation-design.md` (copied into this repo in Task 1 from the Cayenne repository, where it was written; sections *The columns*, *What the API writes, per medium*, *The backfill, per measured value*, *Touchpoints*, *Testing*). Its stitching document is Cayenne's `docs/superpowers/plans/2026-09-11-add-recipe-workstream.md`, Stage A. The Cayenne twin of this plan is `docs/superpowers/plans/2026-09-11-source-citation-client.md` in that repository.

## Global Constraints

- **Branch:** `feat/source-citation-columns`, worktree `.claude/worktrees/feat+source-citation-columns`, off `master` at `bee2d73`. The Cayenne twin is `feat/source-citation` in the Cayenne repository. **Both pull requests open together, are reviewed together, and merge Cayenne first:** the migration deploys on Cayenne's merge (Supabase Branching), and a writer that sends `source_kind` to a database without the column gets a PostgREST 400 on every insert. Then this branch merges and the RecipeParser container is redeployed.
- Python 3.9 syntax only: `Optional[X]`, `List[X]`, `Dict[K, V]`, `Tuple[...]` from `typing`; no `X | Y` in annotations, no `match`. `str.removeprefix` is 3.9 and allowed.
- `recipeparser/core/**` must not import from `recipeparser.io` or `recipeparser.adapters` (ruff TID rule). `ruff check recipeparser scripts tests` before every commit.
- Tests never reach a real Supabase project: `recipeparser.config.live_writes_blocked()` refuses writes under pytest unless `ALLOW_LIVE_WRITES_IN_TESTS=1`. A test that exercises the writer sets that variable **and** patches `httpx.post`. The scripts' pure planning functions take rows as lists of dicts and never open a client.
- **The extraction schema is Gemini-facing.** Adding a field to `RecipeExtraction` changes `response_json_schema` and the prompt text, so `tests/goldens/test_prompts_snapshot.py` changes. Re-approve with `pytest tests/goldens tests/snapshots --snapshot-update -q`, review the `.ambr` diff, and stage `tests/goldens/__snapshots__` alongside the code. Recorded replies are keyed by prompt body; a changed prompt warns (`prompt_sha256 mismatch`) and still serves.
- **Reader goldens** (`tests/goldens/readers/*.json`) record `Chunk.source_url`. Task 4 changes what the book readers put there, so those files are rewritten with `pytest tests/goldens/test_readers_golden.py --update-goldens` and the diff is reviewed: the only expected change is `source_url` going from a `"Title — Author"` string to `null` on EPUB and PDF chunks.
- Every prompt is a `build_*_prompt(...)` function, never an f-string at the call site.
- `source` (free text) is unchanged in meaning: it stays the display string. **Ruling for this plan:** on a fresh insert the writer fills `source` with the citation's display form (`"Title — Author"` for a book, the title for a site or a person) when nothing else supplied it, so the library row, which reads `source`, shows the same thing for a recipe ingested tomorrow as for one the backfill repaired today. Paprika's own `source` still wins.
- `og:site_name` and JSON-LD publisher extraction are **not** in this plan. The r.jina.ai markdown the URL path reads does not carry the page's `<meta>` tags reliably; the site name comes from the model's `stated_source`, else the host. Recorded here so it is a known gap, not a forgotten one.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `docs/superpowers/specs/2026-09-11-recipe-source-citation-design.md` | The design, byte-identical to Cayenne's copy | **Create** (copy) |
| `docs/superpowers/specs/2026-09-07-recipe-edit-philosophy-design.md` | §3.1 lists the four columns as user-owned metadata | Modify (identical edit in both repositories) |
| `recipeparser/core/citation.py` | `Citation`, `normalise_key`, `host_of`, `book_citation`, `web_citation`, `classify_source`, `resolve_citation` | **Create** |
| `tests/fixtures/citation_keys.json` | The normalisation contract shared with the Cayenne client | **Create** |
| `tests/unit/test_citation.py` | The rules, against the measured strings | **Create** |
| `recipeparser/models.py` | `RecipeExtraction` gains `stated_source`, `byline`; `CayenneRecipe` gains the four columns | Modify |
| `recipeparser/gemini.py` | Both extraction prompt builders gain the source rule | Modify |
| `recipeparser/core/models.py` | `Chunk.citation` | Modify |
| `recipeparser/io/readers/epub.py`, `pdf.py`, `url.py`, `paprika.py` | Attach the citation; book readers stop writing `source_url` | Modify |
| `recipeparser/core/stages/assemble.py` | Resolves and writes the four fields and the `source` display | Modify |
| `recipeparser/core/pipeline.py` | Passes the citation to `assemble()` at its three call sites | Modify |
| `recipeparser/adapters/api.py` | The URL path's chunk carries `web_citation(body.url)` | Modify |
| `recipeparser/io/writers/supabase.py` | The row carries the four columns | Modify |
| `scripts/backfill_sources.py` | The seven rules over `source`, dry run by default | **Create** |
| `scripts/restore_source_urls.py` | `source_url` from the Paprika archive, dry run by default | **Create** |
| `tests/unit/scripts/test_backfill_sources.py`, `tests/unit/scripts/test_restore_source_urls.py` | The scripts' planning functions | **Create** |
| `CHANGELOG.md` | The entry | Modify |

---

### Task 1: Bring the spec in, and amend the philosophy spec

**Files:**
- Create: `docs/superpowers/specs/2026-09-11-recipe-source-citation-design.md`
- Modify: `docs/superpowers/specs/2026-09-07-recipe-edit-philosophy-design.md:43-48`

- [ ] **Step 1: Copy the design from the Cayenne repository**

The file is on Cayenne `main` once Cayenne PR #57 has merged (commit `43962b0` on the branch before that). From this worktree:

```bash
cp "C:/Users/iball/Arduino/Projects/Cayenne/docs/superpowers/specs/2026-09-11-recipe-source-citation-design.md" docs/superpowers/specs/
```

Then confirm the two are identical: `cmp docs/superpowers/specs/2026-09-11-recipe-source-citation-design.md "C:/Users/iball/Arduino/Projects/Cayenne/docs/superpowers/specs/2026-09-11-recipe-source-citation-design.md"` prints nothing.

- [ ] **Step 2: Amend §3.1 of the philosophy spec**

Replace the paragraph under `### 3.1 User-owned metadata (never triggers regen)` with exactly:

```markdown
`source`, `source_url`, `notes`, `description`, `nutritional_info`,
`difficulty`, `rating` (nullable, null = unrated), `image_url`, the
duration and servings columns defined in 3.6, and the four citation columns
`source_kind`, `source_key`, `source_title`, `source_author` (all nullable;
written by ingestion at insert, derived for older rows by
`scripts/backfill_sources.py`, corrected by a cook through *Set source*;
`docs/superpowers/specs/2026-09-11-recipe-source-citation-design.md`).
Categories live in `recipe_categories` and are user-owned after ingest.
```

The Cayenne twin makes the same edit to its copy; the two files must stay byte-identical (the convention since RecipeParser#35 / Cayenne#51).

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/
git commit -m "docs: the source citation design, and the four columns in the philosophy spec's 3.1"
```

---

### Task 2: The citation module and the shared key fixture

**Files:**
- Create: `recipeparser/core/citation.py`
- Create: `tests/fixtures/citation_keys.json`
- Create: `tests/unit/test_citation.py`

**Interfaces:**
- Produces:
  - `Citation(kind: str, key: Optional[str], title: Optional[str], author: Optional[str])`, frozen dataclass, with `display() -> Optional[str]`.
  - `KINDS = ("book", "web", "periodical", "person", "unknown")`, `UNKNOWN_BOOK_KEY = "unknown-book"`.
  - `normalise_key(value: str) -> str`
  - `host_of(url_or_host: str) -> str`
  - `book_citation(title: Optional[str], author: Optional[str]) -> Citation`
  - `web_citation(url_or_host: str, site_name: Optional[str] = None, byline: Optional[str] = None) -> Citation`
  - `Classified(rule: int, citation: Citation)` (NamedTuple) and `classify_source(text: Optional[str], byline: Optional[str] = None) -> Classified`
  - `resolve_citation(known: Optional[Citation], stated_source: Optional[str], byline: Optional[str]) -> Citation`

- [ ] **Step 1: Write the shared fixture**

`tests/fixtures/citation_keys.json` (the Cayenne twin copies this file byte-for-byte to `cayenne-web/tests/fixtures/citation_keys.json`; both test suites iterate it):

```json
[
  { "input": "The Food of Sichuan", "key": "the food of sichuan" },
  { "input": "  Completely  Perfect ", "key": "completely perfect" },
  { "input": "Flour + Water", "key": "flour + water" },
  { "input": "An Invitation to Indian Cooking", "key": "an invitation to indian cooking" },
  { "input": "Joe Beef: Surviving the Apocalypse", "key": "joe beef surviving the apocalypse" },
  { "input": "Italian Food (Penguin Classics)", "key": "italian food (penguin classics)" },
  { "input": "‘Perfect’", "key": "perfect" },
  { "input": "\"The Woks of Life\"", "key": "the woks of life" },
  { "input": "Nik Sharma", "key": "nik sharma" },
  { "input": "https://www.Cooking.NYTimes.com/recipes/1", "key": "cooking nytimes com recipes 1" },
  { "input": "www.thewoksoflife.com", "key": "thewoksoflife com" },
  { "input": "brown butter—braised leeks", "key": "brown butter braised leeks" },
  { "input": "home-style_cooking", "key": "home style cooking" }
]
```

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_citation.py`:

```python
"""The citation rules, against the strings measured in the library on 2026-09-11."""
import json
from pathlib import Path

import pytest

from recipeparser.core.citation import (
    UNKNOWN_BOOK_KEY,
    Citation,
    book_citation,
    classify_source,
    host_of,
    normalise_key,
    resolve_citation,
    web_citation,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "citation_keys.json"


@pytest.mark.parametrize("case", json.loads(FIXTURE.read_text(encoding="utf-8")), ids=lambda c: c["input"])
def test_normalise_key_matches_the_shared_fixture(case):
    assert normalise_key(case["input"]) == case["key"]


def test_host_of_strips_scheme_path_case_and_www():
    assert host_of("https://www.Cooking.NYTimes.com/recipes/1") == "cooking.nytimes.com"
    assert host_of("thewoksoflife.com") == "thewoksoflife.com"
    assert host_of("www.thewoksoflife.com") == "thewoksoflife.com"


def test_book_citation_with_metadata():
    c = book_citation("Italian Food", "Elizabeth David")
    assert c == Citation("book", "italian food", "Italian Food", "Elizabeth David")
    assert c.display() == "Italian Food — Elizabeth David"


def test_book_citation_without_metadata_is_unknown_book():
    c = book_citation(None, None)
    assert c == Citation("unknown", UNKNOWN_BOOK_KEY, None, None)
    assert c.display() is None
    assert book_citation("", "Someone") == Citation("unknown", UNKNOWN_BOOK_KEY, None, None)


def test_web_citation_title_is_site_name_else_host():
    assert web_citation("https://cooking.nytimes.com/recipes/1") == Citation(
        "web", "cooking.nytimes.com", "cooking.nytimes.com", None
    )
    assert web_citation("https://cooking.nytimes.com/recipes/1", "NYT Cooking", "Melissa Clark") == Citation(
        "web", "cooking.nytimes.com", "NYT Cooking", "Melissa Clark"
    )


# The seven rules, in order, first match wins (design: The backfill, per measured value).
@pytest.mark.parametrize(
    "text, rule, expected",
    [
        (None, 1, Citation("unknown", None, None, None)),
        ("   ", 1, Citation("unknown", None, None, None)),
        ("The Food of Sichuan — Fuchsia Dunlop", 2, Citation("book", "the food of sichuan", "The Food of Sichuan", "Fuchsia Dunlop")),
        ("Italian Food — Elizabeth David", 2, Citation("book", "italian food", "Italian Food", "Elizabeth David")),
        ("EPUB Auto-Import", 3, Citation("unknown", UNKNOWN_BOOK_KEY, None, None)),
        ("PDF Auto-Import", 3, Citation("unknown", UNKNOWN_BOOK_KEY, None, None)),
        ("cooking.nytimes.com", 4, Citation("web", "cooking.nytimes.com", "cooking.nytimes.com", None)),
        ("Cooking.nytimes.com", 4, Citation("web", "cooking.nytimes.com", "cooking.nytimes.com", None)),
        ("www.thewoksoflife.com", 4, Citation("web", "thewoksoflife.com", "thewoksoflife.com", None)),
        ("thewoksoflife.com", 4, Citation("web", "thewoksoflife.com", "thewoksoflife.com", None)),
        ("https://www.seriouseats.com/x", 4, Citation("web", "seriouseats.com", "seriouseats.com", None)),
        ("Womanandhome.com Justin Gellatly", 5, Citation("web", "womanandhome.com", "womanandhome.com", "Justin Gellatly")),
        ("Gemini", 6, Citation("unknown", None, None, None)),
        ("Nik Sharma", 7, Citation("person", "nik sharma", "Nik Sharma", None)),
        ("Perfect", 7, Citation("person", "perfect", "Perfect", None)),
    ],
)
def test_classify_source_rules(text, rule, expected):
    got = classify_source(text)
    assert got.rule == rule
    assert got.citation == expected


def test_the_two_spellings_of_a_site_share_one_key():
    a = classify_source("cooking.nytimes.com").citation.key
    b = classify_source("Cooking.nytimes.com").citation.key
    c = classify_source("www.thewoksoflife.com").citation.key
    d = classify_source("thewoksoflife.com").citation.key
    assert a == b and c == d


def test_classify_source_carries_a_byline_where_the_rule_has_no_author():
    assert classify_source("cooking.nytimes.com", "Melissa Clark").citation.author == "Melissa Clark"
    assert classify_source(None, "Melissa Clark").citation.author == "Melissa Clark"
    # A book string's own author beats a byline.
    assert classify_source("Italian Food — Elizabeth David", "Someone Else").citation.author == "Elizabeth David"


def test_resolve_citation_reader_beats_model():
    book = book_citation("Italian Food", "Elizabeth David")
    assert resolve_citation(book, "Some Site", "Someone") == book
    unknown_book = book_citation(None, None)
    assert resolve_citation(unknown_book, "Some Site", "Someone") == unknown_book


def test_resolve_citation_web_takes_site_name_and_byline_from_the_model():
    host = web_citation("https://cooking.nytimes.com/recipes/1")
    got = resolve_citation(host, "NYT Cooking", "Melissa Clark")
    assert got == Citation("web", "cooking.nytimes.com", "NYT Cooking", "Melissa Clark")
    assert resolve_citation(host, None, None) == host


def test_resolve_citation_with_nothing_known_classifies_the_stated_source():
    assert resolve_citation(None, "Bon Appétit", "Molly Baz") == Citation("person", "bon appétit", "Bon Appétit", "Molly Baz")
    assert resolve_citation(None, "cooking.nytimes.com", None).kind == "web"
    assert resolve_citation(None, "Gemini", None) == Citation("unknown", None, None, None)
    assert resolve_citation(None, None, None) == Citation("unknown", None, None, None)
```

Note the last block: with nothing known and a stated source that is neither a domain nor a "Title — Author" string, rule 7 files it as `person`. That is the design's rule 7 and the reason the `periodical` kind exists without a rule: "Bon Appétit" is a periodical, and the first one seen gets corrected with *Set source*. Do not add a periodical rule here.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_citation.py -q`
Expected: FAIL at import, `ModuleNotFoundError: No module named 'recipeparser.core.citation'`.

- [ ] **Step 4: Write the module**

`recipeparser/core/citation.py`:

```python
"""
recipeparser/core/citation.py — where a recipe came from, as four columns.

Provenance is written as structured citation fields at insert time, derived
deterministically wherever a deterministic source exists, and the free-text
`source` stays as the display string (design 2026-09-11). This module is the
one place the derivation lives: readers call the constructors, `assemble()`
calls `resolve_citation`, and the backfill script calls `classify_source`.

Pure: no I/O, no imports from recipeparser.io or recipeparser.adapters.
`normalise_key` is mirrored in the Cayenne client
(`cayenne-web/src/lib/domain/citation.ts`); tests/fixtures/citation_keys.json
is the contract both must satisfy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple, Optional
from urllib.parse import urlparse

KINDS = ("book", "web", "periodical", "person", "unknown")
UNKNOWN_BOOK_KEY = "unknown-book"
AUTO_IMPORT_STRINGS = ("EPUB Auto-Import", "PDF Auto-Import")

_BOOK_SEPARATOR = " \u2014 "  # " — ", what the book readers wrote between title and author
_SCHEME_AND_WWW = re.compile(r"^(https?://)?(www\.)?", re.I)
_HTTP = re.compile(r"^https?://", re.I)
_QUOTES = re.compile("[\u2018\u2019\u201c\u201d'\"`]")
_SEPARATORS = re.compile("[\\s\\-\u2013\u2014_.,:;/]+")
_DOMAIN = re.compile(r"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+$", re.I)
_DOMAIN_THEN_TEXT = re.compile(r"^(?P<host>(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+)\s+(?P<rest>\S.*)$", re.I)


@dataclass(frozen=True)
class Citation:
    kind: str
    key: Optional[str]
    title: Optional[str]
    author: Optional[str]

    def display(self) -> Optional[str]:
        """The free-text `source` for a row that has nothing better: 'Title — Author', or the title."""
        if self.title and self.author:
            return f"{self.title}{_BOOK_SEPARATOR}{self.author}"
        return self.title


def _clean(value: Optional[str]) -> Optional[str]:
    stripped = (value or "").strip()
    return stripped or None


def normalise_key(value: str) -> str:
    """The key two source strings share when a cook would call them one source. Never shown."""
    v = value.strip().lower()
    v = _SCHEME_AND_WWW.sub("", v, count=1)
    v = _QUOTES.sub("", v)
    return _SEPARATORS.sub(" ", v).strip()


def host_of(url_or_host: str) -> str:
    """The host, lower-cased, without a leading www.; a bare domain passes through."""
    v = url_or_host.strip()
    host = urlparse(v).netloc if _HTTP.match(v) else v
    return host.lower().removeprefix("www.")


def book_citation(title: Optional[str], author: Optional[str]) -> Citation:
    """A book the reader knows by its metadata; without a title it is an unknown book."""
    clean_title = _clean(title)
    if clean_title is None:
        return Citation("unknown", UNKNOWN_BOOK_KEY, None, None)
    return Citation("book", normalise_key(clean_title), clean_title, _clean(author))


def web_citation(url_or_host: str, site_name: Optional[str] = None, byline: Optional[str] = None) -> Citation:
    """A page: the key is the host; the title is the site's stated name, else the host."""
    host = host_of(url_or_host)
    return Citation("web", host, _clean(site_name) or host, _clean(byline))


class Classified(NamedTuple):
    rule: int
    citation: Citation


def classify_source(text: Optional[str], byline: Optional[str] = None) -> Classified:
    """
    The design's ordered rules over a free-text source string; first match wins.
    The rule number is returned so the backfill can report its counts per rule.
    """
    value = _clean(text)
    author = _clean(byline)
    if value is None:
        return Classified(1, Citation("unknown", None, None, author))
    if _BOOK_SEPARATOR in value:
        title, book_author = (part.strip() for part in value.split(_BOOK_SEPARATOR, 1))
        return Classified(2, book_citation(title, book_author or author))
    if value in AUTO_IMPORT_STRINGS:
        return Classified(3, Citation("unknown", UNKNOWN_BOOK_KEY, None, None))
    if _DOMAIN.match(value) or _HTTP.match(value):
        return Classified(4, web_citation(value, byline=author))
    domain_then_text = _DOMAIN_THEN_TEXT.match(value)
    if domain_then_text:
        return Classified(5, web_citation(domain_then_text.group("host"), byline=domain_then_text.group("rest")))
    if value.lower() == "gemini":
        # The model named itself. A prompt defect, fixed alongside; the value is not a source.
        return Classified(6, Citation("unknown", None, None, None))
    return Classified(7, Citation("person", normalise_key(value), value, author))


def resolve_citation(known: Optional[Citation], stated_source: Optional[str], byline: Optional[str]) -> Citation:
    """
    The reader's knowledge beats the model's reading; the model fills only what the reader could not.

    A book with metadata, and an unknown book, are settled by the reader. A page keeps its host as
    the key and takes the site's stated name and the byline from the model. With nothing known, the
    model's stated source is classified by the same rules the backfill uses.
    """
    if known is None:
        return classify_source(stated_source, byline).citation
    if known.kind == "web":
        return Citation("web", known.key, _clean(stated_source) or known.title, known.author or _clean(byline))
    return known
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_citation.py -q`
Expected: all pass. Then `ruff check recipeparser/core/citation.py tests/unit/test_citation.py`: no findings.

- [ ] **Step 6: Commit**

```bash
git add recipeparser/core/citation.py tests/fixtures/citation_keys.json tests/unit/test_citation.py
git commit -m "feat(core): the citation module, and the key fixture the client shares"
```

---

### Task 3: Ask the model for a stated source and a byline

**Files:**
- Modify: `recipeparser/models.py:6-60` (`RecipeExtraction`)
- Modify: `recipeparser/gemini.py:435-472` (`build_extract_prompt`) and the other extraction prompt builder, the one whose call site is labelled `what="Gemini plain-text extraction"` (`recipeparser/gemini.py:426-432`)
- Test: `tests/goldens/test_prompts_snapshot.py` (snapshots), `tests/unit/test_models.py`

**Interfaces:**
- Produces: `RecipeExtraction.stated_source: Optional[str]`, `RecipeExtraction.byline: Optional[str]`, both default `None`, both in the Gemini schema. Task 5 reads them off the extraction result.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_models.py`:

```python
def test_extraction_asks_for_a_stated_source_and_a_byline():
    from recipeparser.models import RecipeExtraction

    schema = RecipeExtraction.model_json_schema()
    assert "stated_source" in schema["properties"]
    assert "byline" in schema["properties"]
    # The instruction the prompt defect needs: the model must never name itself.
    assert "Never the name of an AI model" in schema["properties"]["stated_source"]["description"]
    r = RecipeExtraction(name="x", ingredients=[], directions=[])
    assert r.stated_source is None and r.byline is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/unit/test_models.py -q -k stated_source`
Expected: FAIL, `KeyError: 'stated_source'`.

- [ ] **Step 3: Add the two fields**

In `recipeparser/models.py`, after `notes` (line 48-51) and before the `categories` field:

```python
    stated_source: Optional[str] = Field(
        default=None,
        description=(
            "The publication, website or book this text names as its own source, exactly as the "
            "text names it: a masthead, a book title, a site name. Never the name of an AI model or "
            "a tool. null when the text does not say."
        ),
    )
    byline: Optional[str] = Field(
        default=None,
        description=(
            "The recipe author's name when a byline is printed ('By Melissa Clark' gives 'Melissa Clark'). "
            "null when none is printed."
        ),
    )
```

- [ ] **Step 4: Add the rule to both extraction prompts**

In `build_extract_prompt` (`gemini.py:467-468`), after the line `- If a field is entirely absent from the text, leave it null.` add:

```
- stated_source is the publication or book as the text names itself; byline is the author's
  name if one is printed. Never the name of a model or a tool. Leave both null if the text
  does not say.
```

Find the other extraction builder: `grep -n "def build_.*prompt" recipeparser/gemini.py` and take the one rendered by the call site at `gemini.py:426-432` (`what="Gemini plain-text extraction"`). Add the same three lines to its rules block.

- [ ] **Step 5: Re-approve the prompt snapshots and run the tests**

Run: `pytest tests/goldens tests/snapshots --snapshot-update -q`, then `git diff --stat tests/goldens/__snapshots__ tests/snapshots` and read the diff: the only changes are the three-line rule in each extraction prompt and the two new schema properties. Then:

Run: `python -m pytest tests/unit/test_models.py tests/goldens -q`
Expected: all pass (a `prompt_sha256 mismatch` UserWarning from recorded replies is expected and not a failure).

- [ ] **Step 6: Commit**

```bash
git add recipeparser/models.py recipeparser/gemini.py tests/unit/test_models.py tests/goldens tests/snapshots
git commit -m "feat(extract): the model states the source and the byline, and never names itself"
```

---

### Task 4: Readers attach the citation; book readers stop writing source_url

**Files:**
- Modify: `recipeparser/core/models.py:89-130` (`Chunk`)
- Modify: `recipeparser/io/readers/epub.py:50-75, 85-93, 227-240`
- Modify: `recipeparser/io/readers/pdf.py:55-70, 90-115, 147-160`
- Modify: `recipeparser/io/readers/url.py` (the one `Chunk(...)` construction)
- Modify: `recipeparser/io/readers/paprika.py:172-192`
- Test: `tests/unit/readers/test_readers.py`, `tests/goldens/test_readers_golden.py`, `tests/goldens/readers/*.json`

**Interfaces:**
- Consumes: `Citation`, `book_citation`, `web_citation`, `classify_source` from Task 2.
- Produces: `Chunk.citation: Optional[Citation]` (default `None`); `get_book_citation(book) -> Citation` in `epub.py`; `_get_book_citation(doc) -> Citation` in `pdf.py`; `load_epub` and `load_pdf` return a `Citation` as their first element in place of the `book_source` string.
- `get_book_source` (epub) and `_get_book_source` (pdf) **stay**, unchanged: `recipeparser/epub.py` and the Paprika export writer still use the `"Title — Author"` string.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/readers/test_readers.py`:

```python
class TestReaderCitations:
    def test_chunk_citation_defaults_to_none(self):
        chunk = Chunk(text="x", input_type=InputType.URL)
        assert chunk.citation is None

    def test_epub_with_metadata_is_a_book_and_writes_no_source_url(self):
        from ebooklib import epub as ebook
        from recipeparser.io.readers.epub import get_book_citation

        book = ebook.EpubBook()
        book.set_title("Italian Food")
        book.add_author("Elizabeth David")
        c = get_book_citation(book)
        assert (c.kind, c.key, c.title, c.author) == ("book", "italian food", "Italian Food", "Elizabeth David")

    def test_epub_without_metadata_is_an_unknown_book(self):
        from ebooklib import epub as ebook
        from recipeparser.io.readers.epub import get_book_citation

        c = get_book_citation(ebook.EpubBook())
        assert (c.kind, c.key, c.title, c.author) == ("unknown", "unknown-book", None, None)

    def test_pdf_citation_from_metadata(self):
        from recipeparser.io.readers.pdf import _get_book_citation

        doc = MagicMock()
        doc.metadata = {"title": "Classic German Baking", "author": "Luisa Weiss"}
        c = _get_book_citation(doc)
        assert (c.kind, c.title, c.author) == ("book", "Classic German Baking", "Luisa Weiss")
        doc.metadata = {}
        assert _get_book_citation(doc).key == "unknown-book"

    def test_url_reader_chunk_carries_the_host(self):
        response = MagicMock()
        response.text = "Tomato soup\n1 tin tomatoes\nHeat."
        response.raise_for_status = MagicMock()
        with patch("recipeparser.io.readers.url.requests.get", return_value=response):
            chunk = UrlReader().read("https://www.seriouseats.com/tomato-soup")[0]
        assert chunk.source_url == "https://www.seriouseats.com/tomato-soup"
        assert chunk.citation is not None
        assert (chunk.citation.kind, chunk.citation.key) == ("web", "seriouseats.com")

    def test_paprika_entry_citation_from_its_source(self, tmp_path):
        archive = _write_paprika_archive(tmp_path, [
            {"name": "A", "ingredients": "x", "directions": "y", "source": "Italian Food — Elizabeth David"},
            {"name": "B", "ingredients": "x", "directions": "y", "source": "", "source_url": "https://cooking.nytimes.com/r/1"},
            {"name": "C", "ingredients": "x", "directions": "y", "source": ""},
        ])
        chunks = PaprikaReader().read(str(archive))
        by_label = {c.label: c for c in chunks}
        assert by_label["A"].citation.kind == "book" and by_label["A"].citation.author == "Elizabeth David"
        # A blank source with a URL is the site, not "no source".
        assert by_label["B"].citation.kind == "web" and by_label["B"].citation.key == "cooking.nytimes.com"
        assert by_label["C"].citation.kind == "unknown" and by_label["C"].citation.key is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/unit/readers/test_readers.py -q -k Citation`
Expected: FAIL: `TypeError: Chunk.__init__() got an unexpected keyword argument 'citation'` and `ImportError` on `get_book_citation`.

- [ ] **Step 3: `Chunk.citation`**

In `recipeparser/core/models.py`, import `Citation` at the top (`from recipeparser.core.citation import Citation`) and add to `Chunk`, after `source_url`:

```python
    citation: Optional[Citation] = None
```

and to the docstring's field list:

```
    citation:
        What the reader knows about where the recipe came from: a book's
        metadata, a URL's host, a Paprika entry's source. None when only the
        text can say (pasted text), in which case assemble() takes the model's
        stated source. Books no longer put "Title — Author" in source_url.
```

- [ ] **Step 4: The EPUB reader**

In `recipeparser/io/readers/epub.py`, next to `get_book_source` (line 227), add:

```python
def get_book_citation(book: epub.EpubBook) -> Citation:
    """The book's DC title and creator as a citation; an unknown book when the title is absent."""
    def _first(key: str) -> str:
        vals = book.get_metadata("DC", key)
        return str(vals[0][0]).strip() if vals else ""

    return book_citation(_first("title"), _first("creator"))
```

with `from recipeparser.core.citation import Citation, book_citation` at the top. Change `load_epub` (line 85-93) so its first return value is `get_book_citation(book)` rather than `get_book_source(book)`, and its annotation from `Tuple[str, ...]` to `Tuple[Citation, ...]`. In `_read_in_dir` (line 52-71): rename the unpacked variable to `citation` and build the chunk as

```python
                Chunk(
                    text=part,
                    input_type=InputType.EPUB,
                    source_url=None,
                    citation=citation,
                    label=None,
                )
```

`source_url=None` is deliberate and the point of the task: the column returns to meaning a URL.

- [ ] **Step 5: The PDF reader**

In `recipeparser/io/readers/pdf.py`, next to `_get_book_source` (line 147), add:

```python
def _get_book_citation(doc: "fitz.Document") -> Citation:
    """PDF title and author metadata as a citation; an unknown book when the title is absent."""
    meta = doc.metadata or {}
    return book_citation(meta.get("title"), meta.get("author"))
```

The filename stem that `_get_book_source` falls back to is **not** a title: a file named `scan_0412.pdf` would become a book called "scan_0412". Change `load_pdf` (line 90-115) to return `_get_book_citation(doc)` first, and the chunk construction (line 60-70) to `source_url=None, citation=citation`, as in the EPUB reader.

- [ ] **Step 6: The URL and Paprika readers**

`recipeparser/io/readers/url.py`: at its `Chunk(...)` construction add `citation=web_citation(url)` (import `web_citation`), keeping `source_url=url`.

`recipeparser/io/readers/paprika.py:172-192`: before the `Chunk(...)`, compute

```python
                entry_source = str(entry.get("source") or "").strip()
                entry_url = str(entry.get("source_url") or "").strip() or None
                if entry_source:
                    citation = classify_source(entry_source).citation
                elif entry_url and entry_url.lower().startswith("http"):
                    citation = web_citation(entry_url)
                else:
                    citation = classify_source(None).citation
```

and pass `citation=citation`, `source_url=entry_url`. Import `classify_source, web_citation` from `recipeparser.core.citation`. Ruling: a blank `source` beside a real URL is the site, which is what the design's Paprika row means by "from the entry"; the design table's "by the backfill rule" covers the `source` column, and the backfill sees no `source_url` because the bulk import dropped it.

- [ ] **Step 7: Rewrite the reader goldens and review the diff**

Run: `pytest tests/goldens/test_readers_golden.py --update-goldens -q`, then `git diff tests/goldens/readers/`.
Expected diff: on `dual-units.epub.json`, `phases-bakers.epub.json`, `gutenberg-multi.epub.json` and `text-pages.pdf.json`, every `"source_url": "<string>"` becomes `"source_url": null`, and nothing else changes. If anything else changed, stop: a reader has altered its text or images and that is a defect, not a golden update. Then extend `_chunk_to_dict` in `tests/goldens/test_readers_golden.py` with

```python
        "citation": None if chunk.citation is None else {
            "kind": chunk.citation.kind, "key": chunk.citation.key,
            "title": chunk.citation.title, "author": chunk.citation.author,
        },
```

and run `--update-goldens` once more so the goldens record the citation from now on. Review: the EPUB fixtures show `"kind": "book"` with the fixture's title ("Dual Units", …); the PDF shows whatever `text-pages.pdf` carries in its metadata; the URL golden shows `example.invalid`; the Paprika golden shows the legacy entry's source classified.

- [ ] **Step 8: Run the reader suites**

Run: `python -m pytest tests/unit/readers tests/goldens/test_readers_golden.py tests/test_epub.py tests/test_export.py -q`
Expected: all pass. `test_export.py::test_book_source_written_to_export` still passes because `get_book_source` is untouched. `ruff check recipeparser tests`: no findings.

- [ ] **Step 9: Commit**

```bash
git add recipeparser/core/models.py recipeparser/io/readers tests/unit/readers/test_readers.py tests/goldens
git commit -m "feat(readers): the chunk carries a citation, and a book no longer writes its title into source_url"
```

---

### Task 5: The four columns on the recipe, resolved in assemble() and written by the writer

**Files:**
- Modify: `recipeparser/models.py:140-196` (`CayenneRecipe`)
- Modify: `recipeparser/core/stages/assemble.py:20-131`
- Modify: `recipeparser/core/pipeline.py:298-311, 325-338, 381-394`
- Modify: `recipeparser/adapters/api.py:799-806`
- Modify: `recipeparser/io/writers/supabase.py:188-215`
- Test: `tests/unit/stages/test_assemble.py` (create if absent; `ls tests/unit/stages` first), `tests/unit/writers/test_supabase_row.py`, `tests/unit/test_pipeline.py`

**Interfaces:**
- Consumes: `Citation`, `resolve_citation`, `web_citation` (Task 2); `RecipeExtraction.stated_source`/`.byline` (Task 3); `Chunk.citation` (Task 4).
- Produces: `CayenneRecipe.source_kind`, `.source_key`, `.source_title`, `.source_author`: `Optional[str]`, default `None`; `assemble(..., citation: Optional[Citation] = None)`; the writer's row carries the four keys.

- [ ] **Step 1: Write the failing tests**

`tests/unit/stages/test_assemble.py` (append if it exists, create with these imports otherwise):

```python
from recipeparser.core.citation import Citation, book_citation, web_citation
from recipeparser.core.models import SourceMeta
from recipeparser.core.stages.assemble import assemble
from recipeparser.models import CayenneRefinement, StructuredIngredient, TokenizedDirection


def _refined() -> CayenneRefinement:
    return CayenneRefinement(
        title="Cake",
        base_servings=2,
        structured_ingredients=[StructuredIngredient(id="ing_01", name="flour", fallback_string="1 cup flour", line_index=0)],
        tokenized_directions=[TokenizedDirection(step=1, text="Mix {{ing_01|flour}}.")],
    )


def _assemble(**kwargs):
    return assemble(recipe=_refined(), embedding=[0.0] * 1536, source_url=None, image_url=None, grid_categories={}, **kwargs)


def test_assemble_writes_the_four_columns_and_the_display_source():
    r = _assemble(citation=book_citation("Italian Food", "Elizabeth David"))
    assert (r.source_kind, r.source_key, r.source_title, r.source_author) == (
        "book", "italian food", "Italian Food", "Elizabeth David")
    assert r.source == "Italian Food — Elizabeth David"


def test_assemble_without_a_citation_leaves_all_five_null():
    r = _assemble()
    assert (r.source_kind, r.source_key, r.source_title, r.source_author, r.source) == (None, None, None, None, None)


def test_assemble_unknown_book_has_a_key_but_no_display():
    r = _assemble(citation=book_citation(None, None))
    assert (r.source_kind, r.source_key, r.source_title, r.source) == ("unknown", "unknown-book", None, None)


def test_paprika_source_beats_the_citation_display():
    meta = SourceMeta(source="NYT Cooking")
    r = _assemble(citation=web_citation("https://cooking.nytimes.com/r/1"), meta=meta)
    assert r.source == "NYT Cooking"
    assert r.source_key == "cooking.nytimes.com"
```

Append to `tests/unit/writers/test_supabase_row.py`, extending `_recipe()` with `source_kind="book", source_key="cake book", source_title="Cake Book", source_author="A. Baker"`:

```python
def test_row_has_the_citation_columns(posted):
    assert (posted["source_kind"], posted["source_key"], posted["source_title"], posted["source_author"]) == (
        "book", "cake book", "Cake Book", "A. Baker")
```

Append to `tests/unit/test_pipeline.py` (read its existing helpers first and reuse the pipeline fixture it builds; the assertion is the only new thing):

```python
def test_pipeline_resolves_the_chunk_citation_with_the_models_stated_source(pipeline_with_fake_client, make_extraction):
    # The fake client returns an extraction with stated_source="NYT Cooking", byline="Melissa Clark".
    # The chunk knows the host. The result keeps the host as key and takes name and byline from the model.
    chunk = Chunk(text="Tomato soup ...", input_type=InputType.URL,
                  source_url="https://cooking.nytimes.com/r/1",
                  citation=web_citation("https://cooking.nytimes.com/r/1"))
    [result] = pipeline_with_fake_client.run([chunk], user_id="u1")
    assert (result.source_kind, result.source_key, result.source_title, result.source_author) == (
        "web", "cooking.nytimes.com", "NYT Cooking", "Melissa Clark")
```

If `test_pipeline.py` has no fixture that runs a chunk through a faked Gemini, look at how `tests/unit/test_pipeline.py` already drives `RecipePipeline.run` (it does, for the skip and progress cases) and build the same shape inline with the extraction result carrying `stated_source` and `byline`.

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/unit/stages/test_assemble.py tests/unit/writers/test_supabase_row.py tests/unit/test_pipeline.py -q`
Expected: FAIL: `TypeError: assemble() got an unexpected keyword argument 'citation'`, `ValidationError` on `source_kind`, `KeyError: 'source_kind'`.

- [ ] **Step 3: The model fields**

In `recipeparser/models.py`, in `CayenneRecipe` after `difficulty` (line 165):

```python
    # Citation (design 2026-09-11): written at insert by assemble(), never by the regen worker.
    source_kind: Optional[str] = Field(default=None, description="book, web, periodical, person, unknown.")
    source_key: Optional[str] = Field(default=None, description="The identity the library filters and sorts on. Never shown.")
    source_title: Optional[str] = Field(default=None, description="What the Source pill and the kitchen line show.")
    source_author: Optional[str] = Field(default=None, description="The book's author or a byline.")
```

- [ ] **Step 4: `assemble()`**

Add the parameter `citation: Optional[Citation] = None` after `servings_text`, import `Citation` from `recipeparser.core.citation`, and document it in the docstring:

```
        citation:        What the reader and the model settled about the source
                         (core.citation.resolve_citation). Fills the four
                         citation columns and, when nothing else supplies
                         `source`, its display form.
```

In the `IngestResponse(...)` construction replace `source=meta.source if meta else None,` with

```python
        # Paprika's own statement first; else the citation's display form, so the
        # library row (which reads `source`) shows the same thing for a fresh
        # insert as for a backfilled row; else null.
        source=(meta.source if meta else None) or (citation.display() if citation else None),
        source_kind=citation.kind if citation else None,
        source_key=citation.key if citation else None,
        source_title=citation.title if citation else None,
        source_author=citation.author if citation else None,
```

- [ ] **Step 5: The pipeline's three call sites and the API's chunk**

`recipeparser/core/pipeline.py`: import `resolve_citation`. At the two pre-parsed call sites (lines 298 and 325) add `citation=chunk.citation,`. At the full-pipeline call site (line 381) add

```python
                citation=resolve_citation(
                    chunk.citation,
                    getattr(raw, "stated_source", None),
                    getattr(raw, "byline", None),
                ),
```

`recipeparser/adapters/api.py:799-806`: the URL/text chunk gains `citation=web_citation(body.url) if body.url else None,` (import `web_citation`). Pasted text carries no citation; `resolve_citation` classifies the model's stated source for it. Nothing else in `api.py` changes in this plan: `source_hint` is rewritten to the key in Stage D, and `_update_total_chunks` (line 667) is the seam it will extend.

- [ ] **Step 6: The writer**

`recipeparser/io/writers/supabase.py`, in the `row` dict after `"difficulty": recipe.difficulty,`:

```python
        # Citation columns (migration recipe_source_citation in the Cayenne repo).
        "source_kind": recipe.source_kind,
        "source_key": recipe.source_key,
        "source_title": recipe.source_title,
        "source_author": recipe.source_author,
```

Check the regen worker's write-back does not touch them: `grep -n "source" recipeparser/adapters/regen_worker.py recipeparser/core/regen.py` should show no citation column in any update payload. The worker regenerates derived columns only (philosophy spec 5.5).

- [ ] **Step 7: Run the tests, then the whole suite**

Run: `python -m pytest tests/unit/stages/test_assemble.py tests/unit/writers/test_supabase_row.py tests/unit/test_pipeline.py -q`
Expected: pass.

Run: `python -m pytest -q`
Expected: pass, except syrupy snapshots under `tests/snapshots` that serialise a full `IngestResponse` (they now carry four more keys). Re-approve those with `pytest tests/snapshots --snapshot-update -q`, review the diff (four new null keys per recipe and nothing else), and re-run.

- [ ] **Step 8: Commit**

```bash
git add recipeparser tests
git commit -m "feat: the four citation columns, resolved reader-first in assemble() and written on every insert"
```

---

### Task 6: `scripts/backfill_sources.py`

**Files:**
- Create: `scripts/backfill_sources.py`
- Create: `tests/unit/scripts/test_backfill_sources.py`

**Interfaces:**
- Consumes: `classify_source`, `host_of` (Task 2).
- Produces: `plan_backfill(rows: List[Dict[str, Any]], force: bool = False) -> List[RowPlan]` where `RowPlan = Tuple[str, Dict[str, Any], int]` is `(recipe_id, columns_to_write, rule_number)`; a row whose `source_kind` is already set is skipped (not in the plan) unless `force`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/scripts/test_backfill_sources.py`:

```python
"""The backfill's planning function, against the literal strings measured on 2026-09-11."""
import importlib.util
from collections import Counter
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "backfill_sources", Path(__file__).parents[3] / "scripts" / "backfill_sources.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _row(i, source, kind=None):
    return {"id": f"r{i}", "title": f"Recipe {i}", "source": source, "source_kind": kind}


MEASURED = [
    None,                                        # 1
    "The Food of Sichuan — Fuchsia Dunlop",      # 2
    "EPUB Auto-Import",                          # 3
    "cooking.nytimes.com",                       # 4
    "cooking.nytimes.com",                       # 4
    "Cooking.nytimes.com",                       # 4  (the minority spelling)
    "www.thewoksoflife.com",                     # 4
    "thewoksoflife.com",                         # 4
    "Womanandhome.com Justin Gellatly",          # 5
    "Gemini",                                    # 6
    "Nik Sharma",                                # 7
    "Perfect",                                   # 7
]


def test_every_rule_fires_once_at_least_and_counts_are_reported():
    plan = mod.plan_backfill([_row(i, s) for i, s in enumerate(MEASURED)])
    assert len(plan) == len(MEASURED)
    assert Counter(rule for _, _, rule in plan) == {1: 1, 2: 1, 3: 1, 4: 5, 5: 1, 6: 1, 7: 2}


def test_book_row_columns():
    [(rid, cols, rule)] = mod.plan_backfill([_row(0, "The Food of Sichuan — Fuchsia Dunlop")])
    assert cols == {"source_kind": "book", "source_key": "the food of sichuan",
                    "source_title": "The Food of Sichuan", "source_author": "Fuchsia Dunlop"}
    assert "source" not in cols  # the display string is untouched


def test_site_title_is_the_majority_spelling_and_one_key():
    rows = [_row(0, "cooking.nytimes.com"), _row(1, "cooking.nytimes.com"), _row(2, "Cooking.nytimes.com")]
    plan = mod.plan_backfill(rows)
    assert {cols["source_key"] for _, cols, _ in plan} == {"cooking.nytimes.com"}
    assert {cols["source_title"] for _, cols, _ in plan} == {"cooking.nytimes.com"}
    rows = [_row(0, "www.thewoksoflife.com"), _row(1, "www.thewoksoflife.com"), _row(2, "thewoksoflife.com")]
    plan = mod.plan_backfill(rows)
    assert {cols["source_key"] for _, cols, _ in plan} == {"thewoksoflife.com"}
    assert {cols["source_title"] for _, cols, _ in plan} == {"www.thewoksoflife.com"}


def test_gemini_is_cleared():
    [(_, cols, rule)] = mod.plan_backfill([_row(0, "Gemini")])
    assert rule == 6
    assert cols == {"source_kind": "unknown", "source_key": None, "source_title": None, "source_author": None, "source": None}


def test_unknown_book_and_no_source_are_distinct_pills():
    plan = mod.plan_backfill([_row(0, "EPUB Auto-Import"), _row(1, None)])
    assert plan[0][1]["source_key"] == "unknown-book"
    assert plan[1][1]["source_key"] is None
    assert plan[0][1]["source_kind"] == plan[1][1]["source_kind"] == "unknown"


def test_a_row_already_classified_is_skipped_unless_forced():
    rows = [_row(0, "Nik Sharma", kind="person"), _row(1, "Nik Sharma")]
    assert [rid for rid, _, _ in mod.plan_backfill(rows)] == ["r1"]
    assert [rid for rid, _, _ in mod.plan_backfill(rows, force=True)] == ["r0", "r1"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/unit/scripts/test_backfill_sources.py -q`
Expected: FAIL, `FileNotFoundError` on the script path.

- [ ] **Step 3: Write the script**

`scripts/backfill_sources.py`:

```python
"""
One-off: derive the four citation columns from the free-text `source` on every
recipe already in the library (design 2026-09-11, "The backfill, per measured
value"). Seven ordered rules, first match wins; `recipeparser.core.citation`
holds them, so the backfill and the ingestion path cannot disagree.

Mirrors the CLI shape of `scripts/backfill_durations.py`: a dry run is the
default and writing is an explicit opt-in.

    # dry run -- prints the per-rule counts and writes nothing
    python scripts/backfill_sources.py --user-id <uuid>

    # for real
    python scripts/backfill_sources.py --user-id <uuid> --live

    # list every row and what it would get
    python scripts/backfill_sources.py --user-id <uuid> --verbose

Rows whose `source_kind` is already set are left alone (a cook's Set source
must survive a re-run); `--force` reclassifies them too. Rule 6 (`Gemini`)
also clears `source`: the model named itself, and that is not a source.

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipeparser.core.citation import classify_source  # noqa: E402

PAGE = 500
RULE_NAMES = {
    1: "no source",
    2: "book: Title — Author",
    3: "unknown book (Auto-Import)",
    4: "site domain or URL",
    5: "domain then author",
    6: "Gemini (cleared)",
    7: "person / other text",
}

RowPlan = Tuple[str, Dict[str, Any], int]


def _spelling(source: str) -> str:
    """The site as the row wrote it, without scheme or path, case kept: the title candidates for rule 4."""
    v = source.strip()
    if v.lower().startswith(("http://", "https://")):
        v = v.split("://", 1)[1]
    return v.split("/", 1)[0]


def plan_backfill(rows: List[Dict[str, Any]], force: bool = False) -> List[RowPlan]:
    """Decide what to write, writing nothing. Pure, so the rules are testable without a database."""
    classified = []
    spellings: Dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        if row.get("source_kind") is not None and not force:
            continue
        result = classify_source(row.get("source"))
        classified.append((row, result))
        if result.rule == 4:
            spellings[result.citation.key][_spelling(row["source"])] += 1

    plan: List[RowPlan] = []
    for row, result in classified:
        cit = result.citation
        cols: Dict[str, Any] = {
            "source_kind": cit.kind,
            "source_key": cit.key,
            "source_title": cit.title,
            "source_author": cit.author,
        }
        if result.rule == 4:
            # "the host as written on the majority of its rows"
            cols["source_title"] = spellings[cit.key].most_common(1)[0][0]
        if result.rule == 6:
            cols["source"] = None
        plan.append((row["id"], cols, result.rule))
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill the four citation columns from `source`.")
    ap.add_argument("--user-id", required=True, help="The owner whose library to repair.")
    ap.add_argument("--live", action="store_true", help="Actually write. Omit for a dry run.")
    ap.add_argument("--force", action="store_true", help="Reclassify rows whose source_kind is already set.")
    ap.add_argument("--verbose", action="store_true", help="List every row that would change.")
    args = ap.parse_args()
    from supabase import create_client  # noqa: PLC0415
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    # Read everything first: rule 4's majority spelling needs the whole library.
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = (sb.table("recipes").select("id,title,source,source_kind").eq("user_id", args.user_id)
                .order("id").range(offset, offset + PAGE - 1).execute().data or [])
        if not page:
            break
        rows.extend(page)
        offset += PAGE
    titles = {r["id"]: r.get("title") for r in rows}

    plan = plan_backfill(rows, force=args.force)
    counts = Counter(rule for _, _, rule in plan)
    updated, failed = 0, 0
    for rid, cols, rule in plan:
        if args.verbose:
            print(f"  rule {rule}  {rid}  {titles[rid]!r}  {cols}")
        if args.live:
            try:
                sb.table("recipes").update(cols).eq("id", rid).execute()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  FAILED to update {rid} {titles[rid]!r}: {exc}")
                continue
        updated += 1

    # Printed on a dry run and a live run alike, so the two are comparable.
    print(f"{len(rows)} recipes read, {len(rows) - len(plan)} already classified and skipped")
    for rule in sorted(RULE_NAMES):
        print(f"  rule {rule}  {counts.get(rule, 0):5d}  {RULE_NAMES[rule]}")
    print(f"{'updated' if args.live else 'would update'} {updated} recipe(s)" + (f", {failed} failed" if failed else ""))
    if not args.live:
        print("DRY RUN — nothing was written. Re-run with --live to apply.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/scripts/test_backfill_sources.py -q`
Expected: pass. `ruff check scripts tests/unit/scripts`: no findings.

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_sources.py tests/unit/scripts/test_backfill_sources.py
git commit -m "feat(scripts): the seven-rule citation backfill, dry run by default, counted per rule"
```

---

### Task 7: `scripts/restore_source_urls.py`

**Files:**
- Create: `scripts/restore_source_urls.py`
- Create: `tests/unit/scripts/test_restore_source_urls.py`

**Interfaces:**
- Consumes: `normalise_title` from `scripts/backfill_paprika_metadata.py` (the entry-matching rule the metadata backfill established); `PaprikaReader.read_entries(path)`.
- Produces: `plan_restore(entries, rows) -> RestorePlan` with `updates: List[Tuple[str, str, str]]` as `(recipe_id, title, url)`, `ambiguous: List[str]`, `unmatched: List[str]`, `untouched: int`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/scripts/test_restore_source_urls.py`:

```python
from scripts.restore_source_urls import plan_restore


def _entry(name, url):
    return {"name": name, "source_url": url}


def _row(i, title, url=None):
    return {"id": f"r{i}", "title": title, "source_url": url}


def test_restores_only_where_the_row_has_no_url_and_the_entry_has_one():
    plan = plan_restore(
        [_entry("Tomato Soup", "https://cooking.nytimes.com/r/1"), _entry("Plain", ""), _entry("Kept", "https://a.example/x")],
        [_row(0, "Tomato Soup"), _row(1, "Plain"), _row(2, "Kept", "https://already.example/y")],
    )
    assert plan.updates == [("r0", "Tomato Soup", "https://cooking.nytimes.com/r/1")]
    assert plan.untouched == 2


def test_matches_on_alphanumerics_only_like_the_metadata_backfill():
    plan = plan_restore([_entry("Brown Butter—Braised Leeks", "https://a.example/l")], [_row(0, "brown butter braised leeks")])
    assert [u[0] for u in plan.updates] == ["r0"]


def test_ambiguous_and_unmatched_are_reported_not_written():
    plan = plan_restore(
        [_entry("Twins", "https://a.example/1"), _entry("Twins", "https://a.example/2"), _entry("Lost", "https://a.example/3")],
        [_row(0, "Twins")],
    )
    assert plan.updates == []
    assert plan.ambiguous == ["Twins"]
    assert plan.unmatched == ["Lost"]


def test_a_non_http_entry_value_is_not_a_url():
    plan = plan_restore([_entry("Book", "Italian Food — Elizabeth David")], [_row(0, "Book")])
    assert plan.updates == [] and plan.untouched == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/unit/scripts/test_restore_source_urls.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'scripts.restore_source_urls'`.

- [ ] **Step 3: Write the script**

`scripts/restore_source_urls.py`:

```python
"""
scripts/restore_source_urls.py — put the Paprika archive's per-entry clip URL
back onto the rows the bulk import dropped it from.

Measured 2026-09-11: 788 recipes, `source_url` set on 5, while about 115 rows
carry a site domain in `source` that came from the Paprika clipper. The archive
still has each entry's `source_url`. This matches entries to rows by title,
with the rule `backfill_paprika_metadata.py` established (alphanumerics only),
and PATCHes `source_url` only where the row has none and the entry has an
http(s) one.

    # dry run -- prints what it would do and writes nothing
    python scripts/restore_source_urls.py --archive "/path/to/Export.paprikarecipes" --user-id <uuid>

    # the real thing
    python scripts/restore_source_urls.py --archive "/path/to/Export.paprikarecipes" --user-id <uuid> --live

Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the environment. Never
creates a recipe; an entry with no matching row is reported and left alone.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recipeparser.io.readers.paprika import PaprikaReader  # noqa: E402
from scripts.backfill_paprika_metadata import normalise_title  # noqa: E402

PAGE = 500


@dataclass
class RestorePlan:
    updates: List[Tuple[str, str, str]] = field(default_factory=list)  # (recipe_id, title, url)
    ambiguous: List[str] = field(default_factory=list)
    unmatched: List[str] = field(default_factory=list)
    untouched: int = 0


def _url(value: Any) -> str:
    v = str(value or "").strip()
    return v if v.lower().startswith(("http://", "https://")) else ""


def plan_restore(entries: Sequence[Dict[str, Any]], rows: Sequence[Dict[str, Any]]) -> RestorePlan:
    """Decide what to write, writing nothing."""
    plan = RestorePlan()
    rows_by_title: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_title[normalise_title(row.get("title"))].append(row)
    entries_by_title: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        entries_by_title[normalise_title(entry.get("name"))].append(entry)

    for key, matched_entries in entries_by_title.items():
        matched_rows = [] if key == "" else rows_by_title.get(key, [])
        title = str(matched_entries[0].get("name") or "")
        if not matched_rows:
            plan.unmatched.append(title)
            continue
        if len(matched_rows) > 1 or len(matched_entries) > 1:
            plan.ambiguous.append(title)
            continue
        row, url = matched_rows[0], _url(matched_entries[0].get("source_url"))
        if url and not str(row.get("source_url") or "").strip():
            plan.updates.append((row["id"], title, url))
        else:
            plan.untouched += 1
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description="Restore source_url from a Paprika archive.")
    ap.add_argument("--archive", required=True, help="The .paprikarecipes export.")
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--live", action="store_true", help="Actually write. Omit for a dry run.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    from supabase import create_client  # noqa: PLC0415
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    entries = PaprikaReader().read_entries(args.archive)
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = (sb.table("recipes").select("id,title,source_url").eq("user_id", args.user_id)
                .order("id").range(offset, offset + PAGE - 1).execute().data or [])
        if not page:
            break
        rows.extend(page)
        offset += PAGE

    plan = plan_restore(entries, rows)
    updated, failed = 0, 0
    for rid, title, url in plan.updates:
        if args.verbose:
            print(f"  {rid}  {title!r}  <- {url}")
        if args.live:
            try:
                sb.table("recipes").update({"source_url": url}).eq("id", rid).execute()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  FAILED to update {rid} {title!r}: {exc}")
                continue
        updated += 1

    print(f"{len(entries)} archive entries, {len(rows)} rows; {plan.untouched} untouched, "
          f"{len(plan.ambiguous)} ambiguous, {len(plan.unmatched)} unmatched")
    print(f"{'updated' if args.live else 'would update'} {updated} recipe(s)" + (f", {failed} failed" if failed else ""))
    if not args.live:
        print("DRY RUN — nothing was written. Re-run with --live to apply.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Check `PaprikaReader.read_entries` exists with that name and returns the list of entry dicts (`grep -n "def read_entries" recipeparser/io/readers/paprika.py`); `backfill_paprika_metadata.py` already calls it, so match that call.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/scripts/test_restore_source_urls.py -q`
Expected: pass. `ruff check scripts tests`: no findings.

- [ ] **Step 5: Commit**

```bash
git add scripts/restore_source_urls.py tests/unit/scripts/test_restore_source_urls.py
git commit -m "feat(scripts): restore source_url from the Paprika archive, by the metadata backfill's matching rule"
```

---

### Task 8: Changelog, the full gate, and the pull request

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Changelog**

Add an entry at the top of `CHANGELOG.md`, in the file's existing format, headed with the next version and dated 2026-09-11, saying: four citation columns written on every insert (`source_kind`, `source_key`, `source_title`, `source_author`), derived reader-first; the book readers no longer write "Title — Author" into `source_url`; the extraction model states the source and byline and is told never to name itself; `scripts/backfill_sources.py` and `scripts/restore_source_urls.py`, dry run by default. Note the deploy order: the Cayenne migration must be applied before this version runs.

- [ ] **Step 2: The full gate**

Run, from the worktree root:

```bash
ruff check recipeparser scripts tests
python -m pytest -q
```

Expected: no ruff findings; every test passes (the RecipeParser baseline on this worktree was 412 unit tests passing before Task 1). Do not set `ALLOW_LIVE_WRITES_IN_TESTS`; there is no `.env` in this worktree and the suite must not need one.

- [ ] **Step 3: Commit and open the pull request**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for the citation columns"
git push -u origin feat/source-citation-columns
```

Open the PR against `master` titled "The four citation columns, written reader-first on every insert, and the two repair scripts". The description names Cayenne's twin PR and states the merge order: **Cayenne first** (its merge deploys the migration), this PR second, then the container redeploy. Gate A in the workstream plan: both merged, Cayenne's parity test green, a fresh URL ingest and a fresh EPUB ingest show the four columns on the device.

- [ ] **Step 4: After merge**

Append a `**Merged:**` line to this plan's header (PR number, date, anything the review changed) before the worktree is removed, per the repo's post-merge convention; and record the same in Cayenne's `ROADMAP.md` Stage 5 item.

## Self-review

- **Spec coverage.** Columns: Task 5 (model, writer) and the Cayenne twin (migration). Per-medium table: EPUB/PDF with and without metadata (Task 4), URL (Tasks 4, 5 with the model's site name and byline), pasted text (Task 5 via `resolve_citation(None, …)`), photo (Stage D, not this plan), Paprika (Task 4). The backfill's seven rules (Tasks 2, 6) including both collapsing spellings and the `Gemini` clearing. The `source_url` restore (Task 7). The prompt instruction (Task 3). `source_hint` rewrite: Stage D, deliberately not here. Normalisation mirrored: the fixture (Task 2).
- **Placeholders.** None: every step carries its code. Task 5 Step 1's pipeline test names fixtures it may have to build inline, and says how.
- **Type consistency.** `Citation(kind, key, title, author)` everywhere; `classify_source` returns `Classified(rule, citation)` and every caller reads `.citation` or `.rule`; `assemble(citation=…)`; `Chunk.citation`; `plan_backfill(rows, force=False) -> List[(id, cols, rule)]`; `plan_restore(entries, rows) -> RestorePlan`.
