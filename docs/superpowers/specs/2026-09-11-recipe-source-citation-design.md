# Recipe source citation — design

**Date:** 2026-09-11
**Status:** decided 2026-09-11 (the sheet as collapsed groups and the four-step repair approved on the canvas). Not yet planned.
**Amends:** [Library](../../../SpecificationDocumentation/UI_UX_LIBRARY.md), *Sort and filter* (the "no source filter" paragraph is reversed); the Paprika metadata import design (P5, "No source facet")
**Canvas:** the Library canvas, *Sort & filter* and *Set source* boards (<https://claude.ai/code/artifact/b36c7ac9-0b10-45cb-9d8f-a543749f1e97>); the Add Recipe canvas, *Recent imports* (<https://claude.ai/code/artifact/fdfd75ec-c003-4b31-9968-71c90190e087>)

## Why

A cook restricts to source material: everything from one cookbook, everything by one author, everything clipped from one site. The library can sort by source and can find a source through the search box, and neither is a filter. Search matches titles as well as sources, is cut at fifty rows and ranked, and occupies the one box, so "quick weeknight" within one author is not expressible. Rating already has the shape this needs: a predicate applied before the cap, combinable with search and categories. Source, author and kind take that shape.

What stops it today is not the control but the data. A recipe records where it came from in two overloaded columns, and the measurement below shows what is in them.

## The measured library (2026-09-11)

`scripts/measure_sources.py`, read-only over the REST API, against the 788-recipe library:

| | |
|---|---|
| `source` set | 740 (93.9 %) |
| `source_url` set | 5 (0.6 %), all http, one of them example.com |
| both | 0 |
| neither | 43 (5.5 %) |

`source` holds 27 raw strings, 22 once case and a leading `www.` are folded:

- **Books, "Title — Author"**, about 550 recipes across ten titles: The Food of Sichuan 123, An Invitation to Indian Cooking 84, Joe Beef 63, The Woks of Life 52, Flour + Water 48, Completely Perfect 38, three Paul Hollywood titles 19 + 11 + 11, Classic German Baking 5.
- **`EPUB Auto-Import`**, 166 recipes: the readers' fallback when a book carries no title or author metadata. One EPUB, *Italian Food* by Elizabeth David, identified 2026-09-11 from the rows themselves (one nine-minute run on 2026-09-05; titles A to Z in the book's style; no matching ingestion job, so a CLI import). No rule can name it from the data; *Set source* does.
- **Site domains**, about 115 recipes across eleven sites: cooking.nytimes.com 71 plus 4 in lower case, seriouseats.com 19, The Woks of Life's site in two spellings 5 + 2, theguardian.com 5, washingtonpost.com 2, six sites with one each.
- **Oddballs**: `Gemini` 3 (the model named itself), `Nik Sharma` 2 (a person), `Perfect` 1 and `Completely Perfect` 1 (truncations of the Felicity Cloake book), `Womanandhome.com Justin Gellatly` 1 (a domain followed by an author).

Two things follow. The book readers' string and the Paprika clipper's domain both landed in `source`, not `source_url`, so any derivation reads `source`. And the archive's per-entry clip URL was dropped by the bulk import; it is recoverable from the archive with the metadata backfill's pattern, and doing so is what gives the kitchen page a working source link for those 115 recipes.

## Principle

**Provenance is written as structured citation fields by the ingestion API at insert time, derived deterministically wherever a deterministic source exists, and the free-text `source` stays as the display string.** The model is asked for what only it can read, a stated publication or a byline in pasted text, and never for what the reader already knows. A cook can correct any of it in one gesture from the library.

## What changes

### The columns

Four nullable text columns on `recipes`, synced like the six metadata columns of migration 012:

| Column | Meaning | Values |
|---|---|---|
| `source_kind` | the citation type | `book`, `web`, `periodical`, `person`, `unknown`. A closed list held small; a new kind is a migration that extends the check constraint and one pill, so the list grows as new kinds arrive rather than being guessed now. |
| `source_key` | the identity the filter and the Source sort run on | a book: the normalised title; web: the host, lower-cased, without `www.`; person: the normalised name; unknown: `unknown-book` when the reader knew it was a book, else `null` |
| `source_title` | what the Source pill and the kitchen line show | the book title; the site's stated name if the model read one, else the host; a person's name |
| `source_author` | what the Author pill shows | the book's author from metadata; a byline the model read; `null` otherwise |

`source` is unchanged and remains the display string where the four are null. `source_url` returns to meaning a URL: the book readers stop writing "Title — Author" into it.

Normalisation, one function in the parser and mirrored in `cayenne-web/src/lib/domain/`: trim, lower-case, strip a leading `http(s)://` and `www.`, strip quotation marks, collapse runs of whitespace and `-–—_.,:;/` to one space. It is the key, never shown.

### What the API writes, per medium

| Medium | kind | key | title | author |
|---|---|---|---|---|
| EPUB, PDF with metadata | `book` | normalised title | DC or PDF title | DC creator or PDF author |
| EPUB, PDF without metadata | `unknown` | `unknown-book` | `null` | `null` |
| URL | `web` | host of the URL | the page's stated site name (the model, from the markdown; `og:site_name` or a JSON-LD publisher when the scrape carries one), else the host | the byline, if the model reads one |
| Pasted text | from the model's stated source, classified by the same rule the backfill uses | as for that kind | | |
| Photo (when the image reader lands) | as pasted text, from the transcription | | | |
| Paprika entry | from the entry's `source` by the backfill rule; `source_url` from the entry | | | |

The job row gains nothing: `ingestion_jobs.source_hint` becomes the recipes' `source_key` once the reader has returned (the API already updates the row at that moment to write `total_chunks`), so a Recent-imports row on the Add Recipe screen opens the library with that source selected, exactly, for books and sites alike.

### The backfill, per measured value

A one-off script over `source` (the metadata backfill's pattern; dry run by default), in this order, first match wins:

1. `null` or blank → kind `unknown`, key `null`. 43 rows: the "No source" pill.
2. Contains ` — ` → kind `book`; title and author are the two halves, trimmed; key from the title. About 550 rows.
3. Equals `EPUB Auto-Import` or `PDF Auto-Import` → kind `unknown`, key `unknown-book`. 166 rows: the "Unknown book" pill.
4. Matches `^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+$` (a bare domain) or starts with `http` → kind `web`; key the host, lower-cased, without `www.`; title the host as written on the majority of its rows. 115 rows; collapses the two spellings of the Times and of The Woks of Life.
5. A domain token followed by text → kind `web` with the author from the remainder. 1 row.
6. Equals `Gemini` → kind `unknown`, key `null`, and the value is cleared: it is the model naming itself, a prompt defect fixed alongside (the source instruction gains "never the name of a model or a tool"). 3 rows.
7. Anything else → kind `person`, title and key from the value. 2 rows (Nik Sharma); the two truncated "Perfect" rows fall here too and are corrected by hand with *Set source*.

A second script restores `source_url` from the Paprika archive by the entry-matching rule the metadata backfill established.

### The library

The *Sort & filter* sheet keeps Sort and Rating as `Pill` groups at the top, one row each, and gains three **`Disclosure` rows** beneath them, in this order: **Source**, **Author**, **Kind**. Each row is 44 px, collapsed by default, and prints its current selection beside its label ("Source  The Food of Sichuan", "Author  Any"), so the sheet at rest is about 380 px and every control is visible without scrolling. The first cut put eighteen Source pills in the sheet and pushed Author, Kind and Rating below the fold; a list is paid for only when the cook opens it.

Open, a group is a wrap of 44 px multi-select `Pill`s with the count after the name in the 12 px regular role: `Pill`'s `detail` rendered beside the label rather than under it, a `detailInline` prop, so a pill stays 44 px rather than 52.

- **Source**: one list of book titles and site hosts, sorted by count, including `Unknown book` and `No source`. Above thirty entries a "Find a source" `TextInput` heads the open list; at the measured 22 it does not appear.
- **Author**: from `source_author`, sorted by count.
- **Kind**: the kinds present, with counts.

Each is a predicate before the fifty-row cap in both `recipesForLibrary` and `searchCandidates`, `IN` over a JSON list like the category filter. A chosen value joins the chip row as a removable compact `Pill` beside the category and rating chips. Sort by Source orders by `source_key`, then title, `NULLS LAST`.

**Set source**, the repair, in four steps (the canvas draws each):

1. In *Sort & filter*, open Source and choose `Unknown book · 166`. The chip row reads "Unknown book"; the list is those recipes.
2. Press **Select** (the `list-checks` button, ringed while on). The bar docks as x · Select all · **pencil** · Delete; Select all becomes Select none and Delete reads the count. The pencil is the new fourth control, "Set source": 44 + 110 + 44 + 100 and three 8 px gaps is 322 px, inside the phone's 358.
3. Press the pencil. A `Sheet`: Kind pills, Title and Author `TextInput`s, an optional Link, and a collapsed "Pick an existing source instead" whose pills fill the fields for recipes that belong to a source the library already has. One primary button, "Apply to N recipes".
4. Apply writes `source`, the four columns and, when a link was typed, `source_url` on the selected rows locally through PowerSync, the way the editor writes a recipe; they sync up. Selection mode ends, the filter chip follows the recipes to their new source so the cook is looking at what they just filed, and a success notice says "166 recipes filed under The Woks of Life".

The same steps repair the three `Gemini` rows and the two truncated `Perfect` rows on smaller selections. None of it needs the parser: it is a client write of five columns. **It precedes the Add Recipe build**: that screen's Recent-imports rows open the library on a source key, and a library whose sources are wrong opens on the wrong list.

### The kitchen page

The source line reads `source_title`, with the author after a middle dot when present, linked when `source_url` exists. Unchanged otherwise.

## Touchpoints

RecipeParser: `models.py` (four fields), the readers (`book_source` split into title and author, no longer written to `source_url`), `adapters/api.py` (the per-medium table; `source_hint` rewritten when the reader returns), the extraction prompt (stated source, byline; never a model's name), `io/writers/supabase.py`, one migration with the check constraint, the two backfill scripts. Cayenne: the migration mirrored, `powersync/sync-rules.yaml` and `AppSchema` (the parity test catches a miss), `RecipeRow`, the two library queries and the `libraryFilter` store, `Pill` (`detailInline`), `SortFilterSheetBody` (three `Disclosure` rows), `SelectionBar` and a `SetSourceSheetBody`, the kitchen source line, the Library document.

## Rejected alternatives

**A source filter over the raw `source` column.** 27 pills with the Times twice and the truncated book beside the whole one. The normalised key exists to stop that, and once it exists it should be stored, not recomputed on every query.

**`job_id` on every recipe** as the way a Recent-imports row finds what it added. Exact, but a second identity for a question the source key answers, and useless for the library's own purposes. Rewriting `source_hint` to the key gives the row the same answer.

**Kind as the medium** (EPUB, PDF, HTML, photo). How a recipe arrived is the job's business and stays on `ingestion_jobs`; a cook filters by what a source is, not by which reader parsed it.

**Deriving the four on the client at query time.** Keeps the schema still, but a predicate before the cap has to be SQL, and every device would redo the same split on every keystroke.

## Testing

Parser: a golden per row of the per-medium table; the backfill's seven rules each against a literal from the measured list, including the two collapsing spellings. Client: the token census is untouched (the sheet uses existing primitives); `tests/unit/services/recipeDb.test.ts` gains the three predicates in both queries; a browser test mounts the selection bar with four controls at 360 px, since the count in that bar is exactly what F4 was about; the sync-rules parity test guards the four columns.

## Out of scope

Editing a single recipe's source from the editor (the editor's own design decides its fields). A "Periodical" classification rule: nothing in the measured data is one, and the kind exists so the first magazine import has somewhere to go. Deduplicating recipes across sources.
