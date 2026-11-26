# Architecture compliance roadmap

**Status:** proposed, 2026-11-25. **Owner:** the repository owner.
**Scope:** what RecipeParser checks automatically about its own code, and in what order
the remaining checks are added. It covers the Architecture Compliance Framework (ACF:
`config/architecture/`, the `arch-compliance` plugin) and the standard Python tools that
sit beside it (ruff, mypy, vulture), because both feed the same gates.

## 1. Where we are

### What is checked today (origin/master `04ce4d5`)

| Rule | Kind | Runs where | Notes |
|---|---|---|---|
| `fail-loud.bare-except` | ACF built-in, BLOCK | Claude Code edits | Declares `ci` and `precommit` too, but nothing invokes those sites (below). Flags every broad handler in an edited file, old ones included: noisy until the ratchet (Phase 2) lands. |
| `no-shims.hasattr` | ACF built-in, WARN | nowhere in practice | Declares only `ci` and `precommit`, which nothing invokes. |
| `posix-paths.str-serialization` | ACF built-in, BLOCK | Claude Code edits | Same: `ci`/`precommit` declared, not invoked. |
| `ops.no-build-artifacts` | ACF built-in, BLOCK | Claude Code edits | Same. |
| `soft.fail-loud` | ACF review rule | `/architecture-review` | No `exception_base` set, so "exception outside `RecipeParserError`" is off. |
| `soft.no-shims` | ACF review rule | `/architecture-review` | |
| `soft.layer-imports` | ACF review rule (host prompt) | `/architecture-review` | Also enforced by ruff `TID251`, which nothing runs (below). |
| `soft.state-machines` | ACF review rule | `/architecture-review` | `PipelineController.transition()` / `status`. |
| `soft.class-cohesion` | ACF review rule | `/architecture-review` | |
| ruff `E`, `F`, `I`, `TID` | lint | nobody | Configured in `pyproject.toml`; not in CI or any hook. 69 findings today. |
| mypy | types | nobody | 208 errors in 29 files today; `baseline_mypy.txt` is a stale snapshot. |
| pytest (+ coverage) | tests | CI (`build-installer.yml`) | The only automated gate on a pull request. |

The first review (2026-11-19) and its baseline are committed
(`docs/reviews/architecture/`, `config/architecture/review_baseline.json`). The adversarial
design review is configured (`config/architecture/adr.yml`).

### What is not checked

- **No gate runs the ACF rules on a pull request or a push.** There is no
  `arch-compliance` CI job and `.githooks/pre-push` only checks tag versions. A change made
  outside Claude Code is never checked, and the Claude Code hook only warns the agent.
- **Lint and types are not gated at all.** The ruff config even encodes the layer rule
  (`TID251`), but it runs nowhere, and it is mis-scoped: it fires on the package-root
  re-export modules (`recipeparser/epub.py`, `export.py`, `pdf.py`, `supabase_writer.py`,
  `api.py`, `__main__.py`), which are shims, not core.
- **Nothing finds dead code.** `recipeparser/toc.py` (about 260 lines) and
  `PaprikaReader.read_entries_with_images` lived on with no production caller until the
  2026-11-25 fail-loud review happened to notice them.
- **The rules most specific to this app are written down but unchecked:** one writer of
  recipes, the server as sole writer of derived columns, model SDKs called from one
  place, one clock, and quota and cancel errors never swallowed (the largest theme of the
  2026-11-25 review: about a dozen handlers).

## 2. Principles

1. **One gate, many tools.** ACF is the gate (`acf-check-diff` on edit, push and pull
   request). Standard tools (ruff, mypy, vulture) do what they are good at and are wired
   into the same three places, so a developer meets every rule at the same moments.
2. **Deterministic first, judgment second.** A rule an AST or a linter can decide runs on
   every edit and push. Only what needs judgment goes to `/architecture-review`.
3. **Ratchet, never big-bang.** Each new rule starts from today's count and may only go
   down. Old violations are fixed on their own schedule; new ones are blocked.
4. **Every exception is written down.** One marker per rule (`# <rule-id>-ok: <reason>`),
   never a bare `noqa`. The existing `# noqa: BLE001 — <reason>` comments are converted.

## 3. Phases

Each item names its mechanism, where it runs and what "done" means.

### Phase 0 — Land what is in flight (now)

| Item | Done when |
|---|---|
| The four fail-loud fix PRs (quota/cancel, readers/writers/dead code, pipeline/job losses, probing) | Merged. |
| Re-run `/architecture-review --update-baseline` | Baseline reflects the fixes; committed. |
| `tests/fixtures/servings_source_cases.json` byte check | Fails on every Windows checkout (`core.autocrlf=true` rewrites it). Add `tests/fixtures/servings_source_cases.json -text` (or `eol=lf`) to `.gitattributes`. **Done in 9.46.1** (`text eol=lf`, like the other two shared fixtures). |
| Developer setup | `acf` installed into the interpreter that has the project's dependencies (`C:\Python313\python.exe` here), and `git config core.hooksPath .githooks` documented in the README. **Done in 9.46.2** (README and `.githooks/README.md`). |

### Phase 1 — Make the gates real (week 1)

| Item | Mechanism | Done when |
|---|---|---|
| ACF on every pull request | `.github/workflows/arch-compliance.yml` using the plugin's `check-diff` action at the installed plugin version | Runs on PRs; made a required status check. **Runs on PRs since 9.46.2**; making it required is decision 1 below. |
| ACF before every push | `acf-check-diff --base origin/master --call-site precommit` appended to `.githooks/pre-push` (guarded, with `ACF_PREPUSH_OPTIONAL=1` for environments without the package) | A push with a new bare `except:` is refused locally. **Done in 9.46.2.** |
| ruff in CI | `ruff check` on the PR diff in the same workflow | No new ruff finding can merge. **Done in 9.46.2:** `scripts/ci/ruff_changed_lines.py` reports ruff findings only on the lines a pull request adds or changes (ruff has no diff mode; the 236 older findings are Phase 5's). |
| Fix ruff's layer rule scope | Move the `TID251` ban to `recipeparser/core/**` only (per-file config), so it checks what `soft.layer-imports` describes | `ruff check` reports only real core crossings (none today). **Done in 9.46.2** (`"!recipeparser/core/**" = ["TID251"]` replaces 33 per-file exemptions). |
| Fix the real bug ruff already sees | `recipeparser/adapters/gui.py:507` `F821` undefined `PipelineController` | Fixed. **Done in 9.46.2** (a `TYPE_CHECKING` import; it was a string annotation, so not a runtime error). |

### Phase 2 — Tighten the rules we have (weeks 1–2)

| Item | Mechanism | Done when |
|---|---|---|
| `fail-loud.ratchet` | ACF built-in, `ci` + `precommit`, with `config/architecture/fail_loud_ratchet.json` holding each file's broad-handler count after Phase 0 | The count can only fall; the edit hook stops nagging about old handlers. |
| Exemption marker convention | Convert reasoned `# noqa: BLE001 — …` handlers to `# fail-loud.bare-except-ok: …` | No reasoned handler is reported. |
| `soft.fail-loud` exception base | `params.exception_base: {class_name: RecipeParserError, module: recipeparser/exceptions.py}` | Sub-check 4 runs in the review. |
| `no-shims.hasattr` | Add the `hook` site | Checked on Claude Code edits too, not only on push and PR. |
| Package-root shims | `soft.no-shims` already reports them; schedule their removal (`epub.py`, `export.py`, `pdf.py`, `supabase_writer.py`, `category_sources/base.py`) | Callers import the real modules; shims deleted. |

### Phase 3 — Project rules as deterministic checks (weeks 2–4)

Host detectors (`detector: config/architecture/detectors/<file>.py:<callable>`), run on edit,
push and PR. Each person approves them once with `acf-allow`; CI trusts them through the
workflow.

| Rule id (proposed) | What it checks | Source of the rule |
|---|---|---|
| `fail-loud.quota-cancel-propagate` | Every `except Exception` / bare `except` under `recipeparser/` either re-raises, or is preceded in the same `try` by a handler that re-raises `JobCancelledError` and `DailyQuotaExhaustedError` (or a `RecipeParserError` base of both), or carries the exemption marker | `recipeparser/exceptions.py` docstrings |
| `layers.model-sdk-owners` | `google.genai` / `anthropic` are imported only by `gemini.py`, `categories.py`, `core/picture_gen.py`, `core/picture_check.py`, `adapters/ai_search/**` | ARCHITECTURE.md §5, §15 |
| `layers.one-clock` | No `datetime.now()` / `utcnow()` / `time.time()` used to stamp data outside `core/clock.py` (timers and timeouts exempt by name) — or ruff `DTZ` plus a short allowlist | ARCHITECTURE.md module map (`clock.py`) |
| `secrets.service-role-key` | The Supabase service-role key is read only in `config.py` and the server's start-up | ARCHITECTURE.md §12, §15 |
| `ownership.derived-columns` | `structured_ingredients`, `tokenized_directions`, `embedding`, `derived_rev` are written only by `io/writers/supabase.py`, `adapters/regen_worker.py`, `adapters/embed_worker.py` | ARCHITECTURE.md §13 |

`soft.layer-imports` stays as a review rule for lazy, `TYPE_CHECKING` and indirect
crossings; the plain-import case moves to ruff (Phase 1).

### Phase 4 — New review rules (month 2)

Rules the plugin ships, added as `auditor` rows with settings:

| Rule | Settings sketch | Why here |
|---|---|---|
| `single-owner` | `recipes` table (`target_literal_re` on the table name), owner `io/writers/supabase.py`; `categories.yaml`, owners `categories.py`, `adapters/gui.py` | "The API is the sole writer of recipes" (module map). |
| `pipeline-isolation` | `shared_stages`: `core/stages/{extract,refine,tag,embed}.py`; `variant_stages`: the Cayenne restore path; `variant_terms`: `InputType.CAYENNE*`, `pre_parsed`, `_cayenne_meta` | Two 2026-11-25 findings were shared code probing the restore variant. |
| `data-access-boundary` | `policy_files`: the Supabase migrations; `client_paths`: none in this repo | RLS and the service-role key. Blocked until the migrations are tracked here (today `supabase/` is untracked) or the rule points at Cayenne's. |
| `ui-logic-isolation` | surface `adapters/gui.py` | Low priority. |

Not adopted: `presentation-as-data` (no style schema here), `path-provenance` (no
cross-platform path classifier needed).

### Phase 5 — Code standards and dead code (alongside Phases 2–4)

These are not architecture rules, so they are not ACF mandates, but they run in the same
CI job and pre-push hook and follow the same ratchet principle.

**Coding standards — ruff.** Today's selection (`E`, `F`, `I`, `TID`) has 69 findings
(35 long lines, 20 import orders, 6 unused imports, 6 mis-scoped layer bans, 1 ambiguous
name, 1 undefined name). Proposed additions, in order, with today's counts on
`recipeparser/`:

| Rules | Count | Notes |
|---|---|---|
| `BLE` (blind except) | 36 | Overlaps `fail-loud.bare-except`; adopt so non-Claude edits get the same check. |
| `B` (bugbear) | ~50 | `B008` defaults (39, mostly FastAPI `Depends` — configure `extend-immutable-calls`), `B905`, `B007`. |
| `ARG`, `ERA`, `PIE`, `RET`, `SIM` | ~25 | Unused arguments, commented-out code, small simplifications. |
| `S` (bandit) | ~15 | Review `S608` (SQL strings) by hand; the search gate already parses SQL. |
| `DTZ` | 0 | Free to adopt now; backs `layers.one-clock`. |
| `UP` (pyupgrade) | ~1,500 | Almost all auto-fixable annotation modernisation; one mechanical PR, not a ratchet. |

**Types — mypy.** 208 errors today. Record a per-file count (like the fail-loud ratchet),
run mypy in CI on changed files, and refuse a file whose count rises. Delete the stale
`baseline_mypy.txt` / `baseline_ruff.txt` / `baseline_violations.txt` once the ratchets
replace them.

**Dead code — vulture plus a production-reachability check.**
- `vulture recipeparser/ --min-confidence 80` in CI, with a reviewed whitelist file
  (FastAPI routes, Pydantic validators, CLI entry points, `__all__` re-exports). At
  confidence 60 it lists 105 candidates today (84 functions, methods or classes), most of
  them framework-registered; triage once into fixes and whitelist entries.
- A small host check, `deadcode.test-only-modules`: a module under `recipeparser/` that
  only `tests/` imports is reported. This is the case vulture cannot see, because the
  tests count as callers; it is exactly how `toc.py` survived.
- Optional: `deptry` for declared-but-unused and used-but-undeclared dependencies.

## 4. Decisions needed

1. **Required checks:** make the ACF/ruff CI job required on `master` (repository
   settings), or keep it advisory until Phase 2's ratchet removes the old-violation noise.
2. **Supabase migrations:** track them in this repository, or point
   `data-access-boundary` at Cayenne's.
3. **`UP` modernisation:** one mechanical PR now, or leave annotations as they are.
4. **mypy:** ratchet it (proposed), or drop it and rely on tests.

## 5. How to change this roadmap

Each phase lands as ordinary PRs that edit `config/architecture/mandates.yml` (with
`/acf-mandate`), `config/architecture/ARCHITECTURE.md` (the anchor sections) and, for
Phase 1 and 5, `pyproject.toml` and `.github/workflows/`. Tick items off here in the same PR.
