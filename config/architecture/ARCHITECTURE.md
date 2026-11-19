# Architecture doctrine — RecipeParser

Entry point for the arch-compliance plugin's `/arch-compliance:adversarial-design-review`.
Read, in this order:

1. `GEMINI.md` — the project mandates. Its *Development Conventions* section is the
   rule list; read it in full.
2. `ARCHITECTURE.md` (repository root) — the design: module map, engine contract,
   externalized FSM, provider interface, adapter contracts, and the Supabase and
   PowerSync job-status model.
3. `PROVIDER_GUIDE.md` — how a new LLM or embedding provider is added.
4. `README.md` — the run and test commands.

## High-frequency misses

Check these first:

- `GEMINI.md` *Development Conventions*: all LLM and embedding calls go through the
  `LLMProvider` / `EmbeddingProvider` interfaces, and no concrete provider is imported
  outside `recipeparser/core/providers/`; `recipeparser/core/` never imports from
  `recipeparser/io/` or `recipeparser/adapters/`; errors use the hierarchy in
  `recipeparser/exceptions.py`, and providers return `None` on transient failures;
  recipe data uses the Pydantic models in `recipeparser/models.py`, shared with Cayenne;
  tuneable constants live in `recipeparser/config.py`.
- `ARCHITECTURE.md` §4 (Externalized FSM), §10 (Adapter Contracts), §12 (Ingestion Job
  Status).

## Cross-repository rules

The mobile app lives in the sibling repository Cayenne, which consumes this API and
shares the recipe models. A spec that cites it uses the prefix `cy:` for paths in that
repository and quotes the rule it relies on, so the spec stays complete on its own.
Cayenne's specs cite this repository with the prefix `rp:`.
