# Architecture compliance roadmap

**Status:** in progress: Phases 0 to 2 done, Phase 3 started; outside review of 2026-11-28 folded in as Phase 3b (updated 2026-11-28). **Owner:** the repository owner.
**Scope:** what RecipeParser checks automatically about its own code, and in what order
the remaining checks are added. It covers the Architecture Compliance Framework (ACF:
`config/architecture/`, the `arch-compliance` plugin) and the standard Python tools that
sit beside it (ruff, mypy, vulture), because both feed the same gates.

## 1. Where we are

### What is checked today (9.51.3)

| Rule | Kind | Runs where | Notes |
|---|---|---|---|
| `fail-loud.bare-except` | ACF built-in, BLOCK | `ci`, `precommit`, `hook` | Pull requests and pushes since 9.46.2. The edit hook still reports every unexempted handler in a file it scans. |
| `fail-loud.ratchet` | ACF built-in, BLOCK | `ci`, `precommit` | Since 9.49.1: each file's broad-handler count may only fall. |
| `fail-loud.quota-cancel-propagate` | host detector, BLOCK | `ci`, `precommit` | Since 9.51.2: an added broad handler must let `JobCancelledError` and `DailyQuotaExhaustedError` through. Approved once per repository with `/acf-approve`. |
| `no-shims.hasattr` | ACF built-in, WARN | `ci`, `precommit`, `hook` | |
| `posix-paths.str-serialization` | ACF built-in, BLOCK | `ci`, `precommit`, `hook` | |
| `ops.no-build-artifacts` | ACF built-in, BLOCK | `ci`, `precommit`, `hook` | |
| `soft.fail-loud` | ACF review rule | `/architecture-review` | `exception_base` set since 9.49.1. |
| `soft.no-shims` | ACF review rule | `/architecture-review` | |
| `soft.layer-imports` | ACF review rule (host prompt) | `/architecture-review` | Plain imports also checked by ruff `TID251`, declared in `recipeparser/core/ruff.toml`. |
| `soft.state-machines` | ACF review rule | `/architecture-review` | `PipelineController.transition()` / `status`. |
| `soft.class-cohesion` | ACF review rule | `/architecture-review` | |
| ruff `E`, `F`, `I`, `TID` | lint | CI (`ruff` job) | Since 9.46.2, on the lines a pull request changes; older findings are Phase 5's. |
| `layers.model-sdk-owners` | ruff `TID251` | CI (`ruff` job) | Since 9.51.3: only the listed owners import `google.genai` / `anthropic` (doctrine "Model SDK owners"). |
| `layers.one-clock` | ruff `DTZ` | CI (`ruff` job) | Since 9.51.3: no naive datetime under `recipeparser/` (doctrine "One clock"). |
| dependencies | `deptry` and the lock check | CI (`dependencies` job) | Since 9.51.4: every import under `recipeparser/` is declared in `pyproject.toml`, and `requirements.txt` is its generated lock. |
| mypy | types | nobody | 208 errors in 29 files at 2026-11-25; `baseline_mypy.txt` is a stale snapshot. |
| pytest (+ coverage) | tests | CI (`build-installer.yml`) | |

`arch-compliance` and `ruff` run on every pull request but are not yet required checks
(decision 1, recommended now), so a red check can still merge. The reviews of 2026-11-19 and 2026-11-25
and the baseline are committed (`docs/reviews/architecture/`,
`config/architecture/review_baseline.json`). The adversarial design review is configured
(`config/architecture/adr.yml`).

### What is not checked

- **Nothing finds dead code.** `recipeparser/toc.py` (about 260 lines) and
  `PaprikaReader.read_entries_with_images` lived on with no production caller until the
  2026-11-25 fail-loud review happened to notice them (Phase 5).
- **Most rules specific to this app are written down but unchecked:** one writer of
  recipes, the server as sole writer of derived columns (Phases 3 and 4). Quota and cancel
  propagation is checked since 9.51.2; model SDK owners and one clock since 9.51.3.
- **Types are not gated** (Phase 5, decision 4).
- **`core/` reaches the model layer directly.** `core/pipeline.py` and the `extract`,
  `refine` and `embed` stages import `recipeparser.gemini`. The layer ban covers only
  `io/` and `adapters/`, so this passes unseen, and `import recipeparser` loads `.env` and
  builds a `genai.Client` as a side effect (Phase 3b).

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
| The four fail-loud fix PRs (quota/cancel, readers/writers/dead code, pipeline/job losses, probing) | Merged. **Done** (#177 to #180). |
| Re-run `/architecture-review --update-baseline` | Baseline reflects the fixes; committed. **Done** (#181, review of 2026-11-25). |
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
| `fail-loud.ratchet` | ACF built-in, `ci` + `precommit`, with `config/architecture/fail_loud_ratchet.json` holding each file's broad-handler count after Phase 0 | The count can only fall. **Done in 9.49.1:** 141 handlers in 58 files under `recipeparser/` and `scripts/`. The ratchet does not quiet the edit hook, which still reports every unexempted handler in a file it scans; the 50 handlers with no reason on the line need one (or a fix) for that. |
| Exemption marker convention | Convert reasoned `# noqa: BLE001 — …` handlers to `# fail-loud.bare-except-ok: …` | No reasoned handler is reported. **Done in 9.49.1:** 31 handlers converted to `# noqa: BLE001  # fail-loud.bare-except-ok: <reason>`. |
| `soft.fail-loud` exception base | `params.exception_base: {class_name: RecipeParserError, module: recipeparser/exceptions.py}` | Sub-check 4 runs in the review. **Done in 9.49.1.** |
| `no-shims.hasattr` | Add the `hook` site | Checked on Claude Code edits too, not only on push and PR. **Done in 9.49.1.** |
| Package-root shims | `soft.no-shims` already reports them; schedule their removal (`epub.py`, `export.py`, `pdf.py`, `supabase_writer.py`, `category_sources/base.py`) | Callers import the real modules; shims deleted. **Done in 9.49.0** (all six, including `recipeparser/api.py`). |

### Phase 3 — Project rules as deterministic checks (weeks 2–4)

Host detectors (`detector: config/architecture/detectors/<file>.py:<callable>`), run on edit,
push and PR. Each person approves them once with `acf-allow`; CI trusts them through the
workflow.

| Rule id (proposed) | What it checks | Source of the rule |
|---|---|---|
| `fail-loud.quota-cancel-propagate` | Every `except Exception` / bare `except` under `recipeparser/` either re-raises, or is preceded in the same `try` by a handler that re-raises `JobCancelledError` and `DailyQuotaExhaustedError` (or a `RecipeParserError` base of both), or carries the exemption marker. **Done in 9.51.2** (`config/architecture/detectors/quota_cancel.py`, added lines only, `ci` + `precommit`; 88 existing handlers left to fix or exempt as each is edited). | `recipeparser/exceptions.py` docstrings |
| `layers.model-sdk-owners` | `google.genai` / `anthropic` are imported only by `gemini.py`, `categories.py`, `core/picture_gen.py`, `core/picture_check.py`, `adapters/ai_search/**`. **Done in 9.51.3** as ruff `TID251`; the real owner list (entry points that build clients, adapters that catch `APIError`) is in the doctrine's "Model SDK owners". | ARCHITECTURE.md §5, §15 |
| `layers.one-clock` | No `datetime.now()` / `utcnow()` / `time.time()` used to stamp data outside `core/clock.py` (timers and timeouts exempt by name) — or ruff `DTZ` plus a short allowlist. **Done in 9.51.3** as ruff `DTZ`: `clock.py` owns the format, not every reading of the time, so the rule is "no naive datetime". | ARCHITECTURE.md module map (`clock.py`) |
| `secrets.service-role-key` | The Supabase service-role key is read only in `config.py` and the server's start-up | ARCHITECTURE.md §12, §15 |
| `ownership.derived-columns` | `structured_ingredients`, `tokenized_directions`, `embedding`, `derived_rev` are written only by `io/writers/supabase.py`, `adapters/regen_worker.py`, `adapters/embed_worker.py` | ARCHITECTURE.md §13 |

`soft.layer-imports` stays as a review rule for lazy, `TYPE_CHECKING` and indirect
crossings; the plain-import case moves to ruff (Phase 1).

### Phase 3b — Boundary, dependency and doc fixes (from the 2026-11-28 outside review)

An outside review of the repository made five recommendations; each claim was checked
against master at 9.51.3, one of them wrongly: see the first row. Items 1 and 5 were already here (decision 1, Phase 5) and are
updated in place; the rest are new. Suggested order: decision 1, then the items below in
table order. The docs rewrite comes last so it describes the shape the boundary fix leaves.

| Item | Mechanism | Done when |
|---|---|---|
| Declare and unify dependencies | Declare `requests` and `postgrest`; generate `requirements.txt` from `pyproject.toml` (`scripts/ci/lock_requirements.py`); `deptry` in CI | No module under `recipeparser/` imports an undeclared package; the lock matches `pyproject.toml`. **Done in 9.51.4.** The review's proposal to replace `requests` with `httpx` is **rejected**: through the page proxy, Dotdash Meredith's sites answered `httpx` 403 and `requests` 200 (Cayenne F-687, `_fetch_page_meta`'s docstring), so the swap would bring that bug back. |
| Dead models and package-wide ruff | Delete `TocEntry`, `TocList`, `TocRecipeClassification` from `models.py` (no reference outside it); clear the 38 findings `ruff check recipeparser` reports today | `ruff check recipeparser` is clean, so CI runs it on the whole package, not only on changed lines; `scripts/` and `tests/` stay diff-only. |
| `core/` takes its model calls by injection | A typed protocol for the model calls (`LLMCalls`, or one callable per stage) passed into `RecipePipeline`, as `CategorySource`, `ImageStore` and `AskFood` already are; `core/` stops importing `recipeparser.gemini`. A real refactor: spec first, through `/adversarial-design-review` | `recipeparser.gemini` joins the banned-api list in `recipeparser/core/ruff.toml`, and ruff reports nothing. |
| No import side effects | `load_dotenv()` and `genai.Client(...)` move out of `recipeparser/__init__.py` into the entry points (`__main__`/`adapters/cli.py`, `adapters/gui.py`, `adapters/api.py`); the 7 scripts that read `recipeparser.client` build their own | `import recipeparser` reads no file and builds no client; `__init__.py` leaves the model SDK owners (`pyproject.toml` per-file-ignores, doctrine "Model SDK owners"). |
| Rewrite `GEMINI.md`, prune `ARCHITECTURE.md` | `GEMINI.md` describes 9.51+ (`RecipePipeline`, `PipelineController`, the current model ids, no package-root shims or `api.py`); `ARCHITECTURE.md` loses the design blocks that were never built and names each model once, pointing at `config.py` | Both match the code; `soft.layer-imports`, which cites `GEMINI.md`, reads current guidance. |

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

**Coding standards — ruff.** Today's selection (`E`, `F`, `I`, `TID`, `DTZ`) has 38 findings
under `recipeparser/` (2026-11-28; Phase 3b clears them). Proposed additions, in order, with today's counts on
`recipeparser/`:

| Rules | Count | Notes |
|---|---|---|
| `BLE` (blind except) | 36 | Overlaps `fail-loud.bare-except`; adopt so non-Claude edits get the same check. |
| `B` (bugbear) | ~50 | `B008` defaults (39, mostly FastAPI `Depends` — configure `extend-immutable-calls`), `B905`, `B007`. |
| `ARG`, `ERA`, `PIE`, `RET`, `SIM` | ~25 | Unused arguments, commented-out code, small simplifications. |
| `S` (bandit) | ~15 | Review `S608` (SQL strings) by hand; the search gate already parses SQL. |
| `DTZ` | 0 | **Adopted in 9.51.3** (`layers.one-clock`). |
| `UP` (pyupgrade) | ~1,500 | Almost all auto-fixable annotation modernisation; one mechanical PR, not a ratchet. |

**Types — mypy.** 197 errors in 27 files (2026-11-28; was 208 in 29), among them real
`arg-type` / `assignment` errors in `adapters/api.py` worth fixing first. Record a per-file count (like the fail-loud ratchet),
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
- `deptry` moved to Phase 3b (done in 9.51.4; it found two real cases, `requests` and `postgrest`).

## 4. Decisions needed

1. **Required checks:** make `arch-compliance` and `ruff` required on `master` (repository
   settings). Recommended now: both check only changed lines and the Phase 2 ratchet has
   landed, so the old-violation noise this waited on is gone (the 2026-11-28 outside
   review recommends it too).
2. **Supabase migrations:** track them in this repository, or point
   `data-access-boundary` at Cayenne's.
3. **`UP` modernisation:** one mechanical PR now, or leave annotations as they are.
4. **mypy:** ratchet it (proposed), or drop it and rely on tests.

## 5. How to change this roadmap

Each phase lands as ordinary PRs that edit `config/architecture/mandates.yml` (with
`/acf-mandate`), `config/architecture/ARCHITECTURE.md` (the anchor sections) and, for
Phase 1 and 5, `pyproject.toml` and `.github/workflows/`. Tick items off here in the same PR.
