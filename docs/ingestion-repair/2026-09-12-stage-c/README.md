# Stage C, the live-library repair — 2026-09-12

The script half of Stage C of the Add Recipe workstream (Cayenne
`docs/superpowers/plans/2026-09-11-add-recipe-workstream.md`), run against the
live project with the service-role key after Cayenne PR #58 (the columns) merged.
787 recipes (788 on 2026-09-11; one row had gone). Archive of record:
`Export 2026-03-23 20.31.13 Mains, Simple Dinner.paprikarecipes` (827 entries).

| Step | File | Result |
|---|---|---|
| Before | `before.txt` | 740 with `source`, 5 with `source_url`, 27 raw strings |
| Backfill dry run | `backfill-dry.txt` | 47 / 454 / 166 / 112 / 1 / 3 / 4 by rule, 787 would update |
| Backfill live | `backfill-live.txt` | 787 updated, 0 failed |
| Restore dry run | `restore-dry.txt` | 114 would update; 9 ambiguous, 68 unmatched (the parked lost entries) |
| Restore live | `restore-live.txt` | 114 updated, 0 failed |
| After | `after.txt`, `after-columns.txt` | kinds book 454 · unknown 216 · web 113 · person 4; 0 unclassified; `Gemini` 0; one key per site; 119 rows with an http `source_url` |

Left for the by-hand *Set source* passes once Stage B ships the sheet: the 166
`unknown-book` rows (Italian Food, Elizabeth David), the 3 cleared `Gemini` rows,
and the two truncated `Perfect` / `Completely Perfect` rows now filed as `person`.
`source` still reads `EPUB Auto-Import` on the 166 until then; the kitchen line
shows nothing for them (no title, and the fallback text is the reader's marker).