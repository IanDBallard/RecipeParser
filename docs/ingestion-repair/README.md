# Ingestion fixes and library repair

Working documents for the seven ingestion defects found after the 2026-09-04
import of an 827-recipe Paprika export lost twenty recipes and all 466
photographs while reporting success.

These were authored in the **Cayenne** repository (`IanDBallard/Cayenne`), which
holds the deferred ledger the findings were filed into. They are copied here so
this branch is self-contained: the code being changed is in this repository, and
nobody reading a commit should have to go and find the reasoning in another one.

| File | What it is |
|---|---|
| `SPEC-ingestion-repair-design.md` | The design. Sections 4 and 7 govern the work in this repository. |
| `PLAN-1-ingestion-api-fixes.md` | **This branch.** Ten tasks implementing spec section 4. |
| `PLAN-2-cayenne-skip-reporting.md` | The Cayenne half — migrations, sync, the banner. Here for the column names and the shape of what the client expects. |
| `PLAN-3-library-repair.md` | The repair scripts, which also live in this repository under `scripts/`, plus the runbook. Runs after this branch ships. |
| `LEDGER-the-ingestion-api.md` | The seven findings as originally recorded, verbatim. |

## Issues

| # | Finding | Spec |
|---|---|---|
| [#6](https://github.com/IanDBallard/RecipeParser/issues/6) | Silent chunk drops: a truncated extraction reply loses its recipes | §4.1 |
| [#7](https://github.com/IanDBallard/RecipeParser/issues/7) | Nothing is written until every chunk finishes | §4.2, §4.3 |
| [#8](https://github.com/IanDBallard/RecipeParser/issues/8) | Job progress is only ever 0 or 100 | §4.4 |
| [#9](https://github.com/IanDBallard/RecipeParser/issues/9) | Embedded photographs are read and dropped | §4.5 |
| [#10](https://github.com/IanDBallard/RecipeParser/issues/10) | Job status and control endpoints are unauthenticated | §4.6 |
| [#11](https://github.com/IanDBallard/RecipeParser/issues/11) | The service key is read under two different variable names | §4.7 |
| [#12](https://github.com/IanDBallard/RecipeParser/issues/12) | An unquantified ingredient is stored as a zero | §4.8 |

## Source of truth for the repair

`C:\Users\iball\My Drive\Cooking Stuff and Restaurants\Export 2026-03-23 20.31.13 Mains, Simple Dinner.paprikarecipes`
— 827 entries, 466 carrying `photo_data`, no loose image files. Verified 2026-09-05.

## If these drift

The Cayenne copies are canonical. Re-copy rather than editing here, and if a
change is needed, make it there first.
