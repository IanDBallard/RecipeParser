# Architecture doctrine — RecipeParser

Entry point for the arch-compliance plugin: `/arch-compliance:adversarial-design-review`
reads it, and so do the mandate rows in `config/architecture/mandates.yml`, which anchor
on the `##` headings below. Read, in this order:

1. `GEMINI.md`: the project mandates. Its *Development Conventions* section is the
   rule list; read it in full.
2. `ARCHITECTURE.md` (repository root): the design. It covers the module map, the
   engine contract, the externalized FSM, the adapter contracts, and the Supabase and
   PowerSync job-status model.
3. `README.md`: the run and test commands.

## High-frequency misses

Check these first:

- `GEMINI.md` *Development Conventions*:
  - `recipeparser/core/` never imports from `recipeparser/io/` or
    `recipeparser/adapters/`;
  - errors use the hierarchy in `recipeparser/exceptions.py`, and providers return
    `None` on transient failures;
  - recipe data uses the Pydantic models in `recipeparser/models.py`, which Cayenne
    shares;
  - tuneable constants live in `recipeparser/config.py`.
- The provider-interface convention in `GEMINI.md` (all LLM and embedding calls go
  through `LLMProvider` / `EmbeddingProvider`) describes a design that was never built.
  The root `ARCHITECTURE.md` §5 says so ("a design that was never built"):
  `recipeparser/core/providers/` holds an empty `__init__.py`, and the Gemini calls are
  made directly from `recipeparser/gemini.py` and `recipeparser/categories.py`. Do not
  cite that convention as a rule a spec must meet.
- `ARCHITECTURE.md` §4 (Externalized FSM), §10 (Adapter Contracts) and §12 (Ingestion
  Job Status).

## Fail loud

Failures must not disappear. Errors raise a class from the `RecipeParserError` hierarchy
in `recipeparser/exceptions.py`. Do not use a bare `except:`, or an `except Exception:`
that swallows the error, unless it is exempted with a reason. A provider call is the
one sanctioned exception (`GEMINI.md`, Error Handling): on a transient API failure it
returns `None`, and the adapter handles the `None` case.

## Fail-loud ratchet

The number of bare or broad `except` handlers in each file under `recipeparser/` and
`scripts/` may only fall. `config/architecture/fail_loud_ratchet.json` holds each file's
count, and `acf-check-diff` refuses a file whose count rose, on pull requests and before
every push. An exempted handler still counts. When a change removes handlers, regenerate
the baseline in the same change so the lower count is what is held:

```bash
python -c "import json,acf.detectors.fail_loud_ratchet as r,pathlib;print(json.dumps({'files':r.scan_tree(pathlib.Path('.'),roots=['recipeparser','scripts'])},indent=2,sort_keys=True))" > config/architecture/fail_loud_ratchet.json
```

A handler kept on purpose carries its reason on the `except` line:
`# fail-loud.bare-except-ok: <reason>` (after any `# noqa: BLE001`). That marker satisfies
the per-line rule but not the ratchet. A change that adds a broad handler on purpose
raises that file's count in the baseline itself, so the rise is visible in review.

## Quota and cancel propagate

`JobCancelledError` and `DailyQuotaExhaustedError` are not failures of one recipe or one
chunk: a cancel ends the job as 'cancelled', and an exhausted daily quota stops the whole
run (`recipeparser/exceptions.py`). Every handler under `recipeparser/` that would catch
one of them must let it through: re-raise (a bare `raise` or `raise <name>`), or follow a
clause of the same `try` that re-raises it, for example

```python
except (JobCancelledError, DailyQuotaExhaustedError):
    raise
except Exception:  # noqa: BLE001  # fail-loud.bare-except-ok: <reason>
    ...
```

A clause catches them when it is bare or names `Exception`, `BaseException` or
`RecipeParserError`. A clause that names either error itself is deliberate handling (the
job runner recording 'cancelled') and is allowed. Re-raising as another type
(`raise X from exc`) does not count: the caller's `except JobCancelledError` would no
longer match.

`config/architecture/detectors/quota_cancel.py` checks this on pull requests and before
every push, on the `except` lines a change adds. It is a host detector: each person
approves it once per repository with `/acf-approve`, and again after the file changes. A
handler around code that cannot raise either error carries its reason:
`# fail-loud.quota-cancel-ok: <reason>`. The existing stock (88 handlers in 34 files at
introduction; recount with the snippet below) is fixed or exempted as each is next edited.

```bash
python -c "import ast,importlib.util as u,pathlib as p;s=u.spec_from_file_location('q','config/architecture/detectors/quota_cancel.py');q=u.module_from_spec(s);s.loader.exec_module(q);print(sum(len(q.uncovered_handlers(ast.parse(f.read_text(encoding='utf-8')),None,['JobCancelledError','DailyQuotaExhaustedError'],['RecipeParserError'])) for f in p.Path('recipeparser').rglob('*.py')))"
```

## No shims

Do not keep code alive for an old shape: compatibility re-exports ("Backward-compat
re-export"), legacy fallbacks, or `hasattr` probes that paper over a missing interface.
Prefer explicit types, typed optionals and clear control flow.

## POSIX paths

When persisting paths to YAML or JSON, use `Path.as_posix()`. Do not pass `str(path)`
into dump or serialization payloads.

## Build artifacts

Do not stage or commit generated trees such as `dist/` or `build/`.

## Layer imports

`recipeparser/core/` is the engine. It must never import from `recipeparser/io/` or
`recipeparser/adapters/`. Adapters may import from both `core/` and `io/` (`GEMINI.md`,
No Cross-Layer Imports).

ruff checks the plain-import case (`TID251`), with the ban declared in
`recipeparser/core/ruff.toml` so it applies to `core/` only. The review rule
`soft.layer-imports` covers lazy, `TYPE_CHECKING` and indirect crossings.

## Model SDK owners

Only these modules import a model SDK (`google.genai`, `anthropic`):

- `recipeparser/gemini.py`, `recipeparser/core/picture_gen.py` and
  `recipeparser/adapters/ai_search/**`, which call the models;
- `recipeparser/__init__.py`, `recipeparser/__main__.py`, `recipeparser/adapters/api.py` and
  `recipeparser/adapters/gui.py`, which build a client from an API key;
- `recipeparser/adapters/search_api.py`, which types the Anthropic client it is handed, and
  `recipeparser/adapters/api.py` and `recipeparser/adapters/shopping_api.py`, which catch
  `google.genai.errors.APIError` to answer 503.

Every other module takes the client it is given and never imports the SDK. ruff enforces
this (`TID251`, banned-api in `pyproject.toml` and `recipeparser/core/ruff.toml`). An
owner is listed in `pyproject.toml`'s per-file-ignores; `core/picture_gen.py` carries a
line `noqa: TID251` instead, since a file ignore would also lift `core/`'s layer ban.
Adding an owner means adding it to this list and to the ignores together. Tests and
scripts are exempt.

Recount: `ruff check --select TID251 recipeparser` reports nothing.

## One clock

Every datetime RecipeParser makes is timezone-aware. A stored timestamp is written with
`recipeparser/core/clock.py`'s `utc_timestamp`, the one format; a time shown to the person
is local and aware (`datetime.now().astimezone()`). ruff enforces the aware half (`DTZ`)
under `recipeparser/`; tests and scripts are exempt, since tests build naive datetimes on
purpose.

Recount: `ruff check --select DTZ recipeparser` reports nothing.

## State machines

A pipeline run's status changes only through `PipelineController.transition()` (or
`transition_or_raise()`) in `recipeparser/core/fsm.py`. That method looks the next
status up in the `_TRANSITIONS` table. No other code assigns `status` directly
(`ARCHITECTURE.md` §4, Externalized FSM).

## Class cohesion

Each class holds one responsibility. A class whose methods split into groups that share
no instance state, or that mixes orchestration, I/O and policy, should be split.

## Cross-repository rules

The mobile app lives in the sibling repository Cayenne, which consumes this API and
shares the recipe models. A spec that cites it uses the prefix `cy:` for paths in that
repository and quotes the rule it relies on, so the spec stays complete on its own.
Cayenne's specs cite this repository with the prefix `rp:`.

## Post-merge

Host values read by the arch-compliance plugin's `post-merge-housekeeping`. The default
branch is `master`.

- **Shared-checkout branch lock:** none. Feature work runs in worktrees. The main
  checkout may hold someone's uncommitted work, so never check it out or pull it without
  asking.
- **Worktree daemon probe:** a local API server is started by `start_server.py`, which
  runs uvicorn on `recipeparser.adapters.api` and binds port 8000 by default. Match its
  processes on a command line containing `start_server.py` or `recipeparser.adapters.api`
  together with the worktree path. The readiness endpoint is `GET http://127.0.0.1:<port>/health`.
  `kill_server.py <pid>` stops one on Windows.
- **Post-merge CI and deploy verification:** a push to `master` runs two workflows:
  - `Build Windows Installer` runs the unit tests on Python 3.11 and 3.12. It builds and
    publishes the installer only for a `v*` tag.
  - `Deploy` builds the API image, pushes it to ghcr, and deploys it to the Cayenne VM
    over Tailscale. The VM's `deploy.sh` verifies the API through `/health`.

  Both must be green for the merge commit. A green `Deploy` is not proof of a deploy:
  read its run summary. It reports "Deploy skipped" when `CAYENNE_VM_HOST` is unset, and
  "auto-deploy is OFF on the VM" when the VM's switch skipped the run. Say which happened.
  A Cayenne change that depends on this merge is not live until this deploy is.
- **Durable tracking target:** Cayenne's `SpecificationDocumentation/FIX_ROADMAP.md` (`cy:`)
  holds deferred defects and debt for both repositories. Each item reads
  `F-### · RecipeParser · severity · size — title`. Stage work goes to Cayenne's
  `SpecificationDocumentation/ROADMAP.md`. The next free `F-###` is the highest one on
  Cayenne's `main` and in every open Cayenne pull request. A release also gets its entry
  in this repository's `CHANGELOG.md`, and `pyproject.toml` and `installer.iss` carry the
  same version.
- **Other tracking files:** none.
