# Rule: layer-imports (RecipeParser host prompt)

**Principle.** `recipeparser/core/` is the engine and depends on nothing above it. It
never imports from `recipeparser/io/` or `recipeparser/adapters/`. Adapters may import
from both `core/` and `io/`. The rule text is `GEMINI.md`, *Development Conventions*,
"No Cross-Layer Imports".

## Scope and documents

This is a host prompt, so the controller passes no settings: the surface you receive
is the repository's whole review tree. **Only Python files under `recipeparser/core/`
are in scope.** Count every other surface file as examined, with no finding and no
`errors` entry, without opening it.

Read `GEMINI.md` (*Development Conventions*) and `config/architecture/ARCHITECTURE.md`
(*Layer imports*) before auditing.

## Flag

Flag a file under `recipeparser/core/` that reaches `recipeparser/io/` or
`recipeparser/adapters/` by any of these routes:

- a static import: `import recipeparser.io...`, `from recipeparser.adapters... import`,
  or a relative import that resolves there (`from ..io import ...`);
- a lazy import inside a function, or one under `TYPE_CHECKING`. The `TYPE_CHECKING`
  case is a `suggestion`, because it adds no runtime dependency but still couples the
  layers;
- `importlib.import_module` or `__import__` with a string naming those packages;
- an indirect import through a package-root re-export that is itself a shim of an
  `io/` or `adapters/` module. Example: a package-root `recipeparser/epub.py` that
  re-exported `recipeparser.io.readers.epub` (such shims were removed in 9.49.0) would
  make `from recipeparser.epub import ...` inside `core/` a crossing.

Severity: a runtime import is `important`. A crossing that makes the engine depend on
an adapter (FastAPI, Supabase, HTTP) is `blocking`.

## Do not flag

- Imports from `recipeparser/core/` itself, from `recipeparser/exceptions.py`,
  `recipeparser/config.py`, `recipeparser/models.py`, or from other package-root
  modules that are not re-exports of `io/` or `adapters/`.
- A docstring or comment that names `recipeparser.io`, such as the "no imports from
  recipeparser.io" notes in `core/citation.py` and `core/clock.py`.

## Output

Follow the finding contract. Each finding's `why` names the imported module and the
route (static, lazy, `TYPE_CHECKING`, dynamic or re-export), and its `suggested_fix`
names the port or injected dependency that would remove the crossing. Where a
protocol for it already exists in `recipeparser/core/ports.py`, name it.
