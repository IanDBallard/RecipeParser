# RecipeParser Provider Guide

How to add a new LLM or embedding provider.

---

## 1. Adding an LLM Provider

### Step 1 — Create the provider file

Create `recipeparser/core/providers/<name>.py`. Implement `LLMProvider`:

```python
# recipeparser/core/providers/openai.py  (example)
from typing import List, Optional
from recipeparser.core.providers.base import LLMProvider
from recipeparser.models import RecipeList, CayenneRefinement, RecipeExtraction
from recipeparser.io.category_sources.base import CategoryTree

class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "gpt-4o"):
        import openai
        self._client = openai.OpenAI(api_key=api_key)
        self._model = model

    def verify_connectivity(self) -> bool:
        try:
            self._client.models.retrieve(self._model)
            return True
        except Exception:
            return False

    def extract_recipes(self, text: str, units: str) -> Optional[RecipeList]:
        # Call self._client.chat.completions.create(...)
        # Parse response into RecipeList
        # Return None on failure
        ...

    def refine_recipe(
        self,
        raw: RecipeExtraction,
        uom_system: str,
        measure_preference: str,
    ) -> Optional[CayenneRefinement]:
        # Call self._client.chat.completions.create(...)
        # Parse response into CayenneRefinement
        # Return None on failure
        ...

    def categorize(
        self,
        recipe: RecipeExtraction,
        category_tree: CategoryTree,
    ) -> List[str]:
        # Call self._client.chat.completions.create(...)
        # Return list of category names; fallback to ["Uncategorized"] on failure
        ...
```

**Rules:**
- Never import from `io/` or `adapters/` — providers are pure core.
- Implement retry/back-off internally. Do not let transient API errors propagate as exceptions — return `None` from `extract_recipes` and `refine_recipe` on failure; return `["Uncategorized"]` from `categorize` on failure.
- Use structured output / JSON mode where the provider supports it.

### Step 2 — Register in the factory

```python
# recipeparser/core/providers/factory.py
def create_provider(name: str, api_key: str, model: Optional[str] = None) -> LLMProvider:
    match name.lower():
        case "gemini":
            from .gemini import GeminiProvider
            return GeminiProvider(api_key=api_key, model=model or "gemini-2.5-flash")
        case "openai":
            from .openai import OpenAIProvider
            return OpenAIProvider(api_key=api_key, model=model or "gpt-4o")
        case "anthropic":
            from .anthropic import AnthropicProvider
            return AnthropicProvider(api_key=api_key, model=model or "claude-3-5-sonnet-20241022")
        case "mock":
            from .mock import MockProvider
            return MockProvider()
        case _:
            raise ValueError(f"Unknown LLM provider: '{name}'")
```

### Step 3 — Add to `.env` documentation

Document the new provider name in `.env.example`:

```
# LLM_PROVIDER options: gemini | openai | anthropic | mock
LLM_PROVIDER=gemini
LLM_API_KEY=...
```

### Step 4 — Write tests

Create `recipeparser/tests/providers/test_<name>.py`. Use `MockProvider` as a reference for the expected test structure. At minimum, test:

- `verify_connectivity()` returns `True` with a valid key (integration test, skipped in CI)
- `extract_recipes()` returns a valid `RecipeList` for a known input (use VCR cassette or mock HTTP)
- `refine_recipe()` returns a valid `CayenneRefinement`
- `categorize()` returns a list of strings from the provided taxonomy
- All methods return graceful fallbacks on simulated API failure

---

## 2. Adding an Embedding Provider

### Step 1 — Create the provider file

```python
# recipeparser/core/providers/cohere_embed.py  (example)
from typing import List
from recipeparser.core.providers.base import EmbeddingProvider

class CohereEmbeddingProvider(EmbeddingProvider):
    def __init__(self, api_key: str):
        import cohere
        self._client = cohere.Client(api_key)

    def embed(self, text: str) -> List[float]:
        response = self._client.embed(
            texts=[text],
            model="embed-english-v3.0",
            input_type="search_document",
        )
        return response.embeddings[0]

    @property
    def dimensions(self) -> int:
        return 1024  # embed-english-v3.0 output size
```

**Critical constraint:** The `dimensions` property MUST match the `vector(N)` declaration in both:
- Supabase: `recipes.embedding vector(N)`
- Local SQLite: `sqlite-vec` index dimension

If you change the embedding dimension, you must also run a database migration to update both schemas and re-embed all existing recipes. This is a breaking change.

### Step 2 — Register in the factory

```python
# recipeparser/core/providers/factory.py
def create_embedding_provider(name: str, api_key: str) -> EmbeddingProvider:
    match name.lower():
        case "gemini":
            from .gemini import GeminiEmbeddingProvider
            return GeminiEmbeddingProvider(api_key=api_key)
        case "cohere":
            from .cohere_embed import CohereEmbeddingProvider
            return CohereEmbeddingProvider(api_key=api_key)
        case "mock":
            from .mock import MockEmbeddingProvider
            return MockEmbeddingProvider()
        case _:
            raise ValueError(f"Unknown embedding provider: '{name}'")
```

### Step 3 — Write tests

At minimum, test:

- `embed()` returns a list of exactly `provider.dimensions` floats
- `embed()` is deterministic for the same input (or at least stable in length)
- `embed()` raises `RecipeParserError` (not a raw SDK exception) on API failure

---

## 3. Mock Provider (Reference Implementation)

The `MockProvider` and `MockEmbeddingProvider` in `core/providers/mock.py` are the canonical reference for testing. They produce deterministic output with no network calls.

```python
# core/providers/mock.py
import hashlib
from typing import List, Optional
from recipeparser.core.providers.base import LLMProvider, EmbeddingProvider
from recipeparser.models import RecipeList, CayenneRefinement, RecipeExtraction
from recipeparser.io.category_sources.base import CategoryTree

class MockProvider(LLMProvider):
    """Deterministic mock for unit tests. No network calls."""

    def verify_connectivity(self) -> bool:
        return True

    def extract_recipes(self, text: str, units: str) -> Optional[RecipeList]:
        # Returns a single hardcoded RecipeExtraction regardless of input
        return RecipeList(recipes=[
            RecipeExtraction(
                title="Mock Recipe",
                ingredients=["1 cup flour", "2 eggs"],
                directions=["Mix flour and eggs.", "Bake at 350°F for 30 minutes."],
                prep_time="10 minutes",
                cook_time="30 minutes",
                servings=4.0,
            )
        ])

    def refine_recipe(
        self,
        raw: RecipeExtraction,
        uom_system: str,
        measure_preference: str,
    ) -> Optional[CayenneRefinement]:
        # Returns a hardcoded CayenneRefinement with Fat Tokens
        from recipeparser.models import StructuredIngredient, TokenizedDirection, CayenneRefinement
        return CayenneRefinement(
            structured_ingredients=[
                StructuredIngredient(
                    id="ing_01", amount=1.0, unit="cup", name="flour",
                    fallback_string="1 cup flour",
                    converted_amount=None, converted_unit=None, is_ai_converted=False,
                ),
                StructuredIngredient(
                    id="ing_02", amount=2.0, unit=None, name="eggs",
                    fallback_string="2 eggs",
                    converted_amount=None, converted_unit=None, is_ai_converted=False,
                ),
            ],
            tokenized_directions=[
                TokenizedDirection(step=1, text="Mix {{ing_01|1 cup flour}} and {{ing_02|2 eggs}}."),
                TokenizedDirection(step=2, text="Bake at 350°F for 30 minutes."),
            ],
        )

    def categorize(
        self,
        recipe: RecipeExtraction,
        category_tree: CategoryTree,
    ) -> List[str]:
        # Returns first available category, or "Uncategorized"
        return [category_tree.leaf_names[0]] if category_tree.leaf_names else ["Uncategorized"]


class MockEmbeddingProvider(EmbeddingProvider):
    """Deterministic mock embedder. Returns a stable 1536-dim vector derived from input hash."""

    @property
    def dimensions(self) -> int:
        return 1536

    def embed(self, text: str) -> List[float]:
        # Deterministic: same text always produces same vector
        seed = int(hashlib.md5(text.encode()).hexdigest(), 16)
        import random
        rng = random.Random(seed)
        raw = [rng.gauss(0, 1) for _ in range(self.dimensions)]
        # L2-normalize
        magnitude = sum(x ** 2 for x in raw) ** 0.5
        return [x / magnitude for x in raw]
```

---

## 4. Provider Selection at Runtime

### CLI

```bash
recipeparser cookbook.epub --provider gemini --embedding-provider gemini
recipeparser cookbook.epub --provider mock   --embedding-provider mock   # tests / offline
```

### API (environment variables)

```
LLM_PROVIDER=gemini
EMBEDDING_PROVIDER=gemini   # default — reuses GOOGLE_API_KEY, no second key needed
```

The API adapter reads these at startup and instantiates providers once (singleton per worker process).

### GUI

Provider selection is not exposed in the GUI. The GUI always uses the values from `.env`. This is intentional — provider selection is an operator concern, not a user concern.

---

## 5. Prompt Engineering Guidelines

When implementing a new LLM provider, the prompts must produce output that conforms to the Pydantic models in `models.py`. Follow these rules:

1. **Use structured output / JSON mode** — never parse free-form text.
2. **Mirror the Pydantic schema exactly** in the JSON schema passed to the model.
3. **Fat Token format** — the `refine_recipe` prompt must instruct the model to produce `{{ing_id|fallback_string}}` tokens in direction text. Reference the existing `GeminiProvider` prompt as the canonical example.
4. **Ingredient IDs** — must be sequential: `ing_01`, `ing_02`, ... The model must not invent IDs.
5. **`is_ai_converted`** — must be `true` only when the model performed a Volume-to-Weight conversion using ingredient density knowledge. The model must not set this flag for unit normalization (e.g., `tbsp` → `ml`).
6. **Fallback on parse failure** — if the model returns malformed JSON, log the error and return `None` (for `extract_recipes`/`refine_recipe`) or `["Uncategorized"]` (for `categorize`). Never raise an unhandled exception.
