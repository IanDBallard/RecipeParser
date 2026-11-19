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
  made directly from `recipeparser/gemini.py`, `recipeparser/categories.py` and
  `recipeparser/toc.py`. Do not cite that convention as a rule a spec must meet.
- `ARCHITECTURE.md` §4 (Externalized FSM), §10 (Adapter Contracts) and §12 (Ingestion
  Job Status).

## Fail loud

Failures must not disappear. Errors raise a class from the `RecipeParserError` hierarchy
in `recipeparser/exceptions.py`. Do not use a bare `except:`, or an `except Exception:`
that swallows the error, unless it is exempted with a reason. A provider call is the
one sanctioned exception (`GEMINI.md`, Error Handling): on a transient API failure it
returns `None`, and the adapter handles the `None` case.

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
